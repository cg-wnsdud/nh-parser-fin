"""HWP 표 밖·큰 셀 안 본문 문단을 심의 항목 단위 Region으로 묶는다."""

from nh_parser_fin.ingest.hwp_html import dom_rows_to_blocks


def _paragraph_row(index, text, *, group="page1", y=None):
    y = index * 20 if y is None else y
    cells = [{"node_id": f"p{index}", "col": 0, "col_span": 1, "row_span": 1,
              "bbox": [10, y, 400, y + 18], "text": text}] if text else []
    return {
        "node_id": f"domparagraph:p{index}:{index}", "page_no": 1,
        "bbox": [10, y, 400, y + 18], "text": text, "cells": cells,
        "paragraph": {"group": group, "index": index, "blank": not text},
    }


def test_body_paragraphs_become_one_block_per_review_item():
    rows = [
        _paragraph_row(0, "(광고) 위풍당당적금 안내"),
        _paragraph_row(1, ""),
        _paragraph_row(2, "▶ 가입기간"),
        _paragraph_row(3, "- 모집기간 : 07.01 ~ 07.08"),
        _paragraph_row(4, "- 계약기간 : 1년"),
        _paragraph_row(5, "□ 가입대상 : 개인"),
        _paragraph_row(6, "[올원뱅크 바로가기]"),
        # 사이에 표(자식 순번 7)가 끼면 기호가 없어도 새 묶음이다.
        _paragraph_row(8, "표 아래 문장"),
    ]

    blocks = dom_rows_to_blocks(rows, page_no=1, canvas=(800, 800), page_css_size=(800, 800))

    assert [block["content"] for block in blocks] == [
        "(광고) 위풍당당적금 안내",
        "▶ 가입기간\n- 모집기간 : 07.01 ~ 07.08\n- 계약기간 : 1년",
        "□ 가입대상 : 개인\n[올원뱅크 바로가기]",
        "표 아래 문장",
    ]
    assert blocks[1]["bbox"] == [10, 40, 400, 98]
    assert blocks[1]["structured"]["node_kind"] == "dom_paragraph_group"
    assert blocks[1]["structured"]["node_ids"] == ["p2", "p3", "p4"]
