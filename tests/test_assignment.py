from __future__ import annotations

from nh_parser_fin.parse import adapters


def test_block_content_is_canonical_when_it_covers_every_ocr_line():
    """PaddleX 본문이 OCR 줄을 모두 담고 있으면 그쪽을 쓴다.

    같은 OCR 결과를 더 잘 조립한 쪽이므로 줄바꿈·띄어쓰기가 자연스럽다.
    """
    page = adapters.build_page_evidence(
        [{"bbox": [0, 0, 200, 100], "label": "text", "order": 1,
          "content": "첫 줄 둘째 줄"}],
        [
            {"bbox": [10, 10, 180, 30], "text": "첫 줄", "score": 0.9},
            {"bbox": [10, 40, 180, 60], "text": "둘째 줄", "score": 0.9},
        ],
        page_no=1,
        canvas=[200, 100],
    )
    region = page["regions"][0]
    assert region["text"] == "첫 줄 둘째 줄"
    assert region["text_source"] == "paddlex_block_content"
    assert region["content_gap_candidates"] == []
    assert region["ocr_evidence"][0]["text"] == "첫 줄"


def test_incomplete_block_content_loses_to_the_ocr_lines():
    """PaddleX 본문이 OCR 줄을 빠뜨리면 줄 조립본을 쓴다.

    예전에는 디지털 텍스트가 없으면 무조건 `block_content` 가 이겨서, PNG 입력의
    깨진 본문이 멀쩡한 OCR 줄을 밀어냈다 — 실측(2026-09-20,
    `3. 예금성상품(거치식).png` p1_r018): block_content 17자
    (`ㅣ이: 이 / 이무기이해 / (융은이이위||`) vs OCR 줄 168자.
    두 후보 모두 같은 OCR 결과에서 나오므로 새 텍스트가 생기지는 않는다.
    """
    page = adapters.build_page_evidence(
        [{"bbox": [0, 0, 200, 100], "label": "text", "order": 1,
          "content": "이무기이해"}],
        [
            {"bbox": [10, 10, 180, 30], "text": "월수로 나눠 매월 지급", "score": 0.9},
            {"bbox": [10, 40, 180, 60], "text": "만기일시지급식 대비 차감", "score": 0.9},
        ],
        page_no=1,
        canvas=[200, 100],
    )
    region = page["regions"][0]
    assert region["text"] == "월수로 나눠 매월 지급\n만기일시지급식 대비 차감"
    assert region["text_source"] == "ocr_lines_block_incomplete"
    assert region["text_selection_status"] == "conflict_pending_vlm"
    # 버린 후보도 남는다. 나중에 Judge 가 대조할 수 있어야 한다.
    assert region["text_candidates"]["paddlex_block_content"] == "이무기이해"


def test_empty_block_content_falls_back_to_owned_ocr_lines():
    page = adapters.build_page_evidence(
        [{"bbox": [0, 0, 200, 100], "label": "table", "order": 1, "content": ""}],
        [
            {"bbox": [10, 10, 180, 30], "text": "첫 줄", "score": 0.9},
            {"bbox": [10, 40, 180, 60], "text": "둘째 줄", "score": 0.9},
        ],
        page_no=2,
        canvas=[200, 100],
    )
    region = page["regions"][0]
    assert region["text"] == "첫 줄\n둘째 줄"
    assert region["text_source"] == "ocr_fallback"
    assert "card_no" not in region
    assert region["product_id"] is None


def test_digital_pdf_text_is_preferred_but_paddlex_candidate_is_retained():
    page = adapters.build_page_evidence(
        [{"bbox": [0, 0, 200, 100], "label": "text", "order": 1,
          "content": "OCR 2.8%"}],
        [{"bbox": [10, 10, 180, 30], "text": "OCR 2.8%", "score": 0.8}],
        page_no=1,
        canvas=[200, 100],
        digital_lines=[{"bbox": [10, 10, 180, 30], "text": "정확한 금리 2.8%"}],
    )
    region = page["regions"][0]
    assert region["text"] == "정확한 금리 2.8%"
    assert region["text_source"] == "digital_ocr_lines"
    assert region["text_candidates"]["paddlex_block_content"] == "OCR 2.8%"
    assert [line["source"] for line in region["lines"]] == ["digital"]


def test_document_structure_text_is_not_replaced_by_partial_digital_lines():
    """렌더링 PDF 줄은 HWP 구조 파서의 완전한 행 텍스트를 줄이면 안 된다."""
    page = adapters.build_page_evidence(
        [{
            "bbox": [0, 0, 300, 120],
            "label": "text",
            "order": 1,
            "content": "준비서류\n실명확인증표\n재직확인서류\n소득확인서류",
            "text_source": "document_processor_html",
            "bbox_quality": "display_exact",
        }],
        [],
        page_no=1,
        canvas=[300, 120],
        digital_lines=[
            {"bbox": [5, 5, 100, 25], "text": "준비서류"},
            {"bbox": [5, 90, 140, 110], "text": "소득확인서류"},
        ],
    )
    region = page["regions"][0]
    assert region["text"] == "준비서류\n실명확인증표\n재직확인서류\n소득확인서류"
    assert region["text_source"] == "document_processor_html"
    assert region["text_selection_status"] == "structured_primary"
    assert region["text_candidates"]["line_assembled"] == "준비서류\n소득확인서류"


def test_nested_regions_give_ocr_line_to_smaller_region_once():
    page = adapters.build_page_evidence(
        [
            {"bbox": [0, 0, 200, 200], "label": "image", "order": 1, "content": "부모"},
            {"bbox": [20, 20, 100, 60], "label": "text", "order": 2, "content": "자식"},
        ],
        [{"bbox": [25, 25, 90, 50], "text": "자식", "score": 0.9}],
        page_no=1,
        canvas=[200, 200],
    )
    parent, child = page["regions"]
    assert parent["ocr_evidence"] == []
    assert len(child["ocr_evidence"]) == 1
    assert len(child["lines"]) == 1
    assert child["parent_id"] == parent["region_id"]
    assert parent["child_ids"] == [child["region_id"]]


def test_unassigned_line_is_preserved_without_nearest_absorption():
    page = adapters.build_page_evidence(
        [{"bbox": [0, 0, 50, 50], "label": "text", "order": 1, "content": "본문"}],
        [{"bbox": [100, 100, 150, 120], "text": "밖의 줄", "score": 0.8}],
        page_no=1,
        canvas=[200, 200],
    )
    assert page["regions"][0]["ocr_evidence"] == []
    assert page["unassigned_lines"][0]["text"] == "밖의 줄"


def test_block_order_is_scoped_to_each_tile():
    page = adapters.build_page_evidence(
        [
            {"bbox": [0, 10, 100, 20], "label": "text", "order": 1,
             "piece": 0, "content": "첫 타일 첫 영역"},
            {"bbox": [0, 30, 100, 40], "label": "text", "order": 2,
             "piece": 0, "content": "첫 타일 둘째 영역"},
            {"bbox": [0, 110, 100, 120], "label": "text", "order": 1,
             "piece": 1, "content": "둘째 타일 첫 영역"},
        ],
        [],
        page_no=1,
        canvas=[100, 150],
    )
    assert [r["text"] for r in page["regions"]] == [
        "첫 타일 첫 영역", "첫 타일 둘째 영역", "둘째 타일 첫 영역",
    ]


def test_tile_boundary_dedupe_keeps_higher_score_and_alternate():
    lines, merged = adapters.dedupe_ocr_lines([
        {"bbox": [10, 10, 200, 40], "text": "30.2%p 우대", "score": 0.98, "piece": 2},
        {"bbox": [12, 10, 200, 41], "text": "0.2%p 우대", "score": 0.95, "piece": 3},
        {"bbox": [10, 60, 200, 90], "text": "다른 줄", "score": 0.99, "piece": 3},
    ])
    assert merged == 1
    assert len(lines) == 2
    winner = next(line for line in lines if line["text"] == "30.2%p 우대")
    assert winner["tile_alternates"][0]["text"] == "0.2%p 우대"
