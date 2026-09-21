"""한 파일에 상품이 여럿일 때 상품마다 자기 템플릿을 갖는지 검증한다.

`resolve_template`은 규칙으로 좁히지 못하면 Gemma를 부른다. 여기서는 규칙만으로
확정되는 상품군(대출성·예금성 + 상품명 노출 여부)만 써서 네트워크 없이 돈다.
"""
from nh_parser_fin.parse import templates
from nh_parser_fin.parse import semantic
from nh_parser_fin.parse import pipeline as full_pipeline

from nh_parser_fin.review.catalog import load_catalog


def _doc(products, regions, *, source_file, group, shown):
    return {
        "source_file": source_file,
        "product_group": group,
        "product_name_shown": shown,
        "pages": [{"products": products, "regions": regions}],
    }


def test_page_common_labels_are_the_intersection_of_every_template():
    catalog = load_catalog()

    shared = templates.common_gubun(catalog)

    assert shared, "모든 템플릿에 공통인 구분값이 없으면 page_common 라벨을 만들 수 없다"
    for template in catalog["templates"].values():
        gubuns = {item["gubun"] for item in template["items"]}
        assert set(shared) <= gubuns


def test_each_product_resolves_its_own_template_from_its_own_regions():
    """파일명이 카드여도 상품마다 자기 상품군의 템플릿을 받아야 한다.

    `resolve_template`은 product_group이 비면 `_infer_group`으로 내려가 파일명을
    먼저 본다. 상품별 product_group을 넘기는 것이 이 동작을 막는 유일한 방법이다.
    """
    catalog = load_catalog()
    doc = _doc(
        [
            {"product_id": "product_1", "name": "어디든대출",
             "product_group": "대출성", "product_name_shown": "노출"},
            {"product_id": "product_2", "name": "",
             "product_group": "예금성", "product_name_shown": "미노출"},
        ],
        [
            {"region_id": "r1", "product_id": "product_1",
             "lines": [{"text": "대출한도 최대 3억원 이내"}]},
            {"region_id": "r2", "product_id": "product_2",
             "lines": [{"text": "가입금액 월 100만원 이내"}]},
            {"region_id": "r3", "product_id": "page_common",
             "lines": [{"text": "준법감시인 심의필 2026-2731"}]},
        ],
        source_file="4. 카드상품.pdf", group="카드", shown="노출",
    )

    resolved = templates.resolve_product_templates(doc, catalog)

    assert resolved["product_1"]["template_id"] == "대출성상품-상품명 노출"
    assert resolved["product_2"]["template_id"] == "예금성상품-상품명 미노출"
    # 상품별 허용 라벨이 실제로 갈려야 의미가 있다.
    assert resolved["product_1"]["labels"] != resolved["product_2"]["labels"]
    # 상품이 둘이면 공통 영역에 어느 템플릿을 적용할지 모호하므로 교집합만 허용한다.
    assert resolved["page_common"]["labels"] == templates.common_gubun(catalog)
    assert resolved["page_common"]["template_id"] is None
    assert resolved["page_common"]["source"] == "catalog_intersection"


def test_single_product_keeps_the_document_classifier_judgment():
    """상품이 하나뿐이면 문서 분류기를 따른다.

    실측(2026-09-19, `14. 대출성상품.pdf`): 소유권 판정은 `미노출`이라 했지만
    문서 분류는 `노출`이었고 광고에 `NH대한민국 어디든대출`이 실제로 적혀 있었다.
    상품별 값을 무조건 우선하면 구분값이 12개에서 3개로 줄어 라벨이 거의 다 빈다.
    """
    catalog = load_catalog()
    doc = _doc(
        [{"product_id": "product_1", "name": "NH대한민국 어디든대출",
          "product_group": "대출성", "product_name_shown": "미노출"}],
        [{"region_id": "r1", "product_id": "product_1",
          "lines": [{"text": "대출한도 최대 3억원"}]}],
        source_file="14. 대출성상품.pdf", group="대출성", shown="노출",
    )

    resolved = templates.resolve_product_templates(doc, catalog)

    assert resolved["product_1"]["product_name_shown"] == "노출"
    assert resolved["product_1"]["template_id"] == "대출성상품-상품명 노출"
    assert len(resolved["product_1"]["labels"]) > len(templates.common_gubun(catalog))


def test_products_that_disagree_each_keep_their_own_judgment():
    catalog = load_catalog()
    doc = _doc(
        [
            {"product_id": "product_1", "name": "대출",
             "product_group": "대출성", "product_name_shown": "노출"},
            {"product_id": "product_2", "name": "",
             "product_group": "대출성", "product_name_shown": "미노출"},
        ],
        [
            {"region_id": "r1", "product_id": "product_1", "lines": [{"text": "대출한도"}]},
            {"region_id": "r2", "product_id": "product_2", "lines": [{"text": "대출기간"}]},
        ],
        source_file="mixed.pdf", group="대출성", shown="노출",
    )

    resolved = templates.resolve_product_templates(doc, catalog)

    assert resolved["product_1"]["template_id"] == "대출성상품-상품명 노출"
    assert resolved["product_2"]["template_id"] == "대출성상품-상품명 미노출"


def test_unknown_product_can_still_use_the_shared_common_labels():
    """소유권이 unknown으로 흘러도 회사명·심의번호는 라벨을 받을 수 있어야 한다.

    실측: 소유권 판정이 `NHCard`, `여신금융협회 심의필 …`을 page_common이 아닌
    unknown으로 보낸 적이 있다. 문서 템플릿 구분값만 허용하면 그 영역들이
    라벨 없이 남는다.
    """
    catalog = load_catalog()
    doc = _doc(
        [{"product_id": "product_1", "name": "대출",
          "product_group": "대출성", "product_name_shown": "노출"}],
        [
            {"region_id": "r1", "product_id": "product_1", "lines": [{"text": "대출한도"}]},
            {"region_id": "r2", "product_id": "unknown", "lines": [{"text": "NH농협은행"}]},
        ],
        source_file="14. 대출성상품.pdf", group="대출성", shown="노출",
    )

    resolved = templates.resolve_product_templates(doc, catalog)

    unknown = resolved["unknown"]
    assert unknown["source"] == "union_of_document_templates"
    assert set(templates.common_gubun(catalog)) <= set(unknown["labels"])
    # 문서에 등장한 상품 구분값도 모두 허용한다. 소속을 모른다는 것은 어느 상품
    # 것일 수도 있다는 뜻이라 후보를 좁힐 근거가 없다.
    assert set(resolved["product_1"]["labels"]) <= set(unknown["labels"])


def test_unknown_is_not_a_review_unit():
    """소유 상품을 모르는 영역이 실재하는 상품처럼 심의를 받으면 안 된다."""
    pages = [{
        "regions": [
            {"region_id": "r1", "product_id": "product_1"},
            {"region_id": "r2", "product_id": "unknown"},
            {"region_id": "r3", "product_id": "page_common"},
        ],
    }]
    product_templates = {
        "product_1": {"template_id": "대출성상품-상품명 노출", "labels": ["회사명"],
                      "status": "confirmed", "product_name": "대출"},
        "unknown": {"template_id": None, "labels": ["회사명"], "status": "unowned"},
        "page_common": {"template_id": None, "labels": ["회사명"], "status": "page_common"},
    }

    units = templates.review_units(pages, product_templates)

    assert [unit["product_id"] for unit in units] == ["product_1"]


def test_review_units_pair_each_product_with_the_shared_common_regions():
    pages = [{
        "regions": [
            {"region_id": "r1", "product_id": "product_1"},
            {"region_id": "r2", "product_id": "product_2"},
            {"region_id": "r3", "product_id": "page_common"},
        ],
    }]
    product_templates = {
        "product_1": {"template_id": "카드상품-상품명 미노출", "labels": ["회사명"],
                      "status": "confirmed", "product_name": "카드"},
        "product_2": {"template_id": "예금성상품-적립식", "labels": ["회사명"],
                      "status": "confirmed", "product_name": "적금"},
        "page_common": {"template_id": None, "labels": ["회사명"], "status": "page_common"},
    }

    units = templates.review_units(pages, product_templates)

    assert [unit["product_id"] for unit in units] == ["product_1", "product_2"]
    assert units[0]["region_ids"] == ["r1"]
    assert units[1]["region_ids"] == ["r2"]
    # 공통 영역은 두 상품 심의 모두의 근거이므로 양쪽에 들어간다.
    assert units[0]["page_common_region_ids"] == ["r3"]
    assert units[1]["page_common_region_ids"] == ["r3"]


def test_document_template_does_not_pick_one_when_products_differ():
    mixed = full_pipeline._document_template({
        "product_1": {"template_id": "카드상품-상품명 미노출"},
        "product_2": {"template_id": "예금성상품-적립식"},
        "page_common": {"template_id": None},
    })
    single = full_pipeline._document_template({
        "product_1": {"template_id": "카드상품-상품명 미노출", "status": "confirmed"},
        "page_common": {"template_id": None},
    })

    assert mixed["template_id"] is None
    assert mixed["status"] == "multi_product"
    assert mixed["candidates"] == ["예금성상품-적립식", "카드상품-상품명 미노출"]
    assert single["template_id"] == "카드상품-상품명 미노출"
    assert single["scope"] == "document"


def test_label_validation_drops_labels_outside_the_product_template():
    result = semantic.validate_labels(
        {
            "analysis": "",
            "region_labels": [
                {"region_id": "r1", "labels": ["대출한도"],
                 "confidence": 0.9, "reason": ""},
                {"region_id": "r2", "labels": ["연회비"],
                 "confidence": 0.9, "reason": "다른 템플릿 구분값"},
            ],
        },
        ["r1", "r2", "r3"],
        ["대출한도", "대출대상"],
    )

    by_region = {item["region_id"]: item for item in result["region_labels"]}
    assert by_region["r1"]["labels"] == ["대출한도"]
    assert by_region["r2"]["labels"] == []
    assert by_region["r3"]["reason"].endswith("검수 필요")


def test_label_pages_marks_regions_for_review_when_template_is_unresolved():
    page = {
        "page_no": 1,
        "canvas": [1000, 1500],
        "regions": [{"region_id": "r1", "product_id": "product_1", "bbox": [0, 0, 10, 10]}],
    }

    full_pipeline._label_pages([page], {"product_1": {"labels": []}}, {1: None})

    assert page["regions"][0]["semantic_labels"] == []
    assert page["regions"][0]["needs_review"] is True
    assert "미확정" in page["regions"][0]["label_decision"]["reason"]


def test_single_product_does_not_narrow_the_page_common_labels():
    """상품이 하나면 공통 영역의 라벨을 좁히지 않는다.

    좁힐 이유는 "어느 상품의 템플릿인지 모호해서"인데 상품이 하나면 모호하지
    않다. 소유권 판정의 공통/상품 경계는 실행마다 흔들리므로(실측: page_common
    수가 9→10, 6→7로 움직임) 좁히면 실제로는 상품 구분값인 글이 정답 라벨에
    닿지 못한다.
    """
    catalog = load_catalog()
    doc = _doc(
        [{"product_id": "product_1", "name": "어디든대출",
          "product_group": "대출성", "product_name_shown": "노출"}],
        [
            {"region_id": "r1", "product_id": "product_1", "lines": [{"text": "대출한도"}]},
            {"region_id": "r2", "product_id": "page_common",
             "lines": [{"text": "준법감시인 심의필 2026-2731"}]},
        ],
        source_file="14. 대출성상품.pdf", group="대출성", shown="노출",
    )

    resolved = templates.resolve_product_templates(doc, catalog)

    common = resolved["page_common"]
    assert common["source"] == "single_product_template"
    assert common["labels"] == resolved["product_1"]["labels"]
    assert set(templates.common_gubun(catalog)) <= set(common["labels"])
    assert len(common["labels"]) > len(templates.common_gubun(catalog))
