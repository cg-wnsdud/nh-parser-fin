"""PaddleX 관측값을 Region 증거로 바꾼다.

핵심 원칙은 한 출처를 조용히 버리지 않는 것이다. ``parsing_res_list.block_content``는
PaddleX가 정리한 본문 후보로 사용하고, ``overall_ocr_res`` 줄은 다음 용도로 쓴다.

1. ``block_content``가 빈 표·이미지 영역의 안전한 폴백
2. 좌표·신뢰도·한 번만 소유되는 line ID 보존
3. 영역 밖 OCR 줄(unassigned) 진단
4. PaddleX 본문과 다를 때 후속 VLM이 대조할 독립 후보

따라서 Region의 정본 텍스트와 OCR 증거는 서로 다른 필드에 보존한다.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any


MIN_REGION_OVERLAP = 0.5
STRONG_AGREEMENT = 0.8
VLM_CONFLICT = 0.5


def _area(bbox: list[int]) -> int:
    return max(0, bbox[2] - bbox[0]) * max(0, bbox[3] - bbox[1])


def line_overlap_ratio(line_bbox: list[int], region_bbox: list[int]) -> float:
    """OCR 줄 면적 중 Region과 겹치는 비율."""
    ix0 = max(line_bbox[0], region_bbox[0])
    iy0 = max(line_bbox[1], region_bbox[1])
    ix1 = min(line_bbox[2], region_bbox[2])
    iy1 = min(line_bbox[3], region_bbox[3])
    intersection = max(0, ix1 - ix0) * max(0, iy1 - iy0)
    return intersection / max(1, _area(line_bbox))


def _normalized(text: str | None) -> str:
    return re.sub(r"\s+", "", text or "").casefold()


def _reading_key(line: dict[str, Any]) -> tuple[int, int]:
    bbox = line.get("bbox") or [0, 0, 0, 0]
    return int(bbox[1]), int(bbox[0])


def _line_is_covered(line: dict[str, Any], others: list[dict[str, Any]]) -> bool:
    bbox = line.get("bbox")
    return bool(bbox) and any(
        other.get("bbox") and line_overlap_ratio(bbox, other["bbox"]) >= 0.5
        for other in others
    )


def _parent_links(regions: list[dict[str, Any]]) -> None:
    """포함 박스를 삭제하지 않고 가장 가까운 부모/자식 관계만 기록한다."""
    for child in regions:
        cb = child["bbox"]
        containers = []
        for parent in regions:
            if parent is child:
                continue
            pb = parent["bbox"]
            if pb[0] <= cb[0] and pb[1] <= cb[1] and pb[2] >= cb[2] and pb[3] >= cb[3]:
                if _area(pb) > _area(cb):
                    containers.append(parent)
        if not containers:
            continue
        parent = min(containers, key=lambda item: _area(item["bbox"]))
        child["parent_id"] = parent["region_id"]
        parent["child_ids"].append(child["region_id"])


def build_page_evidence(
    parsing: list[dict[str, Any]],
    ocr_lines: list[dict[str, Any]],
    *,
    page_no: int,
    canvas: list[int],
    digital_lines: list[dict[str, Any]] | None = None,
    min_overlap: float = MIN_REGION_OVERLAP,
) -> dict[str, Any]:
    """PaddleX 본문과 좌표 기반 줄을 함께 보존한 Region 증거를 만든다.

    OCR/디지털 줄은 정확히 한 Region 또는 unassigned에만 들어간다. 하지만 줄을 다시
    조립한 결과가 ``block_content``를 무조건 덮지는 않는다. 둘의 일치도를 기록하고,
    충돌하면 후속 VLM/Judge 대상임을 명시한다.
    """
    ordered = sorted(
        enumerate(parsing),
        key=lambda pair: (
            # block_order는 타일마다 다시 1부터 시작한다. 타일 번호가 페이지의 큰 흐름이고
            # order는 그 타일 안에서만 의미가 있다.
            pair[1].get("piece") if isinstance(pair[1].get("piece"), int) else 0,
            pair[1].get("order") if isinstance(pair[1].get("order"), int) else 10**9,
            (pair[1].get("bbox") or [0, 0])[1],
            (pair[1].get("bbox") or [0, 0])[0],
            pair[0],
        ),
    )
    regions: list[dict[str, Any]] = []
    for sequence, (_, block) in enumerate(ordered, start=1):
        content = str(block.get("content") or "").strip()
        regions.append({
            "region_id": f"p{page_no}_r{sequence:03d}",
            "page_no": page_no,
            "sequence": sequence,
            "bbox": list(block["bbox"]),
            "layout_observation": {
                "label": str(block.get("label") or "unknown"),
                "order": block.get("order"),
                "piece": block.get("piece"),
            },
            # 상품 소유권은 다음 단계에서 판정한다. 공통 영역은 이후에도 null이다.
            "product_id": None,
            "ownership_status": "pending",
            "text": content,
            "text_source": "paddlex_block_content" if content else "pending_line_fallback",
            "text_candidates": {
                "paddlex_block_content": content or None,
                "line_assembled": None,
            },
            "text_selection_status": "pending",
            "text_agreement": None,
            "lines": [],
            "ocr_evidence": [],
            "digital_evidence": [],
            "content_gap_candidates": [],
            "parent_id": None,
            "child_ids": [],
        })

    unassigned: list[dict[str, Any]] = []

    def assign_evidence(lines: list[dict[str, Any]], source: str) -> None:
        evidence_key = "digital_evidence" if source == "digital" else "ocr_evidence"
        for offset, raw_line in enumerate(lines):
            line = dict(raw_line)
            line["source"] = source
            line.setdefault("evidence_id", f"p{page_no}_{source[0]}e{offset + 1:04d}")
            bbox = line.get("bbox")
            candidates: list[tuple[float, int, dict[str, Any]]] = []
            if bbox:
                for region in regions:
                    ratio = line_overlap_ratio(bbox, region["bbox"])
                    if ratio >= min_overlap:
                        candidates.append((ratio, -_area(region["bbox"]), region))
            if not candidates:
                continue
            target = max(candidates, key=lambda item: (item[0], item[1]))[2]
            target[evidence_key].append(line)

    def canonical_lines() -> list[dict[str, Any]]:
        """PDF 디지털 줄을 우선하고, 덮이지 않은 OCR 줄만 보충한다."""
        digital = [{**line, "source": "digital"} for line in (digital_lines or [])]
        ocr = [{**line, "source": "ocr"} for line in ocr_lines]
        merged = list(digital)
        for line in ocr:
            if _line_is_covered(line, digital):
                continue
            merged.append(line)
        merged.sort(key=_reading_key)
        for index, line in enumerate(merged, start=1):
            line["line_id"] = f"p{page_no}_l{index:04d}"
        return merged

    def assign_canonical(lines: list[dict[str, Any]]) -> None:
        for line in lines:
            bbox = line.get("bbox")
            candidates: list[tuple[float, int, dict[str, Any]]] = []
            if bbox:
                for region in regions:
                    ratio = line_overlap_ratio(bbox, region["bbox"])
                    if ratio >= min_overlap:
                        candidates.append((ratio, -_area(region["bbox"]), region))
            if not candidates:
                unassigned.append(line)
                continue
            max(candidates, key=lambda item: (item[0], item[1]))[2]["lines"].append(line)

    assign_evidence(list(digital_lines or []), "digital")
    assign_evidence(ocr_lines, "ocr")
    canonical_stream = canonical_lines()
    assign_canonical(canonical_stream)

    fallback_regions = 0
    conflict_regions = 0
    gap_candidates = 0
    for region in regions:
        ocr_evidence = sorted(region["ocr_evidence"], key=_reading_key)
        digital_evidence = sorted(region["digital_evidence"], key=_reading_key)
        owned_lines = sorted(region["lines"], key=_reading_key)
        region["ocr_evidence"] = ocr_evidence
        region["digital_evidence"] = digital_evidence
        region["lines"] = owned_lines
        line_text = "\n".join(
                str(line.get("text") or "").strip()
                for line in owned_lines
                if str(line.get("text") or "").strip()
            )
        region["text_candidates"]["line_assembled"] = line_text or None
        block_text = region["text_candidates"]["paddlex_block_content"] or ""
        if block_text and line_text:
            agreement = SequenceMatcher(None, _normalized(block_text), _normalized(line_text)).ratio()
            region["text_agreement"] = round(agreement, 4)
            # 디지털 텍스트는 OCR보다 문자 정확도가 높아 기존 파이프라인도 정본으로 썼다.
            #
            # 디지털 줄이 없을 때 무조건 `block_content` 를 쓰면 안 된다. PNG 입력은
            # 디지털 줄이 아예 없어 **깨진 본문이 항상 이긴다** — 실측(2026-09-20,
            # `3. 예금성상품(거치식).png` p1_r018): block_content 가
            # `ㅣ이: 이 / 이무기이해 / (융은이이위||` 17자인데 OCR 줄은 168자가 멀쩡했다.
            # 그래서 출처가 아니라 **커버리지**로 고른다. `block_content` 가 OCR 줄을
            # 빠뜨리면 줄 조립본을 쓴다. 두 후보 모두 같은 OCR 결과에서 나오므로
            # 새 텍스트가 생기지 않는다.
            missing = [
                line for line in owned_lines
                if len(_normalized(line.get("text"))) >= 2
                and _normalized(line.get("text")) not in _normalized(block_text)
            ]
            if any(line.get("source") == "digital" for line in owned_lines):
                region["text"] = line_text
                region["text_source"] = "digital_ocr_lines"
            elif missing:
                region["text"] = line_text
                region["text_source"] = "ocr_lines_block_incomplete"
            else:
                region["text"] = block_text
                region["text_source"] = "paddlex_block_content"
            if agreement >= STRONG_AGREEMENT:
                region["text_selection_status"] = "sources_agree"
            elif agreement >= VLM_CONFLICT:
                # 어순·공백·줄 합치기 차이가 대부분이다. 기록은 하되 별도 VLM 호출은 하지 않는다.
                region["text_selection_status"] = "minor_difference"
            else:
                region["text_selection_status"] = "conflict_pending_vlm"
                conflict_regions += 1
        elif block_text:
            region["text"] = block_text
            region["text_source"] = "paddlex_block_content"
            region["text_selection_status"] = "paddlex_only"
        elif line_text:
            region["text"] = line_text
            has_digital = any(line.get("source") == "digital" for line in owned_lines)
            region["text_source"] = "digital_ocr_fallback" if has_digital else "ocr_fallback"
            region["text_selection_status"] = "line_fallback"
            fallback_regions += int(bool(region["text"]))
        else:
            region["text"] = ""
            region["text_source"] = "empty"
            region["text_selection_status"] = "empty"

        if block_text:
            normalized_selected = _normalized(region["text"])
            gaps = [
                line for line in owned_lines
                if len(_normalized(line.get("text"))) >= 2
                and _normalized(line.get("text")) not in normalized_selected
            ]
            region["content_gap_candidates"] = gaps
            gap_candidates += len(gaps)

    _parent_links(regions)
    owned_ids = [
        line["line_id"]
        for region in regions
        for line in region["lines"]
    ]
    unassigned_ids = [line["line_id"] for line in unassigned]
    all_ids = owned_ids + unassigned_ids
    expected = len(canonical_stream)
    if len(all_ids) != len(set(all_ids)) or len(all_ids) != expected:
        raise ValueError("OCR/디지털 줄 소유권 보존 조건이 깨졌습니다")

    return {
        "page_no": page_no,
        "canvas": list(canvas),
        "regions": regions,
        "unassigned_lines": unassigned,
        "diagnostics": {
            "regions": len(regions),
            "block_content_regions": sum(
                r["text_source"] == "paddlex_block_content" for r in regions
            ),
            "ocr_fallback_regions": fallback_regions,
            "empty_regions": sum(r["text_source"] == "empty" for r in regions),
            "ocr_lines": len(ocr_lines),
            "digital_lines": len(digital_lines or []),
            "canonical_lines": len(canonical_stream),
            "unassigned_lines": len(unassigned),
            "content_gap_candidates": gap_candidates,
            "text_conflicts_pending_vlm": conflict_regions,
        },
    }


def dedupe_ocr_lines(lines: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """서로 다른 타일에서 같은 위치를 다시 읽은 OCR 줄을 합친다.

    타일 경계에서는 같은 글자가 일부 잘려 문자열이 달라질 수 있다. 문자열 일치보다
    박스 IoU/포함비를 사용하고, 신뢰도가 높은 판독을 남기되 버린 판독은
    ``tile_alternates``에 보존한다.
    """
    def iou(a: list[int], b: list[int]) -> float:
        ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
        iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
        inter = ix * iy
        return inter / max(1, _area(a) + _area(b) - inter)

    def containment(a: list[int], b: list[int]) -> float:
        ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
        iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
        return (ix * iy) / max(1, min(_area(a), _area(b)))

    kept: list[dict[str, Any]] = []
    merged = 0
    for line in sorted(lines, key=lambda item: -(item.get("score") or 0.0)):
        for other in kept:
            if other.get("piece") == line.get("piece"):
                continue
            if iou(line["bbox"], other["bbox"]) < 0.5 and containment(
                line["bbox"], other["bbox"]
            ) < 0.7:
                continue
            other.setdefault("tile_alternates", []).append({
                key: line.get(key) for key in ("text", "score", "bbox", "piece")
            })
            merged += 1
            break
        else:
            kept.append(line)
    return kept, merged
