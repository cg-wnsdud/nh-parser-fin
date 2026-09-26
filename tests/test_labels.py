"""Region을 쪼개지 않고 복수 구분값을 붙이는 계약을 검증한다."""
from nh_parser_fin.parse import semantic
from nh_parser_fin.parse import pipeline as full_pipeline
from nh_parser_fin.parse import export as export_v2


def _region():
    return {
        "region_id": "p1_r001",
        "bbox": [254, 1357, 641, 1531],
        "product_id": "product_1",
        "text": "가입대상 개인\n가입금액 100만원 이상",
        "needs_review": False,
        "lines": [
            {"line_ref": "p1/p1_r001/L000", "text": "가입대상 개인"},
            {"line_ref": "p1/p1_r001/L001", "text": "가입금액 100만원 이상"},
        ],
    }


def _document(region):
    return {
        "doc_id": "d", "source_file": "s.pdf", "file_type": "pdf",
        "classification": {}, "template": {},
        "pages": [{"page_no": 1, "canvas": [1654, 2339], "regions": [region]}],
    }


def test_validate_labels_keeps_allowed_multiple_labels_and_deduplicates():
    result = semantic.validate_labels(
        {"analysis": "", "region_labels": [{
            "region_id": "p1_r001",
            "labels": ["가입대상", "가입금액", "가입대상", "연회비"],
            "confidence": 0.9,
            "reason": "두 항목이 함께 보임",
        }]},
        ["p1_r001"],
        ["가입대상", "가입금액"],
    )

    assert result["region_labels"][0]["labels"] == ["가입대상", "가입금액"]


def test_fc87_label_schema_avoids_unsupported_unique_items_constraint():
    schema = semantic._label_schema(["p1_r001"], ["가입대상", "가입금액"])
    labels = schema["properties"]["region_labels"]["items"]["properties"]["labels"]

    assert "uniqueItems" not in labels
    assert labels["maxItems"] == 2
    required = schema["properties"]["region_labels"]["items"]["required"]
    assert "evidence" in required


def test_label_evidence_must_be_an_exact_region_quote():
    result = semantic.validate_labels(
        {"analysis": "", "region_labels": [{
            "region_id": "p1_r001",
            "labels": ["가입대상", "가입금액"],
            "evidence": [
                {"label": "가입대상", "quote": "가입대상 개인"},
                {"label": "가입금액", "quote": "월 1억원 이상"},
            ],
            "confidence": 0.9,
            "reason": "",
        }]},
        ["p1_r001"],
        ["가입대상", "가입금액"],
        region_texts={"p1_r001": "가입대상 개인\n가입금액 100만원 이상"},
    )

    decision = result["region_labels"][0]
    assert decision["labels"] == ["가입대상"]
    assert decision["evidence"] == [{"label": "가입대상", "quote": "가입대상 개인"}]


def test_contradictory_vlm_reason_cannot_keep_a_label():
    result = semantic.validate_labels(
        {"analysis": "", "region_labels": [{
            "region_id": "p1_r001", "labels": ["유의사항"],
            "evidence": [{"label": "유의사항", "quote": "가입대상 개인"}],
            "confidence": 1.0, "reason": "가입조건이나 허용 라벨 없음",
        }]},
        ["p1_r001"], ["유의사항"],
        region_texts={"p1_r001": "가입대상 개인"},
    )

    assert result["region_labels"][0]["labels"] == []


def test_label_pages_attaches_multiple_labels_without_children(monkeypatch):
    page = {"page_no": 1, "regions": [_region()]}

    monkeypatch.setattr(full_pipeline, "analyze_product_labels", lambda *args, **kwargs: {
        "analysis": "두 항목",
        "region_labels": [{
            "region_id": "p1_r001", "labels": ["가입대상", "가입금액"],
            "confidence": 0.95, "reason": "",
        }],
    })
    full_pipeline._label_pages(
        [page], {"product_1": {"labels": ["가입대상", "가입금액"]}}, {1: None},
    )

    assert len(page["regions"]) == 1
    assert page["regions"][0]["region_id"] == "p1_r001"
    assert page["regions"][0]["semantic_labels"] == ["가입대상", "가입금액"]
    assert "label_spans" not in page["regions"][0]


def test_p3_keeps_one_region_with_plain_label_list_and_no_indexes():
    region = _region()
    region["semantic_labels"] = ["가입대상", "가입금액"]
    p1 = export_v2.build_p1(_document(region))
    p3 = export_v2.build_p3(p1)

    output = p3["pages"][0]["regions"][0]
    assert p3["contract"]["version"] == "nh-ad-region-review-input-v7"
    assert output["region_id"] == "p1_r001"
    assert output["selected_text"] == region["text"]
    assert output["labels"] == ["가입대상", "가입금액"]
    assert output["text_source"] == "ocr"
    assert p3["pages"][0]["canvas"] == [1654, 2339]
    assert "span_id" not in str(p3)
    assert "location_index" not in p3
    assert "label_index" not in p3
    assert "reading_order" not in str(p3)


def test_multiple_labels_do_not_create_review_by_themselves():
    region = _region()
    region["semantic_labels"] = ["가입대상", "가입금액"]

    output = export_v2.build_p3(export_v2.build_p1(_document(region)))["pages"][0]["regions"][0]

    assert output["needs_review"] is False
    assert len(output["labels"]) == 2


def test_explicit_business_term_alias_adds_the_catalog_label():
    assert semantic.add_explicit_alias_labels(
        "이자지급방식 만기일시지급식", [], ["상품명", "이자지급시기"],
    ) == ["이자지급시기"]


def test_explicit_headings_are_labeled_before_vlm_judgment():
    evidence = semantic.explicit_heading_evidence(
        "가입대상 개인(적립식)\n가입금액 월 1만원 이상\n최고금리 연 3.45%",
        ["유의사항", "가입대상", "가입금액", "금리"],
    )

    assert list(evidence) == ["가입대상", "가입금액", "금리"]
    assert evidence["금리"] == "최고금리 연 3.45%"


def test_terms_mentioned_inside_a_warning_are_not_explicit_headings():
    assert semantic.explicit_heading_evidence(
        "정부정책에 따라 대출한도와 대출금리가 변경될 수 있습니다.",
        ["대출한도", "대출금리", "유의사항"],
    ) == {}


def test_deposit_protection_and_notice_headings_are_deterministic():
    evidence = semantic.explicit_heading_evidence(
        "▶ 유의사항\n※ 예금자보호법에 따라 원금과 이자를 보호합니다.",
        ["유의사항", "예금자보호"],
    )

    assert list(evidence) == ["유의사항", "예금자보호"]


def test_short_product_title_cannot_inherit_page_labels():
    region = {
        "text": "NH올원e통장",
        "layout_observation": {"label": "paragraph_title"},
    }

    assert semantic.constrain_title_labels(
        region, ["상품명", "가입대상", "금리", "유의사항"],
    ) == ["상품명"]


def test_rate_calculation_assumptions_do_not_become_product_term_labels():
    region = {
        "text": (
            "(2026.08.04. 현재 기준금리 3.25%, 가산금리 2.76%, "
            "대출기간 2년, 산출금액 2억원, 만기일시상환, "
            "우대금리 2.70%p 적용 시)"
        ),
    }

    assert semantic.constrain_rate_calculation_labels(
        region,
        ["대출기간", "대출한도", "대출금리", "상환방법", "우대금리"],
    ) == ["대출금리"]


def test_explicit_heading_survives_rate_calculation_constraint():
    region = {
        "text": (
            "대출금리 최저 연 3.31%\n"
            "대출기간 2년\n"
            "(기준금리 3.25%, 가산금리 2.76%, 우대금리 2.70%p 적용 시)"
        ),
    }

    assert semantic.constrain_rate_calculation_labels(
        region, ["대출기간", "대출금리"],
    ) == ["대출기간", "대출금리"]
