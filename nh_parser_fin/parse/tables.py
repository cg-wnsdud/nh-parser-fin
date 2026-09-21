"""표 구조 복원 — 찾기는 기하학과 VLM, 좌표는 OCR.

PaddleX의 표 판정을 정본으로 쓰지 않는다. 51페이지 실측(2026-09-19)에서 격자형
Region 29개 중 **13개가 `table`이 아닌 라벨**(text 10 / doc_title 1 / image 2)이었고,
`14. 대출성상품`의 부가서비스 표처럼 아예 검출되지 않는 경우도 있다. 그런 표는
OCR 줄만 남아 낱개 영역으로 흩어진다.

그래서 세 곳을 모두 훑는다.

    A. PaddleX가 table로 잡은 Region        → 다시 읽는다
    B. 아무 Region에도 못 들어간 줄          → 격자면 하나로 묶는다
    C. table이 아닌 라벨이 붙은 격자형 Region → 다시 읽는다

VLM은 **어느 Region 이 한 표인지 고르고 셀을 배치**할 뿐이다. 좌표는 배치된 OCR
줄의 합집합이고 텍스트도 OCR 것이다. 모델이 좌표나 문구를 새로 만들 자리가 없다.
"""
from __future__ import annotations

import copy
import re
from statistics import median
from typing import Any

from PIL import Image, ImageDraw

from ..vlm import client as vlm_client

# 한 행으로 볼 세로 허용 오차(줄 높이 대비)와 격자 판정 기준.
ROW_TOLERANCE = 0.6
MIN_ROWS = 2
MIN_COLS = 3
# 행 사이가 이보다 벌어지면 다른 덩어리로 본다. 표 두 개가 위아래로 붙어 있어도
# 하나로 합치지 않는다.
ROW_GAP_FACTOR = 3.0
# 이보다 줄이 적으면 표로 보지 않는다. 항목명 두세 개가 우연히 줄맞춤된 경우를 거른다.
MIN_LINES = 5
# crop 에 주는 여백. 표 테두리와 머리글이 잘리면 모델이 열을 못 센다.
CROP_PAD = 24
NOTE_START = re.compile(r"^\s*(?:주\s*\d+\s*\)|※)")


def _bbox(line: dict[str, Any]) -> list[int]:
    return [int(value) for value in line["bbox"]]


def _height(line: dict[str, Any]) -> int:
    box = _bbox(line)
    return max(6, box[3] - box[1])


def _union(boxes: list[list[int]]) -> list[int]:
    return [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]


def row_bands(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """줄을 같은 시각 행으로 묶는다. 각 행 안에서는 좌→우로 정렬한다."""
    usable = [line for line in lines if line.get("bbox")]
    items = sorted(usable, key=lambda line: ((_bbox(line)[1] + _bbox(line)[3]) / 2, _bbox(line)[0]))
    bands: list[dict[str, Any]] = []
    for line in items:
        box = _bbox(line)
        center = (box[1] + box[3]) / 2
        if bands and center - bands[-1]["y"] <= _height(line) * ROW_TOLERANCE:
            bands[-1]["lines"].append(line)
        else:
            bands.append({"y": center, "lines": [line]})
    for band in bands:
        band["lines"].sort(key=lambda line: _bbox(line)[0])
    return bands


def looks_like_grid(lines: list[dict[str, Any]]) -> bool:
    """행·열로 읽어야 하는 덩어리인지 판정한다.

    기준은 실측에 쓴 것과 같다 — 열이 `MIN_COLS`개 이상인 행이 `MIN_ROWS`개 이상.
    """
    if len([line for line in lines if line.get("bbox")]) < MIN_LINES:
        return False
    wide = [band for band in row_bands(lines) if len(band["lines"]) >= MIN_COLS]
    return len(wide) >= MIN_ROWS


def find_grids(lines: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """줄 무더기에서 격자로 읽히는 구간만 잘라낸다.

    표가 아닌 줄까지 한 덩어리로 삼키지 않도록, 열이 좁은 행에서 끊고 행 간격이
    크게 벌어지는 곳에서도 끊는다.
    """
    usable = [line for line in lines if line.get("bbox") and str(line.get("text") or "").strip()]
    if len(usable) < MIN_LINES:
        return []
    line_height = float(median(_height(line) for line in usable))
    runs: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []

    def flush() -> None:
        if len(current) >= MIN_ROWS and sum(
            1 for band in current if len(band["lines"]) >= MIN_COLS
        ) >= MIN_ROWS:
            runs.append(list(current))
        current.clear()

    for band in row_bands(usable):
        # 열이 하나뿐인 행은 표가 아니라 문단이다. 여기서 끊는다.
        if len(band["lines"]) < 2:
            flush()
            continue
        if current and band["y"] - current[-1]["y"] > line_height * ROW_GAP_FACTOR:
            flush()
        current.append(band)
    flush()

    output = []
    for run in runs:
        group = [line for band in run for line in band["lines"]]
        if len(group) >= MIN_LINES:
            output.append(group)
    return output


def table_candidates(
    lines: list[dict[str, Any]], *, page_no: int, start: int = 1,
) -> list[dict[str, Any]]:
    """미배정 줄에서 표 후보를 만든다. bbox는 원래 줄의 합집합만 쓴다."""
    output = []
    for index, group in enumerate(find_grids(lines), start=start):
        group = sorted(group, key=lambda line: (_bbox(line)[1], _bbox(line)[0]))
        output.append({
            "candidate_id": f"p{page_no}_t{index:03d}",
            "kind": "table",
            "bbox": _union([_bbox(line) for line in group]),
            "bbox_source": "ocr_pdf_lines",
            "bbox_quality": "exact",
            "line_refs": [str(line["line_ref"]) for line in group],
            "lines": group,
            "text": "\n".join(str(line.get("text") or "").strip() for line in group),
            "decision": None,
        })
    return output


def grow_cells(
    seeds: list[dict[str, Any]], pool: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """씨앗 칸에서 시작해 같은 행에 붙어 있는 칸을 끌어온다.

    VLM 이 고른 ID 는 표의 모든 칸을 다 담지 못하기도 한다. **씨앗과 세로로
    겹치고 가로로 붙어 있는 칸**만 더해 빠진 칸을 메운다. 떨어져 있는 다른 단은
    가로 간격에서 걸러진다 — 실측(2026-09-20, `2. 카드상품`): 표에서 510px
    떨어진 `p1_r024` 는 세로로 58% 겹쳐도 들어오지 않는다.
    """
    if not seeds:
        return []
    chosen = {id(region): region for region in seeds}
    heights = [
        max(6, region["bbox"][3] - region["bbox"][1])
        for region in seeds if region.get("bbox")
    ]
    reach = float(median(heights)) * 2.0 if heights else 0.0
    for _ in range(4):  # 한 번에 한 칸씩 번져도 네 번이면 멈춘다
        box = _union([list(region["bbox"]) for region in chosen.values()])
        added = False
        for region in pool:
            if id(region) in chosen or not region.get("bbox"):
                continue
            rb = region["bbox"]
            height = max(1, rb[3] - rb[1])
            overlap = max(0, min(rb[3], box[3]) - max(rb[1], box[1]))
            if overlap / height < 0.5:
                continue  # 같은 행이 아니다
            gap = max(0, max(rb[0] - box[2], box[0] - rb[2]))
            if gap > reach:
                continue  # 붙어 있지 않다 — 다른 단이다
            chosen[id(region)] = region
            added = True
        if not added:
            break
    return list(chosen.values())


def _cell_schema(line_refs: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "analysis": {"type": "string"},
            "rows": {"type": "integer"},
            "cols": {"type": "integer"},
            "cells": {
                "type": "array",
                # 줄 하나가 한 칸에만 들어가므로 셀 수는 줄 수를 넘지 않는다.
                # 상한이 없으면 모델이 같은 줄을 반복 배치해 응답이 끝나지 않는다.
                "minItems": 1,
                "maxItems": max(1, len(line_refs)),
                "items": {
                    "type": "object",
                    "properties": {
                        "line_ref": {"type": "string", "enum": line_refs or ["__none__"]},
                        "row": {"type": "integer"},
                        "col": {"type": "integer"},
                        "is_header": {"type": "boolean"},
                    },
                    "required": ["line_ref", "row", "col", "is_header"],
                    "additionalProperties": False,
                },
            },
            "confidence": {"type": "number"},
        },
        "required": ["analysis", "rows", "cols", "cells", "confidence"],
        "additionalProperties": False,
    }


def _render(grid: dict[str, Any], cells: list[dict[str, Any]]) -> str:
    """격자를 사람이 읽고 심의 모델이 쓰기 좋은 표로 만든다."""
    rows, cols = int(grid["rows"]), int(grid["cols"])
    table = [["" for _ in range(cols)] for _ in range(rows)]
    for cell in cells:
        row, col = int(cell["row"]), int(cell["col"])
        if 0 <= row < rows and 0 <= col < cols:
            text = " ".join(str(cell.get("text") or "").split())
            table[row][col] = f"{table[row][col]} {text}".strip()
    lines = ["| " + " | ".join(row) + " |" for row in table]
    if rows >= 2 and any(cell.get("is_header") for cell in cells):
        lines.insert(1, "|" + "|".join(["---"] * cols) + "|")
    return "\n".join(lines)


def place_cells(
    image: Image.Image, region: dict[str, Any],
) -> dict[str, Any] | None:
    """표 영역의 OCR 줄을 행·열에 배치한다. 좌표와 문구는 만들지 않는다."""
    lines = [line for line in region.get("lines") or [] if line.get("bbox")]
    if len(lines) < MIN_LINES:
        return None
    box = region.get("bbox") or _union([_bbox(line) for line in lines])
    x0 = max(0, int(box[0]) - CROP_PAD)
    y0 = max(0, int(box[1]) - CROP_PAD)
    x1 = min(image.width, int(box[2]) + CROP_PAD)
    y1 = min(image.height, int(box[3]) + CROP_PAD)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    crop = image.crop((x0, y0, x1, y1))

    by_ref = {str(line["line_ref"]): line for line in lines}
    refs = list(by_ref)
    rows = "\n".join(
        # crop 기준 좌표를 준다. 페이지 좌표를 주면 보이는 그림과 어긋난다.
        f"- {ref} box=[{_bbox(line)[0] - x0},{_bbox(line)[1] - y0},"
        f"{_bbox(line)[2] - x0},{_bbox(line)[3] - y0}] "
        f"text={' '.join(str(line.get('text') or '').split())}"
        for ref, line in by_ref.items()
    )
    overview = image.convert("RGB").copy()
    draw = ImageDraw.Draw(overview)
    stroke = max(4, round(min(image.size) / 300))
    draw.rectangle([int(value) for value in box], outline="#e53935", width=stroke)

    prompt = f"""당신은 표 구조 판독기입니다.

첫 번째 이미지는 페이지 전체이며 빨간 사각형이 대상 표입니다. 두 번째 이미지는
그 사각형을 확대한 상세 화면입니다. 전체 화면은 어느 상품과 문단에 속한 표인지,
상세 화면은 실제 행·열과 주석을 판단하는 데 사용하세요.

아래 줄 {len(refs)}개를 표의 행(row)과 열(col)에 배치하세요.

- row와 col은 0부터 시작합니다. 머리글 행은 is_header=true로 표시하세요.
- 제공된 line_ref만 쓰고 **모든 줄을 정확히 한 번씩** 배치하세요.
- 텍스트를 새로 쓰거나 고치지 마세요. 좌표도 만들지 마세요. 배치만 하세요.
- 한 셀에 여러 줄이 들어가면 같은 row/col을 주세요.
- 병합된 셀은 그 내용이 시작하는 칸에 두세요.
- 표가 아니라고 판단되면 rows=0, cols=0, confidence=0으로 반환하세요.

줄 목록(좌표는 잘라낸 이미지 기준):
{rows}
"""
    result = vlm_client.chat_json(
        [
            {"type": "text", "text": prompt},
            vlm_client.image_part(overview, box=(1000, 1600), quality=82),
            vlm_client.image_part(crop, box=(1400, 2400), quality=92),
        ],
        schema_name="parser_v2_table_cells",
        schema=_cell_schema(refs),
        max_tokens=min(12000, 1500 + 250 * len(refs)),
    )
    return build_grid(result, by_ref)


def build_grid(
    result: dict[str, Any], by_ref: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """VLM 배치를 검증해 셀 구조로 만든다.

    같은 줄이 두 번 배치되면 뒤엣것을 버린다. 배치되지 않은 줄은 버리지 않고
    `unplaced_line_refs`에 남긴다 — 이게 비어야 "줄 누락 0" 계약이 유지된다.
    """
    rows, cols = int(result.get("rows") or 0), int(result.get("cols") or 0)
    if rows <= 0 or cols <= 0:
        return None

    # 긴 각주가 시작된 y 아래의 줄은 VLM이 표 행으로 반환하더라도 notes로 돌린다.
    # 셀 안의 짧은 `주1)` 표시는 제외하고, 설명이 붙은 실제 각주 시작만 경계로 쓴다.
    note_starts = [
        _bbox(line)[1]
        for line in by_ref.values()
        if NOTE_START.match(str(line.get("text") or ""))
        and len("".join(str(line.get("text") or "").split())) >= 8
    ]
    note_y = min(note_starts) if note_starts else None
    automatic_notes = {
        ref for ref, line in by_ref.items()
        if note_y is not None and _bbox(line)[1] >= note_y
    }

    placed: dict[str, dict[str, Any]] = {}
    for item in result.get("cells") or []:
        ref = str(item.get("line_ref") or "")
        if ref not in by_ref or ref in placed:
            continue
        placed[ref] = item

    buckets: dict[tuple[int, int], dict[str, Any]] = {}
    note_buckets: dict[int, list[dict[str, Any]]] = {}
    invalid_refs: set[str] = set()
    for ref, item in placed.items():
        row, col = int(item.get("row") or 0), int(item.get("col") or 0)
        if ref in automatic_notes:
            note_buckets.setdefault(_bbox(by_ref[ref])[1], []).append(by_ref[ref])
            continue
        # VLM이 "표는 3행"이라고 해놓고 각주를 row=3,4,5로 반환하는 경우가
        # 있다. 표 밖의 아래쪽 행은 셀로 버리지 않고 관련 각주로 보존한다.
        if row >= rows and 0 <= col < cols:
            note_buckets.setdefault(row, []).append(by_ref[ref])
            continue
        if row < 0 or row >= rows or col < 0 or col >= cols:
            invalid_refs.add(ref)
            continue
        key = (row, col)
        cell = buckets.setdefault(key, {
            "row": key[0], "col": key[1],
            "is_header": bool(item.get("is_header")),
            "line_refs": [], "lines": [],
        })
        cell["is_header"] = cell["is_header"] or bool(item.get("is_header"))
        cell["line_refs"].append(ref)
        cell["lines"].append(by_ref[ref])

    cells = []
    for key in sorted(buckets):
        cell = buckets[key]
        ordered = sorted(cell["lines"], key=lambda line: (_bbox(line)[1], _bbox(line)[0]))
        cells.append({
            "row": cell["row"],
            "col": cell["col"],
            "is_header": cell["is_header"],
            "line_refs": [str(line["line_ref"]) for line in ordered],
            "text": " ".join(
                str(line.get("text") or "").strip() for line in ordered
            ).strip(),
            "bbox": _union([_bbox(line) for line in ordered]),
        })
    if not cells:
        return None

    # 모델이 아예 배치하지 않은 각주도 원문 좌표가 있으면 notes로 보존한다.
    for ref in automatic_notes - set(placed):
        note_buckets.setdefault(_bbox(by_ref[ref])[1], []).append(by_ref[ref])

    notes = []
    for row in sorted(note_buckets):
        ordered = sorted(note_buckets[row], key=lambda line: (_bbox(line)[1], _bbox(line)[0]))
        notes.append({
            "text": " ".join(
                str(line.get("text") or "").strip() for line in ordered
                if str(line.get("text") or "").strip()
            ),
            "line_refs": [str(line["line_ref"]) for line in ordered],
            "bbox": _union([_bbox(line) for line in ordered]),
        })

    # 각주를 표 행에서 꺼냈다면 끝의 빈 행도 함께 제거한다.
    rows = min(rows, max(int(cell["row"]) for cell in cells) + 1)
    grid = {"rows": rows, "cols": cols}
    return {
        "grid": grid,
        "cells": cells,
        "notes": notes,
        "unplaced_line_refs": [
            ref for ref in by_ref
            if (ref not in placed or ref in invalid_refs) and ref not in automatic_notes
        ],
        "confidence": float(result.get("confidence") or 0.0),
        "analysis": str(result.get("analysis") or ""),
        "text_grid": _render(grid, cells),
        "bbox_source": "ocr_pdf_lines",
        "bbox_quality": "exact",
    }


def merge_regions(
    regions: list[dict[str, Any]], *, region_id: str,
    anchor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """흩어진 복구 Region을 표 Region 하나로 합친다.

    줄은 그대로 옮기므로 소유권이 늘거나 줄지 않는다.
    """
    regions = sorted(regions, key=lambda r: ((r.get("bbox") or [0, 0])[1], (r.get("bbox") or [0, 0])[0]))
    lines = [line for region in regions for line in region.get("lines") or []]
    lines.sort(key=lambda line: (_bbox(line)[1], _bbox(line)[0]))
    merged = copy.deepcopy(anchor or regions[0])
    # 맨 앞 Region 이 `unknown` 이라고 나머지의 소속까지 버리면 안 된다.
    owned = [
        str(region.get("product_id")) for region in regions
        if region.get("product_id") and str(region.get("product_id")) != "unknown"
    ]
    if owned:
        merged["product_id"] = owned[0]
    merged.update({
        "region_id": region_id,
        "kind": "table",
        "label": "table",
        "bbox": _union([list(region["bbox"]) for region in regions if region.get("bbox")]),
        "bbox_source": "ocr_pdf_lines",
        "bbox_quality": "exact",
        "lines": lines,
        "text": "\n".join(str(line.get("text") or "").strip() for line in lines),
        "text_source": "ocr_pdf_recovery",
        "merged_from": [str(region["region_id"]) for region in regions],
    })
    return merged
