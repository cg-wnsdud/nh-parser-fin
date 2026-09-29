"""영역 판독 — VLM Reader/Judge가 최종 텍스트를 선택하는 규칙을 검증한다."""
from nh_parser_fin.parse import adapters, export, reading


def test_matching_reading_verifies_but_keeps_digital_parser_text():
    region = {"bbox": [0, 0, 10, 10], "text": "가입금액 100만원 이상",
              "text_source": "digital_ocr_lines"}

    status = reading.apply_reading(
        region, {"text": "가입금액 100만원 이상", "confidence": 0.95})

    assert status == "parser_verified"
    assert region["text"] == "가입금액 100만원 이상"
    assert region["text_source"] == "digital_ocr_lines"
    assert region.get("needs_review") is not True
    # 대조 근거는 남긴다.
    assert region["text_candidates"]["vlm_reading"] == "가입금액 100만원 이상"


def test_disagreement_selects_reader_when_judge_is_unavailable_and_flags_review():
    region = {"bbox": [0, 0, 10, 10],
              "text": "※상환능력에비해신용카드사용액이과도할경우,귀하의개인신용평점이그을V",
              "text_source": "paddlex_block_content"}

    status = reading.apply_reading(region, {
        "text": "※ 상환능력에 비해 신용카드 사용액이 과도할 경우, "
                "귀하의 개인신용평점이 하락할 수 있습니다.",
        "confidence": 0.9,
    })

    assert status == "disagree"
    assert region["text"].startswith("※ 상환능력에")
    assert region["text_source"] == "vlm_reader"
    assert region["needs_review"] is True
    assert region["vlm_reading"]["agreement"] < reading.AGREE


def test_vlm_only_text_becomes_canonical_with_a_coarser_bbox():
    """OCR 이 못 읽은 디자인 문구는 VLM 판독만이 유일한 텍스트다."""
    region = {"bbox": [0, 0, 10, 10], "text": "", "text_source": "empty",
              "bbox_quality": "exact"}

    status = reading.apply_reading(region, {"text": "NH농협은행", "confidence": 0.9})

    assert status == "vlm_only"
    assert region["text"] == "NH농협은행"
    assert region["text_source"] == "vlm_reader"
    # 줄 단위 좌표가 없으므로 품질을 낮춰 표시한다.
    assert region["bbox_quality"] == "region"
    assert region["needs_review"] is True


def test_blank_reading_never_erases_the_ocr_text():
    region = {"bbox": [0, 0, 10, 10], "text": "기본금리 연 2.25%",
              "text_source": "digital_ocr_lines"}

    status = reading.apply_reading(region, {"text": "", "confidence": 0.0})

    assert status == "vlm_blank"
    assert region["text"] == "기본금리 연 2.25%"
    assert region["text_source"] == "digital_ocr_lines"


def test_digital_parser_text_wins_when_judge_disagrees():
    region = {"bbox": [0, 0, 10, 10], "text": "기본금리 연 2.25%",
              "text_source": "digital_ocr_lines"}
    reader = {"text": "기본금리 연 2.26%", "confidence": 0.9}
    judge = {"text": "기본금리 연 2.26%", "confidence": 0.98,
             "source": "reader", "analysis": "이미지 숫자는 6"}

    status = reading.apply_reading(region, reader, judge)

    assert status == "parser_preserved"
    assert region["text"] == "기본금리 연 2.25%"
    assert region["text_source"] == "digital_ocr_lines"
    assert region["needs_review"] is True
    assert "digital_text_vlm_disagreement" in region["review_reasons"]
    assert region["text_candidates"]["parser_selected"] == "기본금리 연 2.25%"
    assert region["vlm_judge"]["source"] == "reader"


def test_hwp_corroboration_prevents_a_vlm_typo_review():
    region = {
        "bbox": [0, 0, 10, 10], "text": "최저 연 3.08% ~ 최고 5.78%",
        "text_source": "digital_ocr_lines",
        "hwp_structure_corroboration": {"status": "agrees", "source_ids": ["t1/r1c1"]},
    }

    status = reading.apply_reading(
        region, {"text": "최저 연 3.08% ~ 최고 5.76%", "confidence": 0.9},
    )

    assert status == "parser_verified"
    assert region.get("needs_review") is not True


def test_hwp_structure_allows_vlm_to_replace_only_private_use_glyphs():
    region = {
        "bbox": [0, 0, 100, 30],
        "text": "\uf000지수연동예금(E LD) 안내",
        "text_source": "digital_ocr_lines",
        "text_candidates": {"hwp_structure": "\uf3da 지수연동예금(ELD) 안내"},
        "hwp_structure_validation": {"status": "agrees"},
    }

    status = reading.apply_reading(
        region, {"text": "☐ 지수연동예금(ELD) 안내", "confidence": 1.0},
    )

    assert status == "parser_verified"
    assert region["text"] == "☐ 지수연동예금(ELD) 안내"
    assert region["text_source"] == "vlm_structure_verified"
    assert region.get("needs_review") is not True


def test_judge_can_correct_non_digital_ocr_text():
    region = {"bbox": [0, 0, 10, 10], "text": "기본금리 연 2.2S%",
              "text_source": "paddlex_block_content",
              "lines": [{"text": "기본금리 연 2.2S%", "source": "ocr"}]}
    reader = {"text": "기본금리 연 2.25%", "confidence": 0.9}
    judge = {"text": "기본금리 연 2.25%", "confidence": 0.98,
             "source": "reader", "analysis": "이미지는 5"}

    status = reading.apply_reading(region, reader, judge)

    assert status == "judge_selected"
    assert region["text"] == "기본금리 연 2.25%"
    assert region["text_source"] == "vlm_judge"


def test_ocr_dominant_pdf_region_selects_judge_despite_one_digital_line():
    """광고 이미지의 버튼 한 줄 때문에 OCR 본문 전체를 PDF 원문으로 취급하지 않는다."""
    page = adapters.build_page_evidence(
        [{"bbox": [0, 0, 200, 100], "label": "image", "order": 1,
          "content": "특판 적금"}],
        [
            {"bbox": [10, 10, 190, 25], "text": "re-뱅킹 예금 출시 25주년"},
            {"bbox": [10, 30, 190, 45], "text": "최고 7.1%"},
            {"bbox": [10, 50, 190, 65], "text": "NH대박 7적금"},
        ],
        page_no=1, canvas=[200, 100],
        digital_lines=[{"bbox": [130, 75, 190, 90], "text": "상품가입"}],
    )
    region = page["regions"][0]
    assert region["text_source"] == "ocr_digital_lines"
    assert region["digital_text_share"] < adapters.DIGITAL_PRIMARY_SHARE

    corrected = "「e-뱅킹 예금」 출시 25주년\n최고 7.1%\nNH대박 7(칠)적금\n상품가입"
    status = reading.apply_reading(
        region,
        {"text": corrected, "confidence": 0.95},
        {"text": corrected, "confidence": 1.0, "source": "corrected"},
    )
    assert status == "judge_selected"
    assert region["text_source"] == "vlm_judge"
    assert region["text_candidates"]["parser_selected"] != corrected
    assert export.build_p3({"pages": [page]})["pages"][0]["regions"][0]["selected_text"] == corrected


def test_digital_dominant_region_still_preserves_pdf_text():
    region = {
        "bbox": [0, 0, 100, 50],
        "text": "기본금리 연 2.25%\n가입",
        "text_source": "digital_ocr_lines",
        "lines": [
            {"text": "기본금리 연 2.25%", "source": "digital"},
            {"text": "가입", "source": "ocr"},
        ],
    }
    status = reading.apply_reading(
        region,
        {"text": "기본금리 연 2.26%\n가입", "confidence": 0.9},
        {"text": "기본금리 연 2.26%\n가입", "confidence": 0.98},
    )
    assert status == "parser_preserved"
    assert region["text"] == "기본금리 연 2.25%\n가입"


def test_table_regions_are_never_re_read():
    """원본 셀로 확정된 표는 Reader가 평문으로 덮어쓰지 않는다."""
    table = {"bbox": [0, 0, 10, 10], "kind": "table", "text": "| 가 | 나 |",
             "table": {"grid": {"rows": 1, "cols": 2}}}

    assert reading.should_read(table, "all") is False
    assert reading.should_read(table, "targeted") is False


def test_document_structure_text_is_never_re_read_or_replaced():
    region = {
        "bbox": [0, 0, 100, 30],
        "text": "준비서류\n실명확인증표\n재직확인서류",
        "text_source": "document_processor_html",
        "bbox_quality": "display_exact",
    }

    assert reading.should_read(region, "all") is False
    status = reading.apply_reading(
        region,
        {"text": "준비서류", "confidence": 0.99},
    )
    assert status == "parser_preserved"
    assert region["text"] == "준비서류\n실명확인증표\n재직확인서류"
    assert region["text_source"] == "document_processor_html"


def test_targeted_scope_picks_only_suspicious_regions():
    broken = {"bbox": [0, 0, 10, 10], "text": ")", "lines": [{"text": ")"}]}
    conflict = {"bbox": [0, 0, 10, 10], "text": "긴 본문입니다 " * 3,
                "text_selection_status": "conflict_pending_vlm", "lines": [{}]}
    healthy = {"bbox": [0, 0, 10, 10], "text": "가입금액 100만원 이상",
               "text_selection_status": "sources_agree", "lines": [{}]}

    assert reading.should_read(broken, "targeted") is True
    assert reading.should_read(conflict, "targeted") is True
    assert reading.should_read(healthy, "targeted") is False
    # all 은 전부 본다.
    assert reading.should_read(healthy, "all") is True
    assert reading.should_read(healthy, "off") is False


def test_a_failed_reading_does_not_stop_the_page():
    class Boom:
        width = height = 100
        def crop(self, box):
            raise RuntimeError("crop 실패")

    page = {"regions": [
        {"region_id": "r1", "bbox": [0, 0, 50, 50], "text": "원본 유지"},
    ]}

    stats = reading.read_page(page, Boom(), scope="all")

    assert stats["failed"] == 1
    assert page["regions"][0]["text"] == "원본 유지"
    assert page["regions"][0]["needs_review"] is True


def test_whitespace_only_differences_never_count_as_disagreement():
    """공백을 지우고 비교하므로 줄바꿈·띄어쓰기 차이는 잡음이 되지 않는다."""
    assert reading.agreement("가입금액100만원이상(원단위)",
                             "가입금액 100만원 이상 (원 단위)") == 1.0
    assert reading.agreement("대출한도\n최대 3억원 이내",
                             "대출한도 최대 3억원 이내") == 1.0


def test_a_single_digit_difference_is_flagged():
    """금리 숫자 한 자 차이는 광고 심의에서 가장 크게 문제 되는 종류다."""
    region = {"bbox": [0, 0, 10, 10], "text": "기본금리 연 2.25%",
              "text_source": "digital_ocr_lines"}

    status = reading.apply_reading(region, {"text": "기본금리 연 2.26%", "confidence": 0.9})

    assert status == "parser_preserved"
    assert region["needs_review"] is True
    # PDF 내장 텍스트는 정본으로 유지하고 VLM 후보만 P1에 남긴다.
    assert region["text"] == "기본금리 연 2.25%"
    assert region["text_candidates"]["parser_selected"] == "기본금리 연 2.25%"
    assert region["text_candidates"]["vlm_reading"] == "기본금리 연 2.26%"


def test_stray_json_tail_is_stripped_from_the_text():
    """모델이 JSON 을 닫고 이어 쓴 잡담이 정본 후보에 섞이면 안 된다.

    실측(2026-09-20): `NH농협카드"} (Note: The user requested to transc…`
    """
    assert reading.clean_text('NH농협카드"} (Note: The prompt asks to…') == "NH농협카드"
    assert reading.clean_text("정상 텍스트") == "정상 텍스트"
    # 값 안의 따옴표가 정상인 경우까지 잘라내지 않는다.
    assert reading.clean_text('연회비 1만2천원') == "연회비 1만2천원"


def test_defaults_are_page_reader_with_digital_anchor(monkeypatch):
    """운영 기본값. 이전 방식은 환경변수로 되돌릴 수 있다."""
    from nh_parser_fin.parse import digital_anchor, table_html
    for name in ("PARSER_V2_READER_MODE", "PARSER_V2_PAGE_READER_TEXT", "PARSER_V2_JUDGE_TRIGGER",
                 "PARSER_V2_JUDGE_PROMPT", "PARSER_V2_REVIEW_RULES", "PARSER_V2_DIGITAL_MODE",
                 "PARSER_V2_TABLE_FORMAT"):
        monkeypatch.delenv(name, raising=False)
    assert reading.reader_mode_from_env() == "page"
    assert reading.page_reader_text_from_env() is False
    assert reading.judge_trigger_from_env() == "strict"
    assert reading.judge_prompt_from_env() == "blind"
    assert reading.review_rules_from_env() == "default"
    assert digital_anchor.mode_from_env() == "anchor"
    assert table_html.format_from_env() == "html"
    monkeypatch.setenv("PARSER_V2_READER_MODE", "region")
    monkeypatch.setenv("PARSER_V2_DIGITAL_MODE", "primary")
    monkeypatch.setenv("PARSER_V2_TABLE_FORMAT", "pipe")
    assert reading.reader_mode_from_env() == "region"
    assert digital_anchor.mode_from_env() == "primary"
    assert table_html.format_from_env() == "pipe"
