"""상품별 템플릿 결정.

한 파일에 상품이 여럿이면 상품마다 따르는 광고 템플릿이 다르다. 문서 하나에
템플릿 하나를 쓰면 `4. 카드상품.pdf` 안의 예금 상품에는 맞는 구분값이 아예 없다.

`resolve_template`은 `product_group`, `product_name_shown`, 그리고 Region의 줄
텍스트만 본다. Region을 `product_id`로 걸러 같은 함수에 다시 넣으면 상품별
판정이 그대로 된다 — 새 판정기를 만들지 않는다.
"""
from __future__ import annotations

from typing import Any

from ..review.resolution import resolve_template

PAGE_COMMON = "page_common"
UNKNOWN = "unknown"


def common_gubun(catalog: dict[str, Any]) -> list[str]:
    """19개 템플릿 **전부**에 들어 있는 구분값.

    페이지 공통 영역(회사명·유의사항·심의번호)은 어느 상품에도 속하지 않으므로
    특정 템플릿의 구분값을 쓸 수 없다. 교집합을 직접 계산해 카탈로그가 바뀌어도
    따라가게 한다.
    """
    templates = catalog.get("templates") or {}
    if not templates:
        return []
    sets = [{item["gubun"] for item in template["items"]} for template in templates.values()]
    shared = set.intersection(*sets)
    # 프롬프트가 실행마다 흔들리지 않도록 카탈로그 정의 순서를 유지한다.
    first = next(iter(templates.values()))
    return [item["gubun"] for item in first["items"] if item["gubun"] in shared]


def template_labels(catalog: dict[str, Any], template_id: str | None) -> list[str]:
    if not template_id:
        return []
    template = (catalog.get("templates") or {}).get(template_id)
    if not template:
        return []
    return [item["gubun"] for item in template["items"]]


def _product_meta(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """페이지 소유권 판정이 돌려준 상품 정보를 product_id로 모은다."""
    output: dict[str, dict[str, Any]] = {}
    for page in doc.get("pages") or []:
        for product in page.get("products") or []:
            product_id = str(product.get("product_id") or "")
            if product_id and product_id not in output:
                output[product_id] = product
    return output


def _regions_by_product(doc: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """문서 전체에서 상품별 Region을 모은다.

    같은 `product_1`이 여러 페이지에 걸쳐 있어도 템플릿은 하나여야 하므로
    페이지가 아니라 문서 단위로 모은다.
    """
    output: dict[str, list[dict[str, Any]]] = {}
    for page in doc.get("pages") or []:
        for region in page.get("regions") or []:
            product_id = str(region.get("product_id") or UNKNOWN)
            output.setdefault(product_id, []).append(region)
    return output


def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or text == "판단불가":
        return None
    return text


def _per_product_value(
    field: str, meta: dict[str, dict[str, Any]], doc_value: str | None,
) -> dict[str, str | None]:
    """상품별 값은 **상품끼리 갈릴 때만** 쓰고, 그 외에는 문서 분류를 따른다.

    문서 분류기(`vlm.client.classify`)는 이 판정 하나만 보는 전용 프롬프트라
    더 정확하다. 소유권 판정은 한 응답에서 다섯 가지를 동시에 하므로 이런
    세부 항목에서 틀리기 쉽다.

    실측(2026-09-19, `14. 대출성상품.pdf`): 문서 분류는 `product_name_shown=노출`,
    소유권 판정은 `미노출`이었다. 광고에 `NH대한민국 어디든대출`이라는 상품명이
    실제로 적혀 있으므로 문서 분류가 맞았다. 상품별 값을 무조건 우선하면
    템플릿이 `상품명 미노출`로 바뀌어 구분값이 12개에서 3개로 줄어든다.
    """
    values = {_clean(product.get(field)) for product in meta.values()}
    values.discard(None)
    if len(values) <= 1 and doc_value:
        return {product_id: doc_value for product_id in meta}
    return {
        product_id: _clean(product.get(field)) or doc_value
        for product_id, product in meta.items()
    }


def resolve_product_templates(
    doc: dict[str, Any], catalog: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """상품마다 템플릿과 허용 라벨을 정한다.

    `product_group`을 **반드시** 채워 넣는다. 비워두면 `resolve_template`이
    `_infer_group`으로 내려가 **파일명을 먼저** 보기 때문에, `4. 카드상품.pdf`
    안의 예금 상품도 "카드"로 추론된다. 그래서 소유권 판정이 상품별 상품군과
    상품명 노출 여부를 함께 돌려주고 그 값을 여기서 그대로 쓴다.
    """
    meta = _product_meta(doc)
    shared = common_gubun(catalog)
    doc_group = _clean(doc.get("product_group"))
    doc_shown = _clean(doc.get("product_name_shown"))
    group_of = _per_product_value("product_group", meta, doc_group)
    shown_of = _per_product_value("product_name_shown", meta, doc_shown)
    output: dict[str, dict[str, Any]] = {}

    by_product = _regions_by_product(doc)
    for product_id, regions in by_product.items():
        # 둘 다 특정 상품에 속하지 않아 어느 상품 텍스트로 템플릿을 고를 수 없다.
        # 상품 판정이 끝난 뒤에 정한다.
        if product_id in (PAGE_COMMON, UNKNOWN):
            continue

        product = meta.get(product_id) or {}
        group = group_of.get(product_id) or doc_group
        shown = shown_of.get(product_id) or doc_shown
        product_doc = {
            "source_file": doc.get("source_file"),
            "product_group": group,
            "product_name_shown": shown,
            # `_document_text`가 Region의 줄을 긁어모으므로 상품 Region만 넘기면
            # 그 상품의 텍스트만 템플릿 판정 근거가 된다.
            "pages": [{"regions": regions, "unassigned_lines": []}],
        }
        resolution = dict(resolve_template(product_doc, catalog))
        resolution["labels"] = template_labels(catalog, resolution.get("template_id"))
        resolution["product_group"] = group
        resolution["product_name_shown"] = shown
        resolution["product_name"] = product.get("name")
        resolution["region_count"] = len(regions)
        output[product_id] = resolution

    if PAGE_COMMON in by_product:
        output[PAGE_COMMON] = _page_common_resolution(
            output, shared, region_count=len(by_product[PAGE_COMMON]),
        )
    if UNKNOWN in by_product:
        output[UNKNOWN] = _unknown_resolution(
            output, shared, region_count=len(by_product[UNKNOWN]),
        )
    return output


def _unknown_resolution(
    resolved: dict[str, dict[str, Any]], shared: list[str], *, region_count: int,
) -> dict[str, Any]:
    """소유권을 확정하지 못한 Region의 허용 라벨.

    이 Region 하나만 모아 `resolve_template`을 부르면 근거가 한두 줄뿐이라
    거의 항상 실패하고(실측 2026-09-20: 3건 모두 `판단불가`), 라벨이 통째로
    비면서 문서당 템플릿 판정 호출만 한 번 더 나간다.

    소속을 모른다는 것은 어느 상품 것일 수도 있다는 뜻이다. 후보를 좁힐 근거가
    없으므로 이 문서에 등장한 모든 템플릿의 구분값과 공통 구분값을 합쳐 준다.
    잘못된 라벨이 붙을 위험보다, 라벨이 아예 없어 검수 목록만 늘어나는 쪽이 나쁘다.
    """
    labels: list[str] = []
    for product_id, item in resolved.items():
        if product_id in (PAGE_COMMON, UNKNOWN):
            continue
        labels += [gubun for gubun in item.get("labels") or [] if gubun not in labels]
    labels += [gubun for gubun in shared if gubun not in labels]
    return {
        "template_id": None,
        "status": "unowned",
        "source": "union_of_document_templates",
        "confidence": 0.0,
        "reason": "소유 상품을 확정하지 못해 문서에 등장한 구분값을 모두 허용",
        "candidates": [],
        "labels": labels,
        "product_group": None,
        "product_name_shown": None,
        "region_count": region_count,
    }


def _page_common_resolution(
    resolved: dict[str, dict[str, Any]], shared: list[str], *, region_count: int,
) -> dict[str, Any]:
    """페이지 공통 영역의 허용 라벨을 정한다.

    상품이 둘 이상이면 어느 상품의 템플릿을 적용할지 모호하므로 19개 템플릿의
    교집합(회사명·유의사항·심의번호)만 허용한다.

    상품이 하나면 모호하지 않으므로 좁히지 않는다. 소유권 판정의 공통/상품 경계는
    실행마다 흔들린다(실측 2026-09-19: `4. 카드상품`에서 공통 영역 10건이 unknown으로
    샜고, 프롬프트 수정 뒤 page_common 수가 9→10, 6→7로 움직였다). 흔들리는 신호로
    enum을 좁히면 실제로는 `대출한도`인 글이 공통으로 분류됐을 때 정답 라벨이
    목록에 없어 영원히 미배정으로 남는다.
    """
    products = {
        product_id: item for product_id, item in resolved.items()
        if product_id not in (PAGE_COMMON, UNKNOWN) and item.get("labels")
    }
    if len(products) == 1:
        only = next(iter(products.values()))
        labels = list(only["labels"])
        labels += [gubun for gubun in shared if gubun not in labels]
        return {
            "template_id": only.get("template_id"),
            "status": "page_common",
            "source": "single_product_template",
            "confidence": 1.0,
            "reason": "상품이 하나뿐이라 그 상품의 구분값을 그대로 허용",
            "candidates": [only.get("template_id")] if only.get("template_id") else [],
            "labels": labels,
            "product_group": None,
            "product_name_shown": None,
            "region_count": region_count,
        }
    return {
        "template_id": None,
        "status": "page_common",
        "source": "catalog_intersection",
        "confidence": 1.0,
        "reason": (
            f"상품 {len(products)}종이라 어느 템플릿인지 모호함. 공통 구분값만 허용"
        ),
        "candidates": [],
        "labels": list(shared),
        "product_group": None,
        "product_name_shown": None,
        "region_count": region_count,
    }


def review_units(
    pages: list[dict[str, Any]], product_templates: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """심의 호출 단위를 미리 묶어 P3에 싣는다.

    한 상품을 심의할 때 근거는 그 상품의 Region + 페이지 공통 Region이다.
    공통 영역(유의사항·심의번호)은 두 상품 모두의 심의 근거이므로 양쪽에
    중복으로 들어가는 것이 맞다.
    """
    common_ids = [
        str(region["region_id"])
        for page in pages
        for region in page.get("regions") or []
        if str(region.get("product_id")) == PAGE_COMMON
    ]
    units = []
    for product_id, resolution in product_templates.items():
        # `unknown`은 상품이 아니다. 심의 단위로 만들면 소속을 모르는 영역이
        # 실재하는 상품인 것처럼 별도 심의를 받게 된다. P3에는 `unowned_region_ids`로
        # 따로 실어 검수 대상임을 드러낸다.
        if product_id in (PAGE_COMMON, UNKNOWN):
            continue
        region_ids = [
            str(region["region_id"])
            for page in pages
            for region in page.get("regions") or []
            if str(region.get("product_id")) == product_id
        ]
        units.append({
            "unit_id": f"review_{product_id}",
            "product_id": product_id,
            "product_name": resolution.get("product_name"),
            "template_id": resolution.get("template_id"),
            "template_status": resolution.get("status"),
            "allowed_labels": list(resolution.get("labels") or []),
            "region_ids": region_ids,
            "page_common_region_ids": common_ids,
        })
    return units
