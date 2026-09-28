"""PDF 텍스트 추출과 스타일 근거에 실제로 쓰이는 최소 모델.

P1/P3 Region 계약은 ``parse.export``가 dict에서 생성한다. 이전 파이프라인의
AdPage/Region/표 셀 모델은 현재 실행 경로에서 사용하지 않아 제거했다.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Source = Literal["digital", "ocr"]
SizeBasis = Literal["declared", "fontbox"]


class TextStyle(BaseModel):
    """글자 크기·굵기·색의 원본 근거. PDF와 HWP는 크기 기준을 구분한다."""

    size_pt: float | None = None
    size_pt_min: float | None = None
    size_pt_max: float | None = None
    size_basis: SizeBasis | None = None
    bold: bool | None = None
    font_weight: int | None = None
    color: str | None = None
    colors: list[str] = Field(default_factory=list)
    font: str | None = None
    style_source: str | None = None


class Line(BaseModel):
    """PDF 디지털 텍스트의 줄과 렌더 페이지 기준 좌표."""

    text: str
    bbox: list[int] | None = None
    confidence: float | None = None
    source: Source
    style: TextStyle | None = None
