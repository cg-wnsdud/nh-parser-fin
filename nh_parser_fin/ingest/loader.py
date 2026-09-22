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
import os
from pathlib import Path
import tempfile

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
    digital_lines: list[dict] = field(default_factory=list)
    hwp_structure: dict | None = None

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
    """HWP의 실제 페이지를 로컬 PDF로 렌더하고 구조 텍스트를 함께 싣는다.

    내장 이미지를 가상 페이지로 만들던 예전 경로는 사용자 화면의 페이지/bbox와 맞지
    않았다. 이제 원본 HWP 이름은 유지한 채 한컴 PDF의 페이지 캔버스를 사용한다.
    """
    import pypdfium2 as pdfium

    from .canvas import native_image_dpi, render_pdf_page
    from .hwp_render import render_hwp_to_pdf
    from .hwp_structure import parse_hwp_structure, repartition_by_rendered_text
    from .triage import extract_digital_lines, triage_page

    try:
        structure = parse_hwp_structure(path)
    except Exception as exc:
        raise SystemExit(f"{path.name}: HWP 구조 파싱 실패: {exc}") from exc

    persistent = os.environ.get("HWP_RENDER_DIR", "").strip()
    temp = None
    if persistent:
        render_dir = Path(persistent)
        render_dir.mkdir(parents=True, exist_ok=True)
    else:
        temp = tempfile.TemporaryDirectory(prefix="nh-hwp-pdf-")
        render_dir = Path(temp.name)
    pdf_path = render_dir / f"{path.stem}.pdf"
    try:
        render_info = render_hwp_to_pdf(path, pdf_path)
        pdf = pdfium.PdfDocument(str(pdf_path))
        pages: list[LabPage] = []
        try:
            for index, pdf_page in enumerate(pdf):
                page_no = index + 1
                width_pt, height_pt = pdf_page.get_size()
                verdict = triage_page(pdf_page)
                native = native_image_dpi(pdf_page)
                dpi_asis = ASIS_PDF_DPI
                if verdict.verdict in ("scan_like", "hybrid") and native:
                    dpi_asis = native
                dpi = dpi_asis
                if sizing == "maxside":
                    fit = max_side / (max(width_pt, height_pt) / 72.0)
                    dpi = max(1, min(dpi_asis, fit))
                dpi_used = int(round(dpi))
                canvas = render_pdf_page(pdf_page, page_no, dpi=dpi_used)
                px_per_pt = dpi_used / 72.0
                digital = []
                if verdict.verdict in ("structured", "hybrid"):
                    digital = [
                        line.model_dump(mode="json")
                        for line in extract_digital_lines(pdf_page, px_per_pt)
                    ]
                pages.append(LabPage(
                    doc_id=path.stem,
                    source_file=path.name,
                    page_no=page_no,
                    image=canvas.image,
                    digital_lines=digital,
                    hwp_structure=None,
                    origin={
                        "kind": "hwp",
                        "render": render_info,
                        "rendered_pdf": str(pdf_path) if persistent else None,
                        "page_pt": [round(width_pt, 1), round(height_pt, 1)],
                        "page_mm": [round(width_pt / 72 * 25.4), round(height_pt / 72 * 25.4)],
                        "triage": verdict.verdict,
                        "triage_detail": verdict.as_dict(),
                        "native_image_dpi": native,
                        "dpi_asis": round(dpi_asis, 1),
                        "dpi_used": dpi_used,
                        "sent_px": list(canvas.image.size),
                        "structure_parser": structure["parser"],
                        "structure_parser_version": structure["parser_version"],
                        "structure_page_count": structure["page_count"],
                    },
                ))
        finally:
            pdf.close()
        rendered_texts = [
            "\n".join(str(line.get("text") or "") for line in page.digital_lines)
            for page in pages
        ]
        assigned_structure = repartition_by_rendered_text(structure, rendered_texts)
        for page, page_structure in zip(pages, assigned_structure, strict=True):
            page.hwp_structure = page_structure
        if len(pages) != int(structure["page_count"]):
            for page in pages:
                page.origin["structure_page_count_mismatch"] = True
        return pages
    finally:
        if temp is not None:
            temp.cleanup()


def load_pages(path: Path, *, sizing: str = "asis", max_side: int = 2500) -> list[LabPage]:
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return _image_pages(path, sizing, max_side)
    if suffix == ".pdf":
        return _pdf_pages(path, sizing, max_side)
    if suffix in HWP_SUFFIXES:
        return _hwp_pages(path, sizing, max_side)
    raise SystemExit(f"지원하지 않는 형식입니다: {path.name}")
