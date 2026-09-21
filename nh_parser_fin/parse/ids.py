"""Final Region ID contract.

PaddleX regions (``pN_r...``), recovered OCR/PDF regions (``pN_x...``), and
temporary table anchors are useful while assembling a page.  They are internal
provenance, not a stable downstream contract.  At the end of parsing, this
module gives every surviving region one page-scoped ``pN_rNNN`` ID without
changing the region list order, bbox, text, or labels.
"""
from __future__ import annotations

from typing import Any


def _map_reference(
    owner: dict[str, Any], key: str, mapping: dict[str, str], *, source_key: str,
) -> None:
    value = owner.get(key)
    if value is None or str(value) not in mapping:
        return
    owner.setdefault(source_key, str(value))
    owner[key] = mapping[str(value)]


def _map_decision(decision: dict[str, Any] | None, mapping: dict[str, str]) -> None:
    """Keep the VLM's original ID while making its live references resolvable."""
    if not decision:
        return
    for key in ("region_id", "candidate_id", "target_region_id", "near_id"):
        value = decision.get(key)
        if value is None or str(value) not in mapping:
            continue
        decision.setdefault(f"source_{key}", str(value))
        decision[key] = mapping[str(value)]


def normalize_region_ids(pages: list[dict[str, Any]]) -> None:
    """Rename final regions to ``p<page>_r<sequence>`` in their current order.

    The function runs after ownership, table assembly, text selection, and
    labeling.  It therefore cannot affect those decisions.  ``source_region_id``
    and the page-level ``region_id_map`` preserve the internal assembly ID in P1.
    """
    for page in pages:
        page_no = int(page["page_no"])
        regions = page.get("regions") or []
        mapping: dict[str, str] = {}
        sources: dict[str, str] = {}

        for sequence, region in enumerate(regions, start=1):
            old_id = str(region.get("region_id") or "").strip()
            if not old_id:
                raise ValueError(f"page {page_no}: region_id가 비어 있습니다")
            if old_id in mapping:
                raise ValueError(f"page {page_no}: region_id가 중복되었습니다: {old_id}")
            new_id = f"p{page_no}_r{sequence:03d}"
            mapping[old_id] = new_id
            sources[old_id] = str(region.get("source_region_id") or old_id)

        page["region_id_map"] = [
            {
                "region_id": mapping[old_id],
                "source_region_id": sources[old_id],
                "origin": region.get("origin"),
            }
            for old_id, region in zip(mapping, regions)
        ]

        for region in regions:
            old_id = str(region["region_id"])
            region["source_region_id"] = sources[old_id]
            region["region_id"] = mapping[old_id]

            _map_reference(
                region, "related_region_id", mapping,
                source_key="source_related_region_id",
            )
            _map_reference(region, "parent_id", mapping, source_key="source_parent_id")
            child_ids = [str(value) for value in region.get("child_ids") or []]
            if child_ids:
                region.setdefault("source_child_ids", list(child_ids))
                region["child_ids"] = [mapping.get(value, value) for value in child_ids]

            _map_decision(region.get("semantic_decision"), mapping)
            _map_decision(region.get("label_decision"), mapping)

        for area in page.get("table_areas") or []:
            member_ids = [str(value) for value in area.get("member_ids") or []]
            if member_ids:
                area.setdefault("source_member_ids", list(member_ids))
                area["member_ids"] = [mapping.get(value, value) for value in member_ids]

        for item in page.get("coarse_missing_candidates") or []:
            _map_decision(item, mapping)
