"""전체 근거(P1)와 간결한 심의 입력(P3)을 만든다."""
from __future__ import annotations

import copy
import re
from typing import Any


P1_VERSION = "nh-ad-parse-evidence-v3"
P3_VERSION = "nh-ad-region-review-input-v7"


def build_p1(document: dict[str, Any]) -> dict[str, Any]:
    """모든 OCR/PDF/VLM 관측과 후처리 근거를 보존한다."""
    evidence = copy.deepcopy(document)
    evidence["contract"] = {
        "version": P1_VERSION,
        "purpose": "OCR/PDF/VLM 관측과 정확한 페이지 bbox 보존",
        "bbox_policy": "OCR/PDF/레이아웃 좌표만 exact; VLM은 텍스트와 ID만 판정",
        "text_policy": "parser, VLM Reader, VLM Judge 후보와 최종 선택을 모두 보존",
    }
    region_count = sum(len(page.get("regions") or []) for page in evidence.get("pages") or [])
    evidence["summary"] = {
        "page_count": len(evidence.get("pages") or []),
        "region_count": region_count,
        "recovery_region_count": sum(
            1
            for page in evidence.get("pages") or []
            for region in page.get("regions") or []
            if region.get("origin") == "recovery"
        ),
        "unassigned_line_count": sum(
            len(page.get("unassigned_lines") or []) for page in evidence.get("pages") or []
        ),
    }
    return evidence


def _compact_table(
    table: dict[str, Any] | None, status: str | None,
) -> dict[str, Any] | None:
    """검증된 표만 반복 키 없는 행렬로 줄여 P3에 싣는다.

    VLM 셀 배치가 부분적이면 추정 구조를 노출하지 않는다. P3의 selected_text가
    원문을 보존하고, 셀·bbox·line_ref·미배치 사유는 P1에서 조회한다.
    """
    if not table:
        return None
    grid = table.get("grid") or {}
    rows = max(0, int(grid.get("rows") or 0))
    cols = max(0, int(grid.get("cols") or 0))
    compact: dict[str, Any] = {
        "status": "complete" if status == "complete" else "partial",
        "shape": [rows, cols],
    }
    if status != "complete" or not rows or not cols:
        return compact

    matrix: list[list[str | None]] = [[None for _ in range(cols)] for _ in range(rows)]
    header_rows: set[int] = set()
    for cell in table.get("cells") or []:
        row, col = int(cell.get("row") or 0), int(cell.get("col") or 0)
        if not (0 <= row < rows and 0 <= col < cols):
            continue
        value = str(cell.get("text") or "").strip()
        matrix[row][col] = value or None
        if cell.get("is_header"):
            header_rows.add(row)
    compact["header_rows"] = sorted(header_rows)
    compact["rows"] = matrix
    notes = []
    for note in table.get("notes") or []:
        value = str(note.get("text") or "").strip() if isinstance(note, dict) else str(note).strip()
        if value:
            notes.append(value)
    if notes:
        compact["notes"] = notes
    return compact


def _review_units(evidence: dict[str, Any], kept_ids: set[str]) -> list[dict[str, Any]]:
    """상품별 심의 묶음에 템플릿과 실제 Region ID만 싣는다."""
    templates = evidence.get("product_templates") or {}
    output = []
    for unit in evidence.get("review_units") or []:
        product_id = str(unit.get("product_id") or "")
        template = templates.get(product_id) or {}
        region_ids = [
            str(value) for value in unit.get("region_ids") or []
            if str(value) in kept_ids
        ]
        common_ids = [
            str(value) for value in unit.get("page_common_region_ids") or []
            if str(value) in kept_ids
        ]
        output.append({
            "product_id": product_id,
            "product_name": unit.get("product_name") or template.get("product_name"),
            "template_id": unit.get("template_id") or template.get("template_id"),
            "region_ids": region_ids,
            "page_common_region_ids": common_ids,
        })
    return output


def _text_source(region: dict[str, Any]) -> str:
    """정본 텍스트를 OCR/PDF 가 읽었는지 VLM 이 썼는지만 남긴다.

    P3 는 후보 텍스트를 싣지 않으므로, 이 값이 없으면 "이 문장이 광고에 찍힌
    그대로인지 모델이 고쳐 쓴 것인지"를 P1 을 열어야만 알 수 있다. 근거의 등급이
    갈리는 값이라 Region 마다 채운다. 어느 후보였는지·일치도·판정 사유 같은
    세부는 같은 ``region_id`` 로 P1 에서 조회한다.
    """
    source = str(region.get("text_source") or "")
    if source.startswith("vlm"):
        return "vlm"
    if source.startswith("digital"):
        return "digital"
    if source.startswith("hwp_structure"):
        return "hwp"
    if source.startswith("document_processor_html"):
        return "hwp"
    if source.startswith("document_processor"):
        return "digital"
    return "ocr"


def build_p3(evidence: dict[str, Any]) -> dict[str, Any]:
    """P1을 Region 중심의 간결한 심의 입력으로 투영한다.

    P3는 심의 모델이 읽을 최종 텍스트와 구분값만 담는다. OCR 줄, 후보 텍스트,
    읽기 순서, 색인, 진단과 좌표 출처는 P1의 같은 ``region_id``로 조회한다.
    """
    pages_out = []
    kept_ids: set[str] = set()
    for page in evidence.get("pages") or []:
        regions_out = []
        for region in page.get("regions") or []:
            text = str(region.get("text") or "").strip()
            bbox = copy.deepcopy(region.get("bbox"))
            if not text and not bbox:
                continue
            region_id = str(region["region_id"])
            expected = rf"p{int(page['page_no'])}_r\d{{3,}}"
            if re.fullmatch(expected, region_id) is None:
                raise ValueError(
                    f"P3 region_id 형식이 잘못되었습니다: {region_id}; expected {expected}"
                )
            if region_id in kept_ids:
                raise ValueError(f"P3 region_id가 중복되었습니다: {region_id}")
            kept_ids.add(region_id)
            labels = []
            for label in region.get("semantic_labels") or []:
                value = str(label or "").strip()
                if value and value not in labels:
                    labels.append(value)
            item = {
                "region_id": region_id,
                "product_id": region.get("product_id"),
                "bbox": bbox,
                "selected_text": text,
                "labels": labels,
                "kind": "table" if region.get("table") else "text",
                "needs_review": bool(region.get("needs_review")),
                "text_source": _text_source(region),
            }
            table = _compact_table(region.get("table"), region.get("table_status"))
            if table:
                item["table"] = table
            regions_out.append(item)
        pages_out.append({
            "page_no": int(page["page_no"]),
            # bbox를 화면 좌표로 환산하는 데 필요한 최소 메타데이터다.
            "canvas": copy.deepcopy(page.get("canvas")),
            "regions": regions_out,
        })

    return {
        "contract": {
            "version": P3_VERSION,
            "source_evidence_version": P1_VERSION,
            "review_unit": "region",
            "text_policy": "one evidence-verified final text per region; all candidates stay in P1",
            "text_source_values": ["hwp", "digital", "ocr", "vlm"],
            "region_id_policy": "page-scoped pN_rNNN in final page order",
            "reference_policy": "P1 and P3 share region_id; review results return region_ids",
        },
        "document": {
            key: copy.deepcopy(evidence.get(key))
            for key in ("doc_id", "source_file", "file_type", "classification", "template")
        },
        "review_units": _review_units(evidence, kept_ids),
        "pages": pages_out,
        "review_result_contract": {
            "required_fields": ["result", "reason", "region_ids"],
            "result_enum": ["위반", "판정불가", "충족"],
            "region_ids": "P3 pages[].regions[].region_id에 존재하는 값만 허용",
        },
    }
