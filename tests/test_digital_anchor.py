from nh_parser_fin.parse.digital_anchor import align_spans, anchor_page, snap_text


def _line(text, bbox):
    return {"text": text, "bbox": bbox}


def test_vlm_term_normalization_is_snapped_to_printed_word():
    # 실측(20. 대출성상품): 인쇄는 "할부상환", VLM은 "분할상환"으로 바꿔 읽었다.
    lines = [_line("ㆍ원금균등할부상환 : 대출원금을 대출 개월수 만큼", [0, 0, 100, 10])]
    text, changes, unmatched, _ = snap_text("• 원금균등분할상환 : 대출원금을 대출개월수 만큼", lines)
    assert text == "• 원금균등할부상환 : 대출원금을 대출개월수 만큼"  # 띄어쓰기·기호는 VLM 그대로
    assert changes == [{"from": "원금균등분할상환", "to": "원금균등할부상환"}]
    assert not unmatched


def test_one_char_misread_is_snapped():
    lines = [_line("※고객별실제우대금리는대출신청영업점에서상담하여확인", [0, 0, 100, 10])]
    text, _, _, _ = snap_text("※ 고객별 실제 우대금리는 대출시청 영업점에서 상담하여 확인", lines)
    assert "대출신청" in text and "대출시청" not in text


def test_broken_digital_characters_are_never_inserted():
    lines = [_line("금융의모든순간 N H비대면", [0, 0, 100, 10])]
    text, changes, _, _ = snap_text("금융의 모든 순간 NH 비대면", lines)
    assert text == "금융의 모든 순간 NH 비대면"
    assert not changes


def test_number_is_snapped_only_when_unique():
    lines = [_line("우대금리 연 0.30%p", [0, 0, 100, 10])]
    assert snap_text("우대금리 연 0.50%p", lines)[0] == "우대금리 연 0.30%p"
    lines = [_line("우대금리 연 0.30%p 0.20%p", [0, 0, 100, 10])]
    text, _, unmatched, _ = snap_text("우대금리 연 0.50%p", lines)
    assert text == "우대금리 연 0.50%p" and unmatched == ["0.50"]


def test_short_words_are_not_corrected():
    text, changes, unmatched, _ = snap_text("가입 대상", [_line("가입대장", [0, 0, 10, 10])])
    assert text == "가입 대상" and not changes and unmatched == ["대상"]


def _page(regions, lines):
    return {"regions": regions, "digital_anchor_lines": lines}


def test_digital_line_absent_from_page_text_is_flagged_missing():
    # 실측(20. 대출성상품 r015): Judge가 끝줄만 남기고 두 줄을 버렸다.
    region = {"region_id": "r15", "bbox": [0, 0, 200, 60], "text": "• 한도대출(마이너스통장) : 대출 원금은 만기에 일시상환"}
    lines = [
        _line("ㆍ원금균등할부상환 : 대출원금을 균등하게 분할", [0, 0, 200, 18]),
        _line("ㆍ한도대출(마이너스통장): 대출 원금은 만기에 일시상환", [0, 40, 200, 58]),
    ]
    page = _page([region], lines)
    stats = anchor_page(page)
    assert stats["missing"] == 1
    assert "digital_text_inserted" in region["review_reasons"]
    assert region["digital_anchor"]["missing_lines"] == ["ㆍ원금균등할부상환 : 대출원금을 균등하게 분할"]


def test_digital_line_read_into_neighbor_is_moved_not_missing():
    a = {"region_id": "a", "bbox": [0, 0, 200, 20], "text": "첫째 줄 문구입니다"}
    b = {"region_id": "b", "bbox": [0, 22, 200, 40], "text": "첫째 줄 문구입니다\n가입기간 십이개월"}
    lines = [_line("첫째줄문구입니다", [0, 0, 200, 9]), _line("가입기간십이개월", [0, 10, 200, 19])]
    page = _page([a, b], lines)
    anchor_page(page)
    assert not a.get("needs_review")
    assert a["digital_anchor"]["moved_lines"] == ["가입기간십이개월"]


def test_blank_region_is_filled_from_its_digital_lines():
    region = {"region_id": "r1", "bbox": [0, 0, 200, 20], "text": ""}
    anchor_page(_page([region], [_line("가입기간 12개월", [0, 0, 200, 18])]))
    assert region["text"] == "가입기간 12개월"
    assert region["text_source"] == "digital_anchor_fallback"


def test_structured_hwp_text_is_left_alone():
    region = {"region_id": "r1", "bbox": [0, 0, 200, 20], "text": "대출시청", "text_source": "document_processor_html"}
    anchor_page(_page([region], [_line("대출신청", [0, 0, 200, 18])]))
    assert region["text"] == "대출시청"


def test_dropped_char_prefers_insertion_over_wrong_substitution():
    # 실측(20. 대출성상품 r011): 인쇄 "농어업전문교육이수자", VLM "농업전문교육이수자".
    lines = [_line("일자리 창출기업 0.10%p, 농어업전문교육이수자 0.10%p", [0, 0, 100, 10])]
    assert "농어업전문교육이수자" in snap_text("농업전문교육이수자 0.10%p", lines)[0]


def test_word_wrapped_inside_table_cell_is_matched():
    lines = [_line("운전", [0, 0, 10, 10]), _line("1년 이내", [20, 0, 90, 10]), _line("자금", [0, 12, 10, 22])]
    _, _, unmatched, _ = snap_text("운전자금 | 1년 이내", lines)
    assert not unmatched


def test_graphic_only_line_is_not_suspect():
    lines = [_line("준법감시인 심의필 2026-1725", [0, 0, 100, 10])]
    _, _, unmatched, suspect = snap_text("NH농협금융\n준법감시인 심의필 2026-1725", lines)
    assert unmatched == ["농협금융"] and suspect == []
    _, _, _, suspect = snap_text("준법감시인 심의필 2026-1725 대출시점", lines)
    assert suspect == ["대출시점"]
    # 배너 전체를 한 줄로 적어도, 이어진 그림 글자는 의심하지 않는다.
    _, _, _, suspect = snap_text("금융의 모든 순간 준법감시인 심의필 2026-1725", lines)
    assert suspect == []


def test_tie_prefers_whole_digital_word():
    lines = [_line("가입 전 ‘상품서비스 안내동의서(⑦번동의서)’ 전체 동의 시", [0, 0, 100, 10])]
    assert "(⑦번동의서)" in snap_text("상품서비스 안내동의서(⑦변동의서)", lines)[0]


def test_align_restores_dropped_footnote_marker():
    # 실측(1. 예금성상품 r018): 인쇄 "…동의 시 주1)", VLM "…동의 시1)". 주1)은 본문 옆 작은 줄이다.
    lines = [_line("가입 전 ‘상품서비스 안내동의서’ 전체 동의 시", [1001, 1820, 1395, 1838]),
             _line("주1)", [1399, 1819, 1416, 1831]), _line("0.20%p", [1475, 1821, 1533, 1840])]
    text, spans = align_spans("| 가입 전 ‘상품서비스 안내동의서’ 전체 동의 시1) | 0.20%p |", lines)
    assert text == "| 가입 전 ‘상품서비스 안내동의서’ 전체 동의 시주1) | 0.20%p |"
    assert spans == [{"from": "", "to": "주"}]


def test_align_removes_words_the_vlm_added():
    # 실측(20. 대출성상품 r016): 인쇄 "자동이체", VLM "자동으로 이체".
    lines = [_line("※대출실행일/별도지정일에 지정계좌에서 자동이체 처리", [0, 0, 900, 20])]
    text, _ = align_spans("※ 대출실행일/별도지정일에 지정계좌에서 자동으로 이체 처리", lines)
    assert text == "※ 대출실행일/별도지정일에 지정계좌에서 자동이체 처리"


def test_align_restores_dropped_symbols_at_end():
    # 실측(3. 대출성상품 r020): 인쇄 "최고 1.50%p (①+②)", VLM "최고 1.50%p".
    lines = [_line("● 우대금리 : 최고 1.50%p (①+②)", [0, 0, 400, 20])]
    text, _ = align_spans("•우대금리 : 최고 1.50%p", lines, own=lines)
    assert text == "•우대금리 : 최고 1.50%p (①+②)"  # 글머리 기호는 VLM 표기 그대로


def test_align_keeps_vlm_bullets_and_long_differences():
    lines = [_line("ㆍ만기일시상환 : 대출원금은 만기에 일시상환", [0, 0, 400, 20])]
    assert align_spans("• 만기일시상환 : 대출원금은 만기에 일시상환", lines)[1] == []
    lines = [_line("가입대상 개인 가입금액 1천원 이상", [0, 0, 400, 20])]
    assert align_spans("가입대상 개인 적립방식 자유적립식 가입금액 1천원 이상", lines)[1] == []


def test_missing_line_is_inserted_in_vertical_order():
    region = {"region_id": "r15", "bbox": [0, 0, 200, 60], "text": "• 한도대출 : 대출 원금은 만기에 일시상환"}
    lines = [_line("ㆍ원금균등할부상환 : 대출원금을 균등하게 분할", [0, 0, 200, 18]),
             _line("ㆍ한도대출: 대출 원금은 만기에 일시상환", [0, 40, 200, 58])]
    anchor_page(_page([region], lines))
    assert region["text"].split("\n") == ["ㆍ원금균등할부상환 : 대출원금을 균등하게 분할", "• 한도대출 : 대출 원금은 만기에 일시상환"]
    assert "digital_text_inserted" in region["review_reasons"]


def test_line_copied_from_neighbor_is_flagged():
    # 실측(14. 대출성상품 r004): 옆 박스의 "· 공무원 연금공단…" 줄이 이 박스에도 들어왔다.
    a = {"region_id": "a", "bbox": [0, 0, 400, 20], "text": "· 재직기간 3개월 이상 재직중인 공무원\n· 공무원 연금공단을 통해 확인이 가능한 자"}
    b = {"region_id": "b", "bbox": [0, 30, 400, 50], "text": "· 공무원 연금공단을 통해 확인이 가능한 자"}
    lines = [_line("재직기간 3개월 이상 재직중인 공무원", [0, 2, 400, 18]), _line("공무원 연금공단을 통해 확인이 가능한 자", [0, 32, 400, 48])]
    anchor_page(_page([a, b], lines))
    assert "neighbor_line_duplicated" in a["review_reasons"]
    assert not b.get("needs_review")


def test_align_does_not_pull_neighbor_values_or_break_tables():
    # 실측(2. 예금성상품(적립식) r005): 근처 줄에 옆 박스의 "연 2.30%"가 섞여 있었다.
    lines = [_line("기본금리", [0, 0, 100, 20]), _line("연 2.30%", [0, 40, 100, 60])]
    assert align_spans("기본금리", lines, own=lines[:1])[1] == []
    # 표 칸 경계를 걸친 차이는 두지 않는다(20. 대출성상품 r021).
    lines = [_line("운전", [0, 0, 40, 20]), _line("일시상환 1년 이내", [60, 0, 300, 20]), _line("자금", [0, 22, 40, 42])]
    text = "| 운전자금 | 일시상환 | 1년 이내 |\n| 운전자금 | 할부상환 | 3년 |"
    assert align_spans(text, lines)[0] == text


def test_grid_table_markdown_is_not_span_aligned_but_cells_get_word_fixes():
    # 실측(040): 머리글 경로 `세부조건 / 가입기간`의 ` / `가 디지털 글자 `우대금리`로 메워졌다.
    markdown = "| 우대항목 | 세부조건 / 가입기간 | 우대금리 |\n|---|---|---|\n| 아동수당 수령 | 1년 | 1.5%p |"
    cells = [
        {"row": 0, "col": 0, "row_span": 1, "col_span": 1, "is_header": True, "text": "우대항목"},
        {"row": 0, "col": 1, "row_span": 1, "col_span": 1, "is_header": True, "text": "세부조건"},
        {"row": 2, "col": 0, "row_span": 1, "col_span": 1, "is_header": False, "text": "아동수당 수령"},
    ]
    region = {
        "region_id": "p1_r001", "bbox": [0, 0, 400, 60], "text": markdown,
        "text_source": "vlm_table_html",
        "visual_table": {"rows": [["우대항목", "세부조건"]], "cells": cells, "notes": []},
    }
    lines = [
        _line("우대항목 세부조건 우대금리", [0, 0, 400, 18]),
        _line("가입기간", [100, 20, 200, 38]),
        _line("아동수당수령 1년 1.5%p", [0, 40, 400, 58]),
    ]

    anchor_page(_page([region], lines))

    assert "세부조건 / 가입기간" in region["text"]
    assert region["digital_anchor"]["aligned"] == []
