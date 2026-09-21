"""세로 스크롤 타일링 [B2-2] — 설계서 6.3절.

초장신 캔버스(높이 4000px↑)를 오버랩 수평 밴드로 분할해 OCR에 보내고,
결과 라인 좌표를 원본 캔버스 좌표로 복원한 뒤 오버랩 구간 중복을 제거한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image

from ..config import SETTINGS
from ..ir import Line


@dataclass
class Tile:
    image: Image.Image
    y_offset: int


def make_tiles(canvas: Image.Image) -> list[Tile]:
    """초장신 캔버스를 OCR 에 보낼 조각으로 자른다.

    자를지 말지는 높이(4000px)로 정하고, **어디서 자를지는 글자 밀도가 정한다** —
    산술로 1600px 마다 자르면 조각마다 담기는 글자량이 2.3~3.4배까지 차이 나고 컷이
    글자 한가운데를 지난다.

    다만 조각 높이 상한(1600px)은 반드시 지킨다. 글자량만 맞추면 성긴 구간이 2000px 넘는
    조각이 되는데, 그러면 레이아웃 검출이 블록을 거칠게 묶는다 — 실측(2026-07-28,
    PaddleX 만 사용·같은 입력 2회 동일 확인)에서 상한 없이 돌렸을 때 002 의 레이아웃
    블록이 84→62 로 줄었고, 그 탓에 '최고연 7.1%' 를 담던 작은 영역이 큰 영역에 흡수돼
    통독 후보가 부실해졌다(풀 실행에서 섹션 -2·필드 -1 로 나타남).

    상한을 걸면 조각 수가 15→24 장으로 늘지만(PaddleX 호출만 증가, VLM 과 무관) 세 문서
    합계로 골드 회수 138→139, 레이아웃 블록 332→360 으로 **손실 없이** 개선된다.
    """
    _, h = canvas.size
    if h <= SETTINGS.tile_trigger_height_px:
        return [Tile(canvas, 0)]
    from .bands import content_bands

    return [
        Tile(band.image, band.offset)
        for band in content_bands(canvas, span=SETTINGS.tile_max_height_px,
                                  max_span=SETTINGS.tile_max_height_px)
    ]


def restore_coords(lines: list[Line], y_offset: int) -> list[Line]:
    out = []
    for line in lines:
        bbox = line.bbox
        shifted = [bbox[0], bbox[1] + y_offset, bbox[2], bbox[3] + y_offset] if bbox else None
        out.append(line.model_copy(update={"bbox": shifted}))
    return out


def _iou(a: list[int], b: list[int]) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if inter == 0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / max(1, area_a + area_b - inter)


def _containment(a: list[int], b: list[int]) -> float:
    """겹침 면적 / 작은 쪽 면적 — 조각 라인(부분 재검출)이 큰 라인 안에
    포함될 때 IoU 는 낮게 나와 dedupe 를 통과하는 문제를 잡는다 (올원 실측:
    '1천원 이상3'·'세저'·'기보그리' 같은 타일 경계 조각이 온전한 라인과 병존)."""
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if inter == 0:
        return 0.0
    area_a = max(1, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1, (b[2] - b[0]) * (b[3] - b[1]))
    return inter / min(area_a, area_b)


def sort_reading_order(lines: list[Line]) -> list[Line]:
    """행 클러스터 읽기 순서 정렬 — 수직 겹침 50% 이상이면 같은 행, 행 안은 좌→우.

    단순 (y, x) 정렬은 표의 값 셀 박스가 라벨보다 몇 px 위에 잡히면
    '12개월 / 가입기간'처럼 순서가 뒤집힌다 (올원 실측). 캔버스 크기와
    무관한 범용 기하 규칙이다.
    """
    boxed = sorted((l for l in lines if l.bbox), key=lambda l: l.bbox[1])
    unboxed = [l for l in lines if not l.bbox]
    rows: list[list[Line]] = []
    for line in boxed:
        if rows:
            row = rows[-1]
            r_top = min(l.bbox[1] for l in row)
            r_bot = max(l.bbox[3] for l in row)
            overlap = min(r_bot, line.bbox[3]) - max(r_top, line.bbox[1])
            height = min(line.bbox[3] - line.bbox[1], r_bot - r_top)
            if height > 0 and overlap / height >= 0.5:
                row.append(line)
                continue
        rows.append([line])
    out: list[Line] = []
    for row in rows:
        row.sort(key=lambda l: l.bbox[0])
        out.extend(row)
    return out + unboxed


def dedupe_lines(lines: list[Line]) -> list[Line]:
    """오버랩 밴드 이중 검출 제거 — bbox 가 심하게 겹치면 신뢰도 높은 쪽만 유지.

    타일 경계에서 잘린 텍스트는 같은 위치에 '깨진 텍스트+낮은 신뢰도'로 다시
    검출되므로(실측: '수 있는 권리가 있습니다.' 0.99 vs '7리콘극' 0.6),
    텍스트 동일성 조건 없이 위치 겹침만으로 판정한다. IoU 외에 포함비도 보아
    부분 조각(작은 박스가 큰 라인 안에 포함)도 제거한다.
    """
    kept: list[Line] = []
    for line in sorted(lines, key=lambda l: -(l.confidence or 0.0)):
        dup = False
        for other in kept:
            if not (line.bbox and other.bbox):
                continue
            if (_iou(line.bbox, other.bbox) >= SETTINGS.dedupe_iou
                    or _containment(line.bbox, other.bbox) >= SETTINGS.dedupe_containment):
                dup = True
                break
        if not dup:
            kept.append(line)
    return sort_reading_order(kept)
