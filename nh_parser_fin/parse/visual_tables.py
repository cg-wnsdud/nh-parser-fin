"""시각적 표 후보를 검증하고 기존 Region만으로 심의 단위를 만든다.

표 셀이나 의미 관계를 추정해 출력하지 않는다. 모델은 이미지에서 표 여부와
후보에 실제로 속한 Region ID만 고른다. 문구와 좌표는 기존 Region의 증거다.
"""
from __future__ import annotations

import copy
from statistics import median
from typing import Any

from PIL import Image, ImageDraw

from ..vlm import client as vlm_client
from . import reading, table_html
from .quality import flag


def _union(boxes: list[list[int]]) -> list[int]:
    return [
        min(box[0] for box in boxes), min(box[1] for box in boxes),
        max(box[2] for box in boxes), max(box[3] for box in boxes),
    ]


def _normalized(value: Any) -> str:
    return "".join(str(value or "").split()).casefold()


def _candidates(page: dict[str, Any]) -> list[dict[str, Any]]:
    regions = {str(region["region_id"]): region for region in page.get("regions") or []}
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()

    # 단일 Paddle 표를 먼저 검증한다. 페이지 VLM이 그 주변 글자까지 지목했다면
    # 단일 박스가 거부된 경우에만 더 넓은 후보가 채택될 수 있다.
    for region in page.get("regions") or []:
        if region.get("table"):
            continue  # HWP/문서 구조의 실제 셀은 재추정하지 않는다.
        if str((region.get("layout_observation") or {}).get("label") or "").casefold() == "table":
            ids = [str(region["region_id"])]
            seen.add(tuple(ids))
            output.append({"source": "paddle", "member_ids": ids})

    for area in page.get("table_areas") or []:
        ids = list(dict.fromkeys(
            str(value) for value in area.get("member_ids") or []
            if str(value) in regions and not regions[str(value)].get("table")
        ))
        key = tuple(sorted(ids))
        if not ids or key in seen:
            continue
        seen.add(key)
        output.append({"source": "vlm_page", "member_ids": ids})
    return output


def _verification_image(
    image: Image.Image, regions: list[dict[str, Any]],
) -> tuple[Image.Image, Image.Image]:
    box = _union([list(region["bbox"]) for region in regions])
    whole = image.convert("RGB").copy()
    draw = ImageDraw.Draw(whole)
    draw.rectangle(box, outline="#e53935", width=max(3, min(image.size) // 300))
    pad = 24
    crop_box = (
        max(0, box[0] - pad), max(0, box[1] - pad),
        min(image.width, box[2] + pad), min(image.height, box[3] + pad),
    )
    crop = image.crop(crop_box).convert("RGB")
    draw = ImageDraw.Draw(crop)
    for index, region in enumerate(regions, start=1):
        x0, y0, x1, y1 = (int(value) for value in region["bbox"])
        local = [x0 - crop_box[0], y0 - crop_box[1], x1 - crop_box[0], y1 - crop_box[1]]
        draw.rectangle(local, outline="#2563eb", width=2)
        draw.text((max(0, local[0]), max(0, local[1] - 12)), str(index), fill="#c1121f")
    return whole, crop


def _verify(
    image: Image.Image, regions: list[dict[str, Any]], source: str,
) -> dict[str, Any]:
    ids = [str(region["region_id"]) for region in regions]
    whole, crop = _verification_image(image, regions)
    listing = "\n".join(
        f"{index}. {region['region_id']} bbox={region['bbox']} "
        f"text={str(region.get('text') or '')[:220]}"
        for index, region in enumerate(regions, start=1)
    )
    prompt = f"""금융광고 페이지의 표 후보가 실제로 하나의 심의 항목을 설명하는 표인지 확인하세요.

첫 이미지는 페이지 전체의 빨간 후보 위치, 둘째 이미지는 확대 화면과 파란 구성 영역입니다.
Paddle table 라벨과 페이지 VLM 후보는 가설일 뿐입니다. 구조나 문구를 추측하지 마세요.
여러 독립 심의 항목(예: 대출대상·대출한도·대출기간)의 나열, LMS 문단,
표 밖 각주를 한 표로 합치지 마세요. 한 후보에 다른 항목이 섞였다면 실제 표에
속하는 member_ids만 선택하세요. 하나의 Region 자체가 과하게 넓어 분리할 수
없으면 is_table=false로 두세요. 복잡한 중첩 표의 구조는 추정하지 마세요.
is_table은 선택한 ID들이 함께 하나의 표 심의 단위일 때만 true입니다.
문구·bbox·셀·의미 관계를 반환하지 마세요.

후보 출처: {source}
기존 영역:\n{listing}
"""
    schema = {
        "type": "object",
        "properties": {
            "is_table": {"type": "boolean"},
            "member_ids": {"type": "array", "minItems": 1, "maxItems": len(ids),
                           "items": {"type": "string", "enum": ids}},
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
        },
        "required": ["is_table", "member_ids", "confidence", "reason"],
        "additionalProperties": False,
    }
    return vlm_client.chat_json(
        [
            {"type": "text", "text": prompt},
            vlm_client.image_part(whole, box=(1200, 1800), quality=86),
            vlm_client.image_part(crop, box=(1600, 1600), quality=92),
        ],
        schema_name="parser_v2_visual_table_verify", schema=schema, max_tokens=850,
    )


def _same_content(left: dict[str, Any], right: dict[str, Any]) -> bool:
    a, b = list(left["bbox"]), list(right["bbox"])
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0, min(a[3], b[3]) - max(a[1], b[1])
    )
    area = min(max(1, (a[2] - a[0]) * (a[3] - a[1])),
               max(1, (b[2] - b[0]) * (b[3] - b[1])))
    return intersection / area >= 0.9 and _normalized(left.get("text")) == _normalized(right.get("text"))


def _contains_duplicate(region: dict[str, Any], others: list[dict[str, Any]]) -> bool:
    """Paddle 표 박스가 별도 Region의 본문을 통째로 반복하는 경우는 보류한다."""
    outer = region["bbox"]
    outer_text = _normalized(region.get("text"))
    if not outer_text:
        return False
    for other in others:
        if other is region or other.get("product_id") != region.get("product_id"):
            continue
        inner = other.get("bbox") or []
        if len(inner) != 4:
            continue
        area = max(1, (inner[2] - inner[0]) * (inner[3] - inner[1]))
        intersection = max(0, min(outer[2], inner[2]) - max(outer[0], inner[0])) * max(
            0, min(outer[3], inner[3]) - max(outer[1], inner[1])
        )
        inner_text = _normalized(other.get("text"))
        if intersection / area >= 0.85 and len(inner_text) >= 8 and inner_text in outer_text:
            return True
    return False


def _plain_text(regions: list[dict[str, Any]]) -> tuple[str, bool]:
    """단일 줄 조각만 같은 행에 나란히 적는다. 불확실하면 원문 줄바꿈을 보존한다."""
    unique: list[dict[str, Any]] = []
    for region in regions:
        if not any(_same_content(region, kept) for kept in unique):
            unique.append(region)
    if len(unique) == 1:
        return str(unique[0].get("text") or "").strip(), False
    if any("\n" in str(region.get("text") or "") for region in unique):
        ordered = sorted(unique, key=lambda region: (region["bbox"][1], region["bbox"][0]))
        return "\n".join(str(region.get("text") or "").strip() for region in ordered if region.get("text")), True
    heights = [max(1, region["bbox"][3] - region["bbox"][1]) for region in unique]
    tolerance = median(heights) * 0.55
    ordered = sorted(unique, key=lambda region: (
        (region["bbox"][1] + region["bbox"][3]) / 2, region["bbox"][0],
    ))
    rows: list[list[dict[str, Any]]] = []
    for region in ordered:
        center = (region["bbox"][1] + region["bbox"][3]) / 2
        if rows and abs(center - median(
            (item["bbox"][1] + item["bbox"][3]) / 2 for item in rows[-1]
        )) <= tolerance:
            rows[-1].append(region)
        else:
            rows.append([region])
    return "\n".join(
        " | ".join(
            str(region.get("text") or "").strip()
            for region in sorted(row, key=lambda item: item["bbox"][0])
            if str(region.get("text") or "").strip()
        ) for row in rows
    ).strip(), False


def _remove_unexplained_repeats(text: str, parser_text: str) -> tuple[str, int]:
    """원문 후보에 한 번 있는 긴 줄만 VLM의 연속 중복에서 한 번으로 줄인다."""
    parser = _normalized(parser_text)
    kept: list[str] = []
    removed = 0
    for line in text.splitlines():
        normalized = _normalized(line)
        if (
            kept and len(normalized) >= 8
            and normalized == _normalized(kept[-1])
            and parser.count(normalized) == 1
        ):
            removed += 1
            continue
        kept.append(line)
    return "\n".join(kept).strip(), removed


def _select_html_table_text(image: Image.Image, anchor: dict[str, Any]) -> bool:
    """[실험] 표를 HTML로 받아 격자가 온전할 때만 마크다운 문구와 격자를 채택한다.

    격자 폭이 행마다 다르거나 빈 자리가 있으면 병합 구조를 잘못 읽은 것이다. 이때는
    기록만 남기고 False를 돌려 기존 `항목 | 값` 경로가 문구를 정하게 한다.
    """
    crop = reading._crop(image, anchor["bbox"])
    if crop is None:
        return False
    source_text = str(anchor.get("text") or "")
    attempts: list[dict[str, Any]] = []
    feedback = ""
    chosen = None
    # 같은 표도 호출마다 병합 구조를 다르게 읽는다. 무효면 사유를 알려 한 번 더 묻는다.
    for _ in range(2):
        try:
            got = table_html.extract(crop, feedback=feedback)
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            attempts.append({"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:200]})
            feedback = ""
            continue
        response, table = got["response"], got["table"]
        title = str(response.get("title") or "")
        notes = [str(value) for value in response.get("notes") or []]
        text = table_html.compose_text(title, table_html.to_markdown(table), notes)
        coverage = table_html.coverage(source_text, text)
        attempt = {
            "html": str(response.get("html") or ""),
            # 모델이 0~1 범위를 지키지 않는다(실측 5.0). 기록만 하고 판단에 쓰지 않는다.
            "confidence": response.get("confidence"),
            "rows": table["rows"], "cols": table["width"],
            "ragged_widths": table["ragged_widths"], "holes": table["holes"],
            "coverage": round(coverage, 4),
        }
        if not table_html.is_valid(table):
            attempts.append({**attempt, "status": "rejected", "reason": "grid"})
            feedback = ("직전 응답은 행마다 칸 수(colspan 포함)가 달라 무효였습니다. "
                        "rowspan/colspan을 다시 확인해 모든 행의 칸 수를 맞추세요.")
            continue
        if coverage < table_html.MIN_COVERAGE:
            attempts.append({**attempt, "status": "rejected", "reason": "coverage"})
            feedback = ("직전 응답은 이미지 안의 문구 일부가 빠져 무효였습니다. 표 위 제목·"
                        "표 밖 문구도 이미지에 있으면 title 또는 notes에 모두 옮기세요.")
            continue
        attempts.append({**attempt, "status": "selected"})
        chosen = (table, title, notes, text)
        break
    anchor["table_html_attempts"] = attempts
    anchor["table_html_attempt"] = attempts[-1] if attempts else {"status": "failed"}
    if chosen is None:
        flag(anchor, "table_html_invalid")
        return False
    table, title, notes, text = chosen
    confidence = attempts[-1].get("confidence")
    anchor.setdefault("text_candidates", {})["vlm_table_html"] = text
    anchor["text"] = text
    anchor["text_source"] = "vlm_table_html"
    anchor["reading_status"] = "table_vlm_selected"
    anchor["visual_table"] = table_html.p3_table(table, title, notes)
    anchor["table_reading"] = {
        "status": "selected", "method": "html_grid", "confidence": confidence,
        "attempts": len(attempts),
    }
    return True


def _select_visual_table_text(
    image: Image.Image, anchor: dict[str, Any], *, grouped: bool,
) -> bool:
    """표 전체의 VLM 판독을 정본으로 쓰고 교체 전 문장은 P1에 보존한다."""
    parser_text = str(anchor.get("text") or "")
    candidates = anchor.setdefault("text_candidates", {})
    candidates["pre_table_vlm"] = parser_text
    if table_html.format_from_env() == "html" and _select_html_table_text(image, anchor):
        return True
    # 단일 표의 기존 Judge는 전체 bbox를 보았으므로 표 전용 Judge의 참고
    # 후보로만 쓴다. 그룹 앵커의 Judge는 병합 전 한 조각만 본 것이므로 제외한다.
    prior = anchor.get("vlm_judge") or {}
    reader_text = (
        str(prior.get("text") or "") if not grouped else None
    )
    method = "table_judge_from_region" if reader_text else "full_table_judge"
    try:
        result = reading.read_visual_table(image, anchor, parser_text, reader_text)
    except (RuntimeError, ValueError, KeyError, TypeError) as exc:
        anchor["table_reading"] = {
            "status": "failed", "error": f"{type(exc).__name__}: {exc}"[:200],
        }
        flag(anchor, "table_vlm_read_failed")
        return False
    if not result or not reading._normalized(result.get("text")):
        anchor["table_reading"] = {"status": "blank", "method": method}
        flag(anchor, "table_vlm_read_blank")
        return False
    try:
        confidence = float(result.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < 0.7:
        anchor["table_reading"] = {
            "status": "low_confidence", "method": method,
            "confidence": confidence, "text": str(result["text"]),
        }
        flag(anchor, "table_vlm_read_low_confidence")
        return False
    final_text, removed_repeats = _remove_unexplained_repeats(
        reading.clean_text(result["text"]), parser_text,
    )
    candidates["vlm_table_judge"] = final_text
    if prior:
        candidates["pre_table_judge"] = str(prior.get("text") or "")
    if result.get("reader_text"):
        candidates["vlm_table_reader"] = str(result["reader_text"])
    anchor["vlm_judge"] = {
        "text": final_text, "confidence": confidence,
        "source": str(result.get("source") or "corrected"),
        "analysis": str(result.get("analysis") or "")[:300],
    }
    anchor["text"] = final_text
    anchor["text_source"] = "vlm_table_judge"
    anchor["reading_status"] = "table_vlm_selected"
    anchor["table_reading"] = {
        "status": "selected", "method": method, "confidence": confidence,
        "judge_source": str(result.get("source") or "corrected"),
        "removed_repeated_lines": removed_repeats,
    }
    return True


def verify_visual_tables(image: Image.Image, page: dict[str, Any]) -> dict[str, int]:
    """후보를 검증해 기존 Region에만 `kind=table`을 기록한다."""
    checks: list[dict[str, Any]] = []
    accepted: set[str] = set()
    promoted = 0
    for candidate in _candidates(page):
        by_id = {str(region["region_id"]): region for region in page.get("regions") or []}
        ids = [value for value in candidate["member_ids"] if value in by_id]
        if not ids or any(value in accepted for value in ids):
            continue
        regions = [by_id[value] for value in ids]
        check: dict[str, Any] = {
            "source": candidate["source"], "source_region_ids": ids,
            "accepted": False,
        }
        checks.append(check)
        try:
            result = _verify(image, regions, candidate["source"])
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            check["reason"] = f"VLM 검증 실패: {type(exc).__name__}: {exc}"[:200]
            for region in regions:
                flag(region, "table_verification_failed")
            continue
        try:
            confidence = max(0.0, min(1.0, float(result.get("confidence") or 0)))
        except (TypeError, ValueError):
            confidence = 0.0
        check["confidence"] = confidence
        check["reason"] = str(result.get("reason") or "")[:200]
        selected = list(dict.fromkeys(
            str(value) for value in result.get("member_ids") or [] if str(value) in by_id and str(value) in ids
        ))
        if not result.get("is_table") or confidence < 0.7 or not selected:
            continue
        if any(value in accepted for value in selected):
            continue
        members = [by_id[value] for value in selected]
        owners = {str(region.get("product_id") or "unknown") for region in members}
        if len(owners) != 1 or any(region.get("table") for region in members):
            check["reason"] = "다른 상품 또는 원본 구조 표와 겹쳐 병합 보류"
            continue
        if len(members) == 1 and _contains_duplicate(members[0], page["regions"]):
            check["reason"] = "후보 박스가 별도 영역의 동일 본문을 포함해 중복 보류"
            flag(members[0], "table_region_overlap")
            continue
        text, uncertain_order = _plain_text(members)
        if not text:
            check["reason"] = "원문 텍스트가 없어 표로 확정하지 않음"
            for region in members:
                flag(region, "table_text_missing")
            continue
        anchor = min(members, key=lambda region: page["regions"].index(region))
        boxes = [list(region["bbox"]) for region in members]
        original = [
            {"region_id": str(region["region_id"]), "bbox": list(region["bbox"]),
             "text": str(region.get("text") or ""), "text_source": region.get("text_source")}
            for region in members
        ]
        if len(members) > 1:
            member_lines = [copy.deepcopy(region.get("lines") or []) for region in members]
            anchor.setdefault("text_candidates", {})["pre_table_group"] = str(anchor.get("text") or "")
            anchor["bbox"] = _union(boxes)
            anchor["text"] = text
            anchor["text_source"] = (
                "vlm_table_group" if any(str(region.get("text_source") or "").startswith("vlm") for region in members)
                else "digital_table_group" if all(str(region.get("text_source") or "").startswith("digital") for region in members)
                else "ocr_table_group"
            )
            line_refs: set[str] = set()
            anchor["lines"] = []
            for lines in member_lines:
                for line in lines:
                    ref = str(line.get("line_ref") or line.get("line_id") or "")
                    if ref not in line_refs:
                        anchor["lines"].append(copy.deepcopy(line))
                        line_refs.add(ref)
            anchor["merged_from"] = selected
            removed = set(selected) - {str(anchor["region_id"])}
            page["regions"] = [
                region for region in page["regions"] if str(region["region_id"]) not in removed
            ]
        anchor["kind"] = "table"
        anchor["table_status"] = "verified_visual"
        anchor["table_detection"] = {
            "source": candidate["source"], "confidence": confidence,
            "source_regions": original,
        }
        selected_vlm = _select_visual_table_text(
            image, anchor, grouped=len(members) > 1,
        )
        if uncertain_order and not selected_vlm:
            flag(anchor, "table_reading_order_uncertain")
        accepted.update(selected)
        promoted += 1
        check["accepted"] = True
        check["source_region_ids"] = selected
        check["table_region_id"] = str(anchor["region_id"])
        check["text_status"] = anchor["table_reading"]["status"]
    page["table_checks"] = checks
    page.pop("table_areas", None)
    page["table_count"] = sum(
        region.get("kind") == "table" for region in page.get("regions") or []
    )
    return {"checked": len(checks), "accepted": promoted}
