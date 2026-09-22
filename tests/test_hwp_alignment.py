from nh_parser_fin.parse.hwp_alignment import align_hwp_structure
from nh_parser_fin.parse import reading
from nh_parser_fin.parse.export import build_p1, build_p3


def test_hwp_paragraph_validates_pdf_region_without_changing_bbox():
    region = {
        "region_id": "p1_r001", "bbox": [10, 20, 300, 80],
        "text": "□ 가입대상 : 개인(1인 1계좌)",
        "text_source": "digital_ocr_lines", "text_candidates": {},
    }
    page = {
        "regions": [region],
        "hwp_structure": {"paragraphs": [{
            "source_id": "p1_para0001", "text": "□ 가입대상 : 개인(1인 1계좌)",
        }], "tables": []},
    }

    stats = align_hwp_structure(page)

    assert stats["matched"] == 1
    assert region["bbox"] == [10, 20, 300, 80]
    assert region["hwp_structure_validation"]["status"] == "agrees"


def test_hwp_verified_digital_text_is_not_flagged_for_one_vlm_typo():
    region = {
        "bbox": [0, 0, 100, 30], "text": "기본금리 연 2.25%",
        "text_source": "digital_ocr_lines",
        "hwp_structure_validation": {"status": "agrees", "score": 1.0},
    }

    status = reading.apply_reading(
        region, {"text": "기본금리 연 2.26%", "confidence": 0.9},
    )

    assert status == "parser_verified"
    assert region["text"] == "기본금리 연 2.25%"
    assert region.get("needs_review") is not True


def test_small_structural_table_replaces_vlm_guess_but_keeps_region_bbox():
    region = {
        "region_id": "p1_r002", "bbox": [20, 100, 500, 300],
        "text": "구분 금리 기본 3.0%", "text_candidates": {},
        "table": {"source": "vlm", "grid": {"rows": 1, "cols": 1}},
    }
    page = {"regions": [region], "hwp_structure": {
        "paragraphs": [], "tables": [{
            "source_id": "t2", "rows": 2, "cols": 2, "has_header": True,
            "role_hint": "data_table_candidate", "text": "구분\n금리\n기본\n3.0%",
            "cells": [
                {"row": 0, "col": 0, "text": "구분"},
                {"row": 0, "col": 1, "text": "금리"},
                {"row": 1, "col": 0, "text": "기본"},
                {"row": 1, "col": 1, "text": "3.0%"},
            ],
        }]},
    }

    stats = align_hwp_structure(page)

    assert stats["tables_attached"] == 1
    assert region["bbox"] == [20, 100, 500, 300]
    assert region["table"]["source"] == "kordoc_hwp_structure"
    assert region["table"]["grid"] == {"rows": 2, "cols": 2}


def test_hwp_structure_replaces_duplicated_pdf_text_when_coverage_is_high():
    region = {
        "region_id": "p1_r001", "bbox": [0, 0, 500, 500],
        "text": "제목\n제목\n상품 안내\n가입금액 100만원",
        "text_source": "digital_ocr_lines", "text_candidates": {},
    }
    page = {"regions": [region], "hwp_structure": {
        "paragraphs": [
            {"source_id": "a", "text": "제목"},
            {"source_id": "b", "text": "상품 안내"},
            {"source_id": "c", "text": "가입금액 100만원"},
        ],
        "tables": [],
    }}

    align_hwp_structure(page)

    assert region["text"] == "제목\n상품 안내\n가입금액 100만원"
    assert region["text_source"] == "hwp_structure"
    assert region["text_candidates"]["pre_hwp_selected"].startswith("제목\n제목")


def test_one_structure_cell_spanning_two_regions_only_corroborates_text():
    heading = {"region_id": "p1_r001", "bbox": [0, 0, 100, 20],
               "text": "최대 2.70%p", "text_source": "digital_ocr_lines",
               "text_candidates": {}}
    body = {"region_id": "p1_r002", "bbox": [0, 20, 500, 200],
            "text": "최대 2.70%p\n우대 조건 상세", "text_source": "digital_ocr_lines",
            "text_candidates": {}}
    page = {"regions": [heading, body], "hwp_structure": {
        "paragraphs": [], "tables": [{
            "source_id": "t1", "rows": 1, "cols": 1,
            "cells": [{"row": 0, "col": 0, "text": "최대 2.70%p\n우대 조건 상세"}],
        }]},
    }

    align_hwp_structure(page)

    assert heading["text"] == "최대 2.70%p"
    assert heading["hwp_structure_corroboration"]["status"] == "agrees"
    assert body["text"] == "최대 2.70%p\n우대 조건 상세"


def test_hwp_paragraphs_demote_a_failed_vlm_layout_table():
    region = {
        "region_id": "p1_r001", "bbox": [0, 0, 500, 900],
        "text": "제목\n내용\n가입금액 100만원", "text_source": "digital_ocr_lines",
        "text_candidates": {}, "kind": "table", "table_status": "partial",
        "needs_review": True,
        "review_reasons": ["table_unplaced_lines", "table_structure_incomplete"],
        "table": {
            "grid": {"rows": 2, "cols": 2},
            "cells": [{"row": 0, "col": 0, "text": "제목"}],
            "notes": [{"text": "내용"}, {"text": "가입금액 100만원"}],
        },
    }
    page = {"regions": [region], "hwp_structure": {
        "tables": [], "paragraphs": [
            {"source_id": "a", "text": "제목"},
            {"source_id": "b", "text": "내용"},
            {"source_id": "c", "text": "가입금액 100만원"},
        ],
    }}

    align_hwp_structure(page)

    assert region["table_status"] == "layout_text"
    assert "table" not in region
    assert region.get("needs_review") is not True
    assert region["hwp_discarded_table_hypothesis"]["grid"] == {"rows": 2, "cols": 2}


def test_lines_split_across_structure_cells_corroborate_digital_region():
    region = {
        "region_id": "p1_r001", "bbox": [0, 0, 500, 500],
        "text": "금리 산정 기준 상세\n대출기간 2년\n대출한도 4억원",
        "text_source": "digital_ocr_lines", "text_candidates": {},
        "needs_review": True, "review_reasons": ["digital_text_vlm_disagreement"],
    }
    page = {"regions": [region], "hwp_structure": {"paragraphs": [], "tables": [{
        "source_id": "t1", "rows": 3, "cols": 1,
        "cells": [
            {"row": 0, "col": 0, "text": "최저금리\n금리 산정 기준 상세"},
            {"row": 1, "col": 0, "text": "대출기간 2년"},
            {"row": 2, "col": 0, "text": "대출한도 4억원"},
        ],
    }]}}

    align_hwp_structure(page)

    assert region["hwp_structure_corroboration"]["method"] == "line_coverage"
    assert region["hwp_structure_corroboration"]["coverage"] == 1.0
    assert region.get("needs_review") is not True


def test_one_row_table_uses_hwp_cell_order_when_pdf_order_is_reversed():
    region = {
        "region_id": "p1_r001", "bbox": [0, 0, 500, 80],
        "text": "대출 원금은 만기에 상환\n만기일시상환방식",
        "text_source": "digital_ocr_lines", "text_candidates": {},
    }
    page = {"regions": [region], "hwp_structure": {"paragraphs": [], "tables": [{
        "source_id": "t2", "rows": 1, "cols": 2, "has_header": True,
        "role_hint": "data_table_candidate", "text": "만기일시상환방식\n대출 원금은 만기에 상환",
        "cells": [
            {"row": 0, "col": 0, "text": "만기일시상환방식"},
            {"row": 0, "col": 1, "text": "대출 원금은 만기에 상환"},
        ],
    }]}}

    stats = align_hwp_structure(page)

    assert stats["tables_attached"] == 1
    assert region["text"] == "만기일시상환방식\n대출 원금은 만기에 상환"
    assert region["table"]["grid"] == {"rows": 1, "cols": 2}

    region.update({"product_id": "product_1", "semantic_labels": ["상환방법"]})
    p3 = build_p3(build_p1({
        "doc_id": "d", "source_file": "d.hwp", "file_type": "hwp",
        "classification": {}, "template": {},
        "pages": [{"page_no": 1, "canvas": [500, 500], "regions": [region]}],
    }))
    output = p3["pages"][0]["regions"][0]
    assert output["text_source"] == "hwp"
    assert p3["contract"]["text_source_values"] == ["hwp", "digital", "ocr", "vlm"]
