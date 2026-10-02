from nh_parser_fin.ocr.tiling import dedupe
from run import _attach_visual_supplements, _novel_visual_blocks


def test_dedupe_merges_same_text_across_tiles_despite_layout_label_conflict():
    boxes, merged = dedupe([
        {
            "piece": 0,
            "label": "text",
            "bbox": [67, 668, 976, 772],
            "content": "일별 잔액 100만원 이하 금액에 적용하며, 기본금리 포함 금리 적용기준",
        },
        {
            "piece": 1,
            "label": "doc_title",
            "bbox": [65, 668, 976, 772],
            "content": "일별 잔액 100만원 이하 금액에 적용하며, 기본금리 포함 금리 적용기준",
        },
    ])

    assert merged == 1
    assert len(boxes) == 1
    assert boxes[0]["bbox"] == [65, 668, 976, 772]
    assert boxes[0]["label_candidates"] == ["text", "doc_title"]
    assert boxes[0]["dedupe_label_conflict"] is True


def test_dedupe_keeps_different_text_when_layout_labels_conflict():
    boxes, merged = dedupe([
        {"piece": 0, "label": "text", "bbox": [10, 20, 300, 80], "content": "첫 문구"},
        {"piece": 1, "label": "doc_title", "bbox": [10, 20, 300, 80], "content": "다른 문구"},
    ])

    assert merged == 0
    assert len(boxes) == 2


def test_hybrid_keeps_only_visual_content_missing_from_structured_rows():
    structured = [{
        "bbox": [100, 200, 900, 400],
        "content": "대출기간\n2년 이내",
        "label": "table",
    }]
    visual = [
        {"bbox": [110, 210, 890, 390], "content": "대출기간 2년 이내", "label": "text"},
        {"bbox": [20, 30, 250, 90], "content": "금융의 모든 순간", "label": "text"},
    ]

    kept, suppressed = _novel_visual_blocks(visual, structured)

    assert suppressed == 1
    assert [block["content"] for block in kept] == ["금융의 모든 순간"]


def test_hybrid_suppresses_page_sized_ocr_container_covered_by_structured_rows():
    structured = [
        {"bbox": [100, 100, 900, 200], "content": "대출대상\n직장인 고객"},
        {"bbox": [100, 200, 900, 300], "content": "대출한도\n최대 3억원"},
        {"bbox": [100, 300, 900, 400], "content": "대출기간\n2년 이내"},
        {"bbox": [100, 400, 900, 500], "content": "준비서류\n재직확인서류"},
    ]
    visual = [
        {
            "bbox": [80, 80, 920, 520],
            "content": "대출 대상 직장인고객 대출한도 최대3억원 대출 기간 2년이내 준비서류 재직확인서류",
            "label": "text",
        },
        {"bbox": [20, 20, 250, 70], "content": "금융의 모든 순간", "label": "text"},
    ]

    kept, suppressed = _novel_visual_blocks(visual, structured)

    assert suppressed == 1
    assert [block["content"] for block in kept] == ["금융의 모든 순간"]


def test_hybrid_keeps_large_visual_block_with_novel_logo_text():
    structured = [
        {"bbox": [100, 100, 900, 200], "content": "대출대상\n직장인 고객"},
        {"bbox": [100, 200, 900, 300], "content": "대출한도\n최대 3억원"},
        {"bbox": [100, 300, 900, 400], "content": "대출기간\n2년 이내"},
    ]
    visual = [{
        "bbox": [80, 60, 920, 420],
        "content": "금융의 모든 순간 NH농협은행",
        "label": "image",
    }]

    kept, suppressed = _novel_visual_blocks(visual, structured)

    assert suppressed == 0
    assert kept == visual


def test_hybrid_suppresses_same_row_with_minor_ocr_typo():
    structured = [{
        "bbox": [20, 100, 980, 220],
        "content": "축산물품질평가원 임직원을 위한 전세대출 안내장",
        "text_source": "document_processor_html",
    }]
    visual = [{
        "bbox": [250, 105, 780, 215],
        "content": "축산물품질평가원 임직원을 위한 천세대출 안내장",
        "label": "doc_title",
    }]

    kept, suppressed = _novel_visual_blocks(visual, structured)

    assert suppressed == 1
    assert kept == []


def test_hybrid_suppresses_ocr_subset_of_a_larger_structured_row():
    structured = [{
        "bbox": [100, 100, 900, 300],
        "content": "부대비용\n중도상환해약금 산식과 적용요율 안내\n인지세 및 보증료 안내",
        "text_source": "document_processor_html",
    }]
    visual = [{
        "bbox": [400, 120, 880, 190],
        "content": "중도상환해약금 산식과 적용요율 안내",
        "label": "text",
    }]

    kept, suppressed = _novel_visual_blocks(visual, structured)

    assert suppressed == 1
    assert kept == []


def _structured_row(content="원금 및 이자 상환방법\n만기일시상환"):
    return [{
        "bbox": [100, 100, 900, 300],
        "kind": "table",
        "content": content,
        "text_source": "document_processor_html",
        "table": {"source": "document_processor", "notes": []},
    }]


def test_hybrid_attaches_hwp_original_line_located_by_render_text():
    structured = _structured_row()
    # OCR은 받침을 틀렸고, 렌더 텍스트층은 글자는 맞지만 띄어쓰기가 빠지고 줄이 끊긴다.
    visual = [{"bbox": [400, 240, 850, 300], "content": "※지정계좌에서자동이체처리 적용요을", "label": "text"}]
    digital = [
        {"bbox": [410, 245, 840, 265], "text": "※지정계좌에서자동이체처리적용"},
        {"bbox": [410, 270, 840, 290], "text": "요율"},
    ]
    structure = {"paragraphs": [], "tables": [{"cells": [
        {"text": "원금 및 이자 상환방법"},
        {"text": "만기일시상환\n※ 지정계좌에서 자동이체 처리 적용요율"},
    ]}]}

    free, attached = _attach_visual_supplements(visual, structured, digital, structure)

    assert (attached, free) == (1, [])
    assert structured[0]["content"].endswith("\n※ 지정계좌에서 자동이체 처리 적용요율")
    assert "적용요을" not in structured[0]["content"]
    assert structured[0]["text_source"] == "document_processor_html_with_structure_text"
    record = structured[0]["visual_supplements"][0]
    assert record["action"] == "structure_text_attached"
    assert record["ocr_text"] == "※지정계좌에서자동이체처리 적용요을"
    assert record["render_text"] == "※지정계좌에서자동이체처리적용\n요율"


def test_hybrid_falls_back_to_render_text_without_matching_structure_line():
    structured = _structured_row()
    visual = [{"bbox": [400, 240, 850, 280], "content": "※지정계좌에서자동이체처리 적용요을", "label": "text"}]
    digital = [{"bbox": [410, 245, 840, 275], "text": "※지정계좌에서자동이체처리적용요율"}]

    free, attached = _attach_visual_supplements(visual, structured, digital)

    assert (attached, free) == (1, [])
    assert structured[0]["content"].endswith("※지정계좌에서자동이체처리적용요율")
    assert structured[0]["text_source"] == "document_processor_html_with_render_text"


def test_hybrid_drops_ocr_duplicate_of_structured_row():
    structured = _structured_row("|지점명|\n농협은행\n\n대연동지점")
    visual = [{"bbox": [120, 120, 880, 160], "content": "|지점명]농협은행대연동지점", "label": "text"}]

    free, attached = _attach_visual_supplements(visual, structured, [])

    assert (attached, free) == (1, [])
    assert structured[0]["content"] == "|지점명|\n농협은행\n\n대연동지점"
    assert structured[0]["visual_supplements"][0]["action"] == "duplicate_dropped"


def test_hybrid_keeps_image_text_without_render_text_as_separate_block():
    structured = _structured_row()
    visual = [{"bbox": [400, 240, 850, 280], "content": "NH농협금융", "label": "text"}]

    free, attached = _attach_visual_supplements(visual, structured, [])

    assert (attached, free) == (0, visual)
    assert structured[0]["content"] == "원금 및 이자 상환방법\n만기일시상환"
    assert structured[0]["table"]["notes"] == []


def test_hybrid_discards_paddle_table_html_when_dom_rows_exist():
    structured = [
        {"bbox": [100, 100, 900, 200], "kind": "table", "content": "대출대상\n공무원",
         "table": {"source": "document_processor"}},
        {"bbox": [100, 200, 900, 300], "kind": "table", "content": "대출금리\n연 4.45%",
         "table": {"source": "document_processor"}},
    ]
    paddle = [{"bbox": [100, 100, 900, 300], "label": "table",
               "content": "<html><body><table><tr><td>공무원</td></tr>"
                          "<tr><td>연 4.45%</td></tr></table></body></html>"}]

    novel, discarded = _novel_visual_blocks(paddle, structured)

    assert novel == []
    assert discarded == 1
    assert all("<html>" not in row["content"] for row in structured)
