"""파싱 결과를 19개 농협 광고 템플릿 중 하나로 결정한다."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..vlm import client as vlm_client
from .catalog import load_catalog


@dataclass
class TemplateResolution:
    template_id: str | None
    status: str
    source: str
    confidence: float | None = None
    reason: str = ""
    candidates: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _document_text(doc: dict[str, Any], limit: int = 6000) -> str:
    values: list[str] = []
    for page in doc.get("pages") or []:
        for region in page.get("regions") or []:
            values.extend(str(line.get("text") or "") for line in region.get("lines") or [])
        values.extend(str(line.get("text") or "") for line in page.get("unassigned_lines") or [])
    return "\n".join(value for value in values if value).strip()[:limit]


def _infer_group(filename: str, text: str) -> str | None:
    value = f"{filename}\n{text}".casefold()
    groups = [
        ("투자성", ("투자성", "isa", "개인종합자산관리계좌", "irp", "퇴직연금", "펀드", "집합투자")),
        ("카드", ("카드상품", "nhcard", "농협카드", "카드대출")),
        ("대출성", ("대출성", "대출상품", "대출한도", "대출금리")),
        ("예금성", ("예금성", "예금상품", "적금", "입출금")),
    ]
    for group, hints in groups:
        if any(hint in value for hint in hints):
            return group
    return None


def _rule_choice(
    product_group: str | None,
    product_name_shown: str | None,
    haystack: str,
    known: set[str],
) -> tuple[str | None, str]:
    def available(template_id: str) -> str | None:
        return template_id if template_id in known else None

    value = haystack.casefold()
    if product_group == "대출성":
        if "대출모집" in value or "잔금대출" in value:
            return available("대출성상품-대출모집인(잔금대출)"), "대출모집인/잔금대출 명시"
        if product_name_shown == "노출":
            return available("대출성상품-상품명 노출"), "분류 결과에서 상품명 노출"
        if product_name_shown == "미노출":
            return available("대출성상품-상품명 미노출"), "분류 결과에서 상품명 미노출"

    if product_group == "예금성":
        if product_name_shown == "미노출":
            return available("예금성상품-상품명 미노출"), "분류 결과에서 상품명 미노출"
        hints = [
            ("예금성상품-지수연동예금", ("지수연동", "eld")),
            ("예금성상품-적립식", ("적립식", "적금")),
            ("예금성상품-거치식", ("거치식", "정기예금")),
            ("예금성상품-입출식", ("입출식", "입출금", "요구불")),
        ]
        matched = [template_id for template_id, words in hints if any(word in value for word in words)]
        if len(matched) == 1:
            return available(matched[0]), f"문서의 예금 세부유형 힌트: {matched[0]}"

    if product_group == "카드":
        if any(word in value for word in ("장기카드대출", "단기카드대출", "카드론")):
            return available("카드상품-장·단기카드대출"), "장·단기카드대출 명시"
        if product_name_shown == "미노출":
            return available("카드상품-상품명 미노출"), "분류 결과에서 상품명 미노출"
        if product_name_shown == "노출" and "법인카드" in value:
            return available("카드상품-상품명(법인) 노출"), "상품명 노출 및 법인카드 명시"
        if product_name_shown == "노출" and "개인카드" in value:
            return available("카드상품-상품명(개인) 노출"), "상품명 노출 및 개인카드 명시"

    if product_group == "투자성":
        if "isa" in value or "개인종합자산관리계좌" in value:
            if "신탁형" in value and "일임형" not in value:
                return available("투자성상품-개인종합자산관리계좌(ISA) 신탁형"), "ISA 신탁형 명시"
            if "일임형" in value and "신탁형" not in value:
                return available("투자성상품-개인종합자산관리계좌(ISA) 일임형"), "ISA 일임형 명시"
            return available("투자성상품-개인종합자산관리계좌(ISA) 일반"), "ISA 유형은 확인되나 단일 운용형 미확정"
        if "퇴직연금" in value or "irp" in value:
            if any(word in value for word in ("펀드상품", "집합투자증권", "펀드 노출")):
                return available("투자성상품-퇴직연금(IRP) 펀드상품 노출"), "퇴직연금과 펀드상품 노출"
            if "금융투자상품 미노출" in value:
                return available("투자성상품-퇴직연금(IRP) 금융투자상품 미노출"), "IRP 금융투자상품 미노출 명시"
            return available("투자성상품-퇴직연금 일반"), "퇴직연금 일반 광고"
        if "펀드" in value or "집합투자" in value:
            return available("투자성상품-펀드"), "펀드/집합투자상품 명시"
    return None, "결정론적 규칙만으로 세부 템플릿을 확정하지 못함"


def _candidate_ids(
    catalog: dict[str, Any], product_group: str | None,
) -> list[str]:
    values = [
        template_id
        for template_id, template in catalog["templates"].items()
        if product_group is None or template["product_group"] == product_group
    ]
    return values


def _candidate_description(catalog: dict[str, Any], candidate_ids: list[str]) -> str:
    rows = []
    for template_id in candidate_ids:
        gubuns = ", ".join(item["gubun"] for item in catalog["templates"][template_id]["items"])
        rows.append(f"- {template_id}: 구분값={gubuns}")
    return "\n".join(rows)


def resolve_template(
    doc: dict[str, Any], catalog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """규칙으로 후보를 좁힌 뒤 필요할 때만 VLM으로 하나를 선택한다."""
    active = catalog or load_catalog()
    known = set(active["templates"])
    filename = str(doc.get("source_file") or "")
    text = _document_text(doc)
    group = doc.get("product_group") or _infer_group(filename, text)
    shown = doc.get("product_name_shown")
    candidates = _candidate_ids(active, group)

    choice, rule_reason = _rule_choice(group, shown, f"{filename}\n{text}", known)
    if choice:
        return TemplateResolution(
            template_id=choice,
            status="confirmed",
            source="rules",
            confidence=1.0,
            reason=rule_reason,
            candidates=[choice],
        ).as_dict()
    if not candidates:
        return TemplateResolution(
            template_id=None,
            status="unresolved",
            source="none",
            reason=f"상품군 {group!r}에 해당하는 템플릿이 없음",
        ).as_dict()
    if len(candidates) == 1:
        return TemplateResolution(
            template_id=candidates[0], status="confirmed", source="rules",
            confidence=1.0, reason="상품군 후보가 하나뿐임", candidates=candidates,
        ).as_dict()

    schema = {
        "type": "object",
        "properties": {
            "analysis": {"type": "string"},
            "template_id": {"type": "string", "enum": [*candidates, "판단불가"]},
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
        },
        "required": ["analysis", "template_id", "confidence", "reason"],
        "additionalProperties": False,
    }
    prompt = f"""당신은 금융상품 광고 템플릿 선택기입니다.
문서에 실제로 광고된 상품과 상품명 노출 여부를 근거로 후보 중 정확히 하나를 고르세요.
확인할 수 없으면 판단불가를 고르세요. 구분값이 광고에 모두 보이지 않는다는 이유만으로
템플릿을 배제하지 마세요. 구분값은 광고가 따라야 하는 항목 목록입니다.

파일명: {filename}
사전 분류: product_group={group!r}, product_name_shown={shown!r}
규칙 판정: {rule_reason}

후보:
{_candidate_description(active, candidates)}

광고 텍스트:
{text}
"""
    try:
        result = vlm_client.chat_json(
            [{"type": "text", "text": prompt}],
            schema_name="ad_template_resolution",
            schema=schema,
            max_tokens=900,
        )
    except Exception as exc:  # 외부 장애는 P1에 근거를 남기고 파싱 원문을 보존한다
        return TemplateResolution(
            template_id=None,
            status="unresolved",
            source="vlm_failed",
            reason=f"템플릿 VLM 선택 실패: {exc}",
            candidates=candidates,
        ).as_dict()

    selected = str(result.get("template_id") or "판단불가")
    confidence = float(result.get("confidence") or 0.0)
    if selected == "판단불가" or selected not in candidates:
        return TemplateResolution(
            template_id=None,
            status="unresolved",
            source="vlm",
            confidence=confidence,
            reason=str(result.get("reason") or "VLM 판단불가"),
            candidates=candidates,
        ).as_dict()
    return TemplateResolution(
        template_id=selected,
        status="confirmed" if confidence >= 0.70 else "needs_review",
        source="vlm",
        confidence=confidence,
        reason=str(result.get("reason") or ""),
        candidates=candidates,
    ).as_dict()
