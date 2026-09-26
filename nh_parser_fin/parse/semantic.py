"""fc87 Gemma 페이지 판정 — 소유권 판정과 라벨링을 **두 단계로 나눈다**.

1단계 소유권: 상품을 나누고 Region을 상품에 배정하며 복구 후보의 처리를 정한다.
2단계 라벨링: 상품마다 **그 상품의 템플릿 구분값만** enum으로 주고 라벨을 붙인다.

나눈 이유는 두 가지다.

- 템플릿은 상품군에 따라 달라지는데, 상품군은 소유권 판정이 나와야 알 수 있다.
  한 번에 하면 문서 템플릿 하나를 모든 상품에 강요하게 된다.
- 한 요청에 두 상품의 구분값을 함께 넣으면 모델이 상품1 Region에 템플릿B의
  구분값을 붙일 수 있다. enum은 둘 다 허용 목록에 있으니 막아주지 못한다.
"""
from __future__ import annotations

import copy
import re
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from ..vlm import client as vlm_client


PRODUCT_IDS = ["page_common", "product_1", "product_2", "product_3", "product_4", "unknown"]
ACTIONS = ["new_region", "attach_context", "page_common", "decorative", "needs_review"]
PRODUCT_GROUPS = ["예금성", "대출성", "카드", "투자성", "판단불가"]
NAME_SHOWN = ["노출", "미노출", "판단불가"]
# 표처럼 보이는 영역의 두 갈래. `table` 만 하나로 합친다.
TABLE_KINDS = ["table", "field_list"]
ABSTAIN = "해당없음"

# 한 라벨링 요청이 감당할 Region 수. 넘으면 나눠 부른다. 목록이 길어지면 뒤쪽
# 항목의 정확도가 떨어지고, 응답이 잘리면 그 묶음이 통째로 망가진다.
# 실측(2026-09-19): 모델이 Region 하나당 약 440자를 쓴다. 43개를 한 번에 물었더니
# 18,840자에서 잘렸다. 15개면 약 6,600자로 넉넉히 들어간다.
LABEL_CHUNK = 15

# 템플릿의 정식 구분값과 광고에서 흔히 쓰는 표기 차이. 파일명·Region ID가 아니라
# 업무 용어를 정규화하므로 새 입력에도 같은 규칙을 적용한다.
LABEL_ALIASES = {
    "가입대상": ("가입대상", "가입자격"),
    "가입금액": ("가입금액", "납입금액"),
    "가입기간": ("가입기간", "계약기간"),
    "금리": ("기본금리", "적용금리", "최고금리", "최저금리"),
    "우대금리": ("우대금리", "금리우대"),
    "중도해지이율": ("중도해지이율", "중도해지금리"),
    "만기후이율": ("만기후이율", "만기후금리"),
    "이자지급시기": ("이자지급시기", "이자지급방식", "이자지급방법", "이자지급주기"),
    "이자지급제한": ("이자지급제한",),
    "예상수취이자": ("예상수취이자", "예상이자"),
    "예금자보호": ("예금자보호법", "예금자보호", "예금보호"),
    "유의사항": ("유의사항",),
    "대출대상": ("대출대상",),
    "대출한도": ("대출한도",),
    "대출기간": ("대출기간",),
    "대출금리": ("대출금리",),
    "상환방법": ("상환방법", "상환방식"),
    "부대비용": ("부대비용",),
    "채권보전": ("채권보전",),
    "연회비": ("연회비",),
    "수수료": ("수수료",),
}

# 카탈로그의 예시는 실제 문구 형태를 보여 주지만 구분값 자체의 경계를 설명하지는
# 않는다. 짧은 정의와 대표적인 반례를 함께 줘서, 단어가 언급됐다는 이유만으로
# `유의사항` 문장을 금리·한도 등으로 오인하지 않게 한다.
LABEL_DEFINITIONS = {
    "회사명": "광고의 금융회사·카드사 명칭",
    "상품명": "광고 대상 금융상품의 고유 명칭",
    "가입대상": "예금·적금에 가입할 수 있는 고객 조건",
    "가입금액": "납입 가능 금액·한도·주기",
    "가입기간": "계약 또는 저축 기간",
    "금리": "예금·적금의 기본·적용·최고 금리 값",
    "우대금리": "조건 충족 시 추가되는 우대 금리와 조건",
    "중도해지이율": "만기 전 해지 시 적용 이율",
    "만기후이율": "만기 후 찾아가지 않을 때 적용 이율",
    "이자지급시기": "이자 지급 주기·시점·방식",
    "이자지급제한": "이자 지급이 제한되는 조건",
    "예상수취이자": "예시 원금·기간을 전제로 계산한 예상 이자",
    "예금자보호": "예금보험 보호 여부·한도 안내",
    "대출대상": "대출 신청 자격·소득·신용 조건",
    "대출한도": "대출 가능한 최소·최대 금액",
    "대출기간": "대출 계약·상환 기간",
    "대출금리": "대출에 적용되는 금리 범위와 산정 기준",
    "상환방법": "원금과 이자를 갚는 방식",
    "부대비용": "인지세·중도상환수수료 등 대출 관련 비용",
    "채권보전": "담보·보증서·신용 등 채권 보전 방식",
    "연회비": "카드 연간 이용 대가",
    "수수료": "서비스 이용·발급·거래에 드는 수수료",
    "심의번호": "준법감시인·협회 광고 심의 식별번호",
    "유의사항": "위험·제한·불이익·변동 가능성 등의 주의 문구",
    "상품내용": "상품의 주요 혜택·구조·서비스 설명",
    "기초지수": "투자상품 수익률 산정의 기준 지수",
    "이자율범위": "투자상품 등의 이자율 상·하한 범위",
    "모집기간 이자율": "모집 기간 및 그 기간에 적용되는 이자율",
    "모집인 유의사항": "모집인 관련 자격·행위·책임 주의 문구",
    "대출상담사": "대출상담사 이름·등록번호·소속 정보",
}

LABEL_NEGATIVE_EXAMPLES = {
    "가입대상": "유의사항에서 '가입대상은 변경될 수 있음'이라고 언급만 한 문장",
    "가입금액": "손실 가능성을 설명하며 금액이라는 단어만 쓴 문장",
    "가입기간": "광고 게시기간·이벤트 기간",
    "금리": "대출금리 또는 금리 변동 가능성만 경고하는 유의사항",
    "우대금리": "우대금리가 변경될 수 있다는 경고만 있는 문장",
    "대출대상": "대출자격이 변경될 수 있다는 유의사항",
    "대출한도": "대출한도가 달라질 수 있다는 유의사항 또는 금리 산정 예시의 가정 대출금액",
    "대출기간": "대출금리 산정 예시에서 금리 계산 전제로만 제시한 기간",
    "대출금리": "대출금리가 변경될 수 있다는 유의사항",
    "상환방법": "대출금리 산정 예시에서 금리 계산 전제로만 언급한 상환방식",
    "채권보전": "대출금리 산정 예시에서 금리 계산 전제로만 언급한 담보·보증",
    "상품명": "일반 명사나 다른 상품을 비교 목적으로 언급한 문구",
    "유의사항": "가입대상·금액·기간·금리 값을 항목별로 제시한 조건표",
}


def _short_text(value: Any, limit: int = 700) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _overlay(
    image: Image.Image,
    regions: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    *,
    offset: tuple[int, int] = (0, 0),
) -> Image.Image:
    out = image.convert("RGB").copy()
    draw = ImageDraw.Draw(out)
    font_size = max(14, min(30, out.width // 55))
    try:
        font = ImageFont.load_default(size=font_size)
    except TypeError:  # Pillow 구버전 호환
        font = ImageFont.load_default()
    width = max(2, out.width // 600)
    for item, color, key in (
        *((region, "#1976D2", "region_id") for region in regions),
        *((candidate, "#F57C00", "candidate_id") for candidate in candidates),
    ):
        box = item.get("bbox")
        if not box:
            continue
        ox, oy = offset
        local_box = [box[0] - ox, box[1] - oy, box[2] - ox, box[3] - oy]
        draw.rectangle(local_box, outline=color, width=width)
        label = str(item[key])
        x0, y0 = int(local_box[0]), max(0, int(local_box[1]) - font_size - 4)
        text_box = draw.textbbox((x0, y0), label, font=font)
        draw.rectangle(text_box, fill=color)
        draw.text((x0, y0), label, fill="white", font=font)
    return out


def _contact_sheet(
    image: Image.Image, regions: list[dict[str, Any]], *, columns: int = 3,
) -> Image.Image:
    """페이지 문맥과 별개로 각 Region 자체를 크게 볼 수 있는 판독 시트를 만든다."""
    tile_w, tile_h, header = 460, 300, 30
    rows = max(1, (len(regions) + columns - 1) // columns)
    sheet = Image.new("RGB", (tile_w * columns, tile_h * rows), "white")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=20)
    except TypeError:
        font = ImageFont.load_default()
    for index, region in enumerate(regions):
        box = region.get("bbox")
        if not box:
            continue
        x0, y0, x1, y1 = (int(value) for value in box)
        crop = image.crop((max(0, x0), max(0, y0), min(image.width, x1), min(image.height, y1))).convert("RGB")
        crop.thumbnail((tile_w - 12, tile_h - header - 12), Image.Resampling.LANCZOS)
        left = (index % columns) * tile_w
        top = (index // columns) * tile_h
        draw.rectangle([left, top, left + tile_w - 1, top + tile_h - 1], outline="#B0BEC5")
        draw.text((left + 6, top + 4), str(region["region_id"]), fill="#0D47A1", font=font)
        sheet.paste(crop, (left + 6, top + header))
    return sheet


def explicit_heading_evidence(text: Any, allowed: list[str]) -> dict[str, str]:
    """줄 시작의 명시적 표제어를 결정론적으로 찾는다.

    문장 중간의 단어 언급은 잡지 않는다. 예를 들어 "대출한도는 달라질 수
    있습니다" 같은 유의사항을 한도 값으로 오인하지 않기 위해서다.
    """
    found: dict[str, str] = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        candidate = re.sub(r"^[※*ㆍ·•▶▷▪■□◾§\-–—\s]+", "", line).strip()
        for canonical, aliases in LABEL_ALIASES.items():
            if canonical not in allowed or canonical in found:
                continue
            for alias in sorted(aliases, key=len, reverse=True):
                if canonical == "예금자보호" and re.match(
                    rf"^{re.escape(alias)}(?:에\s*따라|\s*[:：]|\s+|$)", candidate,
                ):
                    found[canonical] = line
                    break
                match = re.match(
                    rf"^{re.escape(alias)}(?:\s*[:：]|\s+|$)", candidate,
                )
                if match:
                    found[canonical] = line
                    break
    return found


def add_explicit_alias_labels(
    text: Any, selected: list[str], allowed: list[str],
) -> list[str]:
    """명시적 표제어 라벨을 VLM 결과보다 우선하여 보완한다."""
    output = [label for label in selected if label in allowed]
    headings = explicit_heading_evidence(text, allowed)
    for label in allowed:
        if label in headings and label not in output:
            output.append(label)
    return output


def _label_guide_text(
    labels: list[str], positive_examples: dict[str, list[str]] | None,
) -> str:
    examples = positive_examples or {}
    blocks = []
    for label in labels:
        positives = [
            _short_text(value, 180) for value in examples.get(label, [])
            if str(value or "").strip()
        ][:2]
        positive_text = " / ".join(positives) or "(템플릿 예시 없음)"
        blocks.append(
            f"- {label}\n"
            f"  정의: {LABEL_DEFINITIONS.get(label, f'{label}에 해당하는 실제 값·조건')}\n"
            f"  긍정 예시: {positive_text}\n"
            f"  부정 예시: {LABEL_NEGATIVE_EXAMPLES.get(label, '다른 항목에서 단어만 언급한 문장')}"
        )
    return "\n".join(blocks)


def constrain_title_labels(region: dict[str, Any], labels: list[str]) -> list[str]:
    """짧은 상품 제목에 페이지 다른 곳의 구분값이 번지는 것을 막는다."""
    layout = str((region.get("layout_observation") or {}).get("label") or "").casefold()
    compact = "".join(str(region.get("text") or "").split())
    if layout in {"doc_title", "paragraph_title", "title"} and len(compact) <= 60:
        if "상품명" in labels:
            return ["상품명"]
    return labels


def constrain_rate_calculation_labels(region: dict[str, Any], labels: list[str]) -> list[str]:
    """금리 산정 예시의 가정값이 기간·한도·상환조건으로 번지는 것을 막는다."""
    text = "".join(str(region.get("text") or "").split()).casefold()
    markers = sum(value in text for value in ("기준금리", "가산금리", "우대금리"))
    is_rate_example = markers >= 2 and ("적용시" in text or "기준," in text)
    if not is_rate_example or "대출금리" not in labels:
        return labels
    headings = explicit_heading_evidence(region.get("text"), labels)
    return [
        label for label in labels
        if label == "대출금리" or label in headings
    ]


# ── 1단계 · 소유권 ──────────────────────────────────────────────────


def _ownership_schema(region_ids: list[str], candidate_ids: list[str]) -> dict[str, Any]:
    region_enum = region_ids or ["__none__"]
    candidate_enum = candidate_ids or ["__none__"]
    return {
        "type": "object",
        "properties": {
            "analysis": {"type": "string"},
            "products": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "product_id": {"type": "string", "enum": PRODUCT_IDS[1:5]},
                        "name": {"type": "string"},
                        # 상품별 템플릿을 고르려면 상품군과 상품명 노출 여부가
                        # 상품 단위로 필요하다. 문서 단위 분류로는 섞인 문서를
                        # 처리할 수 없다.
                        "product_group": {"type": "string", "enum": PRODUCT_GROUPS},
                        "product_name_shown": {"type": "string", "enum": NAME_SHOWN},
                        "confidence": {"type": "number"},
                    },
                    "required": [
                        "product_id", "name", "product_group", "product_name_shown",
                        "confidence",
                    ],
                    "additionalProperties": False,
                },
            },
            "region_decisions": {
                "type": "array",
                # 상한이 없으면 모델이 같은 ID를 계속 다시 뱉어 응답이 끝나지 않는다.
                # 실측(2026-09-19): region_labels 15개짜리 요청이 35,376자까지 늘었다.
                # 각 ID를 정확히 한 번씩 받는 것이 계약이므로 개수를 고정한다.
                "minItems": len(region_ids),
                "maxItems": len(region_ids),
                "items": {
                    "type": "object",
                    "properties": {
                        "region_id": {"type": "string", "enum": region_enum},
                        "product_id": {"type": "string", "enum": PRODUCT_IDS},
                        "confidence": {"type": "number"},
                        # Region마다 자유 서술 reason을 받으면 응답의 대부분이
                        # 거기에 들어간다. 실측: Region 30개 페이지에서 응답이
                        # 14,827자까지 늘어 잘렸고, 예산을 키우자 120초 타임아웃이
                        # 났다. 근거는 analysis 한 곳에 모으고 행마다 두지 않는다.
                    },
                    "required": ["region_id", "product_id", "confidence"],
                    "additionalProperties": False,
                },
            },
            "recovery_decisions": {
                "type": "array",
                "minItems": len(candidate_ids),
                "maxItems": len(candidate_ids),
                "items": {
                    "type": "object",
                    "properties": {
                        "candidate_id": {"type": "string", "enum": candidate_enum},
                        "action": {"type": "string", "enum": ACTIONS},
                        "target_region_id": {"type": "string", "enum": ["", *region_ids]},
                        "product_id": {"type": "string", "enum": PRODUCT_IDS},
                        "confidence": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                    "required": [
                        "candidate_id", "action", "target_region_id", "product_id",
                        "confidence", "reason",
                    ],
                    "additionalProperties": False,
                },
            },
            "table_areas": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        # 좌표가 아니라 **ID** 를 받는다. 백분율을 요구해도 모델이
                        # 픽셀을 주거나(11건 중 4건) 실행마다 크게 흔들린다 —
                        # 실측(2026-09-20): 같은 표를 한 번은 [10,55,40,65],
                        # 다음 실행에서는 [46,56,58,65] 로 찍어 실제 표와 3% 만
                        # 겹쳤다. 목록에 있는 ID 를 고르게 하면 추정이 사라진다.
                        "member_ids": {
                            "type": "array",
                            "minItems": 2,
                            "items": {
                                "type": "string",
                                "enum": (region_ids + candidate_ids) or ["__none__"],
                            },
                        },
                        # 시각적으로는 둘 다 격자라 기하학으로 구분할 수 없다.
                        # 합칠지 말지는 의미 판정이므로 여기서 받는다.
                        "kind": {"type": "string", "enum": TABLE_KINDS},
                        "note": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["member_ids", "kind", "note", "confidence"],
                    "additionalProperties": False,
                },
            },
            "missing_visible_text": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "near_id": {"type": "string"},
                        "y_ratio": {"type": "number"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["text", "near_id", "y_ratio", "confidence"],
                    "additionalProperties": False,
                },
            },
        },
        "required": [
            "analysis", "products", "region_decisions", "recovery_decisions",
            "table_areas", "missing_visible_text",
        ],
        "additionalProperties": False,
    }


def _listing(regions: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> str:
    rows = ["[기존 REGION — 파란색]"]
    for region in regions:
        rows.append(
            f"- {region['region_id']} bbox={region.get('bbox')} "
            f"layout={region.get('label')} text={_short_text(region.get('text'))}"
        )
    rows.append("\n[복구 CANDIDATE — 주황색]")
    for candidate in candidates:
        rows.append(
            f"- {candidate['candidate_id']} bbox={candidate['bbox']} "
            f"text={_short_text(candidate.get('text'))}"
        )
    return "\n".join(rows)


def analyze_page_ownership(
    image: Image.Image,
    page: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    crop_offset: tuple[int, int] = (0, 0),
    band_note: str = "페이지 전체",
) -> dict[str, Any]:
    """상품 소유권과 복구 후보 처리를 판정한다. 라벨은 여기서 붙이지 않는다."""
    regions = page["regions"]
    region_ids = [str(region["region_id"]) for region in regions]
    candidate_ids = [str(candidate["candidate_id"]) for candidate in candidates]
    prompt = f"""당신은 농협 금융광고 페이지 구조 판독기입니다.

이미지의 파란 박스는 기존 PaddleX Region이고 주황 박스는 OCR/PDF 줄로 만든 복구 후보입니다.
다음 작업을 한 번에 수행하세요. **이 단계에서는 라벨을 붙이지 않습니다.**

1. products: 실제 상품이 여러 개면 product_1부터 시각적 순서대로 정의하세요.
   상품마다 product_group(예금성/대출성/카드/투자성)과 상품명 노출 여부를 따로 판정하세요.
   한 광고 안에 성격이 다른 상품이 섞여 있을 수 있으므로 파일명에 끌려가지 마세요.
2. region_decisions: 아래 REGION ID를 정확히 한 번씩 반환하고 상품 소유권을 정하세요.
   회사명·로고·심의번호·연락처·공통 안내문구, 그리고 특정 상품 하나가 아니라
   광고 전체에 걸리는 유의사항은 **반드시 page_common**입니다.
   unknown은 상품 소속도 공통도 아니라고 판단될 때만 쓰는 마지막 선택지입니다.
3. recovery_decisions: 아래 CANDIDATE ID를 정확히 한 번씩 반환하세요.
   - new_region: 독립 의미 영역
   - attach_context: target_region_id의 설명·유의사항이지만 bbox는 별도 보존
   - page_common: 회사명·심의번호 등 공통 영역
   - decorative: 심의 텍스트로 쓰지 않는 순수 장식
   - needs_review: 확정 불가
4. table_areas: 행과 열로 나란히 놓인 영역이 있으면, 거기에 속한 **REGION/CANDIDATE
   ID를 member_ids에 모두** 적으세요. 좌표는 쓰지 않습니다.
   PaddleX가 표로 잡지 못한 영역도 보이는 대로 적고, kind를 반드시 구분하세요.
   - table: 머리글이 있고 칸 전체가 **하나의 항목**을 설명하는 진짜 표.
     예) `구분 | 적립율` 머리글 아래 값이 들어찬 표
   - field_list: 왼쪽이 항목명, 오른쪽이 그 값인 **서로 다른 항목의 나열**.
     예) `대출대상 | …`, `대출한도 | …`, `대출기간 | …` 이 세로로 이어지는 블록
    둘을 헷갈리면 서로 다른 항목이 한 덩어리로 묶여 항목별 구분이 사라집니다.
   같은 높이에 있어도 서로 다른 상품 패널에 속한 조각은 절대 같은 table_areas로
   묶지 마세요. 표 하나가 여러 작은 ID로 쪼개졌다면 그 ID를 빠짐없이 고르세요.
5. missing_visible_text: 이미지에는 분명히 보이지만 REGION/CANDIDATE 목록에 전혀 없는 문구만 적으세요.

중요 규칙:
- analysis는 세 문장 이내로 쓰세요. 길면 응답이 잘립니다.
- bbox를 새로 만들지 말고 제공된 ID만 선택하세요(table_areas의 근사 위치는 예외).
- 다른 상품의 내용을 같은 product_id에 섞지 마세요.
- 표·고지·상품 설명이라는 PaddleX layout 이름은 힌트일 뿐 정답으로 믿지 마세요.
- 원문에 없는 문구를 추측하지 마세요.

페이지 크기: {page['canvas']}
현재 이미지 범위: {band_note}
{_listing(regions, candidates)}
"""
    result = vlm_client.chat_json(
        [
            {"type": "text", "text": prompt},
            vlm_client.image_part(
                _overlay(image, regions, candidates, offset=crop_offset),
                box=(1400, 2400), quality=90,
            ),
        ],
        schema_name="parser_v2_page_ownership",
        schema=_ownership_schema(region_ids, candidate_ids),
        # 한글은 토큰당 글자 수가 적어 영문 기준 예산으로 잡으면 잘린다. 실측:
        # `4. 카드상품`(Region 30 + 후보 2)의 응답이 14,827자였고 5,360토큰
        # 예산에서 잘려 JSON 파싱이 3회 모두 실패했다.
        max_tokens=min(10000, 1500 + 160 * len(region_ids) + 320 * len(candidate_ids)),
    )
    return validate_ownership(result, region_ids, candidate_ids)


def _center(item: dict[str, Any], axis: str) -> float:
    box = item.get("bbox") or [0, 0, 0, 0]
    if axis == "x":
        return (float(box[0]) + float(box[2])) / 2
    return (float(box[1]) + float(box[3])) / 2


def partition_for_semantic_bands(
    page: dict[str, Any], candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """긴 페이지 ID를 겹치지 않는 2~4개 의미 판정 밴드에 정확히 한 번 배정한다."""
    tiling = page.get("tiling") or {}
    if tiling.get("decision") != "split":
        return []
    axis = str(tiling.get("axis") or "y")
    width, height = (int(value) for value in page["canvas"])
    limit = width if axis == "x" else height
    count = min(4, max(2, int(tiling.get("pieces") or 2)))
    overlap = min(200, max(60, int(limit * 0.03)))
    output = []
    for index in range(count):
        start = round(limit * index / count)
        end = round(limit * (index + 1) / count)
        upper = end if index + 1 < count else limit + 1
        regions = [
            item for item in page["regions"]
            if start <= _center(item, axis) < upper
        ]
        selected_candidates = [
            item for item in candidates
            if start <= _center(item, axis) < upper
        ]
        if not regions and not selected_candidates:
            continue
        crop_start, crop_end = max(0, start - overlap), min(limit, end + overlap)
        crop = (
            [crop_start, 0, crop_end, height]
            if axis == "x" else [0, crop_start, width, crop_end]
        )
        output.append({
            "index": index + 1,
            "count": count,
            "axis": axis,
            "range": [start, end],
            "crop": crop,
            "regions": regions,
            "candidates": selected_candidates,
        })
    return output


def analyze_page_context(
    image: Image.Image,
    page: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """일반 페이지는 1회, 긴 페이지는 2~4개 밴드로 소유권 판정을 수행한다."""
    bands = partition_for_semantic_bands(page, candidates)
    if not bands:
        result = analyze_page_ownership(image, page, candidates)
        result["semantic_bands"] = [{"index": 1, "count": 1, "mode": "whole"}]
        return result

    combined: dict[str, Any] = {
        "analysis": "",
        "products": [],
        "region_decisions": [],
        "recovery_decisions": [],
        "table_areas": [],
        "missing_visible_text": [],
        "semantic_bands": [],
    }
    products: dict[str, dict[str, Any]] = {}
    width, height = (int(value) for value in page["canvas"])
    for band in bands:
        x0, y0, x1, y1 = band["crop"]
        cropped = image.crop((x0, y0, x1, y1))
        result = analyze_page_ownership(
            cropped,
            {**page, "regions": band["regions"]},
            band["candidates"],
            crop_offset=(x0, y0),
            band_note=(
                f"긴 페이지 {band['axis']}축 band {band['index']}/{band['count']}, "
                f"원본 crop={band['crop']}. product_1은 문서 전체에서 같은 상품 ID입니다."
            ),
        )
        combined["analysis"] += (
            ("\n" if combined["analysis"] else "")
            + f"band {band['index']}: {result.get('analysis') or ''}"
        )
        combined["region_decisions"].extend(result["region_decisions"])
        combined["recovery_decisions"].extend(result["recovery_decisions"])
        combined["missing_visible_text"].extend(result.get("missing_visible_text") or [])
        # 밴드 crop 기준 백분율을 페이지 전체 기준으로 되돌린다. 그러지 않으면
        # 밴드 2의 표가 페이지 상단에 있는 것으로 기록된다.
        for area in result.get("table_areas") or []:
            box = [float(value) for value in area.get("approx_bbox_pct") or []]
            if len(box) != 4:
                continue
            combined["table_areas"].append({
                **area,
                "approx_bbox_pct": [
                    (x0 + box[0] / 100 * (x1 - x0)) / width * 100,
                    (y0 + box[1] / 100 * (y1 - y0)) / height * 100,
                    (x0 + box[2] / 100 * (x1 - x0)) / width * 100,
                    (y0 + box[3] / 100 * (y1 - y0)) / height * 100,
                ],
            })
        for product in result.get("products") or []:
            products.setdefault(str(product.get("product_id")), copy.deepcopy(product))
        combined["semantic_bands"].append({
            key: copy.deepcopy(band[key])
            for key in ("index", "count", "axis", "range", "crop")
        })
    combined["products"] = list(products.values())
    validated = validate_ownership(
        combined,
        [str(region["region_id"]) for region in page["regions"]],
        [str(candidate["candidate_id"]) for candidate in candidates],
    )
    validated["semantic_bands"] = combined["semantic_bands"]
    return validated


def validate_ownership(
    result: dict[str, Any],
    region_ids: list[str],
    candidate_ids: list[str],
) -> dict[str, Any]:
    """모르는 ID·중복을 버리고 누락 ID는 검수 대상으로 보충한다."""

    def unique(items: list[dict[str, Any]], key: str, known: set[str]) -> dict[str, dict[str, Any]]:
        output = {}
        for item in items or []:
            identifier = str(item.get(key) or "")
            if identifier in known and identifier not in output:
                output[identifier] = item
        return output

    region_map = unique(result.get("region_decisions") or [], "region_id", set(region_ids))
    for region_id in region_ids:
        region_map.setdefault(region_id, {
            "region_id": region_id,
            "product_id": "unknown",
            "confidence": 0.0,
            "reason": "VLM 응답에서 누락되어 검수 필요",
        })
    for item in region_map.values():
        # 소유권 스키마에서 행별 reason을 뺐다. 누락 보충분만 사유를 갖는다.
        item.setdefault("reason", "")
    candidate_map = unique(
        result.get("recovery_decisions") or [], "candidate_id", set(candidate_ids)
    )
    for candidate_id in candidate_ids:
        candidate_map.setdefault(candidate_id, {
            "candidate_id": candidate_id,
            "action": "needs_review",
            "target_region_id": "",
            "product_id": "unknown",
            "confidence": 0.0,
            "reason": "VLM 응답에서 누락되어 검수 필요",
        })
    for item in [*region_map.values(), *candidate_map.values()]:
        if item.get("product_id") not in PRODUCT_IDS:
            item["product_id"] = "unknown"
    return {
        "analysis": str(result.get("analysis") or ""),
        "products": list(result.get("products") or []),
        "region_decisions": list(region_map.values()),
        "recovery_decisions": list(candidate_map.values()),
        "table_areas": list(result.get("table_areas") or []),
        "missing_visible_text": list(result.get("missing_visible_text") or []),
    }


# ── 2단계 · 상품별 라벨링 ───────────────────────────────────────────


def _label_schema(region_ids: list[str], labels: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "analysis": {"type": "string"},
            "region_labels": {
                "type": "array",
                # 비어 있는 배열도 스키마상 유효하면 재시도가 걸리지 않는다.
                # 실측: 모델이 판정을 analysis 문장에만 쓰고 배열을 비워 보냈다.
                # 상한이 없으면 반대로 같은 ID를 반복해 응답이 끝나지 않는다.
                "minItems": len(region_ids),
                "maxItems": len(region_ids),
                "items": {
                    "type": "object",
                    "properties": {
                        "region_id": {"type": "string", "enum": region_ids or ["__none__"]},
                        "labels": {
                            "type": "array",
                            "items": {"type": "string", "enum": labels or ["__none__"]},
                            # fc87의 구조화 출력 엔진은 JSON Schema `uniqueItems`를
                            # HTTP 400으로 거부한다. 중복은 validate_labels에서 제거한다.
                            "maxItems": len(labels),
                        },
                        "evidence": {
                            "type": "array",
                            "maxItems": len(labels),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {
                                        "type": "string",
                                        "enum": labels or ["__none__"],
                                    },
                                    "quote": {"type": "string"},
                                },
                                "required": ["label", "quote"],
                                "additionalProperties": False,
                            },
                        },
                        "confidence": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                    "required": [
                        "region_id", "labels", "evidence", "confidence", "reason",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["analysis", "region_labels"],
        "additionalProperties": False,
    }


def analyze_product_labels(
    image: Image.Image,
    page: dict[str, Any],
    regions: list[dict[str, Any]],
    *,
    product_id: str,
    product_name: str | None,
    template_id: str | None,
    labels: list[str],
    positive_examples: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """한 상품(또는 페이지 공통) 소속 Region에만 그 상품의 구분값을 붙인다."""
    if not regions or not labels:
        return {"analysis": "", "region_labels": [], "calls": 0}

    scope = (
        "페이지 공통 영역입니다. 특정 상품에 속하지 않는 회사명·유의사항·심의번호입니다."
        if product_id == "page_common"
        else f"상품 {product_id}({product_name or '이름 미상'}) 소속 영역입니다."
    )
    merged: list[dict[str, Any]] = []
    analyses: list[str] = []
    calls = 0
    guide_text = _label_guide_text(labels, positive_examples)
    for start in range(0, len(regions), LABEL_CHUNK):
        chunk = regions[start:start + LABEL_CHUNK]
        region_ids = [str(region["region_id"]) for region in chunk]
        rows = "\n".join(
            f"- {region['region_id']} bbox={region.get('bbox')} "
            f"explicit_labels={list(explicit_heading_evidence(region.get('text'), labels))} "
            f"text={_short_text(region.get('text'))}"
            for region in chunk
        )
        prompt = f"""당신은 농협 금융광고 구분값 라벨러입니다.

{scope}
적용 템플릿: {template_id or '공통(모든 템플릿 교집합)'}
허용 라벨: {', '.join(labels)}

선택된 템플릿의 라벨 정의와 예시:
{guide_text}

아래 REGION ID {len(region_ids)}개를 **모두** region_labels 배열에 정확히 한 번씩
넣고, 각 Region에 실제로 들어 있는 구분값을 **모두** labels 배열에 넣으세요.

- 판정은 반드시 region_labels 배열에 넣으세요. analysis에 문장으로 적으면 무효입니다.
- analysis에는 전체 요약 한 문장만, 각 reason은 40자 이내로 쓰세요.
- 허용 라벨 중 맞는 것이 없으면 labels=[]로 두세요. 억지로 고르지 마세요.
- 각 labels 항목마다 evidence에 label과 해당 Region 원문에서 그대로 복사한 정확한
  quote를 하나씩 넣으세요. labels=[]이면 evidence=[]입니다.
- REGION 목록의 explicit_labels는 줄 시작의 명시적 표제어를 결정론적으로 찾은
  결과입니다. 해당 표제어가 실제 값을 이끄는 한 반드시 유지하세요.
- 한 Region에 가입대상과 가입금액처럼 서로 다른 구분값이 함께 있으면
  labels=["가입대상", "가입금액"]처럼 모두 반환하세요.
- 이 목록은 이미 이 상품 소속으로 확정된 영역입니다. 상품 소유권을 다시 판정하지 마세요.
- 첫 이미지는 페이지 전체의 파란 박스로 위치와 주변 문맥을 보여 줍니다.
- 두 번째 이미지는 같은 Region을 ID별로 확대한 시트입니다. **라벨은 해당 Region
  자체의 글자와 구조에 근거할 때만** 붙이세요. 다른 박스나 주변 문단에만 있는
  항목을 가져오면 안 됩니다.
- 유의사항 문장에 `대출한도`, `대출금리`라는 단어가 언급되더라도 그 문장이 해당
  항목의 실제 값을 설명하는 것이 아니면 그 라벨을 붙이지 마세요.
- 기준금리·가산금리·우대금리로 최종 대출금리를 설명하는 산정 예시 안의 대출금액,
  대출기간, 상환방식, 신용등급, 담보는 계산 전제입니다. 별도 표제어가 없는 한 해당
  Region에는 `대출금리`만 붙이고 전제 항목의 라벨은 붙이지 마세요.
- 짧은 상품 제목 Region에는 상품명 외의 페이지 구분값을 붙이지 마세요.

페이지 크기: {page['canvas']}
{rows}
"""
        parts = [
            {"type": "text", "text": prompt},
            vlm_client.image_part(
                _overlay(image, chunk, []), box=(1400, 2400), quality=90,
            ),
            vlm_client.image_part(
                _contact_sheet(image, chunk), box=(1400, 1600), quality=92,
            ),
        ]
        schema = _label_schema(region_ids, labels)
        result = vlm_client.chat_json(
            parts,
            schema_name="parser_v2_product_labels",
            schema=schema,
            max_tokens=min(12000, 1200 + 500 * len(region_ids)),
        )
        calls += 1
        if not (result.get("region_labels") or []):
            # 배열이 비면 모든 Region이 "누락되어 검수 필요"로 떨어진다. 실측에서
            # 모델이 판정을 analysis 문장에만 담아 보낸 적이 있어 한 번 다시 묻는다.
            retry = list(parts)
            retry[0] = {
                "type": "text",
                "text": prompt + (
                    "\n\n직전 응답은 region_labels가 비어 무효였습니다. "
                    f"설명 없이 REGION ID {len(region_ids)}개의 판정만 배열로 반환하세요."
                ),
            }
            result = vlm_client.chat_json(
                retry,
                schema_name="parser_v2_product_labels",
                schema=schema,
                max_tokens=min(12000, 1200 + 500 * len(region_ids)),
            )
            calls += 1
        analyses.append(str(result.get("analysis") or ""))
        merged.extend(
            validate_labels(
                result,
                region_ids,
                labels,
                region_texts={
                    str(region["region_id"]): str(region.get("text") or "")
                    for region in chunk
                },
            )["region_labels"]
        )
    return {
        "analysis": "\n".join(value for value in analyses if value),
        "region_labels": merged,
        "calls": calls,
    }


def validate_labels(
    result: dict[str, Any], region_ids: list[str], labels: list[str],
    *, region_texts: dict[str, str] | None = None,
) -> dict[str, Any]:
    """허용 라벨과 원문 근거를 검증하고 누락 Region을 검수 대상으로 채운다."""
    allowed = set(labels)
    known_ids = set(region_ids)
    output: dict[str, dict[str, Any]] = {}
    for item in result.get("region_labels") or []:
        region_id = str(item.get("region_id") or "")
        if region_id not in known_ids or region_id in output:
            continue
        selected = []
        for label in item.get("labels") or []:
            if label in allowed and label not in selected:
                selected.append(label)
        valid_evidence = []
        source = " ".join(str((region_texts or {}).get(region_id) or "").split())
        for evidence in item.get("evidence") or []:
            label = str(evidence.get("label") or "")
            quote = str(evidence.get("quote") or "").strip()
            normalized_quote = " ".join(quote.split())
            if (
                label in selected
                and label not in {entry["label"] for entry in valid_evidence}
                and quote
                and (region_texts is None or normalized_quote in source)
            ):
                valid_evidence.append({"label": label, "quote": quote})

        reason = str(item.get("reason") or "")
        contradiction = bool(selected) and any(
            phrase in reason.replace(" ", "")
            for phrase in ("허용라벨없음", "해당없음", "근거없음")
        )
        if contradiction:
            selected = []
            valid_evidence = []
            reason = f"모순 응답 제거: {reason}"
        elif region_texts is not None:
            supported = {entry["label"] for entry in valid_evidence}
            selected = [label for label in selected if label in supported]
        output[region_id] = {
            "region_id": region_id,
            "labels": selected,
            "evidence": [
                entry for entry in valid_evidence if entry["label"] in selected
            ],
            "confidence": float(item.get("confidence") or 0.0),
            "reason": reason,
        }
    for region_id in region_ids:
        output.setdefault(region_id, {
            "region_id": region_id,
            "labels": [],
            "evidence": [],
            "confidence": 0.0,
            "reason": "VLM 응답에서 누락되어 검수 필요",
        })
    return {
        "analysis": str(result.get("analysis") or ""),
        "region_labels": [output[region_id] for region_id in region_ids],
    }


# ── 3단계 · 줄 단위 분할 ────────────────────────────────────────────
