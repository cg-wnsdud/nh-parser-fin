"""HWP/HWPX DocIR의 문단·표 셀 구조를 좌표 없이 보존한다."""
from __future__ import annotations

from pathlib import Path
from typing import Any


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


def _cell_background(cell: Any) -> str | None:
    """셀 채우기 색. HWP '제목 셀' 속성은 비어 있는 문서가 많아 머리글 판정 근거로 쓴다."""
    value = getattr(getattr(cell, "cell_style", None), "background", None)
    return str(value) if value else None


def _cell_tables(cell: Any) -> list[Any]:
    tables: list[Any] = []
    for paragraph in getattr(cell, "paragraphs", None) or []:
        tables.extend(_direct_tables(paragraph))
    return tables


def _cell_text(cell: Any) -> str:
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
    return "\n".join(parts).strip()


def load_docir(path: Path) -> Any:
    """사내 패키지가 있을 때만 DocIR을 읽는다. 호출측은 예외 시 기존 경로로 폴백한다."""
    from document_processor import DocIR

    return DocIR.from_file(str(path))


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
                "background": _cell_background(cell),
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
