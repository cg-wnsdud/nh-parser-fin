from nh_parser_fin.ocr.tiling import dedupe
from run import _novel_visual_blocks


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
