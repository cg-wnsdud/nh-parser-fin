"""P1에 파싱 품질 경고를 일관된 코드로 남긴다.

``needs_review``는 광고 심의 결과가 아니다. OCR/PDF/VLM 조립 결과를 그대로
신뢰하기 어려워 사람이 원문과 대조해야 한다는 파싱 품질 신호다. P3에는 boolean만
보내고, 상세 사유는 같은 ``region_id``의 P1 ``review_reasons``에서 확인한다.
"""
from __future__ import annotations

from typing import Any


def flag(region: dict[str, Any], code: str) -> None:
    """Region을 검수 대상으로 표시하고 중복 없는 사유 코드를 남긴다."""
    region["needs_review"] = True
    reasons = region.setdefault("review_reasons", [])
    if code not in reasons:
        reasons.append(code)
