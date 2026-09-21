# -*- coding: utf-8 -*-
"""입력 파일 → PaddleX 에 보낼 페이지 이미지 한 장씩.

**이 모듈이 지키는 규칙은 하나다: 크기는 명시적으로 정한다.**

기존 파이프라인은 triage 판정(= 텍스트 레이어가 쓸만한가)이 렌더 DPI 를 가르고, DPI 가
캔버스 픽셀을 정하고, 픽셀 높이가 타일링을 발동시켰다. 서로 무관한 질문들이 사슬로
엮여 있어서, 실물 크기가 거의 같은 포스터 두 장(1500×1500mm / 1600×1400mm)의 캔버스
면적이 7.7배 차이났다 — 한쪽에 텍스트 레이어가 있었다는 이유만으로.

여기서는 triage 를 계속 계산하지만 **기록만 한다.** 크기는 `sizing` 하나가 모든 입력에
똑같이 적용한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
HWP_SUFFIXES = {".hwp", ".hwpx"}
SUPPORTED = IMAGE_SUFFIXES | HWP_SUFFIXES | {".pdf"}

# 기존 파이프라인의 기본 렌더 해상도. `asis` 기준선을 재현하기 위한 값이다.
ASIS_PDF_DPI = 200


@dataclass
class LabPage:
    """PaddleX 에 보낼 한 장과, 그 한 장이 어떻게 만들어졌는지의 전체 기록."""

    doc_id: str
    source_file: str
    page_no: int
    image: Image.Image
    origin: dict = field(default_factory=dict)

    @property
    def aspect(self) -> float:
        """가로/세로. 레이아웃 모델이 800×800 정사각으로 누르므로 이 값이 왜곡의 크기다."""
        return round(self.image.width / max(1, self.image.height), 3)


def iter_inputs(paths: list[Path], exclude: list[str]) -> list[Path]:
    found: list[Path] = []
    for item in paths:
        path = item.resolve()
        if path.is_dir():
            found.extend(sorted(
                child for child in path.iterdir()
                if child.is_file() and child.suffix.lower() in SUPPORTED
            ))
        elif path.is_file():
            found.append(path)
        else:
            raise SystemExit(f"입력이 없습니다: {path}")
    kept = [p for p in found if not any(tok and tok in p.name for tok in exclude)]
    if not kept:
        raise SystemExit("처리할 입력이 없습니다")
    return kept


def _shrink(image: Image.Image, max_side: int) -> tuple[Image.Image, float]:
    """긴 변이 max_side 를 넘으면 줄인다. **확대는 하지 않는다** — 없는 정보를 만들 수 없다."""
    longest = max(image.size)
    if longest <= max_side:
        return image, 1.0
    scale = max_side / longest
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return image.resize(size, Image.LANCZOS), scale


def _image_pages(path: Path, sizing: str, max_side: int) -> list[LabPage]:
    from .canvas import rgb_on_white

    original = rgb_on_white(Image.open(path))
    origin = {"kind": "image", "original_px": list(original.size)}
    image, scale = (original, 1.0) if sizing == "asis" else _shrink(original, max_side)
    origin.update(sent_px=list(image.size), scale=round(scale, 4))
    return [LabPage(path.stem, path.name, 1, image, origin)]


def _pdf_pages(path: Path, sizing: str, max_side: int) -> list[LabPage]:
    import pypdfium2 as pdfium

    from .canvas import native_image_dpi, render_pdf_page
    from .triage import triage_page

    pages: list[LabPage] = []
    pdf = pdfium.PdfDocument(str(path))
    for index, pdf_page in enumerate(pdf):
        width_pt, height_pt = pdf_page.get_size()
        verdict = triage_page(pdf_page)
        native = native_image_dpi(pdf_page)

        # asis: 기존 파이프라인이 하던 그대로 재현한다(비교 기준선).
        #   structured → 기본 200, scan_like/hybrid → 내장 래스터 해상도(72~200)
        dpi_asis = ASIS_PDF_DPI
        if verdict.verdict in ("scan_like", "hybrid") and native:
            dpi_asis = native

        dpi = dpi_asis
        if sizing == "maxside":
            longest_pt = max(width_pt, height_pt)
            fit = max_side / (longest_pt / 72.0)
            dpi = max(1, min(dpi_asis, fit))   # 확대 금지

        canvas = render_pdf_page(pdf_page, index + 1, dpi=int(round(dpi)))
        pages.append(LabPage(
            doc_id=path.stem,
            source_file=path.name,
            page_no=index + 1,
            image=canvas.image,
            origin={
                "kind": "pdf",
                "page_pt": [round(width_pt, 1), round(height_pt, 1)],
                "page_mm": [round(width_pt / 72 * 25.4), round(height_pt / 72 * 25.4)],
                # 아래 두 줄은 **진단 기록일 뿐이며 크기 결정에 쓰이지 않는다**
                "triage": verdict.verdict,
                "native_image_dpi": native,
                "dpi_asis": round(dpi_asis, 1),
                "dpi_used": int(round(dpi)),
                "sent_px": list(canvas.image.size),
            },
        ))
    return pages


def _hwp_pages(path: Path, sizing: str, max_side: int) -> list[LabPage]:
    """HWP 는 캔버스가 없다. 내장 이미지 자산 하나를 한 페이지로 본다."""
    from .assets import decode_asset_image, is_decorative, iter_assets

    try:
        from document_processor import DocIR
    except Exception as exc:  # 사내 파서가 없는 환경
        raise SystemExit(f"HWP 를 읽으려면 사내 파서(document_processor)가 필요합니다: {exc}")

    docir = DocIR.from_file(str(path))
    pages: list[LabPage] = []
    skipped = 0
    for name, asset in iter_assets(docir):
        image = decode_asset_image(asset)
        if image is None or is_decorative(*image.size):
            skipped += 1
            continue
        original_size = list(image.size)
        sent, scale = (image, 1.0) if sizing == "asis" else _shrink(image, max_side)
        pages.append(LabPage(
            doc_id=path.stem,
            source_file=path.name,
            page_no=len(pages) + 1,
            image=sent,
            origin={
                "kind": "hwp",
                "asset": str(name),
                "original_px": original_size,
                "sent_px": list(sent.size),
                "scale": round(scale, 4),
                "decorative_skipped": skipped,
            },
        ))
    if not pages:
        raise SystemExit(f"{path.name}: OCR 에 보낼 내장 이미지가 없습니다 (장식 {skipped}개 제외)")
    return pages


def load_pages(path: Path, *, sizing: str = "asis", max_side: int = 2500) -> list[LabPage]:
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return _image_pages(path, sizing, max_side)
    if suffix == ".pdf":
        return _pdf_pages(path, sizing, max_side)
    if suffix in HWP_SUFFIXES:
        return _hwp_pages(path, sizing, max_side)
    raise SystemExit(f"지원하지 않는 형식입니다: {path.name}")
