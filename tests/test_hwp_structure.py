from nh_parser_fin.ingest.hwp_structure import normalize_kordoc, repartition_by_rendered_text


def test_kordoc_structure_preserves_cells_spans_and_nested_tables():
    raw = {
        "fileType": "hwp", "pageCount": 1,
        "blocks": [{
            "type": "table", "pageNumber": 1,
            "table": {
                "sourceId": "t1", "rows": 2, "cols": 2, "hasHeader": True,
                "cells": [
                    [{"text": "항목", "colSpan": 2, "rowSpan": 1,
                      "blocks": [{"type": "table", "pageNumber": 1, "table": {
                          "sourceId": "t2", "rows": 2, "cols": 2,
                          "cells": [[{"text": "가"}, {"text": "나"}],
                                    [{"text": "1"}, {"text": "2"}]],
                      }}]}, {"text": ""}],
                    [{"text": "금리"}, {"text": "3.0%"}],
                ],
            },
        }],
    }

    result = normalize_kordoc(raw, parser_version="test")

    assert result["page_count"] == 1
    outer, nested = result["pages"][0]["tables"]
    assert outer["source_id"] == "t1"
    assert outer["cells"][0]["col_span"] == 2
    assert nested["source_id"] == "t2"
    assert nested["parent_cell"] == {"table_id": "t1", "row": 0, "col": 0}
    assert nested["role_hint"] == "data_table_candidate"


def test_large_sparse_merged_table_is_a_layout_container():
    cells = [[{"text": ""} for _ in range(7)] for _ in range(6)]
    cells[0][0] = {"text": "상품명", "colSpan": 7, "rowSpan": 1}
    raw = {"pageCount": 1, "blocks": [{
        "type": "table", "pageNumber": 1,
        "table": {"rows": 6, "cols": 7, "cells": cells},
    }]}

    table = normalize_kordoc(raw)["pages"][0]["tables"][0]

    assert table["role_hint"] == "layout_container"
    assert table["density"] < 0.1


def test_logical_page_blocks_are_repartitioned_to_rendered_pdf_pages():
    structure = {
        "parser": "kordoc", "parser_version": "test",
        "pages": [{"page_no": 1, "tables": [], "paragraphs": [
            {"source_id": "a", "text": "첫 페이지 상품 내용"},
            {"source_id": "b", "text": "준법감시인 심의필 2026-0000"},
        ]}],
    }

    pages = repartition_by_rendered_text(
        structure,
        ["첫 페이지 상품 내용", "준법감시인 심의필 2026-0000 수신거부"],
    )

    assert [item["source_id"] for item in pages[0]["paragraphs"]] == ["a"]
    assert [item["source_id"] for item in pages[1]["paragraphs"]] == ["b"]


def test_single_column_image_title_table_is_layout_not_business_data():
    raw = {"pageCount": 1, "blocks": [{
        "type": "table", "pageNumber": 1,
        "table": {"rows": 3, "cols": 1, "cells": [
            [{"text": "![image](a.bmp)"}],
            [{"text": "상품 안내 제목"}],
            [{"text": "![image](a.bmp)"}],
        ]},
    }]}

    table = normalize_kordoc(raw)["pages"][0]["tables"][0]

    assert table["role_hint"] == "layout_container"
