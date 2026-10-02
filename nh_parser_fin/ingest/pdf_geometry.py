"""PDF 페이지 객체의 좌표를 페이지 좌표로 맞춘다.

pdfium 은 Form XObject 안에 든 객체의 ``get_bounds()``·``get_matrix()`` 를 **폼 좌표**로
돌려준다. 은행이 만든 샘플 PDF 는 글자·그림이 전부 최상위(level 0)라 문제가 없었지만,
Chromium 이 인쇄한 HWP 렌더 PDF 는 본문 전체를 폼 하나(행렬 0.24, -0.24 — 축소와 y
뒤집기)에 넣는다. 실측(2026-10-02, `004-예금성` 렌더): 글자 870개 중 869개가 자기 TEXT
런 상자 밖에 있었다. 그 결과 소속 없는 공백이 엉뚱한 줄에 붙어 줄 상자가 페이지 높이만큼
늘어났고, 그 줄들이 묶인 복구 후보가 페이지 전체 영역이 됐다.
"""
from __future__ import annotations

import pypdfium2 as pdfium


def page_matrix(obj) -> pdfium.PdfMatrix | None:
    """객체 좌표 → 페이지 좌표 행렬. 최상위 객체면 None (변환 없음)."""
    matrix = None
    form = getattr(obj, "container", None)
    while form is not None:
        step = form.get_matrix()
        # pdfium 행렬은 행벡터 규약이다: 안쪽 폼을 먼저, 바깥 폼을 나중에 적용한다.
        matrix = step if matrix is None else matrix.multiply(step)
        form = getattr(form, "container", None)
    return matrix


def page_bounds(obj) -> tuple[float, float, float, float]:
    """``get_bounds()`` 를 페이지 좌표로. (left, bottom, right, top) PDF pt."""
    bounds = obj.get_bounds()
    matrix = page_matrix(obj)
    return bounds if matrix is None else matrix.on_rect(*bounds)


def page_text_matrix(obj) -> pdfium.PdfMatrix:
    """TEXT 객체의 글자 공간 → 페이지 좌표 행렬."""
    text = obj.get_matrix()
    matrix = page_matrix(obj)
    return text if matrix is None else text.multiply(matrix)
