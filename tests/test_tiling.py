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


def test_hybrid_attaches_novel_visual_note_inside_structured_table_row():
    structured = [{
        "bbox": [100, 100, 900, 300],
        "kind": "table",
        "content": "원금 및 이자 상환방법\n만기일시상환",
        "text_source": "document_processor_html",
        "table": {"source": "document_processor", "notes": []},
    }]
    visual = [{
        "bbox": [400, 240, 850, 280],
        "content": "※ 지정계좌에서 자동이체 처리",
        "label": "text",
    }]

    free, attached = _attach_visual_supplements(visual, structured)

    assert attached == 1
    assert free == []
    assert structured[0]["content"].endswith("※ 지정계좌에서 자동이체 처리")
    assert structured[0]["table"]["notes"] == ["※ 지정계좌에서 자동이체 처리"]
