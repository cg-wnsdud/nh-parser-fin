"""표 의미 관계는 기존 원문 줄만 가리키고 그 좌표에서 만들어져야 한다."""
from nh_parser_fin.parse import relations
from nh_parser_fin.parse.export import build_p3
from PIL import Image


def test_cross_region_condition_result_preserves_exact_source_text_and_bbox():
    evidence = {
        "region_a/L1": {"text": "가입기간 중 급여이체 시", "bbox": [100, 200, 400, 230], "region_id": "p1_r001"},
        "region_b/L1": {"text": "0.30%p", "bbox": [500, 200, 620, 230], "region_id": "p1_r002"},
        "region_c/L1": {"text": "공통", "bbox": [20, 180, 80, 250], "region_id": "p1_r003"},
    }
    result = {"relations": [{
        "type": "condition_result", "key_refs": ["L01"],
        "value_refs": ["L02"], "context_refs": ["L03"], "confidence": 0.95,
    }]}

    found = relations.build_relations(
        result,
        {"L01": "region_a/L1", "L02": "region_b/L1", "L03": "region_c/L1"},
        evidence, ["우대금리"],
    )

    assert len(found) == 1
    assert found[0]["key"] == "가입기간 중 급여이체 시"
    assert found[0]["value"] == "0.30%p"
    assert found[0]["context"] == "공통"
    assert found[0]["bbox"] == [100, 200, 620, 230]
    assert found[0]["region_ids"] == ["p1_r001", "p1_r002", "p1_r003"]


def test_unknown_or_reused_value_refs_do_not_become_claims():
    evidence = {
        "a": {"text": "일시상환", "bbox": [10, 20, 60, 40], "region_id": "p1_r001"},
        "b": {"text": "1년 이내", "bbox": [70, 20, 150, 40], "region_id": "p1_r002"},
    }
    result = {"relations": [
        {"type": "option_description", "key_refs": ["L01"], "value_refs": ["L02"], "context_refs": [], "confidence": 0.9},
        {"type": "option_description", "key_refs": ["L01"], "value_refs": ["L02"], "context_refs": [], "confidence": 0.9},
        {"type": "option_description", "key_refs": ["UNKNOWN"], "value_refs": ["L02"], "context_refs": [], "confidence": 0.9},
    ]}

    found = relations.build_relations(result, {"L01": "a", "L02": "b"}, evidence, [])

    assert len(found) == 1
    assert found[0]["key"] == "일시상환"
    assert found[0]["value"] == "1년 이내"


def test_p3_exposes_compact_relations_without_line_level_evidence():
    document = {
        "doc_id": "x", "source_file": "x.pdf", "file_type": "pdf",
        "pages": [{
            "page_no": 1, "canvas": [1000, 1000],
            "regions": [
                {"region_id": "p1_r001", "bbox": [10, 20, 60, 40], "text": "일시상환", "semantic_labels": ["대출기간"]},
                {"region_id": "p1_r002", "bbox": [70, 20, 150, 40], "text": "1년 이내", "semantic_labels": ["대출기간"]},
            ],
            "semantic_structures": [{
                "structure_id": "p1_s001", "kind": "table", "field_label": "대출기간",
                "member_region_ids": ["p1_r001", "p1_r002"], "bbox": [10, 20, 150, 40],
                "status": "verified", "detected_by": ["paddle"],
                "relations": [{
                    "type": "option_description", "key": "일시상환", "value": "1년 이내",
                    "context": "", "bbox": [10, 20, 150, 40],
                    "key_bbox": [10, 20, 60, 40], "value_bbox": [70, 20, 150, 40],
                    "line_refs": {"key": ["a"], "value": ["b"], "context": []},
                    "region_ids": ["p1_r001", "p1_r002"], "confidence": 0.9,
                }],
            }],
        }],
    }

    page = build_p3(document)["pages"][0]

    assert page["semantic_structures"][0]["relations"][0]["value"] == "1년 이내"
    assert "line_refs" not in page["semantic_structures"][0]["relations"][0]
    assert page["semantic_structures"][0]["detected_by"] == ["paddle"]


def test_paddle_false_positive_returns_to_plain_text(monkeypatch):
    page = {
        "page_no": 1, "table_areas": [],
        "regions": [{
            "region_id": "p1_r001", "bbox": [10, 10, 200, 70],
            "layout_observation": {"label": "table"},
            "kind": "table", "table": {"grid": {"rows": 2, "cols": 2}},
            "text": "일반 문단 첫 줄\n일반 문단 둘째 줄",
            "semantic_labels": [],
            "lines": [
                {"line_ref": "a", "text": "일반 문단 첫 줄", "bbox": [10, 10, 170, 30]},
                {"line_ref": "b", "text": "일반 문단 둘째 줄", "bbox": [10, 40, 180, 60]},
            ],
        }],
    }
    monkeypatch.setattr(relations.vlm_client, "image_part", lambda *args, **kwargs: {"type": "image"})
    monkeypatch.setattr(relations.vlm_client, "chat_json", lambda *args, **kwargs: {
        "structure_type": "not_table", "field_label": "", "relations": [], "analysis": "문단",
    })

    relations.analyze_page_relations(Image.new("RGB", (300, 100), "white"), page)

    assert page["semantic_structures"][0]["status"] == "rejected"
    assert page["semantic_structures"][0]["detected_by"] == ["paddle"]
    assert page["regions"][0]["kind"] == "text"
    assert "table" not in page["regions"][0]
    assert page["regions"][0]["text"] == "일반 문단 첫 줄\n일반 문단 둘째 줄"


def test_rate_value_validation_rejects_condition_text_as_value():
    evidence = {
        "h": {"text": "우대금리", "bbox": [1, 1, 20, 10], "region_id": "r1"},
        "v1": {"text": "0.30%p", "bbox": [80, 20, 130, 35], "region_id": "r1"},
        "v2": {"text": "0.20%p", "bbox": [80, 40, 130, 55], "region_id": "r1"},
        "c": {"text": "급여이체 시", "bbox": [20, 20, 70, 35], "region_id": "r1"},
    }
    aliases = {"L01": "h", "L02": "v1", "L03": "v2", "L04": "c"}
    rate_aliases = relations._rate_value_aliases(aliases, evidence)
    invalid = [{"line_refs": {"value": ["c"]}}]

    accepted, coverage = relations._filter_rate_relations(
        invalid, {aliases[alias] for alias in rate_aliases},
    )

    assert rate_aliases == ["L02", "L03"]
    assert accepted == []
    assert coverage == 0.0


def test_single_rate_value_still_requires_rate_as_relation_value():
    evidence = {
        "h": {"text": "적용이자율", "bbox": [1, 1, 50, 10], "region_id": "r1"},
        "term": {"text": "12개월", "bbox": [20, 20, 70, 35], "region_id": "r1"},
        "rate": {"text": "2.60%", "bbox": [80, 20, 130, 35], "region_id": "r1"},
    }
    aliases = {"L01": "h", "L02": "term", "L03": "rate"}
    rate_refs = {aliases[alias] for alias in relations._rate_value_aliases(aliases, evidence)}
    wrong = [{"line_refs": {"value": ["term"]}}]

    assert rate_refs == {"rate"}
    assert relations._filter_rate_relations(wrong, rate_refs) == ([], 0.0)


def test_duplicate_same_text_same_position_is_one_relation_candidate():
    page = {"regions": [{
        "region_id": "p1_r001", "lines": [
            {"line_ref": "a", "text": "기간", "bbox": [100, 100, 150, 130]},
            {"line_ref": "b", "text": "기간", "bbox": [105, 105, 145, 125]},
            {"line_ref": "c", "text": "기간", "bbox": [100, 150, 150, 180]},
        ],
    }]}

    evidence, _ = relations._area_evidence(page, {"member_ids": ["p1_r001"]})

    assert list(evidence) == ["a", "c"]


def test_independent_numeric_rows_are_not_collapsed_into_one_relation():
    evidence = {
        "k1": {"text": "골절수술", "bbox": [10, 100, 90, 120], "region_id": "r1"},
        "v1": {"text": "300,000", "bbox": [100, 100, 160, 120], "region_id": "r1"},
        "k2": {"text": "화상수술", "bbox": [10, 140, 90, 160], "region_id": "r1"},
        "v2": {"text": "100,000", "bbox": [100, 140, 160, 160], "region_id": "r1"},
    }
    aliases = {f"L0{index}": ref for index, ref in enumerate(evidence, 1)}
    response = {"relations": [{
        "type": "category_item_value", "key_refs": ["L01", "L03"],
        "value_refs": ["L02", "L04"], "context_refs": [], "confidence": 0.9,
    }]}

    found = relations.build_relations(response, aliases, evidence, [])

    assert [(relation["key"], relation["value"]) for relation in found] == [
        ("골절수술", "300,000"), ("화상수술", "100,000"),
    ]


def test_field_list_does_not_pair_intro_paragraph_with_following_bullet():
    evidence = {
        "intro": {"text": "아래 조건을 모두 충족", "bbox": [100, 20, 350, 40], "region_id": "r1"},
        "bullet": {"text": "- 사업 기간: 1년 이상", "bbox": [100, 60, 350, 80], "region_id": "r1"},
    }
    result = {"structure_type": "field_list", "relations": [{
        "type": "attribute_value", "key_refs": ["L02"], "value_refs": ["L01"],
        "context_refs": [], "confidence": 0.9,
    }]}

    assert relations.build_relations(
        result, {"L01": "intro", "L02": "bullet"}, evidence, [],
    ) == []


def test_field_list_one_value_keeps_only_the_option_in_its_row():
    evidence = {
        "other": {"text": "종합통장대출", "bbox": [100, 20, 220, 40], "region_id": "r1"},
        "key": {"text": "원금균등분할상환", "bbox": [100, 60, 220, 80], "region_id": "r1"},
        "value": {"text": "매월 균등하게 상환", "bbox": [300, 60, 500, 80], "region_id": "r2"},
    }
    result = {"structure_type": "field_list", "relations": [{
        "type": "option_description", "key_refs": ["L01", "L02"],
        "value_refs": ["L03"], "context_refs": [], "confidence": 0.9,
    }]}

    found = relations.build_relations(
        result, {"L01": "other", "L02": "key", "L03": "value"}, evidence, [],
    )
    assert [(relation["key"], relation["value"]) for relation in found] == [
        ("원금균등분할상환", "매월 균등하게 상환"),
    ]


def test_rate_column_header_is_not_part_of_rate_value():
    evidence = {
        "term": {"text": "12개월", "bbox": [10, 20, 80, 40], "region_id": "r1"},
        "header": {"text": "적용이자율", "bbox": [100, 20, 180, 40], "region_id": "r1"},
        "rate": {"text": "2.60%", "bbox": [100, 60, 180, 80], "region_id": "r1"},
    }
    result = {"structure_type": "table", "relations": [{
        "type": "condition_result", "key_refs": ["L01"],
        "value_refs": ["L02", "L03"], "context_refs": [], "confidence": 0.9,
    }]}

    found = relations.build_relations(
        result, {"L01": "term", "L02": "header", "L03": "rate"}, evidence, [],
    )
    assert found[0]["value"] == "2.60%"


def test_hwp_document_rows_do_not_pair_values_across_paddle_box():
    page = {
        "page_no": 1,
        "regions": [
            {
                "region_id": f"p1_r{number:03d}", "semantic_labels": [key],
                "table": {"source": "document_processor", "cells": [
                    {"row": 0, "col": 0, "text": key, "bbox": [10, y, 110, y + 30]},
                    {"row": 0, "col": 1, "text": value, "bbox": [120, y, 500, y + 30]},
                ]},
            }
            for number, (key, value, y) in enumerate([
                ("대출대상", "공무원", 100),
                ("대출금리", "최저 연 4.45%", 140),
            ], start=1)
        ] + [{
            "region_id": "p1_r003", "bbox": [10, 90, 500, 180],
            "layout_observation": {"label": "table"}, "text": "Paddle의 큰 후보",
        }],
    }

    relations.document_row_relations(page)

    found = page["semantic_structures"]
    assert [(item["relations"][0]["key"], item["relations"][0]["value"]) for item in found] == [
        ("대출대상", "공무원"), ("대출금리", "최저 연 4.45%"),
    ]
    assert found[0]["member_region_ids"] == ["p1_r001"]
    assert found[1]["member_region_ids"] == ["p1_r002"]


def test_other_row_cannot_be_relation_context():
    evidence = {
        "k": {"text": "일시상환", "bbox": [100, 20, 200, 40], "region_id": "r1"},
        "v": {"text": "1년", "bbox": [300, 20, 400, 40], "region_id": "r2"},
        "other": {"text": "종합통장대출", "bbox": [20, 60, 90, 80], "region_id": "r3"},
    }
    result = {"relations": [{
        "type": "option_description", "key_refs": ["L01"], "value_refs": ["L02"],
        "context_refs": ["L03"], "confidence": 0.9,
    }]}

    found = relations.build_relations(
        result, {"L01": "k", "L02": "v", "L03": "other"}, evidence, [],
    )

    assert found[0]["context"] == ""
    assert found[0]["region_ids"] == ["r1", "r2"]


def test_partial_relations_stay_in_p1_but_do_not_enter_p3():
    document = {
        "doc_id": "x", "source_file": "x.pdf", "file_type": "pdf",
        "pages": [{"page_no": 1, "canvas": [100, 100], "regions": [],
                   "semantic_structures": [{
                       "structure_id": "p1_s001", "kind": "table", "status": "partial",
                       "relations": [{
                           "type": "attribute_value", "key": "a", "value": "b", "context": "",
                           "bbox": [1, 1, 2, 2], "key_bbox": [1, 1, 1, 2],
                           "value_bbox": [2, 1, 2, 2], "region_ids": [],
                       }],
                   }]}],
    }

    assert document["pages"][0]["semantic_structures"][0]["relations"]
    assert build_p3(document)["pages"][0]["semantic_structures"][0]["relations"] == []
