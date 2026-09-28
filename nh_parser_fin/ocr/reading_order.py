"""PDF 디지털 텍스트 줄을 행 단위로 정렬한다.

타일 분할과 OCR 중복 제거는 각각 ``ocr.tiling``과 ``parse.adapters``가 담당한다.
"""
from __future__ import annotations

from ..ir import Line


def sort_reading_order(lines: list[Line]) -> list[Line]:
    """수직으로 절반 이상 겹치는 줄을 같은 행으로 보고 행 안에서는 왼쪽부터 읽는다."""
    boxed = sorted((line for line in lines if line.bbox), key=lambda line: line.bbox[1])
    unboxed = [line for line in lines if not line.bbox]
    rows: list[list[Line]] = []
    for line in boxed:
        if rows:
            row = rows[-1]
            top = min(item.bbox[1] for item in row)
            bottom = max(item.bbox[3] for item in row)
            overlap = min(bottom, line.bbox[3]) - max(top, line.bbox[1])
            height = min(line.bbox[3] - line.bbox[1], bottom - top)
            if height > 0 and overlap / height >= 0.5:
                row.append(line)
                continue
        rows.append([line])
    output: list[Line] = []
    for row in rows:
        row.sort(key=lambda line: line.bbox[0])
        output.extend(row)
    return output + unboxed
