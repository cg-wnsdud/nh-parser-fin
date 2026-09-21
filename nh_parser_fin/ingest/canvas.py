"""입력 정규화 [B0] — 광고물을 '페이지 캔버스' 목록으로 변환.

캔버스 = 원본 픽셀 좌표계를 가진 단일 RGB 이미지.
이후 모든 단계(타일링/OCR/영역)는 이 좌표계만 사용한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

from ..config import SETTINGS

Image.MAX_IMAGE_PIXELS = None  # 세로 초장신 캔버스(36MP+) 허용


@dataclass
class CanvasPage:
    image: Image.Image
    page_no: int
    dpi: int | None = None           # PDF 렌더 시 기록
    px_per_pt: float | None = None   # PDF 좌표(pt) → 픽셀 환산 배율
    notes: list[str] = field(default_factory=list)


def rgb_on_white(image: Image.Image) -> Image.Image:
    if image.mode == "RGB":
        return image
    if image.mode in ("RGBA", "LA", "P"):
        rgba = image.convert("RGBA")
        base = Image.new("RGB", rgba.size, (255, 255, 255))
        base.paste(rgba, mask=rgba.split()[-1])
        return base
    return image.convert("RGB")


def load_image_canvas(path: Path) -> CanvasPage:
    return CanvasPage(image=rgb_on_white(Image.open(path)), page_no=1)


def render_pdf_page(page: pdfium.PdfPage, page_no: int, dpi: int | None = None) -> CanvasPage:
    dpi = dpi or SETTINGS.pdf_render_dpi
    scale = dpi / 72.0
    bitmap = page.render(scale=scale)
    return CanvasPage(
        image=rgb_on_white(bitmap.to_pil()),
        page_no=page_no,
        dpi=dpi,
        px_per_pt=scale,
    )


def native_image_dpi(page: pdfium.PdfPage) -> int | None:
    """페이지 내 최대 이미지 오브젝트의 원본 해상도 기준 DPI.

    이미지 기반 페이지(scan_like)를 원본보다 크게 렌더하면 정보 없이
    업스케일만 일어나 OCR 검출이 단어 단위로 조각난다 (001 실측, 2026-07-18).
    내장 래스터의 픽셀 밀도에 맞춰 렌더하기 위한 근거값을 계산한다.
    """
    import pypdfium2.raw as pdfium_c

    best: float | None = None
    try:
        for obj in page.get_objects(max_depth=2):
            if obj.type != pdfium_c.FPDF_PAGEOBJ_IMAGE:
                continue
            left, bottom, right, top = obj.get_bounds()  # pypdfium2 v5 (v4: get_pos)
            w_pt = right - left
            if w_pt <= 1:
                continue
            meta = obj.get_metadata()
            px_w = getattr(meta, "width", 0)
            if px_w <= 0:
                continue
            dpi = px_w / w_pt * 72.0
            if best is None or dpi > best:
                best = dpi
    except Exception:
        return None
    if best is None:
        return None
    return int(min(max(best, 72), SETTINGS.pdf_render_dpi))
