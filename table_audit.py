"""samples 전체 페이지의 Paddle 표 bbox와 VLM 판단만 검증한다.

일반 P1/P3 파이프라인은 실행하지 않는다. PDF는 PDFium 원문 줄을 쓰고,
HWP는 document-processor의 문단/표 셀과 HTML 렌더를 쓴다.
"""
from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path
import sys
from urllib.parse import quote

from PIL import Image, ImageDraw, ImageFont

from nh_parser_fin.config import OUTPUT_ROOT, Profile
from nh_parser_fin.ingest.loader import iter_inputs, load_pages
from nh_parser_fin.ocr import paddlex
from nh_parser_fin.parse.adapters import digital_text_needs_ocr, line_overlap_ratio
from nh_parser_fin.parse.relations import analyze_page_relations


def _safe(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in value)[:110]


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _bbox(value: object) -> list[int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    return [int(round(float(item))) for item in value]


def _inside(line: dict, candidate: list[int]) -> bool:
    box = _bbox(line.get("bbox"))
    return bool(box) and line_overlap_ratio(box, candidate) >= 0.45


def _evidence_lines(page, candidate: list[int], ocr_lines: list[dict]) -> tuple[list[dict], str]:
    rows: list[dict] = []
    weak_digital = (
        page.origin.get("kind") == "pdf"
        and digital_text_needs_ocr(page.digital_lines, ocr_lines)
    )
    if page.origin.get("kind") == "hwp":
        for block in page.structured_blocks:
            cells = (block.get("table") or {}).get("cells") or []
            if cells:
                for cell in cells:
                    text = str(cell.get("text") or "").strip()
                    box = _bbox(cell.get("bbox"))
                    if text and box and _inside({"bbox": box}, candidate):
                        rows.append({"bbox": box, "text": text, "source": "hwp_docir_cell"})
            else:
                text = str(block.get("content") or "").strip()
                box = _bbox(block.get("bbox"))
                if text and box and _inside({"bbox": box}, candidate):
                    rows.append({"bbox": box, "text": text, "source": "hwp_docir_row"})
        source = (
            "hwp_docir_mixed" if len({row["source"] for row in rows}) > 1
            else rows[0]["source"] if rows else "rendered_pdfium"
        )
    else:
        source = (
            "ocr_fallback_weak_pdf_text" if weak_digital
            else "pdfium" if page.digital_lines else "paddlex_ocr"
        )
    if not rows:
        rows = [
            {"bbox": _bbox(line["bbox"]), "text": str(line.get("text") or "").strip(),
             "source": "rendered_pdfium" if page.origin.get("kind") == "hwp" else "pdfium"}
            for line in ([] if weak_digital else page.digital_lines)
            if _inside(line, candidate) and str(line.get("text") or "").strip()
        ]
    # 디지털 PDF/HWP 구조 텍스트가 이미 있는 표에서는 Paddle의 표 내부 OCR이
    # 깨진 글자를 보충 증거로 섞을 수 있다. OCR은 텍스트 층이 없는 표에만 쓴다.
    for line in ([] if rows else ocr_lines):
        if not _inside(line, candidate):
            continue
        box = _bbox(line["bbox"])
        if box is None or any(line_overlap_ratio(box, other["bbox"]) >= 0.65 for other in rows):
            continue
        text = str(line.get("text") or line.get("content") or "").strip()
        if text:
            rows.append({"bbox": box, "text": text, "source": "paddlex_ocr"})
    rows.sort(key=lambda item: (item["bbox"][1], item["bbox"][0]))
    for index, line in enumerate(rows, start=1):
        line["line_ref"] = f"p{page.page_no}/table/L{index:03d}"
    return rows, source


def _save_images(image: Image.Image, boxes: list[list[int]], folder: Path, base: str) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    source_name = f"{base}__page.png"
    image.save(folder / source_name, format="PNG")
    overlay = image.convert("RGB").copy()
    draw = ImageDraw.Draw(overlay)
    try:
        font = ImageFont.load_default(size=24)
    except TypeError:
        font = ImageFont.load_default()
    crops = []
    for index, box in enumerate(boxes, start=1):
        x0, y0, x1, y1 = box
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(image.width, x1), min(image.height, y1)
        if x1 <= x0 or y1 <= y0:
            crops.append(None)
            continue
        pad = 30
        name = f"{base}__table{index:02d}.png"
        image.crop((max(0, x0 - pad), max(0, y0 - pad),
                    min(image.width, x1 + pad), min(image.height, y1 + pad))).save(folder / name)
        crops.append(name)
        draw.rectangle((x0, y0, x1, y1), outline="#ef4444", width=max(4, image.width // 300))
        label = f"Paddle table {index}"
        ty = max(0, y0 - 30)
        draw.rectangle(draw.textbbox((x0, ty), label, font=font), fill="#ef4444")
        draw.text((x0, ty), label, fill="white", font=font)
    overlay_name = f"{base}__paddle.png"
    overlay.save(folder / overlay_name, format="PNG")
    return {"page": source_name, "overlay": overlay_name, "crops": crops}


def _candidate_decision(image: Image.Image, page, block: dict, ocr_lines: list[dict]) -> dict:
    bbox = _bbox(block["bbox"])
    assert bbox is not None
    lines, source = _evidence_lines(page, bbox, ocr_lines)
    synthetic = {
        "page_no": page.page_no,
        "table_areas": [],
        "regions": [{
            "region_id": f"p{page.page_no}_r001", "bbox": bbox,
            "layout_observation": {"label": "table"},
            "kind": "text", "text": "", "semantic_labels": [],
            "lines": lines,
        }],
    }
    analyze_page_relations(image, synthetic)
    decision = synthetic["semantic_structures"][0]
    return {
        "bbox": bbox,
        "paddle_text": str(block.get("content") or ""),
        "evidence_source": source,
        "evidence_lines": lines,
        "vlm": decision,
    }


def _esc(value: object) -> str:
    return html.escape(str(value or "")).replace("\n", "<br>")


def build_report(documents: list[dict], title: str) -> str:
    tabs: list[str] = []
    panes: list[str] = []
    for doc_no, document in enumerate(documents):
        for page in document.get("pages") or []:
            key = f"d{doc_no}p{page['page_no']}"
            name = _esc(document["source_file"])
            tabs.append(f"<button class='tab' data-key='{key}'>{name} p{page['page_no']}</button>")
            if page.get("error"):
                body = f"<p class='error'>{_esc(page['error'])}</p>"
            else:
                overlay = quote(page["images"]["overlay"])
                count = len(page.get("candidates") or [])
                cards = []
                for index, candidate in enumerate(page.get("candidates") or [], start=1):
                    vlm = candidate["vlm"]
                    crop = page["images"]["crops"][index - 1]
                    links = f"<a href='images/{quote(crop)}' target='_blank'>확대 PNG</a>" if crop else ""
                    relations = "".join(
                        f"<tr><td>{_esc(rel.get('context'))}</td><td>{_esc(rel.get('key'))}</td>"
                        f"<td>{_esc(rel.get('value'))}</td></tr>"
                        for rel in vlm.get("relations") or []
                    )
                    grid = (
                        "<table><tr><th>상위 분류</th><th>조건·항목</th><th>값·설명</th></tr>"
                        f"{relations}</table>"
                    ) if relations else "<p class='muted'>검증된 의미 짝 없음</p>"
                    issues = vlm.get("quality_issues") or []
                    quality = (
                        f"<p class='error'>자동 점검: {_esc(', '.join(issues))}</p>"
                        if issues else ""
                    )
                    if "rate_value_coverage" in vlm:
                        quality += f"<p>금리 값 줄 연결률: {_esc(vlm['rate_value_coverage'])}</p>"
                    cards.append(
                        f"<article><h3>Paddle 표 후보 {index} · VLM: {_esc(vlm.get('verified_kind') or 'unresolved')}"
                        f" ({_esc(vlm.get('status'))})</h3>"
                        f"<p>bbox [{', '.join(map(str, candidate['bbox']))}] · 텍스트 근거: "
                        f"{_esc(candidate['evidence_source'])} · {len(candidate['evidence_lines'])}줄 · "
                        f"VLM 자신도 {_esc(vlm.get('confidence'))} · {links}</p>"
                        f"<p>{_esc(vlm.get('analysis') or vlm.get('reason'))}</p>{quality}"
                        f"{grid}"
                        f"<details><summary>Paddle가 읽은 원문과 근거 줄</summary>"
                        f"<pre>{_esc(candidate['paddle_text'])}</pre>"
                        f"<pre>{_esc(chr(10).join(line['text'] for line in candidate['evidence_lines']))}</pre>"
                        f"</details></article>"
                    )
                body = (
                    f"<p>{_esc(page['input_mode'])} · Paddle table 후보 {count}개 · "
                    f"<a href='images/{overlay}' target='_blank'>전체 페이지 bbox PNG</a></p>"
                    f"<div class='split'><div class='page'><img src='images/{overlay}' alt='Paddle 표 후보'></div>"
                    f"<div>{''.join(cards) if cards else '<article>표 후보 없음</article>'}</div></div>"
                )
            panes.append(f"<section class='pane' data-key='{key}'>{body}</section>")
    return f"""<!doctype html><html lang='ko'><head><meta charset='utf-8'>
<title>{_esc(title)}</title><meta name='viewport' content='width=device-width,initial-scale=1'>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#f5f7fa;color:#17212d;font:14px/1.5 Segoe UI,Malgun Gothic,sans-serif}}
header{{position:sticky;top:0;background:white;border-bottom:1px solid #cbd5e1;padding:10px 16px;z-index:2}}
h1{{font-size:16px;margin:0 0 8px}}.tabs{{display:flex;gap:5px;overflow:auto;white-space:nowrap}}
.tab{{border:1px solid #cbd5e1;border-radius:5px;background:white;padding:6px;cursor:pointer}}
.tab.on{{background:#1e293b;color:white}}.pane{{display:none;padding:12px}}.pane.on{{display:block}}
.split{{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px;align-items:start}}
.page{{position:sticky;top:85px;max-height:calc(100vh - 100px);overflow:auto;background:white}}
.page img{{width:100%;display:block}}article{{border:1px solid #cbd5e1;background:white;border-radius:6px;padding:10px;margin-bottom:10px}}
h3{{font-size:14px;margin:0 0 6px}}p{{margin:5px 0 9px}}a{{color:#1d4ed8}}table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #cbd5e1;padding:5px;vertical-align:top;word-break:break-word}}th{{background:#eff6ff}}
pre{{white-space:pre-wrap;word-break:break-word;max-height:250px;overflow:auto}}.muted{{color:#64748b}}.error{{color:#b91c1c}}
@media(max-width:900px){{.split{{grid-template-columns:1fr}}.page{{position:static}}}}
</style></head><body><header><h1>{_esc(title)}</h1>
<p class='muted'>전체 페이지 Paddle 표 후보 → 후보 이미지와 원문 줄을 이용한 VLM 판정.
verified는 줄 ID 연결의 자동 점검 통과를 뜻하며 의미 관계의 정답을 보증하지 않습니다.
표 후보 0개도 페이지를 직접 확인하세요.</p><div class='tabs'>{''.join(tabs)}</div></header>
{''.join(panes)}<script>
const tabs=[...document.querySelectorAll('.tab')],panes=[...document.querySelectorAll('.pane')];
function show(key){{tabs.forEach(x=>x.classList.toggle('on',x.dataset.key===key));panes.forEach(x=>x.classList.toggle('on',x.dataset.key===key));}}
tabs.forEach(x=>x.onclick=()=>show(x.dataset.key));if(tabs.length)show(tabs[0].dataset.key);
</script></body></html>"""


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Paddle 전체 페이지 표 검출 및 VLM 후보 검증")
    parser.add_argument("--input", type=Path, nargs="+", required=True)
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--force", action="append", default=[], help="기존 결과 중 파일명에 포함된 문자열만 재실행")
    args = parser.parse_args()
    # HWP는 반드시 사내 원본 구조 파서와 HTML 렌더 경로를 쓴다.
    os.environ["HWP_STRUCTURE_ENGINE"] = "document_processor"
    os.environ["HWP_RENDER_BACKEND"] = "document_processor_html"
    os.environ["STRUCTURED_DOCUMENT_ROUTE"] = "off"
    profile = Profile.from_env()
    out = OUTPUT_ROOT / args.run_name
    out.mkdir(parents=True, exist_ok=True)
    sources = iter_inputs(list(args.input), [])
    documents: list[dict] = []

    def flush() -> None:
        # 재사용 문서까지 포함한 전체 목록을 매번 다시 쓴다. 한 건만 남는 것을 막는다.
        _write_json(out / "results.json", documents)
        (out / "report.html").write_text(
            build_report(documents, f"Paddle 표 검출 → VLM 검증 — {args.run_name}"), encoding="utf-8",
        )

    for source in sources:
        safe = _safe(source.name)
        result_file = out / "final" / f"{safe}.table-audit.json"
        if result_file.exists() and not any(token in source.name for token in args.force):
            documents.append(json.loads(result_file.read_text(encoding="utf-8")))
            print(f"재사용: {source.name}", flush=True)
            flush()
            continue
        document: dict = {"source_file": source.name, "file_type": source.suffix.lower().lstrip("."), "pages": []}
        try:
            pages = load_pages(source)
        except (Exception, SystemExit) as exc:
            document["pages"].append({"page_no": 1, "error": f"{type(exc).__name__}: {exc}"})
            pages = []
        for page in pages:
            page_result: dict = {
                "page_no": page.page_no,
                "input_mode": "whole_page",
                "canvas": list(page.image.size),
                "text_engine": "document_processor" if source.suffix.lower() in {".hwp", ".hwpx"} else "pdfium_or_ocr",
                "candidates": [],
            }
            try:
                response = paddlex.call(
                    page.image, url=profile.paddlex_url,
                    payload=profile.request_payload, timeout=profile.timeout,
                    fmt=profile.encode,
                )
                blocks = paddlex.parsing_boxes(response["pruned"])
                ocr_lines = paddlex.ocr_lines(response["pruned"])
                table_blocks = [
                    block for block in blocks
                    if str(block.get("label") or "").casefold() == "table" and _bbox(block.get("bbox"))
                ]
                base = f"{safe}__p{page.page_no:03d}"
                page_result["images"] = _save_images(
                    page.image, [_bbox(block["bbox"]) for block in table_blocks], out / "images", base,
                )
                page_result["paddle_layout_count"] = len(blocks)
                page_result["paddle_ocr_line_count"] = len(ocr_lines)
                for index, block in enumerate(table_blocks, start=1):
                    item = _candidate_decision(page.image, page, block, ocr_lines)
                    page_result["candidates"].append(item)
                    print(
                        f"  표 {index}: VLM={item['vlm'].get('verified_kind') or 'unresolved'}, "
                        f"관계 {len(item['vlm'].get('relations') or [])}개",
                        flush=True,
                    )
                print(f"· {source.name} p{page.page_no}: 전체 페이지, Paddle 표 {len(table_blocks)}개", flush=True)
            except Exception as exc:
                page_result["error"] = f"{type(exc).__name__}: {exc}"
                print(f"· {source.name} p{page.page_no}: 오류 {exc}", flush=True)
            document["pages"].append(page_result)
            _write_json(result_file, document)
        if not pages:
            _write_json(result_file, document)
        documents.append(document)
        flush()
    flush()
    summary = {
        "files": len(documents),
        "pages": sum(len(doc.get("pages") or []) for doc in documents),
        "paddle_tables": sum(len(page.get("candidates") or []) for doc in documents for page in doc.get("pages") or []),
        "vlm_verified": sum(
            item["vlm"].get("status") == "verified"
            for doc in documents for page in doc.get("pages") or [] for item in page.get("candidates") or []
        ),
        "vlm_partial": sum(
            item["vlm"].get("status") == "partial"
            for doc in documents for page in doc.get("pages") or [] for item in page.get("candidates") or []
        ),
        "vlm_visual_only": sum(
            item["vlm"].get("status") == "visual_only"
            for doc in documents for page in doc.get("pages") or [] for item in page.get("candidates") or []
        ),
        "vlm_rejected": sum(
            item["vlm"].get("status") == "rejected"
            for doc in documents for page in doc.get("pages") or [] for item in page.get("candidates") or []
        ),
        "errors": [
            {"source_file": doc["source_file"], "page_no": page["page_no"], "error": page["error"]}
            for doc in documents for page in doc.get("pages") or [] if page.get("error")
        ],
    }
    _write_json(out / "summary.json", summary)
    print(f"완료: {out / 'report.html'}", flush=True)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
