"""영역 판독 — VLM이 Region을 독립적으로 읽고 OCR 결과와 대조한다.

OCR 은 디자인 문구·스타일 글자에서 무너진다. 실측(2026-09-20, 9개 파일):

    2. 카드상품 p1_r005   `※상환능력에비해신용카드사용액이과도할경우,귀하의개인신용평점이그을V`
    3. 예금성(거치식) p1_r019  `이아융이HN이ㄷ`
    2. 대출성상품 p1_r025  `)`

그런데 깨진 채로 라벨링에 가면 엉뚱한 구분값이 붙는다 — `p1_r025` 는 `)` 한 글자로
`상품명` 라벨을 받았다. 그래서 라벨링 **앞에서** 판독을 끝낸다.

페이지 전체를 훑어 "빠진 문구"를 찾는 방식은 쓰지 않는다. 같은 9개 파일에서 0건
나왔다 — 모델이 목록에 없는 것을 스스로 대조하지 못한다. 영역마다 물어야 한다.

Reader가 이미지에서 독립 전사한 뒤 OCR/PDF 후보와 비교한다. 둘이 다르면 두 후보와
같은 crop을 Judge에게 다시 주고, Judge의 최종 전사를 Region 정본으로 채택한다.
OCR/PDF 원문과 Reader 결과는 P1 후보에 그대로 남겨 되짚을 수 있게 한다.
"""
from __future__ import annotations

import os
import unicodedata
from difflib import SequenceMatcher
from typing import Any

from PIL import Image

from ..vlm import client as vlm_client
from .quality import flag

# 공백을 지우고 비교하므로 띄어쓰기·줄바꿈만 다른 경우는 1.000 이 나온다.
# 임계값은 실측으로 골랐다(2026-09-20).
#
#     띄어쓰기만 다름                                        1.000
#     줄바꿈만 다름                                          1.000
#     오탈자 하나 (`연 2.25%` vs `연 2.26%`)                  0.900
#     꼬리가 깨짐 (`…개인신용평점이그을V`)                      0.842
#     통째로 깨짐 (`이아융이HN이ㄷ`)                            0.176
#
# 0.8 이면 꼬리가 깨진 경우가 통과하고, 0.9 는 오탈자 하나가 경계에 정확히
# 걸터앉는다. 0.95 로 두면 둘 다 잡히면서 형식 차이는 그대로 통과한다 —
# 금리 숫자 한 자 차이는 광고 심의에서 가장 크게 문제 되는 종류라 검수로
# 올리는 쪽이 맞다.
AGREE = 0.95
# crop 여백. 글자가 테두리에 붙어 잘리면 모델이 앞뒤를 못 읽는다.
CROP_PAD = 12
# 너무 작은 영역은 확대해 넣는다. 원본 그대로면 글자가 뭉개진다.
MIN_CROP_SIDE = 320
SCOPES = ("all", "targeted", "off")

_SCHEMA = {
    "type": "object",
    "properties": {
        # 설명할 자리를 **먼저** 준다. 없으면 모델이 JSON 을 닫고 그 뒤에 계속
        # 말해서 그 문장이 text 값에 섞인다 — 실측(2026-09-20):
        # `NH농협카드"} (Note: The user requested to transc…`.
        # 같은 이유로 응답이 5,100자까지 늘어 10건이 파싱 실패했다.
        "analysis": {"type": "string"},
        "text": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["analysis", "text", "confidence"],
    "additionalProperties": False,
}

_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "analysis": {"type": "string"},
        "text": {"type": "string"},
        "confidence": {"type": "number"},
        "source": {"type": "string", "enum": ["parser", "reader", "corrected"]},
    },
    "required": ["analysis", "text", "confidence", "source"],
    "additionalProperties": False,
}

_PROMPT = """첨부 이미지는 광고 문서에서 잘라낸 영역 하나입니다.

보이는 글자를 **그대로** 옮겨 적으세요.

- 줄바꿈은 보이는 대로 유지하세요.
- 로고·아이콘 안의 글자도 읽을 수 있으면 적으세요.
- 읽을 수 없거나 글자가 없으면 빈 문자열을 반환하세요.
- 없는 내용을 채우거나 요약하지 마세요.
- 하고 싶은 말은 analysis 에 한 문장으로 쓰고, text 에는 **글자만** 담으세요.
"""


def scope_from_env() -> str:
    value = str(os.environ.get("PARSER_V2_READING_SCOPE", "all")).strip().lower()
    return value if value in SCOPES else "all"


def _normalized(value: Any) -> str:
    return "".join(str(value or "").split())


def _semantic_alnum(value: Any) -> str:
    """PUA/기호/공백을 빼고 실제 문자와 숫자만 비교한다."""
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return "".join(char for char in normalized if char.isalnum()).casefold()


def agreement(left: str, right: str) -> float:
    a, b = _normalized(left), _normalized(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def should_read(region: dict[str, Any], scope: str) -> bool:
    """이 Region 을 VLM 에 보낼지 정한다.

    표 Region 은 제외한다. 셀 배치 단계에서 이미 같은 crop 을 VLM 이 봤고,
    격자를 평문으로 다시 읽으면 무조건 불일치로 나온다.
    """
    if scope == "off" or not region.get("bbox"):
        return False
    if region.get("kind") == "table" or region.get("table"):
        return False
    # document-processor가 HWP/PDF 내부 구조에서 직접 읽은 텍스트는 이미지 전사가
    # 보완할 대상이 아니다. VLM Reader가 행 일부만 읽어 정본을 줄이는 일을 막고,
    # 이미지 전용 신규 블록은 기존대로 Reader를 거친다.
    if (
        str(region.get("text_source") or "").startswith("document_processor")
        and _normalized(region.get("text"))
    ):
        return False
    if scope == "all":
        return True
    # targeted — 의심스러운 곳만.
    text = _normalized(region.get("text"))
    if len(text) <= 3:
        return True
    if region.get("text_selection_status") == "conflict_pending_vlm":
        return True
    return not (region.get("lines") or [])


def _crop(image: Image.Image, box: list[int]) -> Image.Image | None:
    x0 = max(0, int(box[0]) - CROP_PAD)
    y0 = max(0, int(box[1]) - CROP_PAD)
    x1 = min(image.width, int(box[2]) + CROP_PAD)
    y1 = min(image.height, int(box[3]) + CROP_PAD)
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    raw = image.crop((x0, y0, x1, y1)).convert("RGB")
    # 여백은 글자가 잘리지 않게 모델 입력 크기를 확보하기 위한 것이지, 이웃 Region을
    # 읽으라는 뜻이 아니다. 실제 bbox 밖을 흰색으로 가려 r027/r028처럼 맞닿은 영역의
    # 문장이 서로 복사되는 것을 막는다.
    crop = Image.new("RGB", raw.size, "white")
    inner = (
        max(0, int(box[0]) - x0), max(0, int(box[1]) - y0),
        min(raw.width, int(box[2]) - x0), min(raw.height, int(box[3]) - y0),
    )
    if inner[2] <= inner[0] or inner[3] <= inner[1]:
        return None
    crop.paste(raw.crop(inner), (inner[0], inner[1]))
    scale = MIN_CROP_SIDE / max(1, min(crop.size))
    if scale > 1.0:
        scale = min(scale, 4.0)
        crop = crop.resize(
            (max(1, round(crop.width * scale)), max(1, round(crop.height * scale))),
            Image.LANCZOS,
        )
    return crop


def read_region(image: Image.Image, region: dict[str, Any]) -> dict[str, Any] | None:
    """Region crop 하나를 독립적으로 전사한다. OCR 결과는 보여주지 않는다.

    OCR 텍스트를 프롬프트에 넣으면 모델이 그것을 따라 적어 대조가 무의미해진다.
    """
    crop = _crop(image, region["bbox"])
    if crop is None:
        return None
    # 영역 길이에 맞춰 예산을 잡는다. 실측 최장 Region 텍스트가 697자였다.
    budget = min(6000, 1200 + 3 * len(str(region.get("text") or "")))
    result = vlm_client.chat_json(
        [
            {"type": "text", "text": _PROMPT},
            vlm_client.image_part(crop, box=(1400, 1400), quality=92),
        ],
        schema_name="parser_v2_region_reading",
        schema=_SCHEMA,
        max_tokens=budget,
    )
    return {
        "text": clean_text(result.get("text")),
        "confidence": float(result.get("confidence") or 0.0),
        "analysis": str(result.get("analysis") or "")[:300],
    }


def judge_region(
    image: Image.Image,
    region: dict[str, Any],
    reading: dict[str, Any],
) -> dict[str, Any] | None:
    """OCR/PDF와 Reader가 다를 때 이미지를 기준으로 최종 전사를 만든다."""
    crop = _crop(image, region["bbox"])
    if crop is None:
        return None
    parser_text = str(region.get("text") or "")
    reader_text = str(reading.get("text") or "")
    prompt = f"""첨부 이미지는 광고 문서에서 잘라낸 영역 하나입니다.

아래 두 전사 후보를 참고하되 **이미지에 실제로 보이는 글자**를 최종 기준으로 삼아
정확한 전체 텍스트를 반환하세요.

- 후보 A (OCR/PDF 파서):
{parser_text}

- 후보 B (독립 VLM Reader):
{reader_text}

규칙:
- 글자를 요약하거나 설명하지 말고 보이는 순서와 줄바꿈을 최대한 유지하세요.
- 숫자, 금리, 날짜, 괄호, 주석 기호를 임의로 고치거나 만들지 마세요.
- 두 후보가 모두 틀리면 이미지에 맞게 고친 텍스트를 반환하세요.
- source는 A가 맞으면 parser, B가 맞으면 reader, 둘을 고쳤으면 corrected입니다.
- analysis는 선택 이유 한 문장, text에는 최종 글자만 넣으세요.
"""
    budget = min(7000, 1500 + 3 * max(len(parser_text), len(reader_text)))
    result = vlm_client.chat_json(
        [
            {"type": "text", "text": prompt},
            vlm_client.image_part(crop, box=(1400, 1400), quality=92),
        ],
        schema_name="parser_v2_region_reading_judge",
        schema=_JUDGE_SCHEMA,
        max_tokens=budget,
    )
    return {
        "text": clean_text(result.get("text")),
        "confidence": float(result.get("confidence") or 0.0),
        "source": str(result.get("source") or "corrected"),
        "analysis": str(result.get("analysis") or "")[:300],
    }


def clean_text(value: Any) -> str:
    """모델이 JSON 을 닫고 이어 쓴 잡담을 잘라낸다.

    analysis 자리를 준 뒤에도 가끔 새어 나온다. 정본 후보로 쓰이는 값이라
    방어적으로 한 번 더 자른다.
    """
    text = str(value or "")
    for marker in ('"}', '"]'):
        index = text.find(marker)
        if index > 0:
            text = text[:index]
    return text.strip()


def _has_digital_evidence(region: dict[str, Any]) -> bool:
    """PDF 내장 텍스트처럼 글자와 좌표를 직접 얻은 Region인지 확인한다."""
    if str(region.get("text_source") or "").startswith(("digital_", "document_processor")):
        return True
    return any(
        str(line.get("source") or "").casefold() == "digital"
        for line in region.get("lines") or []
    )


def _store_judge(region: dict[str, Any], judge: dict[str, Any] | None) -> str:
    if not judge or not _normalized(judge.get("text")):
        return ""
    final_text = str(judge["text"])
    region["vlm_judge"] = {
        "text": final_text,
        "confidence": judge.get("confidence"),
        "source": judge.get("source"),
        "analysis": judge.get("analysis"),
    }
    region["text_candidates"]["vlm_judge"] = final_text
    return final_text


def apply_reading(
    region: dict[str, Any],
    reading: dict[str, Any],
    judge: dict[str, Any] | None = None,
) -> str:
    """Reader/Judge를 대조하되 직접 추출한 PDF 텍스트는 잃지 않는다.

    디지털 PDF 텍스트는 글자와 줄 좌표가 원본에서 직접 나온 근거다. VLM이 이를
    고쳐 쓰면 실제로 없던 문장을 더하거나 ``(①+②)`` 같은 기호를 지운 사례가 있어,
    VLM 결과는 후보로만 보존하고 parser 텍스트를 정본으로 둔다. 이미지 OCR만 있는
    Region은 기존처럼 Reader/Judge가 교정할 수 있다.
    """
    ocr_text = str(region.get("text") or "")
    vlm_text = str(reading.get("text") or "")
    score = agreement(ocr_text, vlm_text)
    region["vlm_reading"] = {
        "text": vlm_text,
        "confidence": reading.get("confidence"),
        "agreement": round(score, 4),
    }
    region.setdefault("text_candidates", {})["parser_selected"] = ocr_text or None
    region.setdefault("text_candidates", {})["vlm_reading"] = vlm_text or None

    if not _normalized(vlm_text):
        region["reading_status"] = "vlm_blank"
        return "vlm_blank"

    final_text = _store_judge(region, judge) or vlm_text
    # 디지털 PDF 텍스트는 VLM 검증 결과가 같아도 출처를 OCR/PDF로 유지한다. 다르면
    # 후보를 P1에 남기고 파싱 품질 검수 대상으로 올리되 원문을 바꾸지 않는다.
    if ocr_text and _has_digital_evidence(region):
        if _normalized(ocr_text) == _normalized(final_text):
            region["reading_status"] = "parser_verified"
            return "parser_verified"
        structure_validation = region.get("hwp_structure_validation") or {}
        structure_corroboration = region.get("hwp_structure_corroboration") or {}
        if (
            structure_validation.get("status") == "agrees"
            or structure_corroboration.get("status") == "agrees"
        ):
            # 체크박스 같은 HWP 글리프는 PDF와 구조 파서가 서로 다른 PUA 문자로
            # 내보내기도 한다. 구조와 VLM의 한글·영숫자 내용이 완전히 같을 때만
            # VLM이 본 표준 Unicode 기호를 채택한다.
            if (
                any(unicodedata.category(char) == "Co" for char in ocr_text)
                and not any(
                    unicodedata.category(char) == "Co" for char in final_text
                )
                and _semantic_alnum(final_text)
                == _semantic_alnum(
                    (region.get("text_candidates") or {}).get("hwp_structure")
                )
            ):
                region["text"] = final_text
                region["text_source"] = "vlm_structure_verified"
                region["reading_status"] = "vlm_glyph_verified_by_hwp_structure"
                return "parser_verified"
            region["reading_status"] = "parser_verified_by_hwp_structure"
            return "parser_verified"
        flag(region, "digital_text_vlm_disagreement")
        region["reading_status"] = "parser_preserved"
        return "parser_preserved"

    if judge is not None and _normalized(judge.get("text")):
        region["text"] = final_text
        region["text_source"] = "vlm_judge"
        region["reading_status"] = "judge_selected"
        if float(judge.get("confidence") or 0.0) < 0.7:
            flag(region, "vlm_judge_low_confidence")
        if not _normalized(ocr_text):
            region["bbox_quality"] = "region"
        return "judge_selected"

    # 두 후보가 일치하면 독립 Reader의 전사를 최종 텍스트로 쓴다. OCR/PDF 후보는
    # text_candidates에 남아 있으므로 P1에서 언제든 대조할 수 있다.
    region["text"] = vlm_text
    region["text_source"] = "vlm_reader"
    if not _normalized(ocr_text):
        region["bbox_quality"] = "region"
        flag(region, "vlm_only_text")
        region["reading_status"] = "vlm_only"
        return "vlm_only"
    if score >= AGREE:
        region["reading_status"] = "agree"
        return "agree"
    # Judge를 부르지 못한 경우에도 사용자 선택에 따라 Reader 결과를 정본으로 쓰되,
    # 불일치 사실은 검수 대상으로 남긴다.
    region["reading_status"] = "disagree"
    flag(region, "ocr_vlm_disagreement")
    return "disagree"


def read_page(
    page: dict[str, Any], image: Image.Image, *, scope: str = "all",
) -> dict[str, int]:
    """페이지의 Region 을 훑어 판독하고 통계를 돌려준다."""
    stats = {"read": 0, "agree": 0, "disagree": 0, "judge_selected": 0,
             "parser_verified": 0, "parser_preserved": 0,
             "vlm_only": 0, "vlm_blank": 0, "skipped": 0, "failed": 0,
             "judge_failed": 0}
    for region in page.get("regions") or []:
        if not should_read(region, scope):
            stats["skipped"] += 1
            continue
        try:
            reading = read_region(image, region)
        except Exception as exc:  # noqa: BLE001
            # 한 영역의 판독 실패가 페이지 전체를 멈추게 하지 않는다. OCR 정본은
            # 그대로 남고 검수 대상으로만 표시된다.
            region["reading_status"] = "failed"
            region["vlm_reading"] = {"error": str(exc)[:200]}
            flag(region, "vlm_read_failed")
            stats["failed"] += 1
            continue
        if reading is None:
            stats["skipped"] += 1
            continue
        stats["read"] += 1
        parser_text = str(region.get("text") or "")
        reader_text = str(reading.get("text") or "")
        judge = None
        if _normalized(reader_text) and agreement(parser_text, reader_text) < AGREE:
            try:
                judge = judge_region(image, region, reading)
            except Exception as exc:  # noqa: BLE001
                region["vlm_judge"] = {"error": str(exc)[:200]}
                stats["judge_failed"] += 1
        stats[apply_reading(region, reading, judge)] += 1
    page["reading_stats"] = stats
    return stats
