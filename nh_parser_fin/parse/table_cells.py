"""표 Region의 셀 정본과, 그것으로 만든 읽기용 행 문장.

표 구조의 정본은 칸 목록 하나다. 칸마다 표 전체 기준의 행·열(0부터), 병합 크기,
머리 여부, 문구를 가진다. HWP는 원본 문서 구조(DocIR)의 칸을, PDF·이미지는 VLM이
준 HTML 격자의 칸을 같은 모양으로 쓴다. 출처가 다르므로 ``source``(``hwp``/``vlm``)를
함께 남긴다.

HWP 표는 화면의 행 하나가 Region 하나라서 한 표가 여러 Region에 나뉜다. 칸의 행·열은
항상 표 전체 기준이므로 같은 ``table_id``의 칸을 모으면 PDF 표와 같은 방식으로 표를
되살릴 수 있다. 페이지 ``tables`` 색인이 표마다 Region 목록을 준다.

``row_texts``는 칸 목록에서 결정론으로 만든 보기다. 행마다 ``머리글: 값``을 이어 쓴다.
병합된 값은 그 값이 걸친 행·열마다 적용되므로 해당 행에 반복해 넣는다.
"""
from __future__ import annotations

import copy
from typing import Any

from . import table_html

_NO_FILL = {"", "none", "transparent", "#ffffff", "#fff", "white"}


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _covers(cell: dict[str, Any], row: int) -> bool:
    return int(cell["row"]) <= row < int(cell["row"]) + int(cell.get("row_span") or 1)


def _size(cells: list[dict[str, Any]]) -> tuple[int, int]:
    rows = max((int(c["row"]) + int(c.get("row_span") or 1) for c in cells), default=0)
    cols = max((int(c["col"]) + int(c.get("col_span") or 1) for c in cells), default=0)
    return rows, cols


def _section_title(covering: list[dict[str, Any]], cols: int) -> bool:
    """칸 하나가 표 전체 폭을 덮는 행. 열 머리글이 아니라 구역 제목(`• 시설자금`)이다."""
    return cols > 1 and len(covering) == 1 and int(covering[0].get("col_span") or 1) >= cols


def header_row_count(cells: list[dict[str, Any]]) -> int:
    """맨 위부터 글자가 있는 칸이 모두 머리글인 행 수. 모든 행이 머리글이면 0으로 본다.

    구역 제목 행은 머리글 행으로 세지 않는다(`table_html.header_rows`와 같은 규칙).
    """
    rows, cols = _size(cells)
    count = 0
    for row in range(rows):
        covering = [c for c in cells if _covers(c, row) and _clean(c.get("text"))]
        if covering and not _section_title(covering, cols) and all(c.get("is_header") for c in covering):
            count += 1
        else:
            break
    return 0 if count >= rows else count


def _header_paths(cells: list[dict[str, Any]], heads: int, cols: int) -> list[list[str]]:
    paths: list[list[str]] = [[] for _ in range(cols)]
    for row in range(heads):
        for cell in sorted(cells, key=lambda item: int(item["col"])):
            text = _clean(cell.get("text"))
            if not text or not _covers(cell, row):
                continue
            start = int(cell["col"])
            for col in range(start, min(cols, start + int(cell.get("col_span") or 1))):
                if not paths[col] or paths[col][-1] != text:
                    paths[col].append(text)
    return paths


def _key(paths: list[list[str]], col: int, span: int) -> str:
    """칸이 걸친 열들의 머리글 경로. 여러 열에 걸치면 공통 상위 머리글을 쓴다."""
    chosen = [paths[c] for c in range(col, min(len(paths), col + span)) if paths[c]]
    if not chosen:
        return ""
    prefix = []
    for parts in zip(*chosen):
        if len(set(parts)) != 1:
            break
        prefix.append(parts[0])
    if prefix:
        return " / ".join(prefix)
    return "·".join(dict.fromkeys(path[-1] for path in chosen))


def row_texts(cells: list[dict[str, Any]], rows: set[int] | None = None) -> list[str]:
    """행마다 `머리글: 값 | 머리글: 값` 문장을 만든다. 머리글 행 자체는 내보내지 않는다.

    열 머리글이 없는 표(왼쪽 칸이 항목명인 표)는 `항목: 값 | 값`으로 쓴다.
    표 전체 폭의 구역 제목 행은 따로 내보내지 않고 뒤따르는 행 앞에 `[구역] `으로 붙인다.
    """
    if not cells:
        return []
    total_rows, cols = _size(cells)
    heads = header_row_count(cells)
    paths = _header_paths(cells, heads, cols)
    column_headers = any(paths)
    output = []
    section = ""
    for row in range(heads, total_rows):
        covering = sorted(
            (c for c in cells if _covers(c, row) and _clean(c.get("text"))),
            key=lambda item: int(item["col"]),
        )
        if covering and _section_title(covering, cols):
            section = _clean(covering[0].get("text"))
            continue
        if rows is not None and row not in rows:
            continue
        if not covering:
            continue
        prefix = f"[{section}] " if section else ""
        if column_headers:
            parts = []
            for cell in covering:
                key = _key(paths, int(cell["col"]), int(cell.get("col_span") or 1))
                text = _clean(cell.get("text"))
                parts.append(f"{key}: {text}" if key and key != text else text)
            output.append(prefix + " | ".join(parts))
            continue
        first, rest = covering[0], covering[1:]
        if first.get("is_header") and rest:
            output.append(prefix + f"{_clean(first.get('text'))}: " + " | ".join(_clean(c.get("text")) for c in rest))
        else:
            output.append(prefix + " | ".join(_clean(c.get("text")) for c in covering))
    return output


# ── HWP ──────────────────────────────────────────────────────────────


def _shaded(value: Any) -> bool:
    return str(value or "").strip().casefold() not in _NO_FILL


def _hwp_canonical(table: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """DocIR 표의 칸을 0부터 시작하는 행·열로 맞추고 머리글을 칸 배경색으로 판정한다.

    HWP의 '제목 셀' 속성은 대부분 비어 있다(샘플 78칸 모두 0). 대신 머리글 칸은
    배경을 칠하고 값 칸은 비워 두는 경우가 많다. 글자 있는 칸 가운데 칠한 칸과 안 칠한
    칸이 함께 있을 때만 칠한 칸을 머리글로 본다. 아니면 머리글을 표시하지 않는다.
    """
    raw = [cell for cell in table.get("cells") or [] if "row" in cell and "col" in cell]
    if not raw:
        return [], "none"
    row0 = min(int(cell["row"]) for cell in raw)
    col0 = min(int(cell["col"]) for cell in raw)
    texted = [cell for cell in raw if _clean(cell.get("text"))]
    filled = [cell for cell in texted if _shaded(cell.get("background"))]
    styled = bool(filled) and len(filled) < len(texted)
    cells = [
        {
            "row": int(cell["row"]) - row0,
            "col": int(cell["col"]) - col0,
            "row_span": int(cell.get("row_span") or 1),
            "col_span": int(cell.get("col_span") or 1),
            "is_header": bool(styled and _shaded(cell.get("background"))),
            "text": str(cell.get("text") or ""),
            "source_id": cell.get("source_id"),
        }
        for cell in raw
    ]
    return cells, "hwp_style" if styled else "none"


def _hwp_index(structure: dict[str, Any] | None) -> dict[str, Any]:
    tables: dict[str, dict[str, Any]] = {}
    owner: dict[str, str] = {}
    for table in (structure or {}).get("tables") or []:
        table_id = str(table.get("source_id") or "")
        if not table_id:
            continue
        cells, header_source = _hwp_canonical(table)
        tables[table_id] = {"cells": cells, "header_source": header_source}
        for cell in cells:
            if cell.get("source_id"):
                owner[str(cell["source_id"])] = table_id
    return {"tables": tables, "owner": owner}


def _public(cell: dict[str, Any], bbox: Any = None) -> dict[str, Any]:
    item = {key: cell[key] for key in ("row", "col", "row_span", "col_span", "is_header", "text")}
    if bbox is not None:
        item["bbox"] = list(bbox)
    return item


def _hwp_dom_view(region: dict[str, Any], index: dict[str, Any]) -> dict[str, Any] | None:
    surface = str((region.get("structured") or {}).get("surface_node_id") or "")
    parts = surface.split(":")
    own = parts[1] if len(parts) > 1 and parts[0] == "domrow" else ""
    dom_cells = (region.get("table") or {}).get("cells") or []
    known = index["tables"].get(own)
    if known and dom_cells and all(index["owner"].get(str(c.get("node_id") or "")) for c in dom_cells):
        by_id = {str(cell["source_id"]): cell for cell in known["cells"] if cell.get("source_id")}
        cells, context = [], []
        for dom in dom_cells:
            node_id = str(dom.get("node_id") or "")
            if index["owner"][node_id] == own and node_id in by_id:
                cells.append(_public(by_id[node_id], dom.get("bbox")))
            else:
                # 중첩 표 행에 붙은 부모 표의 표제어 칸. 이 표의 칸이 아니다.
                context.append({"text": str(dom.get("text") or ""), "bbox": dom.get("bbox")})
        if cells:
            row = min(cell["row"] for cell in cells)
            view = {
                "table_id": own, "source": "hwp", "row": row, "cells": cells,
                "row_texts": row_texts(known["cells"], rows={row}),
                "_table": {"cells": known["cells"], "header_source": known["header_source"]},
            }
            if context:
                view["context"] = context
            return view
    # 원본 칸과 연결하지 못하면 화면 행의 칸을 그대로 쓰고 머리글은 위치 추정임을 남긴다.
    cells = [
        _public({
            "row": 0, "col": int(cell.get("col") or 0),
            "row_span": int(cell.get("row_span") or 1), "col_span": int(cell.get("col_span") or 1),
            "is_header": bool(cell.get("is_header")), "text": str(cell.get("text") or ""),
        }, cell.get("bbox"))
        for cell in dom_cells
    ]
    if not cells:
        return None
    return {
        "table_id": own or str(region["region_id"]), "source": "hwp", "row": 0, "cells": cells,
        "row_texts": row_texts(cells),
        "_table": {"cells": cells, "header_source": "position"},
    }


def _hwp_whole_view(region: dict[str, Any], index: dict[str, Any]) -> dict[str, Any] | None:
    """원본 표 전체가 시각 Region 하나에 붙은 경우(HWP visual 경로). 칸 상자는 없다."""
    table = region.get("table") or {}
    table_id = str(table.get("source_id") or "")
    known = index["tables"].get(table_id)
    if known:
        cells, header_source = known["cells"], known["header_source"]
    else:
        cells, header_source = _hwp_canonical(table)
    if not cells:
        return None
    return {
        "table_id": table_id or str(region["region_id"]), "source": "hwp",
        "cells": [_public(cell) for cell in cells], "row_texts": row_texts(cells),
        "_table": {"cells": cells, "header_source": header_source},
    }


# ── PDF·이미지 ───────────────────────────────────────────────────────


def _visual_cells(region: dict[str, Any]) -> list[dict[str, Any]]:
    visual = region.get("visual_table") or {}
    if visual.get("cells") is not None:
        # 디지털 글자 교정은 anchor 단계에서 칸에도 이미 적용됐다.
        return copy.deepcopy(visual["cells"])
    # 셀 목록이 생기기 전 실행의 P1도 다시 내보낼 수 있게 채택된 HTML에서 되살린다.
    attempt = region.get("table_html_attempt") or {}
    if not visual or attempt.get("status") != "selected":
        return []
    cells = table_html.to_cells(table_html.to_grid(str(attempt.get("html") or "")))
    # 최종 문구에 적용된 디지털 글자 교정을 칸 문구에도 같게 적용한다.
    corrections = (region.get("digital_anchor") or {}).get("corrections") or []
    for cell in cells:
        for item in corrections:
            if item.get("from") and item.get("to"):
                cell["text"] = cell["text"].replace(str(item["from"]), str(item["to"]))
    return cells


def _visual_view(region: dict[str, Any], table_id: str) -> dict[str, Any]:
    cells = _visual_cells(region)
    return {
        "table_id": table_id, "source": "vlm",
        "cells": [_public(cell) for cell in cells], "row_texts": row_texts(cells),
        "_table": {"cells": cells, "header_source": "vlm_th" if cells else "none"},
    }


# ── 페이지 ───────────────────────────────────────────────────────────


def build_page_tables(page: dict[str, Any]) -> list[dict[str, Any]]:
    """표 Region마다 ``table_view``를 달고 페이지 표 색인을 돌려준다.

    격자를 얻지 못한 시각 표(`항목 | 값` 줄 나열 등)도 표 ID는 받고 ``cells``는 빈다.
    이때 소비자는 ``selected_text``만 읽는다.
    """
    index = _hwp_index(page.get("hwp_structure"))
    tables: dict[str, dict[str, Any]] = {}
    ordinal = 0
    for region in page.get("regions") or []:
        region.pop("table_view", None)
        table = region.get("table") or {}
        if not (region.get("kind") == "table" or table):
            continue
        if table.get("source") == "document_processor":
            view = _hwp_dom_view(region, index)
        elif table.get("source") == "kordoc_hwp_structure":
            view = _hwp_whole_view(region, index)
        else:
            ordinal += 1
            view = _visual_view(region, f"p{int(page['page_no'])}_t{ordinal:03d}")
        if view is None:
            continue
        whole = view.pop("_table")
        region["table_view"] = view
        entry = tables.get(view["table_id"])
        if entry is None:
            rows, cols = _size(whole["cells"])
            entry = tables[view["table_id"]] = {
                "table_id": view["table_id"], "source": view["source"],
                "rows": rows, "cols": cols,
                "header_rows": header_row_count(whole["cells"]),
                "header_source": whole["header_source"],
                "region_ids": [],
            }
        entry["region_ids"].append(str(region["region_id"]))
    return list(tables.values())
