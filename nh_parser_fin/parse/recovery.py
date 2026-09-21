"""정확한 OCR/PDF 줄 bbox로 복구 Region 후보를 만든다."""
from __future__ import annotations

from statistics import median
from typing import Any


def _height(line: dict[str, Any]) -> int:
    box = line.get("bbox") or [0, 0, 0, 0]
    return max(1, int(box[3]) - int(box[1]))


def _intersects(a: list[int], b: list[int]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _union(boxes: list[list[int]]) -> list[int]:
    return [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]


def build_recovery_candidates(
    lines: list[dict[str, Any]], *, page_no: int,
) -> list[dict[str, Any]]:
    """미배정 줄을 가까운 시각 덩어리로 묶되 bbox는 원래 줄의 합집합만 사용한다.

    의미 판정은 하지 않는다. 세로 팽창을 작게 둬 `대출대상/대출한도/대출기간`처럼
    인접한 서로 다른 행이 하나로 합쳐질 가능성을 줄인다.
    """
    usable = [line for line in lines if line.get("bbox") and str(line.get("text") or "").strip()]
    if not usable:
        return []
    line_height = float(median(_height(line) for line in usable))
    gx = max(8.0, line_height * 1.5)
    gy = max(2.0, line_height * 0.25)
    grown = []
    for line in usable:
        x0, y0, x1, y1 = [int(value) for value in line["bbox"]]
        grown.append([x0 - gx, y0 - gy, x1 + gx, y1 + gy])

    parent = list(range(len(usable)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for left in range(len(usable)):
        for right in range(left + 1, len(usable)):
            if _intersects(grown[left], grown[right]):
                parent[find(left)] = find(right)

    groups: dict[int, list[dict[str, Any]]] = {}
    for index, line in enumerate(usable):
        groups.setdefault(find(index), []).append(line)
    ordered = sorted(
        groups.values(),
        key=lambda group: (
            min(line["bbox"][1] for line in group),
            min(line["bbox"][0] for line in group),
        ),
    )

    output = []
    for index, group in enumerate(ordered, start=1):
        group.sort(key=lambda line: (line["bbox"][1], line["bbox"][0]))
        output.append({
            "candidate_id": f"p{page_no}_x{index:03d}",
            "bbox": _union([list(line["bbox"]) for line in group]),
            "bbox_source": "ocr_pdf_lines",
            "bbox_quality": "exact",
            "line_refs": [str(line["line_ref"]) for line in group],
            "lines": group,
            "text": "\n".join(str(line.get("text") or "").strip() for line in group),
            "decision": None,
        })
    return output
