from nh_parser_fin.parse import recovery
from nh_parser_fin.parse import semantic
from nh_parser_fin.parse import export as export_v2
from nh_parser_fin.parse import pipeline as full_pipeline
from nh_parser_fin.parse.ids import normalize_region_ids


def test_recovery_candidates_keep_exact_line_union_and_separate_rows():
    lines = [
        {"line_ref": "a", "bbox": [100, 100, 300, 120], "text": "첫 줄"},
        {"line_ref": "b", "bbox": [100, 125, 500, 145], "text": "같은 행 설명"},
        {"line_ref": "c", "bbox": [100, 180, 400, 200], "text": "다음 항목"},
    ]

    candidates = recovery.build_recovery_candidates(lines, page_no=1)

    assert len(candidates) == 2
    assert candidates[0]["bbox"] == [100, 100, 500, 145]
    assert candidates[0]["line_refs"] == ["a", "b"]
    assert candidates[1]["bbox"] == [100, 180, 400, 200]
    assert all(item["bbox_quality"] == "exact" for item in candidates)


def test_semantic_validation_fills_missing_ids_without_inventing_bbox():
    result = semantic.validate_ownership(
        {
            "analysis": "판정",
            "products": [],
            "region_decisions": [],
            "recovery_decisions": [],
            "table_areas": [],
            "missing_visible_text": [],
        },
        ["p1_r001"],
        ["p1_x001"],
    )

    assert result["region_decisions"][0]["region_id"] == "p1_r001"
    assert result["region_decisions"][0]["product_id"] == "unknown"
    assert result["recovery_decisions"][0]["action"] == "needs_review"
    # 소유권 단계는 라벨을 만들지 않는다. 라벨은 상품 템플릿이 정해진 뒤에 붙는다.
    assert "label" not in result["region_decisions"][0]


def test_p3_preserves_region_and_unassigned_bboxes_for_highlighting():
    document = {
        "doc_id": "sample",
        "source_file": "sample.pdf",
        "file_type": "pdf",
        "classification": {"product_group": "대출성"},
        "template": {"template_id": "대출성상품-상품명 노출"},
        "pages": [{
            "page_no": 1,
            "canvas": [1000, 1500],
            "products": [{"product_id": "product_1", "name": "샘플대출"}],
            "regions": [{
                "region_id": "p1_x001",
                "bbox": [100, 200, 900, 400],
                "text": "대출대상 직장인",
                "text_source": "ocr_pdf_recovery",
                "product_id": "product_1",
                "semantic_labels": ["대출대상"],
                "bbox_source": "ocr_pdf_lines",
                "bbox_quality": "exact",
                "origin": "recovery",
                "needs_review": False,
                "lines": [{"line_ref": "p1/unassigned/L001", "text": "대출대상 직장인"}],
            }],
            "unassigned_lines": [{
                "line_ref": "p1/unassigned/L002",
                "bbox": [100, 500, 500, 530],
                "text": "확인 필요 문구",
                "source": "digital",
            }],
        }],
    }

    normalize_region_ids(document["pages"])
    p3 = export_v2.build_p3(export_v2.build_p1(document))

    region = p3["pages"][0]["regions"][0]
    assert region["region_id"] == "p1_r001"
    assert region["bbox"] == [100, 200, 900, 400]
    assert region["product_id"] == "product_1"
    assert region["labels"] == ["대출대상"]
    assert region["selected_text"] == "대출대상 직장인"
    assert p3["pages"][0]["canvas"] == [1000, 1500]
    assert "location_index" not in p3
    assert "label_index" not in p3
    assert "unassigned_text" not in p3["pages"][0]
    assert p3["review_result_contract"]["required_fields"] == [
        "result", "reason", "region_ids",
    ]


def test_decorative_vlm_decision_never_deletes_ocr_text_or_bbox():
    page = {
        "page_no": 1,
        "canvas": [1000, 1500],
        "regions": [],
        "unassigned_lines": [{
            "line_ref": "p1/unassigned/L001",
            "bbox": [100, 200, 300, 230],
            "text": "보장금액",
            "source": "digital",
        }],
        "recovery_candidates": [{
            "candidate_id": "p1_x001",
            "bbox": [100, 200, 300, 230],
            "bbox_source": "ocr_pdf_lines",
            "bbox_quality": "exact",
            "text": "보장금액",
            "lines": [{
                "line_ref": "p1/unassigned/L001",
                "bbox": [100, 200, 300, 230],
                "text": "보장금액",
                "source": "digital",
            }],
        }],
    }
    result = {
        "analysis": "표 머리글을 장식으로 오판",
        "products": [],
        "region_decisions": [],
        "recovery_decisions": [{
            "candidate_id": "p1_x001",
            "action": "decorative",
            "target_region_id": "",
            "product_id": "product_1",
            "confidence": 1.0,
            "reason": "table header",
        }],
        "table_areas": [],
        "missing_visible_text": [],
    }

    full_pipeline._apply_ownership(page, result)

    assert page["unassigned_lines"] == []
    assert page["regions"][0]["text"] == "보장금액"
    assert page["regions"][0]["bbox"] == [100, 200, 300, 230]
    assert page["regions"][0]["vlm_excluded"] is True
    # 장식 판정만으로는 검수를 켜지 않는다. 판정이 끝난 영역이라 사람에게 물을
    # 것이 없고, 켜 두면 검수 목록이 정상 동작으로 가득 찬다(실측 2026-09-21:
    # 26건 검수 170 중 152가 라벨 없음, 그중 7이 장식 판정). 오판이어도 텍스트와
    # bbox 는 위에서 보듯 그대로 남고 `vlm_excluded` 로 따로 걸러낼 수 있다.
    assert page["regions"][0]["needs_review"] is False


def test_reading_order_keeps_engine_order_and_places_context_after_target():
    page = {
        "regions": [
            {"region_id": "r1", "origin": "paddlex", "product_id": "product_1"},
            {"region_id": "r2", "origin": "paddlex", "product_id": "product_2"},
            {
                "region_id": "x1", "origin": "recovery", "product_id": "product_1",
                "related_region_id": "r1", "bbox": [0, 20, 100, 30],
            },
            {
                "region_id": "x2", "origin": "recovery", "product_id": "product_1",
                "related_region_id": "r1", "bbox": [0, 10, 100, 15],
            },
        ],
    }

    full_pipeline._assign_reading_order(page)

    assert [region["region_id"] for region in page["regions"]] == ["r1", "x2", "x1", "r2"]
    assert [region["reading_order"] for region in page["regions"]] == [1, 2, 3, 4]
    assert [region["product_reading_order"] for region in page["regions"]] == [1, 2, 3, 1]


def test_long_page_semantic_bands_assign_every_id_once():
    page = {
        "canvas": [1000, 3000],
        "tiling": {"decision": "split", "axis": "y", "pieces": 3},
        "regions": [
            {"region_id": "r1", "bbox": [0, 100, 500, 200]},
            {"region_id": "r2", "bbox": [0, 1200, 500, 1300]},
            {"region_id": "r3", "bbox": [0, 2600, 500, 2700]},
        ],
    }
    candidates = [
        {"candidate_id": "x1", "bbox": [0, 980, 500, 1020]},
        {"candidate_id": "x2", "bbox": [0, 1980, 500, 2020]},
    ]

    bands = semantic.partition_for_semantic_bands(page, candidates)
    region_ids = [item["region_id"] for band in bands for item in band["regions"]]
    candidate_ids = [
        item["candidate_id"] for band in bands for item in band["candidates"]
    ]

    assert len(bands) == 3
    assert region_ids == ["r1", "r2", "r3"]
    assert candidate_ids == ["x1", "x2"]
    assert len(region_ids) == len(set(region_ids))
    assert len(candidate_ids) == len(set(candidate_ids))
