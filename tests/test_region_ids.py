from __future__ import annotations

import pytest

from nh_parser_fin.parse.export import build_p1, build_p3
from nh_parser_fin.parse.ids import normalize_region_ids
from nh_parser_fin.parse.templates import review_units


def test_final_ids_are_uniform_without_changing_region_order_or_references():
    pages = [{
        "page_no": 1,
        "canvas": [1000, 1500],
        "regions": [
            {
                "region_id": "p1_r004", "origin": "paddlex", "text": "상품명",
                "bbox": [0, 0, 100, 40], "product_id": "product_1",
                "semantic_labels": ["상품명"], "needs_review": False,
                "child_ids": ["p1_x003"],
            },
            {
                "region_id": "p1_x003", "origin": "recovery", "text": "유의사항",
                "bbox": [0, 50, 100, 90], "product_id": "product_1",
                "semantic_labels": ["유의사항"], "needs_review": False,
                "related_region_id": "p1_r004", "parent_id": "p1_r004",
                "label_decision": {"region_id": "p1_x003", "labels": ["유의사항"]},
            },
        ],
        "table_areas": [{"member_ids": ["p1_r004", "p1_x003"]}],
    }]
    original_boxes = [region["bbox"] for region in pages[0]["regions"]]

    normalize_region_ids(pages)

    regions = pages[0]["regions"]
    assert [region["region_id"] for region in regions] == ["p1_r001", "p1_r002"]
    assert [region["bbox"] for region in regions] == original_boxes
    assert regions[0]["source_region_id"] == "p1_r004"
    assert regions[1]["source_region_id"] == "p1_x003"
    assert regions[0]["child_ids"] == ["p1_r002"]
    assert regions[1]["parent_id"] == "p1_r001"
    assert regions[1]["related_region_id"] == "p1_r001"
    assert regions[1]["label_decision"]["region_id"] == "p1_r002"
    assert pages[0]["table_areas"][0]["member_ids"] == ["p1_r001", "p1_r002"]


def test_review_units_and_p3_use_the_same_normalized_ids():
    pages = [{
        "page_no": 2,
        "canvas": [800, 1200],
        "regions": [{
            "region_id": "p2_x009", "origin": "recovery", "text": "금리 3%",
            "bbox": [10, 20, 300, 70], "product_id": "product_1",
            "semantic_labels": ["금리"], "needs_review": False,
        }],
    }]
    normalize_region_ids(pages)
    templates = {"product_1": {"template_id": "sample", "labels": ["금리"]}}
    document = {
        "doc_id": "d", "source_file": "d.pdf", "file_type": "pdf",
        "classification": {}, "template": {}, "pages": pages,
        "product_templates": templates,
        "review_units": review_units(pages, templates),
    }

    p1 = build_p1(document)
    p3 = build_p3(p1)

    assert p1["pages"][0]["regions"][0]["region_id"] == "p2_r001"
    assert p3["pages"][0]["regions"][0]["region_id"] == "p2_r001"
    assert p3["review_units"][0]["region_ids"] == ["p2_r001"]


def test_p3_rejects_an_internal_id_that_leaked_to_the_contract():
    evidence = {
        "doc_id": "d", "source_file": "d.pdf", "file_type": "pdf",
        "classification": {}, "template": {}, "review_units": [],
        "pages": [{"page_no": 1, "canvas": [1, 1], "regions": [{
            "region_id": "p1_x001", "text": "x", "bbox": [0, 0, 1, 1],
        }]}],
    }
    with pytest.raises(ValueError, match="region_id 형식"):
        build_p3(evidence)
