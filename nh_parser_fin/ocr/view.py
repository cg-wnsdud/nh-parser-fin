# -*- coding: utf-8 -*-
"""결과를 눈으로 보는 두 경로 — 오버레이 이미지와 Label Studio task.

라벨 색과 Label Studio 설정 XML 을 **이번 실행에 실제로 나온 라벨에서** 만든다.
PP 20클래스 고정 목록을 쓰면 `vision_footnote` 처럼 목록 밖 라벨이 조용히 사라진다
(실측: 2026-09-17 types-base 에서 1건 누락).
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

PALETTE = [
    "#E53935", "#1E88E5", "#43A047", "#FB8C00", "#8E24AA", "#00897B",
    "#3949AB", "#C0CA33", "#D81B60", "#5E35B1", "#00ACC1", "#7CB342",
    "#F4511E", "#6D4C41", "#546E7A", "#FDD835", "#039BE5", "#00838F",
    "#757575", "#FFB300",
]

OVERLAY_MAX_SIDE = 1600


def color_for(label: str, labels: list[str]) -> str:
    try:
        return PALETTE[labels.index(label) % len(PALETTE)]
    except ValueError:
        return "#FF00FF"


def draw_overlay(image: Image.Image, boxes: list[dict], labels: list[str],
                 out_path: Path) -> None:
    """원본 좌표계에 박스를 그린 뒤, 보기 좋은 크기로 줄여 저장한다."""
    canvas = image.convert("RGB").copy()
    drawer = ImageDraw.Draw(canvas)
    width = max(2, round(max(canvas.size) / 700))
    for box in boxes:
        x0, y0, x1, y1 = box["bbox"]
        drawer.rectangle([x0, y0, x1, y1], outline=color_for(box["label"], labels),
                         width=width)
    scale = min(1.0, OVERLAY_MAX_SIDE / max(canvas.size))
    if scale < 1.0:
        canvas = canvas.resize(
            (max(1, round(canvas.width * scale)), max(1, round(canvas.height * scale))),
            Image.LANCZOS,
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, format="JPEG", quality=88)


def ocr_line_boxes(lines: list[dict], *, label: str = "ocr_line") -> list[dict]:
    """OCR 줄을 레이아웃 박스와 같은 표시 형식으로 바꾼다.

    `overall_ocr_res`의 줄은 레이아웃 클래스가 없으므로 진단용 라벨을 붙인다.
    텍스트와 점수는 그대로 보존해 Label Studio JSON에서도 확인할 수 있게 한다.
    """
    return [dict(line, label=label) for line in lines if line.get("bbox")]


def _line_overlap_ratio(line_bbox: list[int], region_bbox: list[int]) -> float:
    """한 OCR 줄 면적 중 parsing 영역과 겹치는 비율을 계산한다."""
    ix0 = max(line_bbox[0], region_bbox[0])
    iy0 = max(line_bbox[1], region_bbox[1])
    ix1 = min(line_bbox[2], region_bbox[2])
    iy1 = min(line_bbox[3], region_bbox[3])
    intersection = max(0, ix1 - ix0) * max(0, iy1 - iy0)
    line_area = max(
        1,
        (line_bbox[2] - line_bbox[0]) * (line_bbox[3] - line_bbox[1]),
    )
    return intersection / line_area


def unassigned_ocr_lines(
    lines: list[dict], parsing: list[dict], *, min_overlap: float = 0.5,
) -> list[dict]:
    """어느 parsing 영역에도 면적의 절반 이상 들어가지 못한 OCR 줄을 반환한다.

    이것은 PaddleX가 보내는 별도 필드가 아니라 **우리 후처리 후보를 보기 위한 진단층**이다.
    기준 0.5는 실제 파서의 `build_regions`가 줄을 영역에 배정할 때 쓰는 값과 같다.
    """
    regions = [item["bbox"] for item in parsing if item.get("bbox")]
    return [
        dict(line, label="unassigned_ocr")
        for line in lines
        if line.get("bbox")
        and not any(_line_overlap_ratio(line["bbox"], box) >= min_overlap for box in regions)
    ]


def rectangle(box: dict, width: int, height: int, *, box_id: str,
              from_name: str = "layout") -> dict:
    x0, y0, x1, y1 = (float(v) for v in box["bbox"])
    if x1 <= x0 or y1 <= y0:
        return {}

    def percent(value: float, limit: int) -> float:
        return max(0.0, min(100.0, value / max(1, limit) * 100.0))

    result = {
        "id": box_id,
        "from_name": from_name,
        "to_name": "image",
        "type": "rectanglelabels",
        "original_width": width,
        "original_height": height,
        "image_rotation": 0,
        "value": {
            "x": percent(x0, width),
            "y": percent(y0, height),
            "width": percent(x1 - x0, width),
            "height": percent(y1 - y0, height),
            "rotation": 0,
            "rectanglelabels": [box["label"]],
        },
    }
    if box.get("score") is not None:
        result["score"] = box["score"]
    meta = {
        key: box[key]
        for key in ("text", "content", "order", "piece", "source")
        if box.get(key) is not None
    }
    if meta:
        result["meta"] = meta
    return result


def labeling_config(labels: list[str]) -> str:
    """이번 실행에 나온 라벨만으로 Label Studio 설정을 만든다."""
    rows = "\n".join(
        f'    <Label value="{label}" background="{color_for(label, labels)}" />'
        for label in labels
    )
    return (
        "<View>\n"
        '  <Header value="1-det / 2-parsing / 3-ocr / 4-unassigned" />\n'
        # 화면에 입력 파일명을 띄운다. Header 의 value 가 `$키` 면 task data 의 값을
        # 꺼내 쓴다. 이게 없으면 task 를 열었을 때 어느 파일인지 알 수 없다.
        '  <Header value="$source_file" size="3" />\n'
        '  <Image name="image" value="$image" zoom="true" zoomControl="true" '
        'rotateControl="false" />\n'
        '  <RectangleLabels name="layout" toName="image" strokeWidth="2" choice="single">\n'
        f"{rows}\n"
        "  </RectangleLabels>\n"
        '  <TextArea name="note" toName="image" '
        'placeholder="애매한 경계·제외 이유·판정 근거" rows="3" />\n'
        "</View>\n"
    )
