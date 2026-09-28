"""깨진 PDF 문자 매핑은 OCR 근거를 가리지 않아야 한다."""

from nh_parser_fin.parse.adapters import build_page_evidence, digital_text_needs_ocr


def test_sparse_garbled_pdf_text_falls_back_to_ocr():
    digital = [{"bbox": [0, 0, 100, 20], "text": "5 19 0519 12 182 650"}]
    ocr = [{"bbox": [0, 0, 100, 20], "text": "우대금리 적용조건 " * 15}]

    assert digital_text_needs_ocr(digital, ocr)
    page = build_page_evidence(
        [{"bbox": [0, 0, 120, 30], "label": "text", "content": ""}],
        ocr, page_no=1, canvas=[200, 200], digital_lines=digital,
    )

    assert page["diagnostics"]["digital_text_route"] == "ocr_fallback"
    assert page["regions"][0]["text"] == ocr[0]["text"].strip()
    assert page["regions"][0]["text_source"] == "ocr_fallback"


def test_normal_pdf_text_keeps_digital_primary():
    digital = [{"bbox": [0, 0, 100, 20], "text": "가입대상 만 18세 이상 개인"}]
    ocr = [{"bbox": [0, 0, 100, 20], "text": "가입대상 만 18세 이상 개인"}]

    assert not digital_text_needs_ocr(digital, ocr)
    assert not digital_text_needs_ocr([], ocr)
