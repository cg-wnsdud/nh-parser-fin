"""VLM이 Region 이미지를 읽고 OCR/PDF 문장과 대조한다.

일반 Region의 디지털 원문은 우선 보존한다. 시각 표로 확인된 Region은
``visual_tables``가 최종 bbox 전체를 표 전용 Judge로 다시 판독한다.
원문과 모델 판독 후보는 P1에 남겨 비교할 수 있다.
"""
from __future__ import annotations

import os
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

from PIL import Image

from ..vlm import client as vlm_client
from .adapters import DIGITAL_PRIMARY_SHARE, digital_line_share
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


# [실험: 페이지 단위 Reader] `region`(기본)은 Region crop마다 1회, `page`는 페이지
# 이미지 + Region ID·bbox·Paddle 텍스트 목록으로 한 번에 판독한다. Judge 이하는 같다.
READER_MODES = ("region", "page")
# 한 페이지 판독 요청이 감당할 Region 수와 참고 텍스트 길이. 넘으면 페이지 이미지는
# 그대로 두고 Region 목록만 나눠 부른다. 응답이 잘리면 묶음 전체가 실패하기 때문이다.
PAGE_READ_CHUNK = 20
PAGE_READ_CHUNK_CHARS = 2400
BOX_ISSUES = ["none", "cut_off", "mixed", "no_text"]


def reader_mode_from_env() -> str:
    value = str(os.environ.get("PARSER_V2_READER_MODE", "region")).strip().lower()
    return value if value in READER_MODES else "region"


def judge_prompt_from_env() -> str:
    value = str(os.environ.get("PARSER_V2_JUDGE_PROMPT", "default")).strip().lower()
    return value if value in ("default", "blind") else "default"


def review_rules_from_env() -> str:
    value = str(os.environ.get("PARSER_V2_REVIEW_RULES", "default")).strip().lower()
    return value if value in ("default", "ocr") else "default"


_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_HANGUL_WORD = re.compile(r"[가-힣]{2,}")


def ocr_review_codes(
    region: dict[str, Any], parser_text: str, reading: dict[str, Any] | None,
    judge: dict[str, Any] | None,
) -> list[str]:
    """[실험] 디지털 원문 없이 돌릴 때의 판독 품질 검수 사유(N1~N4).

    - N1 `vlm_blank_with_ocr_text`: 재판독 뒤에도 VLM이 비었는데 OCR 글자가 4자 이상
    - N2 `page_reader_fallback`: 페이지 판독에서 빠져 crop Reader로 대체됨
    - N3 `unsupported_number`: 최종 문구에 OCR·Reader 어디에도 없던 숫자가 있음
    - N4 `judge_new_word`: Judge가 두 후보 어디에도 없던 한글 단어를 만듦
    판독 실패는 기존 `vlm_read_failed`가 이미 표시한다.
    """
    codes: list[str] = []
    reader_text = str((reading or {}).get("text") or "")
    final_text = str(region.get("text") or "")
    if region.get("reading_status") == "vlm_blank" and len(_semantic_alnum(parser_text)) >= 4:
        codes.append("vlm_blank_with_ocr_text")
    if (reading or {}).get("fallback"):
        codes.append("page_reader_fallback")
    known_numbers = set(_NUMBER.findall(parser_text)) | set(_NUMBER.findall(reader_text))
    if set(_NUMBER.findall(final_text)) - known_numbers:
        codes.append("unsupported_number")
    judge_text = str((judge or {}).get("text") or "")
    if judge_text:
        known = _semantic_alnum(parser_text) + "\n" + _semantic_alnum(reader_text)
        if any(_semantic_alnum(word) not in known for word in _HANGUL_WORD.findall(judge_text)):
            codes.append("judge_new_word")
    return codes


def judge_trigger_from_env() -> str:
    value = str(os.environ.get("PARSER_V2_JUDGE_TRIGGER", "ratio")).strip().lower()
    return value if value in ("ratio", "strict") else "ratio"


def needs_judge(parser_text: str, reader_text: str) -> bool:
    """Reader와 파서 문구가 달라 crop Judge가 필요한지.

    `ratio`(기본)는 공백 제거 일치도 0.95 미만일 때만 부른다. 긴 문장에서 한 글자만
    틀리면(`여신`→`연신`, `채움`→`채춤`) 0.95를 넘어 Judge 없이 통과한다.
    `strict`는 공백·기호를 뺀 글자·숫자가 한 자라도 다르면 부른다. 띄어쓰기·줄바꿈·
    글머리 기호 차이만으로는 부르지 않는다.
    """
    if agreement(parser_text, reader_text) < AGREE:
        return True
    if judge_trigger_from_env() == "strict":
        return _semantic_alnum(parser_text) != _semantic_alnum(reader_text)
    return False


def page_reader_text_from_env() -> bool:
    """페이지 Reader에 Paddle 텍스트를 참고로 줄지. `off`면 ID·bbox만 준다(독립 판독)."""
    return str(os.environ.get("PARSER_V2_PAGE_READER_TEXT", "on")).strip().lower() != "off"


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

    HWP 원본 구조 표는 독립 전사 대상에서 제외한다. PDF·이미지 시각 표는
    이 단계 뒤에 확정되므로 일반 Region처럼 먼저 읽고, 확인되면 표 전용 Judge를 거친다.
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


def _page_reading_schema(region_ids: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "analysis": {"type": "string"},
            "region_readings": {
                "type": "array",
                "minItems": len(region_ids),
                "maxItems": len(region_ids),
                "items": {
                    "type": "object",
                    "properties": {
                        "region_id": {"type": "string", "enum": region_ids or ["__none__"]},
                        "box_issue": {"type": "string", "enum": BOX_ISSUES},
                        "text": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["region_id", "box_issue", "text", "confidence"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["analysis", "region_readings"],
        "additionalProperties": False,
    }


def _page_chunks(regions: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    chars = 0
    for region in regions:
        size = len(_normalized(region.get("text")))
        if current and (len(current) >= PAGE_READ_CHUNK or chars + size > PAGE_READ_CHUNK_CHARS):
            chunks.append(current)
            current, chars = [], 0
        current.append(region)
        chars += size
    if current:
        chunks.append(current)
    return chunks


def read_page_regions(
    image: Image.Image, page: dict[str, Any], regions: list[dict[str, Any]],
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """페이지 이미지 한 장으로 여러 Region을 한 번에 전사한다.

    2단계(페이지 맥락)와 같은 재료 — 페이지 전체 이미지, ID 박스, Region ID·bbox·
    Paddle 텍스트 — 를 준다. 박스 그림이 글자를 가리지 않도록 원본 페이지를 먼저,
    ID 박스를 그린 페이지를 두 번째로 보낸다. Paddle 결과는 참고용임을 명시한다.
    긴 페이지는 2단계와 같은 밴드로 잘라 부른다.
    """
    from .semantic import _overlay, _short_text, partition_for_semantic_bands

    bands = partition_for_semantic_bands({**page, "regions": regions}, [])
    if not bands:
        bands = [{"crop": [0, 0, image.width, image.height], "regions": regions, "note": "페이지 전체"}]
    else:
        for band in bands:
            band["note"] = (
                f"긴 페이지 {band['axis']}축 band {band['index']}/{band['count']}, 원본 crop={band['crop']}"
            )
    output: dict[str, dict[str, Any]] = {}
    for band in bands:
        x0, y0, x1, y1 = band["crop"]
        view = image.crop((x0, y0, x1, y1)).convert("RGB")
        for chunk in _page_chunks(band["regions"]):
            region_ids = [str(region["region_id"]) for region in chunk]
            with_text = page_reader_text_from_env()
            # layout 이름은 넣지 않는다. `layout=image`를 받은 큰 배너를 모델이
            # "그림이라 글자 없음"으로 보고 비워 돌려준 사례가 있다.
            listing = "\n".join(
                f"- {region['region_id']} bbox={region.get('bbox')}"
                + (f" paddle_text={_short_text(region.get('text'))}" if with_text else "")
                for region in chunk
            )
            reference = (
                """박스와 paddle_text는 PaddleX 레이아웃·OCR 결과이며 **정답이 아니라 참고용**입니다.
- 박스가 글자를 일부만 덮거나, 서로 다른 항목을 한 박스에 섞었을 수 있습니다.
- paddle_text에는 오탈자·누락·띄어쓰기 붙음·줄 순서 뒤섞임이 있을 수 있습니다."""
                if with_text else
                """박스는 PaddleX 레이아웃 결과이며 **정답이 아니라 위치 참고용**입니다.
- 박스가 글자를 일부만 덮거나, 서로 다른 항목을 한 박스에 섞었을 수 있습니다."""
            )
            no_copy = (
                "- paddle_text를 그대로 베끼지 마세요. 이미지와 다르면 이미지를 따르세요.\n"
                if with_text else ""
            )
            prompt = f"""당신은 금융광고 페이지 전사기입니다.

첫 번째 이미지는 원본 페이지이고, 두 번째 이미지는 같은 페이지에 파란 박스와 REGION ID를
그린 것입니다. 박스 위치는 두 번째 이미지로 확인하고, 글자는 가림이 없는 첫 번째 이미지에서
읽으세요.

{reference}

아래 REGION ID {len(region_ids)}개를 **모두** region_readings에 정확히 한 번씩 넣고,
각 박스 안에 실제로 보이는 글자를 이미지에서 직접 읽어 text에 적으세요.
{no_copy}- 해당 박스 밖의 글자는 넣지 마세요. 줄바꿈은 보이는 대로 유지하세요.
- 숫자·금리·날짜·괄호·각주 기호는 보이는 그대로 적고, 요약·설명·추측하지 마세요.
- 읽을 수 없거나 글자가 없으면 text를 빈 문자열로 두세요.
- box_issue: none(정상), cut_off(박스 경계가 글자를 자름), mixed(서로 다른 항목이
  한 박스에 섞임), no_text(박스 안에 글자 없음).
- analysis는 한 문장만 쓰세요.

페이지 크기: {page['canvas']}
현재 이미지 범위: {band['note']}
{listing}
"""
            budget = min(
                16000,
                1500 + 3 * sum(len(str(r.get("text") or "")) for r in chunk) + 120 * len(chunk),
            )
            try:
                result = vlm_client.chat_json(
                    [
                        {"type": "text", "text": prompt},
                        vlm_client.image_part(view, box=(1400, 2400), quality=90),
                        vlm_client.image_part(
                            _overlay(view, chunk, [], offset=(x0, y0)), box=(1400, 2400), quality=90,
                        ),
                    ],
                    schema_name="parser_v2_page_reading",
                    schema=_page_reading_schema(region_ids),
                    max_tokens=budget,
                )
            except Exception as exc:  # noqa: BLE001
                # 한 묶음의 실패가 이미 성공한 다른 묶음의 판독을 버리지 않게 한다.
                # 빠진 Region은 호출측이 crop Reader로 다시 읽는다.
                if errors is not None:
                    errors.append({"region_ids": region_ids, "error": str(exc)[:200]})
                continue
            known = set(region_ids)
            for item in result.get("region_readings") or []:
                region_id = str(item.get("region_id") or "")
                if region_id in known and region_id not in output:
                    output[region_id] = {
                        "text": clean_text(item.get("text")),
                        "confidence": float(item.get("confidence") or 0.0),
                        "analysis": str(result.get("analysis") or "")[:300],
                        "box_issue": str(item.get("box_issue") or "none"),
                    }
    return output


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
    if judge_prompt_from_env() == "blind":
        # [실험] 후보 출처를 숨기고 용어 정규화를 금지한다. 출처를 알려 주면 모델이
        # "VLM 판독"을 더 믿고, 흔한 용어(분할상환)를 인쇄된 표기(할부상환)보다 고른다.
        prompt = f"""첨부 이미지는 광고 문서에서 잘라낸 영역 하나입니다.
아래 두 후보는 서로 다른 판독기가 만든 것이며 어느 쪽도 정답이 아닐 수 있습니다.
**이미지에 인쇄된 글자만** 기준으로 최종 텍스트를 한 글자씩 확인해 적으세요.

- 후보 1:
{reader_text}

- 후보 2:
{parser_text}

규칙:
- 금융 용어·상품명·상환방식 명칭을 더 흔하거나 올바르다고 생각되는 표현으로 바꾸지 마세요.
  이미지에 인쇄된 표기가 일반적인 용어와 달라도 인쇄된 그대로 적으세요.
- 후보끼리 다른 부분은 특히 이미지의 해당 글자를 직접 확인하세요.
- 두 후보 어디에도 없는 글자를 넣을 때는 이미지에서 분명히 보일 때만 넣으세요.
- 글자를 요약하거나 설명하지 말고 보이는 순서를 유지하세요.
- 숫자, 금리, 날짜, 괄호, 주석 기호를 임의로 고치거나 만들지 마세요.
- source는 후보 1이 맞으면 reader, 후보 2가 맞으면 parser, 고쳤으면 corrected입니다.
- analysis는 선택 이유 한 문장, text에는 최종 글자만 넣으세요.
"""
    else:
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


def read_visual_table(
    image: Image.Image, region: dict[str, Any], parser_text: str,
    reader_text: str | None = None,
) -> dict[str, Any] | None:
    """확정된 표의 bbox 전체를 이미지로 보며 항목-값 대응을 재판정한다."""
    crop = _crop(image, region["bbox"])
    if crop is None:
        return None
    reader_text = clean_text(reader_text)
    candidate_b_source = "기존 영역 VLM Judge" if reader_text else "표 이미지 독립 판독"
    if not reader_text:
        prompt = """첨부 이미지는 금융 문서에서 확인된 표 하나의 전체 영역입니다.
이미지에 보이는 글자를 빠짐없이 읽어 text에 적으세요.
각 행의 항목과 대응하는 값은 같은 줄에 `항목 | 값`으로 적고, 행은 줄바꿈으로 구분하세요.
여러 표가 나란히 있으면 각각의 제목과 항목-값 대응을 유지하세요.
표에 없는 행·숫자·단위·제목을 만들거나 반복하지 마세요.
셀 좌표나 JSON 표 구조는 만들지 말고, 읽을 수 없는 곳은 추측하지 마세요.
analysis에는 판독의 한계가 있으면 간단히 적으세요.
"""
        budget = min(8500, 1800 + 4 * len(parser_text))
        reader = vlm_client.chat_json(
            [{"type": "text", "text": prompt},
             vlm_client.image_part(crop, box=(2400, 1800), quality=94)],
            schema_name="parser_v2_visual_table_reading", schema=_SCHEMA,
            max_tokens=budget,
        )
        reader_text = clean_text(reader.get("text"))
        reader_confidence = float(reader.get("confidence") or 0.0)
    else:
        reader_confidence = None
    if not _normalized(reader_text):
        return None
    judge_prompt = f"""첨부 이미지는 같은 표의 전체 영역입니다. 두 후보를 이미지와 대조해
최종 text를 작성하세요. 후보 A에는 OCR과 PDF 텍스트가 겹쳐 같은 숫자나 표제가
두 번 들어갈 수 있습니다. 실제 이미지에 한 번만 보이면 한 번만 적으세요.
각 행의 항목과 그 값을 같은 줄에 `항목 | 값`으로 연결하고 행마다 줄바꿈하세요.
여러 표가 나란히 있으면 각각의 제목과 대응 관계를 유지하세요.
숫자·단위·각주를 만들거나 생략하지 마세요. 확신할 수 없는 글자를 추측하지 마세요.
source는 A가 맞으면 parser, B가 맞으면 reader, 둘을 고쳤으면 corrected입니다.
analysis는 판정 이유 한 문장, text에는 최종 판독만 적으세요.

후보 A (OCR/PDF 조립):\n{parser_text}

후보 B ({candidate_b_source}):\n{reader_text}
"""
    result = vlm_client.chat_json(
        [{"type": "text", "text": judge_prompt},
         vlm_client.image_part(crop, box=(2400, 1800), quality=94)],
        schema_name="parser_v2_visual_table_reading_judge", schema=_JUDGE_SCHEMA,
        max_tokens=min(9500, 2000 + 4 * max(len(parser_text), len(reader_text))),
    )
    return {
        "text": clean_text(result.get("text")),
        "confidence": float(result.get("confidence") or 0.0),
        "source": str(result.get("source") or "corrected"),
        "analysis": str(result.get("analysis") or "")[:300],
        "reader_text": reader_text,
        "reader_confidence": reader_confidence,
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
    """Region의 정본이 실제로 PDF 디지털 텍스트에 주로 의존하는지 확인한다."""
    source = str(region.get("text_source") or "")
    if source.startswith("document_processor"):
        return True
    # HWP 구조와 교차 확인된 원문은 줄 출처 비율과 무관하게 보존한다.
    if (
        (region.get("hwp_structure_validation") or {}).get("status") == "agrees"
        or (region.get("hwp_structure_corroboration") or {}).get("status") == "agrees"
    ):
        return True
    lines = region.get("lines") or []
    share = digital_line_share(lines)
    if share is not None:
        return share >= DIGITAL_PRIMARY_SHARE
    # 줄 증거가 없는 기존 Region에는 출처 표기를 그대로 적용한다.
    return source.startswith("digital_")


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
    mode = reader_mode_from_env()
    page_readings: dict[str, dict[str, Any]] = {}
    page_errors: list[dict[str, Any]] = []
    if mode == "page":
        targets = [region for region in page.get("regions") or [] if should_read(region, scope)]
        page_readings = read_page_regions(image, page, targets, page_errors) if targets else {}
        page["page_reader_errors"] = page_errors
        stats["page_reader_missing"] = 0
    for region in page.get("regions") or []:
        if not should_read(region, scope):
            stats["skipped"] += 1
            continue
        try:
            if mode == "page":
                reading = page_readings.get(str(region["region_id"]))
                if reading is None:
                    # 페이지 판독에서 빠진 Region은 기존 crop Reader로 되돌린다.
                    stats["page_reader_missing"] += 1
                    reading = read_region(image, region)
                    if reading is not None:
                        reading["fallback"] = "region_crop"
                elif (
                    not _normalized(reading.get("text"))
                    and len(_semantic_alnum(region.get("text"))) >= 4
                ):
                    # 페이지 판독이 비었는데 OCR에는 글자가 있다. 큰 배너나 얇게 맞붙은
                    # 줄에서 페이지 판독이 비는 사례가 있어 그 Region만 crop으로 다시 읽는다.
                    stats["page_blank_rechecked"] = stats.get("page_blank_rechecked", 0) + 1
                    recheck = read_region(image, region)
                    if recheck is not None:
                        reading = {**recheck, "blank_recheck": True}
            else:
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
        if _normalized(reader_text) and needs_judge(parser_text, reader_text):
            if agreement(parser_text, reader_text) >= AGREE:
                stats["strict_judge"] = stats.get("strict_judge", 0) + 1
            try:
                judge = judge_region(image, region, reading)
            except Exception as exc:  # noqa: BLE001
                region["vlm_judge"] = {"error": str(exc)[:200]}
                stats["judge_failed"] += 1
        stats[apply_reading(region, reading, judge)] += 1
        if mode == "page":
            region["vlm_reading"]["mode"] = (
                "region_fallback" if reading.get("fallback")
                else "region_blank_recheck" if reading.get("blank_recheck")
                else "page"
            )
            region["vlm_reading"]["box_issue"] = reading.get("box_issue")
        if review_rules_from_env() == "ocr":
            for code in ocr_review_codes(region, parser_text, reading, judge):
                flag(region, code)
                stats[f"review_{code}"] = stats.get(f"review_{code}", 0) + 1
    page["reading_stats"] = stats
    page["reader_mode"] = mode
    return stats
