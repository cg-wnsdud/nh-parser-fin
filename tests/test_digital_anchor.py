from nh_parser_fin.parse.digital_anchor import anchor_page, snap_text


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
    assert "digital_text_missing" in region["review_reasons"]
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
