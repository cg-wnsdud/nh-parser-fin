"""좌표 없는 HWP 구조 텍스트를 렌더 페이지의 Region에 정렬한다."""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from .quality import flag


def _normalized(value: Any) -> str:
    return re.sub(r"[^0-9a-zA-Z가-힣]", "", str(value or "")).casefold()


def _match_score(left: str, right: str) -> float:
    a, b = _normalized(left), _normalized(right)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def _balance(left: str, right: str) -> float:
    a, b = _normalized(left), _normalized(right)
    return min(len(a), len(b)) / max(1, max(len(a), len(b)))


def _text_nodes(structure: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = [
        {"source_id": item["source_id"], "kind": "paragraph", "text": item["text"]}
        for item in structure.get("paragraphs") or [] if _normalized(item.get("text"))
    ]
    for table in structure.get("tables") or []:
        # 중첩 표의 셀은 바깥 셀 text에 이미 합쳐져 있다. 양쪽을 모두 후보로 쓰면
        # 같은 문구가 두 번 들어가므로 실제 문서 흐름을 가진 최상위 셀만 쓴다.
        if table.get("parent_cell"):
            continue
        for cell in table.get("cells") or []:
            text = str(cell.get("text") or "")
            if not _normalized(text) or re.fullmatch(r"!\[image\]\([^)]*\)", text.strip()):
                continue
            nodes.append({
                "source_id": f"{table['source_id']}/r{int(cell['row']) + 1}c{int(cell['col']) + 1}",
                "kind": "table_cell",
                "table_id": table["source_id"],
                "row": int(cell["row"]),
                "col": int(cell["col"]),
                "text": text,
            })
    return nodes


def _source_table_grid(table: dict[str, Any]) -> dict[str, Any]:
    return {
        "grid": {"rows": int(table["rows"]), "cols": int(table["cols"])},
        "cells": [
            {
                "row": int(cell["row"]),
                "col": int(cell["col"]),
                "row_span": int(cell.get("row_span") or 1),
                "col_span": int(cell.get("col_span") or 1),
                "text": str(cell.get("text") or ""),
                "is_header": bool(table.get("has_header") and int(cell["row"]) == 0),
            }
            for cell in table.get("cells") or []
        ],
        "notes": [],
        "unplaced_line_refs": [],
        "confidence": 1.0,
        "text_grid": str(table.get("text") or ""),
        "source": "kordoc_hwp_structure",
        "source_id": table["source_id"],
    }


def align_hwp_structure(page: dict[str, Any]) -> dict[str, int]:
    """구조 노드를 Region 텍스트에 대조한다. bbox는 기존 PDF/PaddleX 값을 유지한다."""
    structure = page.get("hwp_structure")
    if not isinstance(structure, dict):
        return {"nodes": 0, "matched": 0, "unmatched": 0, "tables_attached": 0}
    regions = page.get("regions") or []
    for region in regions:
        region.pop("hwp_structure_evidence", None)
        region.pop("hwp_structure_validation", None)
        region.pop("hwp_structure_corroboration", None)
    nodes = _text_nodes(structure)
    unmatched = []
    matched = 0
    last_region = 0
    mapped: dict[int, dict[str, Any]] = {}

    def attach(
        node: dict[str, Any], region: dict[str, Any], *, method: str,
        score: float, balance: float, ambiguous_region_span: bool = False,
    ) -> None:
        region.setdefault("hwp_structure_evidence", []).append({
            **node,
            "match_method": method,
            "match_score": round(max(0.0, score), 4),
            "length_balance": round(balance, 4),
            "ambiguous_region_span": ambiguous_region_span,
        })

    pending: list[tuple[int, dict[str, Any]]] = []
    for node_index, node in enumerate(nodes):
        candidates = []
        for index, region in enumerate(regions):
            score = _match_score(node["text"], region.get("text"))
            if score < 0.72:
                continue
            order_penalty = 0.03 if index < last_region else 0.0
            candidates.append((score - order_penalty, _balance(node["text"], region.get("text")), index, region))
        if not candidates:
            pending.append((node_index, node))
            continue
        score, balance, index, region = max(candidates, key=lambda item: (item[0], item[1], -item[2]))
        last_region = max(last_region, index)
        containment_matches = sum(
            _match_score(node["text"], candidate[3].get("text")) == 1.0
            for candidate in candidates
        )
        attach(
            node, region, method="normalized_text", score=score, balance=balance,
            ambiguous_region_span=containment_matches > 1,
        )
        mapped[node_index] = region
        matched += 1

    # 한 문단의 PDF 글자 순서가 흔들려 직접 유사도가 낮아도, 앞뒤 구조 문단이 같은
    # 큰 Region에 붙었다면 그 사이 문단 역시 같은 Region이다(009 예상이자 문단 실측).
    for node_index, node in pending:
        before = next((mapped[i] for i in range(node_index - 1, -1, -1) if i in mapped), None)
        after = next((mapped[i] for i in range(node_index + 1, len(nodes)) if i in mapped), None)
        if before is not None and before is after and node.get("kind") == "paragraph":
            attach(node, before, method="sequence_bridge", score=0.0, balance=0.0)
            mapped[node_index] = before
            matched += 1
        else:
            unmatched.append({**node, "reason": "no_region_text_match"})

    validated_regions = 0
    # Region 전체가 어느 구조 노드 안에 그대로 들어 있으면, 그 노드가 여러 Region을
    # 가로질러 정본 교체에는 부적합해도 PDF 문자의 정확성은 확인할 수 있다.
    for region in regions:
        selected_norm = _normalized(region.get("text"))
        corroborating = [
            str(node["source_id"]) for node in nodes
            if len(selected_norm) >= 6 and selected_norm in _normalized(node.get("text"))
        ]
        if corroborating:
            region["hwp_structure_corroboration"] = {
                "status": "agrees", "method": "region_containment",
                "source_ids": corroborating,
            }
            continue
        lines = [
            _normalized(line) for line in str(region.get("text") or "").splitlines()
            if len(_normalized(line)) >= 2
        ]
        covered = []
        for line in lines:
            sources = [
                str(node["source_id"]) for node in nodes
                if line in _normalized(node.get("text"))
            ]
            covered.append((line, sources))
        total_chars = sum(len(line) for line in lines)
        covered_chars = sum(len(line) for line, sources in covered if sources)
        coverage = covered_chars / max(1, total_chars)
        if lines and coverage >= 0.9:
            source_ids = []
            for _, sources in covered:
                for source_id in sources:
                    if source_id not in source_ids:
                        source_ids.append(source_id)
            region["hwp_structure_corroboration"] = {
                "status": "agrees", "method": "line_coverage",
                "coverage": round(coverage, 4), "source_ids": source_ids,
            }

    for region in regions:
        if (region.get("hwp_structure_corroboration") or {}).get("status") != "agrees":
            continue
        reasons = [
            reason for reason in region.get("review_reasons") or []
            if reason != "digital_text_vlm_disagreement"
        ]
        if reasons:
            region["review_reasons"] = reasons
        else:
            region.pop("review_reasons", None)
            region.pop("needs_review", None)

    for region in regions:
        evidence = region.get("hwp_structure_evidence") or []
        if not evidence:
            continue
        unique_texts = []
        seen = set()
        for item in evidence:
            value = str(item.get("text") or "").strip()
            key = _normalized(value)
            if value and key not in seen:
                unique_texts.append(value)
                seen.add(key)
        source_text = "\n".join(unique_texts)
        score = _match_score(source_text, region.get("text"))
        balance = _balance(source_text, region.get("text"))
        region.setdefault("text_candidates", {})["hwp_structure"] = source_text
        region["hwp_structure_validation"] = {
            "status": "agrees" if score >= 0.8 and balance >= 0.7 else "partial",
            "score": round(score, 4),
            "length_balance": round(balance, 4),
            "source_ids": [str(item["source_id"]) for item in evidence],
        }
        if score >= 0.8 and balance >= 0.7:
            validated_regions += 1
            previous = str(region.get("text") or "")
            source_norm = _normalized(source_text)
            previous_norm = _normalized(previous)
            foreign_regions = [
                str(other.get("region_id") or "") for other in regions
                if other is not region
                and len(_normalized(other.get("text"))) >= 6
                and _normalized(other.get("text")) in source_norm
                and _normalized(other.get("text")) not in previous_norm
            ]
            region["hwp_structure_validation"]["foreign_region_ids"] = foreign_regions
            if not foreign_regions and previous_norm != source_norm:
                region["text_candidates"]["pre_hwp_selected"] = previous
                region["text"] = source_text
                region["text_source"] = "hwp_structure"
                region["text_selection_status"] = "hwp_structure_canonical"

    tables_attached = 0
    for table in structure.get("tables") or []:
        if table.get("role_hint") != "data_table_candidate":
            continue
        rows, cols = int(table.get("rows") or 0), int(table.get("cols") or 0)
        if rows * cols < 2 or rows * cols > 30:
            continue
        candidates = []
        cell_texts = [
            _normalized(cell.get("text")) for cell in table.get("cells") or []
            if _normalized(cell.get("text"))
        ]
        for region in regions:
            region_norm = _normalized(region.get("text"))
            covered_chars = sum(len(value) for value in cell_texts if value in region_norm)
            cell_coverage = covered_chars / max(1, sum(len(value) for value in cell_texts))
            score = max(_match_score(table.get("text"), region.get("text")), cell_coverage)
            balance = _balance(table.get("text"), region.get("text"))
            if score >= 0.8 and balance >= 0.55:
                candidates.append((score, balance, cell_coverage, region))
        if not candidates:
            continue
        score, balance, cell_coverage, region = max(candidates, key=lambda item: (item[0], item[1]))
        region["table"] = _source_table_grid(table)
        region["table_status"] = "complete"
        region["table_structure_source"] = "kordoc"
        region["table_geometry_status"] = "region_bbox_only"
        region["kind"] = "table"
        region["hwp_table_match"] = {
            "source_id": table["source_id"],
            "score": round(score, 4),
            "length_balance": round(balance, 4),
            "cell_coverage": round(cell_coverage, 4),
        }
        if cell_coverage == 1.0 and balance >= 0.8:
            previous = str(region.get("text") or "")
            source_text = str(table.get("text") or "")
            if _normalized(previous) != _normalized(source_text):
                region.setdefault("text_candidates", {})["pre_hwp_table_selected"] = previous
                region["text"] = source_text
                region["text_source"] = "hwp_structure"
                region["text_selection_status"] = "hwp_table_canonical"
        tables_attached += 1

    # HWP 구조가 연속 문단으로 확인한 큰 본문을 VLM이 억지 격자로 만들고 대부분을
    # notes로 보낸 경우는 업무 데이터 표가 아니라 조판용 제목/내용 틀이다. 추정 격자는
    # P1에 남기되 P3의 표로 내보내지 않고, 그 때문에 생긴 검수 경고도 제거한다.
    for region in regions:
        evidence = region.get("hwp_structure_evidence") or []
        paragraph_count = sum(item.get("kind") == "paragraph" for item in evidence)
        table = region.get("table") or {}
        cells = [cell for cell in table.get("cells") or [] if str(cell.get("text") or "").strip()]
        notes = table.get("notes") or []
        if (
            region.get("table_status") == "partial"
            and not region.get("hwp_table_match")
            and paragraph_count >= 3
            and len(notes) > len(cells)
        ):
            region["hwp_discarded_table_hypothesis"] = table
            region.pop("table", None)
            region["table_status"] = "layout_text"
            region["kind"] = "text"
            reasons = [
                reason for reason in region.get("review_reasons") or []
                if reason not in {
                    "table_unplaced_lines", "table_low_confidence",
                    "table_sparse_grid", "table_structure_incomplete",
                }
            ]
            if reasons:
                region["review_reasons"] = reasons
            else:
                region.pop("review_reasons", None)
                region.pop("needs_review", None)

    page["hwp_alignment"] = {
        "nodes": len(nodes),
        "matched": matched,
        "unmatched": unmatched,
        "validated_regions": validated_regions,
        "tables_attached": tables_attached,
    }
    if nodes and not matched:
        for region in regions:
            flag(region, "hwp_structure_page_unmatched")
    return {
        "nodes": len(nodes), "matched": matched, "unmatched": len(unmatched),
        "tables_attached": tables_attached,
    }
