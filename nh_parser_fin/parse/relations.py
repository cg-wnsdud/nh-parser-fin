"""이미 찾은 표 구역의 원문 줄을 의미 있는 짝으로 연결한다.

VLM은 줄 ID의 관계만 고른다. 문구와 좌표는 PDFium/OCR/HWP 증거에서 가져온다.
표가 셀 격자이든 항목명-값 목록이든 같은 계약을 사용한다.
"""
from __future__ import annotations

from collections import Counter
import re
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from ..vlm import client as vlm_client


RELATION_TYPES = (
    "attribute_value", "condition_result", "option_description", "category_item_value", "other",
)
_RATE_HEADER = re.compile(r"금리|이율|이자율")
_RATE_VALUE = re.compile(r"\d+(?:\.\d+)?\s*%(?:p|포인트)?", re.I)
_NUMERIC_CELL = re.compile(r"[\d,]+(?:\.\d+)?\s*(?:%p?|원|만원|억원)?", re.I)


def _union(boxes: list[list[int]]) -> list[int]:
    return [
        min(box[0] for box in boxes), min(box[1] for box in boxes),
        max(box[2] for box in boxes), max(box[3] for box in boxes),
    ]


def _text(refs: list[str], evidence: dict[str, dict[str, Any]]) -> str:
    # 모델 출력 순서가 뒤섞여도 원본의 화면 읽기 순서로 문구를 보존한다.
    lines = sorted(
        (evidence[ref] for ref in refs),
        key=lambda line: (line["bbox"][1], line["bbox"][0]),
    )
    return "\n".join(str(line["text"]).strip() for line in lines if str(line["text"]).strip())


def _schema(aliases: list[str], labels: list[str]) -> dict[str, Any]:
    refs = {
        "type": "array", "items": {"type": "string", "enum": aliases or ["__none__"]},
        "maxItems": max(1, len(aliases)),
    }
    return {
        "type": "object",
        "properties": {
            "structure_type": {"type": "string", "enum": ["table", "field_list", "not_table"]},
            "structure_confidence": {"type": "number"},
            "field_label": {"type": "string", "enum": ["", *labels]},
            "relations": {
                "type": "array", "maxItems": len(aliases),
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": list(RELATION_TYPES)},
                        "key_refs": refs,
                        "value_refs": refs,
                        "context_refs": {
                            "type": "array", "maxItems": 3,
                            "items": {"type": "string", "enum": aliases or ["__none__"]},
                        },
                        "confidence": {"type": "number"},
                    },
                    "required": ["type", "key_refs", "value_refs", "context_refs", "confidence"],
                    "additionalProperties": False,
                },
            },
            "analysis": {"type": "string"},
        },
        "required": ["structure_type", "structure_confidence", "field_label", "relations", "analysis"],
        "additionalProperties": False,
    }


def _crop(
    image: Image.Image, evidence: dict[str, dict[str, Any]],
    fallback_bbox: list[int] | None = None,
) -> Image.Image:
    bbox = _union([line["bbox"] for line in evidence.values()]) if evidence else fallback_bbox
    if not bbox:
        bbox = [0, 0, image.width, image.height]
    pad = 28
    left, top = max(0, bbox[0] - pad), max(0, bbox[1] - pad)
    right, bottom = min(image.width, bbox[2] + pad), min(image.height, bbox[3] + pad)
    crop = image.crop((left, top, right, bottom)).convert("RGB")
    draw = ImageDraw.Draw(crop)
    try:
        font = ImageFont.load_default(size=17)
    except TypeError:
        font = ImageFont.load_default()
    for alias, line in evidence.items():
        x0, y0, x1, y1 = line["bbox"]
        x0, y0, x1, y1 = x0 - left, y0 - top, x1 - left, y1 - top
        draw.rectangle((x0, y0, x1, y1), outline="#2563eb", width=2)
        tx, ty = max(0, x0), max(0, y0 - 18)
        label_box = draw.textbbox((tx, ty), alias, font=font)
        draw.rectangle(label_box, fill="#1d4ed8")
        draw.text((tx, ty), alias, fill="white", font=font)
    return crop


def _area_evidence(
    page: dict[str, Any], area: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    regions = {str(region["region_id"]): region for region in page.get("regions") or []}
    members = [str(value) for value in area.get("member_ids") or [] if str(value) in regions]
    evidence: dict[str, dict[str, Any]] = {}
    for region_id in members:
        region = regions[region_id]
        for line in region.get("lines") or []:
            ref = str(line.get("line_ref") or "")
            text = str(line.get("text") or "").strip()
            bbox = line.get("bbox")
            if ref and text and isinstance(bbox, list) and len(bbox) == 4:
                evidence.setdefault(ref, {
                    "text": text, "bbox": [int(value) for value in bbox],
                    "region_id": region_id,
                })
        # OCR이 없어도 구조 파서가 주는 원문과 영역 좌표는 사용 가능하다.
        if not region.get("lines") and str(region.get("text") or "").strip() and region.get("bbox"):
            evidence[f"{region_id}/text"] = {
                "text": str(region["text"]).strip(),
                "bbox": [int(value) for value in region["bbox"]],
                "region_id": region_id,
            }
    # PDF 디지털 줄과 OCR 줄이 같은 글자를 같은 위치에서 각각 돌려주는 경우가
    # 있다. 원문 참조 하나만 관계 후보로 남기되 P1 증거는 그대로 보존한다.
    unique: dict[str, dict[str, Any]] = {}
    for ref, line in evidence.items():
        box = line["bbox"]
        normalized = "".join(str(line["text"]).split())
        duplicate = False
        for kept in unique.values():
            if normalized != "".join(str(kept["text"]).split()):
                continue
            other = kept["bbox"]
            overlap = max(0, min(box[2], other[2]) - max(box[0], other[0])) * max(
                0, min(box[3], other[3]) - max(box[1], other[1])
            )
            smaller = min(
                max(1, (box[2] - box[0]) * (box[3] - box[1])),
                max(1, (other[2] - other[0]) * (other[3] - other[1])),
            )
            if overlap / smaller >= 0.85:
                duplicate = True
                break
        if not duplicate:
            unique[ref] = line
    return unique, members


def build_relations(
    result: dict[str, Any], aliases: dict[str, str],
    evidence: dict[str, dict[str, Any]], labels: list[str],
) -> list[dict[str, Any]]:
    """모델이 고른 짝만 검증한다. 원문에 없는 문구·좌표가 들어올 경로가 없다."""
    output: list[dict[str, Any]] = []
    used_values: set[str] = set()
    items = [
        expanded
        for item in result.get("relations") or []
        if isinstance(item, dict)
        for expanded in _split_numeric_rows(item, aliases, evidence)
    ]
    for item in items:
        if not isinstance(item, dict):
            continue
        groups: list[list[str]] = []
        valid = True
        for name in ("key_refs", "value_refs", "context_refs"):
            raw = item.get(name) or []
            if not isinstance(raw, list) or any(str(alias) not in aliases for alias in raw):
                valid = False
                break
            groups.append(list(dict.fromkeys(aliases[str(alias)] for alias in raw)))
        if not valid:
            continue
        keys, values, context = groups
        if not keys or not values or set(keys) & set(values) or set(values) & used_values:
            continue
        # 좁은 왼쪽 분류 칸과 오른쪽의 선택지가 함께 key_refs에 들어온 경우
        # 짧은 왼쪽 문구를 공통 분류로 옮긴다. 같은 원문 줄을 버리지는 않는다.
        if len(keys) > 1:
            rightmost = max(evidence[ref]["bbox"][0] for ref in keys)
            categories = [
                ref for ref in keys
                if rightmost - evidence[ref]["bbox"][0] >= 45
                and len("".join(evidence[ref]["text"].split())) <= 15
            ]
            if len(categories) < len(keys):
                keys = [ref for ref in keys if ref not in categories]
                context = list(dict.fromkeys([*context, *categories]))
        if len(values) == 1 and len(keys) > 1 and all(
            len(evidence[ref]["text"].strip()) <= 60 for ref in keys
        ):
            # 서로 다른 물리적 행의 선택지가 한 값에 붙은 경우, 값과 실제로
            # 같은 행인 선택지만 남긴다. 공통 분류의 긴 셀은 위에서 분리했다.
            value_box = evidence[values[0]]["bbox"]
            aligned = [
                ref for ref in keys
                if min(evidence[ref]["bbox"][3], value_box[3])
                > max(evidence[ref]["bbox"][1], value_box[1])
            ]
            if len(aligned) == 1:
                keys = aligned
        if any(_RATE_VALUE.search(evidence[ref]["text"]) for ref in values):
            values = [
                ref for ref in values
                if not (
                    _RATE_HEADER.search(evidence[ref]["text"])
                    and not _RATE_VALUE.search(evidence[ref]["text"])
                    and len(evidence[ref]["text"].strip()) <= 20
                )
            ]
        if result.get("structure_type") == "field_list":
            key_box = _union([evidence[ref]["bbox"] for ref in keys])
            value_box = _union([evidence[ref]["bbox"] for ref in values])
            if min(key_box[3], value_box[3]) <= max(key_box[1], value_box[1]):
                # 위아래로 이어지는 일반 문단은 억지 항목-값 관계를 만들지 않는다.
                continue
        confidence = max(0.0, min(1.0, float(item.get("confidence") or 0.0)))
        if confidence < 0.7:
            continue
        refs = list(dict.fromkeys([*keys, *values]))
        all_refs = list(dict.fromkeys([*refs, *context]))
        boxes = [evidence[ref]["bbox"] for ref in refs]
        used_values.update(values)
        relation_type = str(item.get("type") or "other")
        if relation_type not in RELATION_TYPES:
            relation_type = "other"
        output.append({
            "type": relation_type,
            "key": _text(keys, evidence),
            "value": _text(values, evidence),
            "context": _text(context, evidence),
            "bbox": _union(boxes),
            "key_bbox": _union([evidence[ref]["bbox"] for ref in keys]),
            "value_bbox": _union([evidence[ref]["bbox"] for ref in values]),
            "line_refs": {"key": keys, "value": values, "context": context},
            "region_ids": list(dict.fromkeys(evidence[ref]["region_id"] for ref in all_refs)),
            "confidence": confidence,
        })
    # 머리글·다른 행의 값이 context로 들어오더라도 표 전체를 오염시키지 않는다.
    occupied = {
        ref for relation in output
        for group in ("key", "value") for ref in relation["line_refs"][group]
    }
    for relation in output:
        key_left = min(evidence[ref]["bbox"][0] for ref in relation["line_refs"]["key"])
        row_box = _union([
            evidence[ref]["bbox"]
            for group in ("key", "value") for ref in relation["line_refs"][group]
        ])
        context = [
            ref for ref in relation["line_refs"]["context"]
            if ref not in occupied and evidence[ref]["bbox"][0] < key_left
            and min(evidence[ref]["bbox"][3], row_box[3])
            > max(evidence[ref]["bbox"][1], row_box[1])
        ][:3]
        relation["line_refs"]["context"] = context
        relation["context"] = _text(context, evidence)
        relation["region_ids"] = list(dict.fromkeys(
            evidence[ref]["region_id"]
            for group in ("key", "value", "context") for ref in relation["line_refs"][group]
        ))
    return output


def _split_numeric_rows(
    item: dict[str, Any], aliases: dict[str, str], evidence: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """VLM이 두 물리적 행의 이름/수치를 한 짝에 넣으면 행별로 분리한다.

    긴 셀의 여러 줄을 잘못 쪼개지 않도록 값이 각각 독립적인 숫자 셀이고
    모든 이름/값 줄의 y행이 일대일로 대응할 때만 적용한다.
    """
    keys = item.get("key_refs") or []
    values = item.get("value_refs") or []
    if not isinstance(keys, list) or not isinstance(values, list):
        return [item]
    if len(keys) != len(values) or len(keys) < 2:
        return [item]
    if any(alias not in aliases for alias in [*keys, *values]):
        return [item]
    if not all(_NUMERIC_CELL.fullmatch(evidence[aliases[alias]]["text"].strip()) for alias in values):
        return [item]
    def center(alias: str) -> float:
        box = evidence[aliases[alias]]["bbox"]
        return (box[1] + box[3]) / 2
    ordered_keys = sorted(keys, key=center)
    ordered_values = sorted(values, key=center)
    for ordered in (ordered_keys, ordered_values):
        if any(
            center(right) - center(left) < 0.75 * max(
                evidence[aliases[left]]["bbox"][3] - evidence[aliases[left]]["bbox"][1],
                evidence[aliases[right]]["bbox"][3] - evidence[aliases[right]]["bbox"][1],
            )
            for left, right in zip(ordered, ordered[1:])
        ):
            return [item]
    if any(abs(center(key) - center(value)) > 25 for key, value in zip(ordered_keys, ordered_values)):
        return [item]
    return [
        {**item, "key_refs": [key], "value_refs": [value]}
        for key, value in zip(ordered_keys, ordered_values)
    ]


def _rate_value_aliases(short_to_ref: dict[str, str], evidence: dict[str, dict[str, Any]]) -> list[str]:
    """표 머리글이 금리/이율이고 실제 수치 열이 있을 때만 값 검증을 켠다."""
    if not any(_RATE_HEADER.search(line["text"]) for line in evidence.values()):
        return []
    found = [
        alias for alias, ref in short_to_ref.items()
        if _RATE_VALUE.search(evidence[ref]["text"])
        and not str(evidence[ref]["text"]).lstrip().startswith(("※", "주1)", "주2)"))
    ]
    # 단일 행 금리 표도 검증해야 한다. 두 건 이상이어야만 켜면
    # '기간 -> 12개월'처럼 실제 금리값을 놓친 짝이 통과한다.
    return found


def _filter_rate_relations(
    found: list[dict[str, Any]], rate_refs: set[str],
) -> tuple[list[dict[str, Any]], float]:
    if not rate_refs:
        return found, 1.0
    valid = [
        relation for relation in found
        if set(relation["line_refs"]["value"]) & rate_refs
    ]
    used = {
        ref for relation in valid for ref in relation["line_refs"]["value"]
    }
    return valid, len(used & rate_refs) / len(rate_refs)


def _relation_issues(found: list[dict[str, Any]], rate_refs: set[str]) -> list[str]:
    issues: list[str] = []
    if rate_refs:
        used = {
            ref for relation in found for ref in relation["line_refs"]["value"]
        }
        if len(used & rate_refs) < len(rate_refs):
            issues.append("rate_values_unpaired")
        if any(
            len(set(relation["line_refs"]["value"]) & rate_refs) > 1
            for relation in found
        ):
            issues.append("multiple_rate_values_in_one_relation")
    keys = [
        ("".join(relation["key"].split()), "".join(relation["context"].split()))
        for relation in found
    ]
    if len(keys) != len(set(keys)):
        issues.append("repeated_key_without_distinct_context")
    return issues


def document_row_relations(page: dict[str, Any]) -> None:
    """HWP 원본 표의 두 셀 행을 별개 의미 단위로 보존한다.

    큰 Paddle table 박스는 여러 HWP 행을 덮을 수 있으므로 관계를 만들 때
    사용하지 않는다. 화면에 실제 표시된 DOM 셀의 텍스트와 bbox만 쓴다.
    """
    structures: list[dict[str, Any]] = []
    for region in page.get("regions") or []:
        table = region.get("table") or {}
        if table.get("source") != "document_processor":
            continue
        cells = sorted(
            (cell for cell in table.get("cells") or [] if str(cell.get("text") or "").strip()),
            key=lambda cell: (int(cell.get("col") or 0), int(cell.get("row") or 0)),
        )
        if len(cells) != 2 or any(
            not isinstance(cell.get("bbox"), list) or len(cell["bbox"]) != 4
            for cell in cells
        ):
            continue
        key, value = cells
        key_text, value_text = str(key["text"]).strip(), str(value["text"]).strip()
        region_id = str(region["region_id"])
        key_bbox = [int(part) for part in key["bbox"]]
        value_bbox = [int(part) for part in value["bbox"]]
        structures.append({
            "structure_id": f"p{page['page_no']}_s{len(structures) + 1:03d}",
            "kind": "field_list", "verified_kind": "field_list",
            "member_region_ids": [region_id],
            "bbox": _union([key_bbox, value_bbox]),
            "field_label": (region.get("semantic_labels") or [""])[0],
            "status": "verified", "detected_by": ["document_processor"],
            "relations": [{
                "type": "attribute_value", "key": key_text, "value": value_text,
                "context": "", "bbox": _union([key_bbox, value_bbox]),
                "key_bbox": key_bbox, "value_bbox": value_bbox,
                "region_ids": [region_id],
                "line_refs": {
                    "key": [f"{region_id}/cell/0"],
                    "value": [f"{region_id}/cell/1"], "context": [],
                },
                "confidence": 1.0,
            }],
        })
    page["semantic_structures"] = structures


def analyze_page_relations(image: Image.Image, page: dict[str, Any]) -> None:
    """페이지 VLM이 지정한 표 구역을 확대해 행 의미를 판정한다."""
    structures: list[dict[str, Any]] = []
    areas = list(page.get("table_areas") or [])
    covered = {
        str(member)
        for area in areas for member in area.get("member_ids") or []
    }
    # Paddle이 표 전체를 한 Region으로 검출하면 page-context 스키마의
    # member_ids 최소 2개 조건에 걸려 table_areas에 나타나지 않을 수 있다.
    # 그 단독 표도 별도 확대 판독 대상으로 삼는다.
    for region in page.get("regions") or []:
        region_id = str(region["region_id"])
        if region_id in covered:
            continue
        layout = str((region.get("layout_observation") or {}).get("label") or "").casefold()
        if layout == "table" and len(region.get("lines") or []) >= 2:
            areas.append({"kind": "table", "member_ids": [region_id], "note": "Paddle table region"})
    for index, area in enumerate(areas, start=1):
        kind = str(area.get("kind") or "")
        if kind not in {"table", "field_list"}:
            continue
        evidence, members = _area_evidence(page, area)
        regions = {str(region["region_id"]): region for region in page.get("regions") or []}
        detected_by = ["vlm_page"] if index <= len(page.get("table_areas") or []) else []
        if any(
            str((regions[member].get("layout_observation") or {}).get("label") or "").casefold() == "table"
            for member in members
        ):
            detected_by.append("paddle")
        # 단순한 2개 Region의 한 항목은 기존 Region 라벨로 읽을 수 있다.
        # 표 또는 여러 행을 가진 field_list는 관계를 명시해야 한다.
        if kind == "field_list" and len(members) < 3:
            continue
        if len(evidence) > 72:
            structures.append({
                "structure_id": f"p{page['page_no']}_s{index:03d}", "kind": kind,
                "member_region_ids": members, "relations": [],
                "status": "unresolved", "reason": f"line_count={len(evidence)}",
                "detected_by": detected_by,
            })
            continue
        ordered = sorted(evidence.items(), key=lambda pair: (pair[1]["bbox"][1], pair[1]["bbox"][0]))
        short_to_ref = {f"L{n:02d}": ref for n, (ref, _) in enumerate(ordered, start=1)}
        short_evidence = {alias: evidence[ref] for alias, ref in short_to_ref.items()}
        label_counts = Counter(
            label for region_id in members for label in regions[region_id].get("semantic_labels") or []
        )
        labels = [label for label, _ in label_counts.most_common()]
        listing = "\n".join(
            f"{alias} [{line['region_id']}] box={line['bbox']} text={line['text'][:300]}"
            for alias, line in short_evidence.items()
        )
        prompt = f"""금융상품 페이지의 표 후보를 검증하고, 실제 표라면 의미 관계를 읽으세요.

전체 페이지 이미지와 줄 ID가 표시된 확대 이미지를 함께 봅니다.
Paddle의 table 태그와 페이지 VLM의 표 후보는 모두 가설입니다. 먼저 structure_type을
table(행·열 표), field_list(항목명-값의 나열), not_table(일반 문단·장식) 중 고르세요.
not_table이면 relations=[]로 두세요. 일반 텍스트는 뒤 단계에서 그대로 처리합니다.
다양한 표 구조를 허용합니다: 항목명-값, 조건-결과, 선택지-설명, 상위 분류-하위 항목-값.
서로 대응하는 원문 줄 ID를 key_refs와 value_refs에 넣고, 그 짝에 공통으로
적용되는 상위 분류만 context_refs에 넣으세요. 표 머리글과 각주는 억지로 짝짓지 마세요.
여러 줄이 한 칸을 이룬다면 같은 refs 배열에 모두 넣으세요. 눈에 보이는 대응 행은
빠짐없이 판단하세요. 하나의 값 줄을 서로
다른 두 관계에 복제하지 마세요. 관계가 확실하지 않으면 해당 짝은 생략하세요.
서로 다른 물리적 행의 항목명과 수치를 한 relations 원소에 묶지 마세요.
같은 금액이 반복되더라도 각 행은 별개의 key_refs/value_refs 짝입니다.
context_refs에는 그 짝에만 적용되는 왼쪽 상위 분류를 최대 3줄만 넣고,
표 머리글·다른 행의 문구·값 줄은 넣지 마세요. 상위 분류는 key_refs에 넣지 마세요.
금리·기간·한도 등 수치를 새로 계산하지 마세요. 텍스트와 좌표도 반환하지 마세요.
field_label은 허용 목록에서 이 구역 전체를 설명하는 구분값을 고르거나 빈 문자열로 두세요.
분석은 한 문장 이내로 쓰세요.

1차 후보: {kind}; 출처: {', '.join(detected_by) or 'unknown'}
구역 힌트: {str(area.get('note') or '')[:120]}
허용 field_label: {', '.join(labels) if labels else '(없음)'}
줄 목록:\n{listing}
"""
        structure: dict[str, Any] = {
            "structure_id": f"p{page['page_no']}_s{index:03d}",
            "kind": kind, "member_region_ids": members,
            "bbox": _union([line["bbox"] for line in evidence.values()]) if evidence else _union([
                list(regions[member]["bbox"]) for member in members if regions[member].get("bbox")
            ]),
            "relations": [], "status": "unresolved", "detected_by": detected_by,
        }
        try:
            result = vlm_client.chat_json(
                [
                    {"type": "text", "text": prompt},
                    vlm_client.image_part(image, box=(1400, 2200), quality=88),
                    vlm_client.image_part(_crop(image, short_evidence, structure["bbox"]), box=(1600, 1400), quality=92),
                ],
                schema_name="parser_v2_semantic_relations",
                schema=_schema(list(short_to_ref), labels),
                max_tokens=min(10000, 1800 + 110 * len(evidence)),
            )
            rate_aliases = _rate_value_aliases(short_to_ref, evidence)
            rate_refs = {short_to_ref[alias] for alias in rate_aliases}
            initial = build_relations(result, short_to_ref, evidence, labels)
            filtered, rate_coverage = _filter_rate_relations(initial, rate_refs)
            quality_issues = _relation_issues(filtered, rate_refs)
            if rate_refs and (len(filtered) != len(initial) or quality_issues):
                # 숫자 열을 다른 조건 문구로 대체한 결과는 그대로 P3에 전달하지 않는다.
                # 눈에 보이는 값 줄 ID를 명시해 한 번만 다시 판독한다.
                retry_prompt = (
                    prompt + "\n직전 관계 배치에서 금리/이율 머리글 아래 값 열이 누락되거나 "
                    "조건 문구를 값으로 골랐습니다. 실제 수치 값 줄 후보는 "
                    + ", ".join(rate_aliases)
                    + " 입니다. 서로 다른 수치 값 줄마다 대응하는 조건·기간을 "
                    "별도 관계로 연결하세요. 한 관계의 value_refs에 여러 독립 수치를 "
                    "합치거나, 같은 key_refs를 다른 값에 반복하지 마세요. "
                    "각 조건에 대응하는 수치 값 줄만 value_refs에 넣고 "
                    "표 밖의 각주와 계산 예시는 제외하세요. 확실하지 않으면 생략하세요."
                )
                try:
                    retry = vlm_client.chat_json(
                        [
                            {"type": "text", "text": retry_prompt},
                            vlm_client.image_part(image, box=(1400, 2200), quality=88),
                            vlm_client.image_part(_crop(image, short_evidence, structure["bbox"]), box=(1600, 1400), quality=92),
                        ],
                        schema_name="parser_v2_semantic_relations_retry",
                        schema=_schema(list(short_to_ref), labels),
                        max_tokens=min(10000, 1800 + 110 * len(evidence)),
                    )
                    retry_found = build_relations(retry, short_to_ref, evidence, labels)
                    retry_filtered, retry_coverage = _filter_rate_relations(retry_found, rate_refs)
                    retry_issues = _relation_issues(retry_filtered, rate_refs)
                    score = rate_coverage * 10 - len(quality_issues) * 3
                    retry_score = retry_coverage * 10 - len(retry_issues) * 3
                    if retry.get("structure_type") != "not_table" and retry_score > score:
                        result, filtered, rate_coverage = retry, retry_filtered, retry_coverage
                        quality_issues = retry_issues
                except (RuntimeError, ValueError, KeyError, TypeError) as exc:
                    structure["retry_error"] = f"{type(exc).__name__}: {exc}"[:200]
            structure_type = str(result.get("structure_type") or "not_table")
            if structure_type not in {"table", "field_list", "not_table"}:
                structure_type = "not_table"
            structure["verified_kind"] = structure_type
            structure["confidence"] = max(0.0, min(1.0, float(result.get("structure_confidence") or 0.0)))
            structure["analysis"] = str(result.get("analysis") or "")[:240]
            field_label = str(result.get("field_label") or "")
            structure["field_label"] = field_label if field_label in labels else ""
            if structure_type == "not_table":
                structure["status"] = "rejected"
                structure["reason"] = "VLM judged the candidate to be ordinary text"
                # Paddle가 단독으로 잘못 붙인 table 태그는 일반 텍스트로 되돌린다.
                if len(members) == 1 and "paddle" in detected_by:
                    region = regions[members[0]]
                    region.pop("table", None)
                    region["kind"] = "text"
                    region["table_status"] = "not_a_table"
            else:
                structure["relations"] = filtered
                if rate_refs:
                    structure["rate_value_coverage"] = round(rate_coverage, 3)
                structure["quality_issues"] = quality_issues
            if structure["relations"]:
                structure["status"] = "partial" if quality_issues else "verified"
            elif structure_type != "not_table":
                structure["status"] = "visual_only"
                structure["reason"] = "no_valid_relations"
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            structure["reason"] = f"{type(exc).__name__}: {exc}"[:240]
        structures.append(structure)
    page["semantic_structures"] = structures
