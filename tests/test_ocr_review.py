from nh_parser_fin.parse.reading import needs_judge, ocr_review_codes


def test_blank_reading_with_ocr_text_is_flagged():
    region = {"text": "위이을해위부터를 위시성 이이무", "reading_status": "vlm_blank"}
    assert "vlm_blank_with_ocr_text" in ocr_review_codes(region, region["text"], {"text": ""}, None)


def test_judge_word_absent_from_both_candidates_is_flagged():
    # 실측(14. 대출성상품): OCR은 "대출신청", Reader는 줄 누락, Judge가 "대출시점"을 만들었다.
    parser = "※고객별실제우대금리는대출신청영업점에서상담하여확인"
    region = {"text": "※ 고객별 실제 우대금리는 대출시점 영업점에서 상담하여 확인"}
    codes = ocr_review_codes(region, parser, {"text": "[2026.04.03. 현재]"}, {"text": region["text"]})
    assert "judge_new_word" in codes


def test_judge_that_only_fixes_spacing_is_not_flagged():
    parser = "※거래실적등에 따라차등적용"
    region = {"text": "※ 거래실적 등에 따라 차등 적용"}
    assert ocr_review_codes(region, parser, {"text": parser}, {"text": region["text"]}) == []


def test_number_missing_from_both_sources_is_flagged():
    region = {"text": "우대금리 연 0.50%p"}
    codes = ocr_review_codes(region, "우대금리 연 0.30%p", {"text": "우대금리 연 0.30%p"}, {"text": region["text"]})
    assert "unsupported_number" in codes


def test_strict_trigger_ignores_spacing(monkeypatch):
    monkeypatch.setenv("PARSER_V2_JUDGE_TRIGGER", "strict")
    assert not needs_judge("▶거래실적우대", "▶ 거래실적 우대")
    assert needs_judge("원금균등할부상환 대출원금을 분할하고 이자를 더하여", "원금균등분할상환 대출원금을 분할하고 이자를 더하여")
