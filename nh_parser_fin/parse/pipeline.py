"""OCR 결과를 VLM 의미 판정과 엮어 P1/P3 까지 연결한다.

단계 순서는 `run_full_pipeline` 하나에 모여 있다 — 분류 → 소유권 → 표 → 판독
→ 템플릿 → 구분값 → P1/P3.
"""
from __future__ import annotations

import copy
import json
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from PIL import Image

from ..review.catalog import load_catalog
from ..vlm import client as vlm_client

from . import reading
from . import tables
from .export import build_p1, build_p3
from .hwp_alignment import align_hwp_structure
from .ids import normalize_region_ids
from .quality import flag
from .recovery import build_recovery_candidates
from .semantic import (
    analyze_page_context,
    analyze_product_labels,
    constrain_title_labels,
    constrain_rate_calculation_labels,
    explicit_heading_evidence,
)
from .templates import (
    PAGE_COMMON,
    resolve_product_templates,
    review_units,
    template_label_examples,
)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _media_map(tasks: list[dict[str, Any]], media_dir: Path) -> dict[tuple[str, int], Path]:
    output = {}
    for task in tasks:
        data = task["data"]
        name = unquote(str(data["image"]).split("pages/", 1)[1])
        output[(str(data["source_file"]), int(data["page_no"]))] = media_dir / name
    return output


def _structure_duplicate_candidate(
    candidate: dict[str, Any], regions: list[dict[str, Any]],
) -> bool:
    """여러 구조 행을 한꺼번에 반복한 OCR 복구 후보인지 판정한다."""
    bbox = candidate.get("bbox") or []
    text = "".join(str(candidate.get("text") or "").split()).casefold()
    if len(bbox) != 4 or len(text) < 20:
        return False
    bx0, by0, bx1, by1 = (int(value) for value in bbox)
    enclosed: list[str] = []
    for region in regions:
        if region.get("origin") != "document_processor":
            continue
        other = region.get("bbox") or []
        if len(other) != 4:
            continue
        ax0, ay0, ax1, ay1 = (int(value) for value in other)
        area = max(1, (ax1 - ax0) * (ay1 - ay0))
        intersection = (
            max(0, min(ax1, bx1) - max(ax0, bx0))
            * max(0, min(ay1, by1) - max(ay0, by0))
        )
        value = "".join(str(region.get("text") or "").split()).casefold()
        if intersection / area >= 0.8 and value:
            enclosed.append(value)
    if len(enclosed) < 3:
        return False
    known = "".join(enclosed)
    shared = sum((Counter(text) & Counter(known)).values())
    return shared / len(text) >= 0.72


def _prepare_page(page: dict[str, Any]) -> dict[str, Any]:
    prepared = copy.deepcopy(page)
    page_no = int(prepared["page_no"])
    for region_order, region in enumerate(prepared.get("regions") or [], start=1):
        structured_source = (region.get("structured") or {}).get("source")
        region["origin"] = "document_processor" if structured_source else "paddlex"
        region["engine_order"] = region_order
        region.setdefault("bbox_source", "paddlex_layout")
        region.setdefault("bbox_quality", "exact")
        for index, line in enumerate(region.get("lines") or []):
            line["line_ref"] = f"p{page_no}/{region['region_id']}/L{index:03d}"
    for index, line in enumerate(prepared.get("unassigned_lines") or []):
        line["line_ref"] = f"p{page_no}/unassigned/L{index:03d}"
    prepared["raw_unassigned_lines"] = copy.deepcopy(prepared.get("unassigned_lines") or [])
    unassigned = prepared.get("unassigned_lines") or []
    # 상품 소유권을 알기 전에 같은 높이의 줄을 표 하나로 합치면 좌우 상품의 표가
    # 하나가 된다. 여기서는 작은 복구 후보로만 보존하고, 페이지 전체를 보는 의미
    # 판정이 product_id와 table_areas를 정한 뒤 같은 상품 안에서만 합친다.
    recovery_candidates = build_recovery_candidates(unassigned, page_no=page_no)
    structure_duplicates = [
        candidate for candidate in recovery_candidates
        if _structure_duplicate_candidate(candidate, prepared.get("regions") or [])
    ]
    duplicate_refs = {
        line_ref
        for candidate in structure_duplicates
        for line_ref in candidate.get("line_refs") or []
    }
    prepared["structure_duplicate_lines"] = [
        copy.deepcopy(line)
        for line in unassigned
        if line.get("line_ref") in duplicate_refs
    ]
    prepared["unassigned_lines"] = [
        line for line in unassigned if line.get("line_ref") not in duplicate_refs
    ]
    prepared["recovery_candidates"] = sorted(
        [candidate for candidate in recovery_candidates if candidate not in structure_duplicates],
        key=lambda item: ((item.get("bbox") or [0, 0])[1], (item.get("bbox") or [0, 0])[0]),
    )
    return prepared


def _document_template(product_templates: dict[str, Any]) -> dict[str, Any]:
    """문서 단위 요약. 상품이 하나면 그 템플릿, 여럿이면 목록을 남긴다.

    P3의 `document.template`은 기존 계약이라 유지하되, 상품이 섞인 문서에서
    임의로 하나를 고르지 않는다. 실제 판정 근거는 `product_templates`다.
    """
    products = {
        product_id: resolution
        for product_id, resolution in product_templates.items()
        if product_id != PAGE_COMMON
    }
    ids = sorted({
        str(resolution.get("template_id"))
        for resolution in products.values()
        if resolution.get("template_id")
    })
    if len(ids) == 1:
        only = next(
            resolution for resolution in products.values()
            if resolution.get("template_id") == ids[0]
        )
        return {**only, "scope": "document"}
    return {
        "template_id": None,
        "status": "multi_product" if ids else "unresolved",
        "source": "per_product",
        "confidence": None,
        "reason": (
            f"상품별 템플릿 {len(ids)}종: {', '.join(ids)}" if ids
            else "상품별 템플릿을 확정하지 못함"
        ),
        "candidates": ids,
        "scope": "per_product",
    }


def _assign_reading_order(page: dict[str, Any]) -> None:
    """엔진 순서를 유지하고 연결된 복구 Region만 대상 바로 뒤에 둔다.

    전체 y 정렬은 2단 문서의 좌우 열을 교차시키므로 사용하지 않는다. 같은 대상에
    붙는 복구 후보끼리만 원본 bbox의 y/x 순서로 배치한다.
    """
    original = [
        region for region in page["regions"]
        if region.get("origin") in {"paddlex", "document_processor"}
    ]
    recovered = [region for region in page["regions"] if region.get("origin") == "recovery"]
    attached: dict[str, list[dict[str, Any]]] = {}
    unattached = []
    original_ids = {str(region["region_id"]) for region in original}
    for region in recovered:
        target = str(region.get("related_region_id") or "")
        if target in original_ids:
            attached.setdefault(target, []).append(region)
        else:
            unattached.append(region)
    for rows in attached.values():
        rows.sort(key=lambda region: ((region.get("bbox") or [0, 0])[1], (region.get("bbox") or [0, 0])[0]))

    ordered = []
    for region in original:
        ordered.append(region)
        ordered.extend(attached.get(str(region["region_id"]), []))
    # 명시적 대상이 없는 복구 후보는 위치로 임의 재배열하지 않고 후보 생성 순서를 둔다.
    ordered.extend(unattached)

    product_orders: dict[str, int] = {}
    for order, region in enumerate(ordered, start=1):
        product_id = str(region.get("product_id") or "unknown")
        product_orders[product_id] = product_orders.get(product_id, 0) + 1
        region["reading_order"] = order
        region["product_reading_order"] = product_orders[product_id]
    page["regions"] = ordered


def _apply_ownership(
    page: dict[str, Any], result: dict[str, Any],
) -> None:
    """1단계 결과를 적용한다. 라벨은 상품별 템플릿이 정해진 뒤 2단계에서 붙인다."""
    by_region = {item["region_id"]: item for item in result["region_decisions"]}
    for region in page.get("regions") or []:
        decision = by_region[region["region_id"]]
        region["product_id"] = decision["product_id"]
        region["semantic_labels"] = []
        region["needs_review"] = False
        if decision.get("confidence", 0.0) < 0.7:
            flag(region, "ownership_low_confidence")
        if decision.get("product_id") == "unknown":
            flag(region, "ownership_unknown")
        region["semantic_decision"] = decision

    candidates = {item["candidate_id"]: item for item in page["recovery_candidates"]}
    remaining = {line["line_ref"]: line for line in page.get("unassigned_lines") or []}
    ignored_lines = []
    for decision in result["recovery_decisions"]:
        candidate = candidates[decision["candidate_id"]]
        candidate["decision"] = decision
        if decision["action"] == "decorative":
            # VLM의 decorative 판정은 오판할 수 있다. 특히 표의 `담보명`,
            # `보장금액` 같은 짧은 머리글을 장식으로 보는 사례가 있었다.
            # OCR/PDF가 실제 텍스트와 좌표를 준 이상 P3에서 삭제하지 않고,
            # 일반 복구 Region으로 보존한다. 판정 자체는 P1의 `vlm_excluded`에 남긴다.
            ignored_lines.extend(
                {**copy.deepcopy(line), "ignore_reason": decision["reason"]}
                for line in candidate["lines"]
            )
        lines = [copy.deepcopy(line) for line in candidate["lines"]]
        for line in lines:
            remaining.pop(line["line_ref"], None)
        region_id = candidate["candidate_id"]
        page["regions"].append({
            "region_id": region_id,
            "bbox": copy.deepcopy(candidate["bbox"]),
            "kind": candidate.get("kind") or "text",
            "label": "table" if candidate.get("kind") == "table" else "recovery",
            "text": candidate["text"],
            "text_source": "ocr_pdf_recovery",
            "lines": lines,
            "origin": "recovery",
            "bbox_source": candidate["bbox_source"],
            "bbox_quality": candidate["bbox_quality"],
            "product_id": (
                "page_common" if decision["action"] == "page_common" else decision["product_id"]
            ),
            "semantic_labels": [],
            # `decorative` 는 검수 사유가 아니다 — VLM 이 장식이라고 **판정을 끝낸**
            # 영역이라 사람에게 다시 물을 것이 없다. `vlm_excluded` 로 따로 남긴다.
            "needs_review": False,
            "vlm_excluded": decision["action"] == "decorative",
            "related_region_id": decision.get("target_region_id") or None,
            "semantic_decision": decision,
        })
        recovered = page["regions"][-1]
        if decision["action"] == "needs_review":
            flag(recovered, "recovery_action_uncertain")
        if decision.get("confidence", 0.0) < 0.7:
            flag(recovered, "recovery_low_confidence")
    page["unassigned_lines"] = sorted(
        remaining.values(), key=lambda line: ((line.get("bbox") or [0, 0])[1], (line.get("bbox") or [0, 0])[0])
    )
    page["ignored_lines"] = ignored_lines
    page["coarse_missing_candidates"] = [
        {
            **item,
            "bbox": None,
            "bbox_source": "vlm_page_context",
            "bbox_quality": "coarse",
            "status": "requires_crop_ocr",
        }
        for item in result.get("missing_visible_text") or []
    ]
    page["semantic_analysis"] = result.get("analysis")
    page["products"] = result.get("products") or []
    page["table_areas"] = copy.deepcopy(result.get("table_areas") or [])
    page["semantic_bands"] = copy.deepcopy(result.get("semantic_bands") or [])
    _assign_reading_order(page)


# 표 한 칸이 Region 하나로 흩어져 있을 수 있어 줄 수가 아니라 칸 수로 센다.
MIN_TABLE_CELLS = 3


def _promote_vlm_table_areas(page: dict[str, Any]) -> list[dict[str, Any]]:
    """VLM이 표라고 지목했는데 기하학이 놓친 자리를 표 Region으로 승격한다.

    VLM은 **어디를 볼지와 합쳐도 되는지**만 알려준다. 승격된 Region의 좌표와
    문구는 그 안에 있던 Region들의 OCR 줄에서 나온다. 모델 좌표는 쓰지 않는다.

    합치지 않는 경우가 둘이다.

    - `kind == "field_list"`: 시각적으로는 격자지만 각 행이 서로 다른 항목이다.
      복수 라벨을 붙일 수 있어도 행 경계가 사라지면 서로 다른 의미가 한 텍스트로
      섞이므로 합치지 않는다. 실측(2026-09-20): VLM 이 `2. 대출성상품` 의
      항목명–값 블록을 confidence 1.0 으로 표라고 지목했다.
    - 안에 든 Region 들의 상품 소속이 갈릴 때. 상품을 가로질러 합치면 심의
      단위가 섞인다.
    """
    areas = page.get("table_areas") or []
    if not areas:
        return []
    page_no = int(page["page_no"])
    promoted = []
    for area in areas:
        if str(area.get("kind") or "table") != "table":
            continue
        # 복구 Region 만 보면 안 된다 — 표 한 칸이 PaddleX Region 으로 잡혀 있을
        # 수 있다. 실측: `2. 카드상품` 의 5칸 중 2칸이 PaddleX Region 이라 복구
        # 3칸만으로는 줄 수 하한에 걸려 통째로 버려졌다.
        pool = [
            region for region in page["regions"]
            if region.get("kind") != "table"
            and region.get("bbox")
            and (region.get("lines") or [])
        ]
        # VLM 은 좌표가 아니라 목록에 있는 ID 를 고른다. 좌표 추정은 실행마다
        # 크게 흔들려 같은 표를 놓쳤다 — 한 번은 [10,55,40,65] 로 잘 찍고 다음
        # 실행에서는 [46,56,58,65] 로 찍어 실제 표와 3% 만 겹쳤다.
        wanted = {str(value) for value in (area.get("member_ids") or [])}
        seeds = [region for region in pool if str(region["region_id"]) in wanted]
        if len(seeds) < MIN_TABLE_CELLS:
            continue
        inside = tables.grow_cells(seeds, pool)
        owners = {str(region.get("product_id") or "unknown") for region in inside}
        if len(owners - {"unknown"}) > 1:
            continue
        # 새 pN_tNNN ID를 만들지 않는다. PaddleX가 table로 본 Region을 우선
        # 기준점으로 삼고, 없으면 구성원 중 페이지 배열에서 가장 앞선 Region을
        # 쓴다. 표 구조는 그 원래 region_id에 붙는다.
        positions = {
            str(region["region_id"]): index
            for index, region in enumerate(page["regions"])
        }
        anchor = min(
            inside,
            key=lambda region: (
                str((region.get("layout_observation") or {}).get("label") or "").casefold()
                != "table",
                positions.get(str(region["region_id"]), 10**9),
            ),
        )
        merged = tables.merge_regions(
            inside, region_id=str(anchor["region_id"]), anchor=anchor,
        )
        merged["promoted_by"] = {
            "source": "vlm_table_area", "note": area.get("note"),
            "confidence": area.get("confidence"),
        }
        gone = {str(region["region_id"]) for region in inside}
        insert_at = min(positions[region_id] for region_id in gone)
        page["regions"] = [
            region for region in page["regions"] if str(region["region_id"]) not in gone
        ]
        page["regions"].insert(insert_at, merged)
        # 사라진 Region 을 가리키던 연결을 새 표 Region 으로 옮긴다. 그대로 두면
        # 읽기 순서 배치가 대상을 잃는다.
        for region in page["regions"]:
            if str(region.get("related_region_id") or "") in gone:
                region["related_region_id"] = merged["region_id"]
        promoted.append(merged)
    if promoted:
        _assign_reading_order(page)
    return promoted


def _place_tables(page: dict[str, Any], image: Image.Image) -> None:
    """표로 보이는 Region의 OCR 줄을 행·열에 배치한다.

    PaddleX가 `table`로 잡았든(A) 못 잡았든(B) 다른 라벨을 붙였든(C) 같은 경로로
    다시 읽는다. PaddleX의 표 판정은 대상 선정 힌트로만 쓴다.
    """
    _promote_vlm_table_areas(page)
    placed = 0
    for region in page.get("regions") or []:
        existing_table = region.get("table") or {}
        if existing_table.get("source") == "document_processor":
            # PDF 내부 셀과 bbox에서 직접 복원한 표 행이다. 같은 crop을 VLM으로 다시
            # 배치하면 정확한 셀을 근사 결과로 덮게 되므로 구조를 그대로 신뢰한다.
            region["kind"] = "table"
            region["table_status"] = "complete"
            cells = [
                cell for cell in existing_table.get("cells") or []
                if str(cell.get("text") or "").strip()
            ]
            slots = max(
                1,
                int((existing_table.get("grid") or {}).get("rows") or 1)
                * int((existing_table.get("grid") or {}).get("cols") or 1),
            )
            region["table_cell_density"] = round(len(cells) / slots, 4)
            region["text_candidates"] = {
                **(region.get("text_candidates") or {}),
                "table_grid": str(region.get("text") or ""),
            }
            placed += 1
            continue
        lines = [line for line in region.get("lines") or [] if line.get("bbox")]
        # PaddleX 라벨은 `region["label"]` 이 아니라 `layout_observation` 안에 있다.
        # 키를 잘못 읽는 바람에 "PaddleX 가 table 이라 부른 영역" 경로가 한 번도
        # 발동하지 않았다 — 실측(2026-09-20, 9개 파일): 후보 5개 중 1개만 복원됐고
        # 나머지 4개는 전부 `우대 조건 | 우대 금리` 형태의 진짜 2열 표였다.
        layout_label = str(
            (region.get("layout_observation") or {}).get("label")
            or region.get("label") or ""
        ).casefold()
        is_candidate = (
            region.get("kind") == "table"
            or layout_label == "table"
            or tables.looks_like_grid(lines)
        )
        if not is_candidate or len(lines) < tables.MIN_LINES:
            continue
        grid = tables.place_cells(image, region)
        if not grid:
            region["table_status"] = "not_a_table"
            continue
        region["kind"] = "table"
        region["table"] = grid
        # 원래 줄 이어붙이기는 후보로 남긴다. 모든 줄이 셀/주석에 배치되고 신뢰도가
        # 충분할 때만 격자 표현을 정본으로 쓴다. 불완전 표가 정확한 OCR 줄을 P3에서
        # 가리는 일을 막는다.
        assembled = str(region.get("text") or "").strip() or "\n".join(
            str(line.get("text") or "").strip() for line in lines
            if str(line.get("text") or "").strip()
        )
        region["text_candidates"] = {
            **(region.get("text_candidates") or {}),
            "line_assembled": assembled,
            "table_grid": grid["text_grid"],
        }
        slots = max(1, int(grid["grid"]["rows"]) * int(grid["grid"]["cols"]))
        density = len([cell for cell in grid["cells"] if str(cell.get("text") or "").strip()]) / slots
        region["table_cell_density"] = round(density, 4)
        complete = (
            not grid["unplaced_line_refs"]
            and grid["confidence"] >= 0.7
            and density >= 0.3
            and len(grid.get("notes") or []) <= len([
                cell for cell in grid["cells"] if str(cell.get("text") or "").strip()
            ])
        )
        region["table_status"] = "complete" if complete else "partial"
        if complete:
            # 셀 배치는 구조 힌트일 뿐 원문을 대체하지 않는다. Markdown 격자를
            # selected_text로 쓰면 완전/부분 표에 따라 표현이 달라지고, 잘못 배치된
            # 셀이 검색 텍스트까지 오염시킨다.
            region["text"] = assembled
            region["text_source"] = "ocr_table_lines"
        else:
            region["text"] = assembled
            # 표 구조는 P1/P3에 남지만 selected_text는 모든 원문 줄을 보존한다.
            region["text_source"] = "ocr_table_lines_fallback"
            if grid["unplaced_line_refs"]:
                flag(region, "table_unplaced_lines")
            if grid["confidence"] < 0.7:
                flag(region, "table_low_confidence")
            if density < 0.3:
                flag(region, "table_sparse_grid")
            if len(grid.get("notes") or []) > len([
                cell for cell in grid["cells"] if str(cell.get("text") or "").strip()
            ]):
                flag(region, "table_structure_incomplete")
        placed += 1
    page["table_count"] = placed


def _label_pages(
    pages: list[dict[str, Any]],
    product_templates: dict[str, dict[str, Any]],
    images: dict[int, Image.Image],
    catalog: dict[str, Any] | None = None,
) -> None:
    """상품별로 나눠 라벨링한다.

    한 요청에 두 상품의 구분값을 함께 넣으면 모델이 상품1 Region에 템플릿B의
    구분값을 붙일 수 있다. enum은 둘 다 허용 목록에 있으니 막아주지 못한다.
    """
    for page in pages:
        groups: dict[str, list[dict[str, Any]]] = {}
        for region in page.get("regions") or []:
            groups.setdefault(str(region.get("product_id") or "unknown"), []).append(region)

        notes = []
        for product_id, regions in groups.items():
            resolution = product_templates.get(product_id) or {}
            labels = list(resolution.get("labels") or [])
            if not labels:
                # 템플릿을 확정하지 못한 상품은 라벨을 억지로 붙이지 않고 남긴다.
                for region in regions:
                    region["semantic_labels"] = []
                    flag(region, "template_unresolved")
                    region["label_decision"] = {
                        "region_id": region["region_id"],
                        "labels": [],
                        "confidence": 0.0,
                        "reason": f"{product_id} 템플릿 미확정으로 라벨링 보류",
                    }
                continue
            result = analyze_product_labels(
                images[int(page["page_no"])],
                page,
                regions,
                product_id=product_id,
                product_name=resolution.get("product_name"),
                template_id=resolution.get("template_id"),
                labels=labels,
                positive_examples=template_label_examples(
                    catalog or load_catalog(), resolution.get("template_id"), labels,
                ),
            )
            notes.append(result.get("analysis") or "")
            by_region = {item["region_id"]: item for item in result["region_labels"]}
            for region in regions:
                decision = by_region[str(region["region_id"])]
                heading_evidence = explicit_heading_evidence(region.get("text"), labels)
                selected = list(heading_evidence)
                selected += [
                    label for label in decision["labels"] if label not in selected
                ]
                selected = constrain_title_labels(region, selected)
                selected = constrain_rate_calculation_labels(region, selected)
                region["semantic_labels"] = selected
                decision["labels"] = list(selected)
                evidence = [
                    {"label": label, "quote": quote, "source": "explicit_heading"}
                    for label, quote in heading_evidence.items() if label in selected
                ]
                evidence += [
                    {**item, "source": "vlm"}
                    for item in decision.get("evidence") or []
                    if item.get("label") in selected
                    and item.get("label") not in {entry["label"] for entry in evidence}
                ]
                decision["evidence"] = evidence
                decision["deterministic_labels"] = [
                    label for label in heading_evidence if label in selected
                ]
                region["label_decision"] = decision
                # 라벨이 없는 것 자체는 검수 사유가 아니다. 광고 수식어구처럼
                # 템플릿의 어느 구분값에도 해당하지 않는 문구가 정상적으로 존재한다.
                # 검수가 필요한 쪽은 **붙였는데 확신이 낮은** 경우다 — 틀린 구분값이
                # 붙는 편이 안 붙는 편보다 나쁘다.
                # 실측(2026-09-21, 26건): 검수 170건 중 152건이 "라벨 없음"이었고,
                # 그중 7건은 VLM 이 이미 장식으로 판정한 영역이었다.
                if decision["confidence"] < 0.7:
                    flag(region, "label_low_confidence")
        page["label_analysis"] = "\n".join(value for value in notes if value)


def _append_p3_label_studio(
    tasks: list[dict[str, Any]], p3_documents: list[dict[str, Any]], out: Path,
) -> None:
    """기존 진단 탭 뒤에 최종 P3 Region을 시각 검수할 탭을 추가한다."""
    from ..ocr import view

    pages: dict[tuple[str, int], list[dict[str, Any]]] = {}
    labels: set[str] = set()
    for document in p3_documents:
        source_file = str(document["document"]["source_file"])
        for page in document["pages"]:
            rows = []
            for region in page["regions"]:
                semantic = ", ".join(region.get("labels") or []) or "미분류"
                label = f"P3 {region.get('product_id') or 'unknown'} | {semantic}"
                labels.add(label)
                rows.append({
                    "bbox": region["bbox"],
                    "label": label,
                    "content": (
                        f"{region['region_id']} | review={region['needs_review']} | "
                        f"labels={semantic} | {region['selected_text']}"
                    ),
                })
            pages[(source_file, int(page["page_no"]))] = rows

    enriched = copy.deepcopy(tasks)
    for task in enriched:
        key = (str(task["data"]["source_file"]), int(task["data"]["page_no"]))
        rows = pages.get(key, [])
        if not rows:
            continue
        # 모든 P3 bbox는 이 task의 원본 canvas 좌표계다.
        first = next(
            page
            for document in p3_documents
            if str(document["document"]["source_file"]) == key[0]
            for page in document["pages"]
            if int(page["page_no"]) == key[1]
        )
        width, height = (int(value) for value in first["canvas"])
        task["predictions"].append({
            "model_version": f"{task['data']['run_name']} 4-p3-semantic",
            "result": [
                result
                for index, row in enumerate(rows)
                if (result := view.rectangle(
                    row, width, height, box_id=f"p3_{index:03d}",
                ))
            ],
        })
    _write_json(out / "label-studio.json", enriched)

    config_path = out / "labeling-config.xml"
    config = config_path.read_text(encoding="utf-8")
    new_rows = "\n".join(
        f'    <Label value="{label}" background="{view.color_for(label, sorted(labels))}" />'
        for label in sorted(labels)
        if f'value="{label}"' not in config
    )
    if new_rows:
        config = config.replace("  </RectangleLabels>", f"{new_rows}\n  </RectangleLabels>")
    config = config.replace(
        "1-raw parsing / 2-text source / 3-unassigned",
        "1-raw / 2-canonical / 3-unassigned / 4-P3 semantic",
    )
    config_path.write_text(config, encoding="utf-8")


def _write_stage_views(documents: list[dict[str, Any]], out: Path) -> None:
    """상품 소유권과 VLM 판정을 P1보다 작은 중간 산출물로 분리한다."""
    ownership, vlm_evidence = [], []
    for document in documents:
        ownership.append({
            "source_file": document["source_file"],
            "product_templates": copy.deepcopy(document.get("product_templates") or {}),
            "review_units": copy.deepcopy(document.get("review_units") or []),
            "pages": [{
                "page_no": page["page_no"],
                "products": copy.deepcopy(page.get("products") or []),
                "regions": [{
                    "region_id": region["region_id"],
                    "product_id": region.get("product_id"),
                    "labels": copy.deepcopy(region.get("semantic_labels") or []),
                    "reading_order": region.get("reading_order"),
                    "product_reading_order": region.get("product_reading_order"),
                    "related_region_id": region.get("related_region_id"),
                } for region in page.get("regions") or []],
            } for page in document.get("pages") or []],
        })
        vlm_evidence.append({
            "source_file": document["source_file"],
            "classification": copy.deepcopy(document.get("classification")),
            "template": copy.deepcopy(document.get("template")),
            "product_templates": copy.deepcopy(document.get("product_templates") or {}),
            "pages": [{
                "page_no": page["page_no"],
                "status": page.get("semantic_status"),
                "analysis": page.get("semantic_analysis"),
                "label_analysis": page.get("label_analysis"),
                "reading_stats": copy.deepcopy(page.get("reading_stats") or {}),
                "region_readings": [
                    {"region_id": region["region_id"],
                     "status": region.get("reading_status"),
                     "reader": copy.deepcopy(region.get("vlm_reading") or {}),
                     "judge": copy.deepcopy(region.get("vlm_judge") or {})}
                    for region in page.get("regions") or []
                    if region.get("vlm_reading")
                ],
                "table_areas": copy.deepcopy(page.get("table_areas") or []),
                "semantic_bands": copy.deepcopy(page.get("semantic_bands") or []),
                "region_decisions": [
                    copy.deepcopy(region.get("semantic_decision"))
                    for region in page.get("regions") or []
                    if region.get("semantic_decision")
                ],
                "label_decisions": [
                    copy.deepcopy(region.get("label_decision"))
                    for region in page.get("regions") or []
                    if region.get("label_decision")
                ],
                "recovery_decisions": [
                    copy.deepcopy(candidate.get("decision"))
                    for candidate in page.get("recovery_candidates") or []
                    if candidate.get("decision")
                ],
                "missing_visible_text": copy.deepcopy(
                    page.get("coarse_missing_candidates") or []
                ),
            } for page in document.get("pages") or []],
        })
    _write_json(out / "03-ownership.json", ownership)
    _write_json(out / "04-vlm-evidence.json", vlm_evidence)


def run_full_pipeline(
    documents: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    *,
    out: Path,
    media_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """기준 OCR 결과에 fc87 Gemma 의미 판정을 붙이고 P1/P3를 저장한다."""
    vlm_client.reset_stats()
    started = time.time()
    read_scope = reading.scope_from_env()
    media = _media_map(tasks, media_dir)
    catalog = load_catalog()
    p1_documents, p3_documents = [], []

    for raw_document in documents:
        pages = [_prepare_page(page) for page in raw_document["pages"]]
        source_file = str(raw_document["source_file"])
        first_image = Image.open(media[(source_file, int(pages[0]["page_no"]))]).convert("RGB")
        classification = vlm_client.classify(first_image, source_file)
        doc = {
            "doc_id": Path(source_file).stem,
            "source_file": source_file,
            "file_type": Path(source_file).suffix.lower().lstrip("."),
            "product_group": classification.product_group,
            "ad_type": classification.ad_type,
            "product_name_shown": classification.product_name_shown,
            "category_source": classification.category_source,
            "classification_confidence": classification.confidence,
            "classification": asdict(classification),
            "pages": pages,
        }
        # 1단계 — 상품 소유권. 템플릿을 아직 모르므로 라벨은 붙이지 않는다.
        images = {
            int(page["page_no"]): Image.open(
                media[(source_file, int(page["page_no"]))]
            ).convert("RGB")
            for page in pages
        }
        for page in pages:
            result = analyze_page_context(
                images[int(page["page_no"])], page, page["recovery_candidates"],
            )
            _apply_ownership(page, result)
            # 표 구조는 라벨링보다 먼저 복원한다. 라벨러가 셀 낱개가 아니라
            # 표 하나를 보게 해야 구분값을 한 번만 붙인다.
            _place_tables(page, images[int(page["page_no"])])
            align_hwp_structure(page)
            # 영역 판독도 라벨링 앞이다. 깨진 텍스트로 라벨을 정하면 엉뚱한
            # 구분값이 붙는다 — `2. 대출성상품` p1_r025 는 `)` 한 글자로
            # `상품명` 라벨을 받았다.
            reading.read_page(page, images[int(page["page_no"])], scope=read_scope)
            page["semantic_status"] = "complete"

        # 2단계 — 상품별 템플릿. 소유권이 나와야 상품군을 알 수 있으므로 여기서 푼다.
        product_templates = resolve_product_templates(doc, catalog)
        doc["product_templates"] = product_templates
        doc["template"] = _document_template(product_templates)

        # 3단계 — 상품별 라벨링. 한 Region에 해당하는 구분값을 한 번에 모두
        # 받는다. 줄별 span이나 자식 Region은 만들지 않는다.
        _label_pages(pages, product_templates, images, catalog)
        doc["multi_label_regions"] = sum(
            1 for page in pages for region in page.get("regions") or []
            if len(region.get("semantic_labels") or []) > 1
        )
        # VLM/표/텍스트/라벨 판단이 모두 끝난 뒤 외부 ID만 통일한다.
        # 배열 순서를 바꾸지 않으므로 reading_order와 상품 소유권은 그대로다.
        normalize_region_ids(pages)
        doc["review_units"] = review_units(pages, product_templates)

        p1 = build_p1(doc)
        p3 = build_p3(p1)
        p1_documents.append(p1)
        p3_documents.append(p3)
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in source_file)
        final_dir = out / "final"
        final_dir.mkdir(parents=True, exist_ok=True)
        _write_json(final_dir / f"{safe}.p1.json", p1)
        _write_json(final_dir / f"{safe}.p3.json", p3)

    _write_stage_views(p1_documents, out)
    _write_json(out / "05-p1.json", p1_documents)
    _write_json(out / "06-p3.json", p3_documents)
    _append_p3_label_studio(tasks, p3_documents, out)
    totals: dict[str, int] = {}
    for document in p1_documents:
        for page in document.get("pages") or []:
            for key, value in (page.get("reading_stats") or {}).items():
                totals[key] = totals.get(key, 0) + value
    stats = {
        "elapsed_seconds": round(time.time() - started, 3),
        "by_schema": copy.deepcopy(vlm_client.STATS),
        "region_reading": {"scope": read_scope, **totals},
    }
    _write_json(out / "vlm-stats.json", stats)
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["semantic_pipeline"] = stats
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return p1_documents, p3_documents
