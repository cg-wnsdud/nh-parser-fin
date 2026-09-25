"""`document-processor` DocIR를 파이프라인의 정확한 구조 후보로 바꾼다.

VLM은 좌표나 표 셀을 발명하지 않는다. 이 모듈이 디지털 PDF의 문단/표 셀 bbox를
현재 페이지 캔버스 좌표로 변환하고, 표는 심의 단위에 가까운 행 단위 Region 후보로
만든다. 구조를 읽을 수 없거나 페이지가 스캔형이면 호출측은 기존 Paddle/OCR 경로를
그대로 사용한다.

`document_processor`는 사내 사설 패키지라 모듈 import 시점에는 의존하지 않는다.
운영 이미지에 패키지가 설치되지 않은 경우 PDF/이미지 기존 경로는 계속 동작한다.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


def _norm(text: Any) -> str:
    return re.sub(r"\s+", "", str(text or "")).casefold()


def _bbox_values(bbox: Any) -> tuple[float, float, float, float] | None:
    if bbox is None:
        return None
    if isinstance(bbox, (tuple, list)) and len(bbox) == 4:
        values = tuple(bbox)
    elif isinstance(bbox, dict):
        values = tuple(
            bbox.get(key) for key in ("left_pt", "bottom_pt", "right_pt", "top_pt")
        )
    else:
        values = tuple(
            getattr(bbox, key, None)
            for key in ("left_pt", "bottom_pt", "right_pt", "top_pt")
        )
    if any(value is None for value in values):
        return None
    left, bottom, right, top = (float(value) for value in values)
    if right <= left or top <= bottom:
        return None
    return left, bottom, right, top


def _union_bbox(values: Iterable[Any]) -> tuple[float, float, float, float] | None:
    boxes = [box for value in values if (box := _bbox_values(value)) is not None]
    if not boxes:
        return None
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _to_pixels(
    bbox: Any, *, page_width_pt: float, page_height_pt: float, canvas: tuple[int, int],
) -> list[int] | None:
    box = _bbox_values(bbox)
    if box is None or page_width_pt <= 0 or page_height_pt <= 0:
        return None
    left, bottom, right, top = box
    width_px, height_px = canvas
    x0 = round(left / page_width_pt * width_px)
    x1 = round(right / page_width_pt * width_px)
    y0 = round((page_height_pt - top) / page_height_pt * height_px)
    y1 = round((page_height_pt - bottom) / page_height_pt * height_px)
    x0 = max(0, min(width_px, x0))
    x1 = max(0, min(width_px, x1))
    y0 = max(0, min(height_px, y0))
    y1 = max(0, min(height_px, y1))
    if x1 <= x0 or y1 <= y0:
        return None
    return [x0, y0, x1, y1]


def _page_number(node: Any, fallback: int = 1) -> int:
    return max(1, int(getattr(node, "page_number", None) or fallback))


def _direct_runs(paragraph: Any) -> list[Any]:
    return [
        node for node in (getattr(paragraph, "content", None) or [])
        if type(node).__name__ == "RunIR" and str(getattr(node, "text", "") or "").strip()
    ]


def _direct_tables(paragraph: Any) -> list[Any]:
    tables = [
        node for node in (getattr(paragraph, "content", None) or [])
        if type(node).__name__ == "TableIR"
    ]
    for table in getattr(paragraph, "tables", None) or []:
        if all(existing is not table for existing in tables):
            tables.append(table)
    return tables


def _direct_images(paragraph: Any) -> list[Any]:
    images = [
        node for node in (getattr(paragraph, "content", None) or [])
        if type(node).__name__ == "ImageIR"
    ]
    for image in getattr(paragraph, "images", None) or []:
        if all(existing is not image for existing in images):
            images.append(image)
    return images


def _paragraph_images(paragraph: Any, seen: set[int] | None = None) -> list[Any]:
    seen = seen if seen is not None else set()
    images: list[Any] = []
    for image in _direct_images(paragraph):
        marker = id(image)
        if marker not in seen:
            seen.add(marker)
            images.append(image)
    for table in _direct_tables(paragraph):
        for _row, _col, cell in _cell_positions(table):
            for child in getattr(cell, "paragraphs", None) or []:
                images.extend(_paragraph_images(child, seen))
    return images


def _cell_positions(table: Any) -> list[tuple[int, int, Any]]:
    try:
        return list(table.iter_cell_positions())
    except Exception:
        result: list[tuple[int, int, Any]] = []
        seen: set[int] = set()
        for row, values in enumerate(getattr(table, "cells", None) or []):
            for col, cell in enumerate(values or []):
                marker = id(cell)
                if marker in seen:
                    continue
                seen.add(marker)
                result.append((row, col, cell))
        return result


def _cell_span(cell: Any, name: str) -> int:
    style = getattr(cell, "cell_style", None)
    return max(1, int(getattr(style, name, None) or getattr(cell, name, None) or 1))


def _cell_tables(cell: Any) -> list[Any]:
    tables: list[Any] = []
    for paragraph in getattr(cell, "paragraphs", None) or []:
        tables.extend(_direct_tables(paragraph))
    return tables


def _run_key(run: Any) -> str:
    return str(getattr(run, "node_id", None) or f"object:{id(run)}")


def _inside(inner: Any, outer: Any, tolerance: float = 0.75) -> bool:
    run_box = _bbox_values(inner)
    cell_box = _bbox_values(outer)
    if run_box is None or cell_box is None:
        return False
    center_x = (run_box[0] + run_box[2]) / 2.0
    center_y = (run_box[1] + run_box[3]) / 2.0
    return (
        cell_box[0] - tolerance <= center_x <= cell_box[2] + tolerance
        and cell_box[1] - tolerance <= center_y <= cell_box[3] + tolerance
    )


def _cell_text(
    cell: Any,
    *,
    page_runs: list[Any] | None = None,
    used_run_ids: set[str] | None = None,
) -> str:
    text = str(getattr(cell, "text", "") or "").strip()
    if text:
        return text
    parts: list[str] = []
    for paragraph in getattr(cell, "paragraphs", None) or []:
        direct = "".join(
            str(getattr(run, "text", "") or "") for run in _direct_runs(paragraph)
        ).strip()
        if not direct and not _direct_tables(paragraph):
            direct = str(getattr(paragraph, "text", "") or "").strip()
        if direct:
            parts.append(direct)
    text = "\n".join(parts).strip()
    if text or not page_runs:
        return text

    # PDF 표 복원기는 격자와 셀 bbox를 만들고도 원문 Run을 셀 안으로 옮기지 않는
    # 경우가 있다(적립식 샘플: 43셀 중 35셀이 이 형태). 셀 bbox 안의 PDF Run을
    # 기하학으로 배치하면 OCR/VLM 없이 원문과 열 관계를 복원할 수 있다.
    matched = [
        run for run in page_runs
        if _inside(getattr(run, "bbox", None), getattr(cell, "bbox", None))
    ]
    matched.sort(key=lambda run: (
        -(_bbox_values(getattr(run, "bbox", None)) or (0, 0, 0, 0))[3],
        (_bbox_values(getattr(run, "bbox", None)) or (0, 0, 0, 0))[0],
    ))
    if used_run_ids is not None:
        used_run_ids.update(_run_key(run) for run in matched)
    return " ".join(
        str(getattr(run, "text", "") or "").strip()
        for run in matched if str(getattr(run, "text", "") or "").strip()
    ).strip()


def _table_rows(
    table: Any,
    *,
    page_width_pt: float,
    page_height_pt: float,
    canvas: tuple[int, int],
    page_runs: list[Any] | None = None,
    used_run_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    positions = _cell_positions(table)
    by_row: dict[int, list[tuple[int, Any]]] = {}
    for row, col, cell in positions:
        by_row.setdefault(int(row), []).append((int(col), cell))

    table_id = str(getattr(table, "node_id", None) or f"table_{id(table)}")
    rows: list[dict[str, Any]] = []
    for row_index in sorted(by_row):
        ordered = sorted(by_row[row_index], key=lambda item: item[0])
        nonempty = [
            (col, cell) for col, cell in ordered
            if _cell_text(cell, page_runs=page_runs, used_run_ids=used_run_ids)
        ]
        nested_tables = [
            nested for _, cell in ordered for nested in _cell_tables(cell)
        ]
        if nested_tables:
            # 레이아웃 컨테이너 셀의 `.text`에는 중첩표 평문이 이미 합쳐져 있다.
            # 부모 행과 자식 표를 모두 내보내면 같은 글자가 반복되므로 자식 행만 쓴다.
            for nested in nested_tables:
                rows.extend(_table_rows(
                    nested,
                    page_width_pt=page_width_pt,
                    page_height_pt=page_height_pt,
                    canvas=canvas,
                    page_runs=page_runs,
                    used_run_ids=used_run_ids,
                ))
            continue
        if not nonempty:
            continue
        row_bbox_pt = _union_bbox(getattr(cell, "bbox", None) for _, cell in nonempty)
        row_bbox = _to_pixels(
            row_bbox_pt,
            page_width_pt=page_width_pt,
            page_height_pt=page_height_pt,
            canvas=canvas,
        )
        if row_bbox is None:
            continue

        cells: list[dict[str, Any]] = []
        texts: list[str] = []
        max_col = 0
        for col, cell in nonempty:
            text = _cell_text(cell, page_runs=page_runs, used_run_ids=used_run_ids)
            col_span = _cell_span(cell, "colspan")
            row_span = _cell_span(cell, "rowspan")
            max_col = max(max_col, col + col_span)
            texts.append(text)
            cells.append({
                "row": 0,
                "col": col,
                "row_span": row_span,
                "col_span": col_span,
                "is_header": col == min(item[0] for item in nonempty),
                "text": text,
                "bbox": _to_pixels(
                    getattr(cell, "bbox", None),
                    page_width_pt=page_width_pt,
                    page_height_pt=page_height_pt,
                    canvas=canvas,
                ),
                "node_id": getattr(cell, "node_id", None),
            })
        rows.append({
            "bbox": row_bbox,
            "label": "table",
            "kind": "table",
            "content": " | ".join(texts),
            "text_source": "document_processor",
            "bbox_source": "document_processor_pdf_cells",
            "bbox_quality": "exact",
            "structured": {
                "source": "document_processor",
                "node_kind": "table_row",
                "table_id": table_id,
                "row": row_index,
                "node_ids": [
                    str(getattr(cell, "node_id", "")) for _, cell in nonempty
                    if getattr(cell, "node_id", None)
                ],
            },
            "table": {
                "source": "document_processor",
                "grid": {"rows": 1, "cols": max(1, max_col)},
                "cells": cells,
                "notes": [],
            },
        })
    return rows


@dataclass(frozen=True)
class StructuredPage:
    blocks: list[dict[str, Any]]
    metrics: dict[str, Any]
    route: str


def load_docir(path: Path) -> Any:
    """사내 패키지가 있을 때만 DocIR을 읽는다. 호출측은 예외 시 기존 경로로 폴백한다."""
    from document_processor import DocIR

    return DocIR.from_file(str(path))


def _doc_page(docir: Any, page_no: int) -> Any | None:
    return next(
        (page for page in (getattr(docir, "pages", None) or [])
         if int(getattr(page, "page_number", 0) or 0) == page_no),
        None,
    )


def pdf_page_structure(
    docir: Any,
    *,
    page_no: int,
    canvas: tuple[int, int],
    digital_lines: list[dict[str, Any]] | None = None,
) -> StructuredPage:
    """PDF DocIR 한 쪽을 문단/표 행 후보로 바꾸고 fast-path 적합성을 판정한다."""
    page = _doc_page(docir, page_no)
    page_width_pt = float(getattr(page, "width_pt", 0.0) or 0.0)
    page_height_pt = float(getattr(page, "height_pt", 0.0) or 0.0)
    parse_status = str(getattr(page, "parse_status", "") or "")
    blocks: list[dict[str, Any]] = []
    image_area = 0
    page_area = max(1, canvas[0] * canvas[1])
    paragraphs = list(getattr(docir, "paragraphs", None) or [])
    page_runs = [
        run for paragraph in paragraphs for run in _direct_runs(paragraph)
        if _page_number(run) == page_no
    ]
    used_run_ids: set[str] = set()

    # 표부터 처리해야 셀 bbox로 회수한 Run을 일반 문단 Region에서 다시 내보내지 않는다.
    for paragraph in paragraphs:
        tables = _direct_tables(paragraph)
        for table in tables:
            if _page_number(table) != page_no:
                continue
            blocks.extend(_table_rows(
                table,
                page_width_pt=page_width_pt,
                page_height_pt=page_height_pt,
                canvas=canvas,
                page_runs=page_runs,
                used_run_ids=used_run_ids,
            ))

    for paragraph in paragraphs:
        runs = [
            run for run in _direct_runs(paragraph)
            if _page_number(run) == page_no and _run_key(run) not in used_run_ids
        ]
        if runs:
            text = "".join(str(getattr(run, "text", "") or "") for run in runs).strip()
            bbox_pt = _bbox_values(getattr(paragraph, "bbox", None)) or _union_bbox(
                getattr(run, "bbox", None) for run in runs
            )
            bbox = _to_pixels(
                bbox_pt,
                page_width_pt=page_width_pt,
                page_height_pt=page_height_pt,
                canvas=canvas,
            )
            if text and bbox is not None:
                blocks.append({
                    "bbox": bbox,
                    "label": "text",
                    "kind": "text",
                    "content": text,
                    "text_source": "document_processor",
                    "bbox_source": "document_processor_pdf_text",
                    "bbox_quality": "exact",
                    "structured": {
                        "source": "document_processor",
                        "node_kind": "paragraph",
                        "node_ids": [
                            str(getattr(run, "node_id", "")) for run in runs
                            if getattr(run, "node_id", None)
                        ],
                    },
                })
        for image in _paragraph_images(paragraph):
            if _page_number(image) != page_no:
                continue
            bbox = _to_pixels(
                getattr(image, "bbox", None),
                page_width_pt=page_width_pt,
                page_height_pt=page_height_pt,
                canvas=canvas,
            )
            if bbox:
                image_area += max(0, bbox[2] - bbox[0]) * max(0, bbox[3] - bbox[1])

    blocks.sort(key=lambda block: (block["bbox"][1], block["bbox"][0]))
    for order, block in enumerate(blocks, start=1):
        block["order"] = order
        block["piece"] = 0

    structured_text = _norm("\n".join(str(block.get("content") or "") for block in blocks))
    digital_text = _norm("\n".join(str(line.get("text") or "") for line in (digital_lines or [])))
    if digital_text and structured_text:
        # 파서별 줄바꿈/문단 순서 차이에 덜 민감하되 같은 글자 하나로 반복 누락을
        # 숨기지 않도록 문자 다중집합 교집합을 쓴다.
        shared = sum((Counter(digital_text) & Counter(structured_text)).values())
        text_coverage = shared / max(1, len(digital_text))
    else:
        text_coverage = 1.0 if structured_text else 0.0
    image_ratio = image_area / page_area
    asset_count = len(getattr(docir, "assets", None) or {})
    table_rows = sum(block.get("structured", {}).get("node_kind") == "table_row" for block in blocks)
    metrics = {
        "parser": "document_processor",
        "parse_status": parse_status,
        "blocks": len(blocks),
        "table_rows": table_rows,
        "text_coverage": round(text_coverage, 4),
        "image_area_ratio": round(image_ratio, 4),
        "assets": asset_count,
    }
    if parse_status != "parsed" or len(blocks) < 3 or text_coverage < 0.85:
        route = "visual"
    elif image_ratio >= 0.25 or (asset_count > 0 and image_area == 0):
        route = "hybrid"
    else:
        route = "structured_fast"
    return StructuredPage(blocks=blocks, metrics=metrics, route=route)


def hwp_structure_from_docir(docir: Any, *, parser_version: str = "unknown") -> dict[str, Any]:
    """HWP/HWPX DocIR을 기존 ``hwp_structure`` 계약으로 정규화한다.

    HWP 좌표는 만들지 않는다. 여기서는 전량 텍스트와 병합 셀/중첩 표 구조를 정본으로
    보존하고, 화면 좌표는 HTML DOM 또는 별도 렌더 결과가 담당한다.
    """
    declared_pages = list(getattr(docir, "pages", None) or [])
    page_count = max(
        1,
        len(declared_pages),
        max((int(getattr(page, "page_number", 0) or 0) for page in declared_pages), default=0),
    )
    pages = [
        {"page_no": number, "paragraphs": [], "tables": []}
        for number in range(1, page_count + 1)
    ]

    def target(number: int) -> dict[str, Any]:
        number = max(1, number)
        while len(pages) < number:
            pages.append({"page_no": len(pages) + 1, "paragraphs": [], "tables": []})
        return pages[number - 1]

    seen_tables: set[int] = set()

    def add_table(table: Any, *, fallback_page: int, parent_cell: dict[str, Any] | None = None) -> None:
        marker = id(table)
        if marker in seen_tables:
            return
        seen_tables.add(marker)
        page_no = _page_number(table, fallback_page)
        positions = _cell_positions(table)
        cells: list[dict[str, Any]] = []
        nonempty = 0
        merged = 0
        for row, col, cell in positions:
            text = _cell_text(cell)
            row_span = _cell_span(cell, "rowspan")
            col_span = _cell_span(cell, "colspan")
            nonempty += int(bool(text))
            merged += int(row_span > 1 or col_span > 1)
            child_tables = _cell_tables(cell)
            cells.append({
                "row": int(row),
                "col": int(col),
                "row_span": row_span,
                "col_span": col_span,
                "text": text,
                "has_nested_table": bool(child_tables),
                "source_id": getattr(cell, "node_id", None),
            })
            for child in child_tables:
                add_table(
                    child,
                    fallback_page=page_no,
                    parent_cell={
                        "table_id": str(getattr(table, "node_id", None) or marker),
                        "row": int(row),
                        "col": int(col),
                    },
                )
        rows = int(getattr(table, "row_count", 0) or (max((c["row"] for c in cells), default=-1) + 1))
        cols = int(getattr(table, "col_count", 0) or (max((c["col"] for c in cells), default=-1) + 1))
        slots = max(1, rows * cols)
        item = {
            "source_id": str(getattr(table, "node_id", None) or f"table_{marker}"),
            "page_no": page_no,
            "rows": rows,
            "cols": cols,
            "has_header": False,
            "cells": cells,
            "nonempty_cells": nonempty,
            "merged_cells": merged,
            "density": round(nonempty / slots, 4),
            "role_hint": (
                "layout_container"
                if slots > 30 and (nonempty / slots < 0.45 or merged >= 3)
                else "data_table_candidate"
            ),
            "text": "\n".join(cell["text"] for cell in cells if cell["text"]),
        }
        if parent_cell is not None:
            item["parent_cell"] = parent_cell
        target(page_no)["tables"].append(item)

    paragraph_counter = 0
    for paragraph in getattr(docir, "paragraphs", None) or []:
        runs = _direct_runs(paragraph)
        by_page: dict[int, list[Any]] = {}
        for run in runs:
            by_page.setdefault(_page_number(run), []).append(run)
        for page_no, page_runs in by_page.items():
            text = "".join(str(getattr(run, "text", "") or "") for run in page_runs).strip()
            if text:
                paragraph_counter += 1
                target(page_no)["paragraphs"].append({
                    "source_id": str(
                        getattr(paragraph, "node_id", None) or f"p{page_no}_para{paragraph_counter:04d}"
                    ),
                    "text": text,
                    "style": {},
                })
        for table in _direct_tables(paragraph):
            add_table(table, fallback_page=1)

    for page in pages:
        page["text"] = "\n".join([
            *(item["text"] for item in page["paragraphs"]),
            *(item["text"] for item in page["tables"] if item.get("text")),
        ])
    return {
        "parser": "document_processor",
        "parser_version": parser_version,
        "file_type": str(getattr(docir, "source_doc_type", None) or "hwp"),
        "page_count": len(pages),
        "pages": pages,
    }
