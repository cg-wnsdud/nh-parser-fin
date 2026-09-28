# -*- coding: utf-8 -*-
"""자를지 말지, 자른다면 어떤 모양으로 — 판단 기준을 비율 하나로 통일한다.

**기존 기준(높이 > 4000px)이 틀린 이유.** 레이아웃 모델은 입력을 800×800 정사각으로
종횡비를 버리고 누른다(`PP-DocLayout_plus-L/inference.yml`, `keep_ratio: false`). 그래서
해로운 것은 픽셀 수가 아니라 **가로세로 비율**이다. 2026-09-17 실측:

    왜곡 배율(긴 변÷짧은 변)   통짜 1회 호출의 layout_det_res
      2. 카드상품   1.00        36개
      1. 카드상품   1.14        25개
      A4 3건        1.41        31 / 29 / 26개
      4. 카드상품   1.46        37개
      올원e적금     5.73        34개  (기존 가로띠 타일링은 40개)
      NH002        8.49         2개  ← 상단 73%에서 박스가 하나도 안 나왔다

1.46과 5.73 사이가 비어 있어 그 사이 어디에 선을 그어도 같게 갈린다. 기본 2.0 은
"한 변이 다른 변의 2배를 넘으면 자른다"는 뜻이고, 정확한 경계는 미측정이다.

**자르는 위치와 목표 조각 크기는 기존 로직 그대로다** (`content_bands`, span 1600px).
산술로 등간격 자르면 조각마다 글자량이 2~3배 차이 나고 컷이 글자 한가운데를 지난다.
글자량으로 등분하고 빈 줄로 스냅한 뒤 오버랩을 준다.

즉 기존 대비 **바뀐 것은 "자를지 말지"의 판단 기준 하나뿐이다** (높이 4000px → 왜곡 2.0).
조각 크기를 정사각으로 바꾸는 것도 시험했지만 측정상 이득이 없어 되돌렸다 —
`DEFAULT_SPAN` 주석 참조.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from PIL import Image

# 긴 변 ÷ 짧은 변이 이 값을 넘으면 자른다.
DEFAULT_ASPECT_LIMIT = 2.0

# 목표 조각 크기. 기존 파이프라인 값이다.
#
# 한때 "짧은 변 = 정사각"으로 바꿔 봤다. 조각을 800×800 정사각으로 누를 때 왜곡이
# 없어지니 나아질 것이라고 봤는데, **측정해 보니 차이가 없었다** (2026-09-18,
# `compare.py --a tiled --b span1600`):
#
#     6개 통짜 문서    커버리지 완전 동일 (자르지 않으므로 당연)
#     NH002           정사각이 4곳 놓침 / 1600 이 2곳 놓침   → 1600 우세
#     올원e적금        정사각이 0곳 놓침 / 1600 이 1곳 놓침   → 정사각 우세
#     합계            영역 268개 중 순증 1곳. 방향도 문서마다 엇갈림
#
# 세로로 긴 문서의 기존 조각도 이미 왜곡 1.43~2.22 로 나쁘지 않았다. 51:1 같은 극단은
# **가로로 넓은 문서에서만** 벌어지던 일이고, 그건 "자를지 말지"를 비율로 판단하는 것으로
# 이미 해결된다(`1. 카드상품` 16조각 → 통짜, OCR 줄 96 → 204).
# 근거 없는 변경을 남겨 두지 않기 위해 기존 값으로 되돌린다.
DEFAULT_SPAN = 1600
# 오버랩은 조각 크기에 비례시킨다. 기존 200px 은 1600px 조각(12.5%) 기준이었는데,
# 폭 720px 문서에 그대로 쓰면 28% 가 겹쳐 중복이 과해진다.
OVERLAP_RATIO = 0.15
OVERLAP_MIN, OVERLAP_MAX = 80, 200


@dataclass
class Piece:
    """보낼 조각 하나와, 원본에서의 위치."""

    image: Image.Image
    x_offset: int = 0
    y_offset: int = 0
    index: int = 0
    mass: float = 1.0        # 이 조각이 담은 글자량 비율
    snapped: bool = True     # 깨끗한 자리로 옮겨 잘랐는가

    @property
    def aspect(self) -> float:
        return round(self.image.width / max(1, self.image.height), 3)


def distortion(size: tuple[int, int]) -> float:
    """긴 변 ÷ 짧은 변. 1.0 이면 정사각, 클수록 800×800 에서 심하게 찌그러진다."""
    width, height = size
    return round(max(width, height) / max(1, min(width, height)), 3)


def plan(
    image: Image.Image,
    *,
    mode: str = "auto",
    aspect_limit: float = DEFAULT_ASPECT_LIMIT,
    overlap: int | None = None,
    span_override: int | None = None,
) -> tuple[list[Piece], dict]:
    """조각 목록과 그 판단의 근거 기록을 돌려준다."""
    width, height = image.size
    ratio = distortion(image.size)
    note = {
        "mode": mode,
        "canvas": [width, height],
        "distortion": ratio,
        "aspect_limit": aspect_limit,
    }

    if mode == "off" or (mode == "auto" and ratio <= aspect_limit):
        note.update(decision="whole", pieces=1, reason=(
            "타일링 끔" if mode == "off" else f"왜곡 {ratio} ≤ 한계 {aspect_limit}"
        ))
        return [Piece(image, 0, 0, 0)], note

    axis = "y" if height >= width else "x"
    if span_override == 0:
        # 정사각 목표. 측정상 이득이 없어 기본값이 아니지만, 재현할 수 있게 남겨 둔다.
        span = width if axis == "y" else height
        max_span = max(span + 1, round(span * aspect_limit))
    else:
        # 기본값: 목표 크기를 픽셀로 고정하고 상한도 같은 값으로 둔다(기존 파이프라인).
        span = span_override or DEFAULT_SPAN
        max_span = span
    gap = overlap if overlap is not None else int(
        min(OVERLAP_MAX, max(OVERLAP_MIN, round(span * OVERLAP_RATIO)))
    )

    from .bands import content_bands

    bands = content_bands(image, axis=axis, span=span, overlap=gap, max_span=max_span)
    pieces = [
        Piece(
            image=band.image,
            x_offset=band.offset if axis == "x" else 0,
            y_offset=band.offset if axis == "y" else 0,
            index=index,
            mass=round(band.mass, 4),
            snapped=band.snapped,
        )
        for index, band in enumerate(bands)
    ]
    note.update(
        decision="split",
        axis=axis,
        span_mode="square" if span_override == 0 else "fixed",
        target_span=span,
        max_span=max_span,
        overlap=gap,
        pieces=len(pieces),
        piece_sizes=[list(p.image.size) for p in pieces],
        piece_distortion=[distortion(p.image.size) for p in pieces],
        unsnapped=[p.index for p in pieces if not p.snapped],
        reason=f"왜곡 {ratio} > 한계 {aspect_limit} — {axis}축 {len(pieces)}조각",
    )
    return pieces, note


def shift(box: dict, x_offset: int, y_offset: int) -> dict:
    """조각 좌표 → 원본 페이지 좌표."""
    x0, y0, x1, y1 = box["bbox"]
    out = dict(box)
    out["bbox"] = [x0 + x_offset, y0 + y_offset, x1 + x_offset, y1 + y_offset]
    return out


def _overlap_share(a0: int, a1: int, b0: int, b1: int) -> float:
    return max(0, min(a1, b1) - max(a0, b0)) / max(1, min(a1 - a0, b1 - b0))


def _same_content(left: dict, right: dict) -> bool:
    """타일 위치 때문에 라벨만 달라진 동일 블록인지 확인한다."""
    normalize = lambda value: "".join(str(value or "").split()).casefold()
    a = normalize(left.get("content"))
    b = normalize(right.get("content"))
    return bool(a) and a == b


def dedupe(boxes: list[dict], *, x_share: float = 0.90, y_share: float = 0.50) -> tuple[list[dict], int]:
    """조각 경계에서 같은 요소가 두 번 온 것만 좁게 합친다 (기존 파이프라인과 같은 기준).

    같은 조각 안의 중첩 박스는 모델이 의도한 계층일 수 있으므로 건드리지 않는다 —
    그래서 `piece` 가 다른 것끼리만 본다.
    """
    kept: list[dict] = []
    merged = 0
    for box in boxes:
        for index, other in enumerate(kept):
            if other.get("piece") == box.get("piece"):
                continue
            ax0, ay0, ax1, ay1 = other["bbox"]
            bx0, by0, bx1, by1 = box["bbox"]
            x_overlap = _overlap_share(ax0, ax1, bx0, bx1)
            y_overlap = _overlap_share(ay0, ay1, by0, by1)
            same_label = other["label"].casefold() == box["label"].casefold()
            # 같은 요소가 타일 0의 하단에서는 text, 타일 1의 상단에서는 doc_title로
            # 분류되는 실측 사례가 있다. 라벨이 다를 때는 일반 중복 기준을 완화하지
            # 않고, 양축 95% 이상 + 정규화 원문 완전 일치일 때만 동일 요소로 본다.
            label_conflict_duplicate = (
                not same_label
                and x_overlap >= 0.95
                and y_overlap >= 0.95
                and _same_content(other, box)
            )
            if not same_label and not label_conflict_duplicate:
                continue
            if x_overlap < x_share:
                continue
            if y_overlap < y_share:
                continue
            union = dict(other)
            union["bbox"] = [min(ax0, bx0), min(ay0, by0), max(ax1, bx1), max(ay1, by1)]
            scores = [s for s in (other.get("score"), box.get("score")) if s is not None]
            if scores:
                union["score"] = max(scores)
            if label_conflict_duplicate:
                union["label_candidates"] = list(dict.fromkeys([
                    *(other.get("label_candidates") or [other["label"]]),
                    *(box.get("label_candidates") or [box["label"]]),
                ]))
                union["dedupe_label_conflict"] = True
            kept[index] = union
            merged += 1
            break
        else:
            kept.append(box)
    return kept, merged
