"""HWP를 최종 검토 화면과 같은 self-contained HTML surface로 만든다.

이 HTML은 원본 한컴 화면의 픽셀 복제가 아니라 DocIR의 문단/표/스타일을 재구성한
검토 화면이다. 대신 모든 요소에 안정적인 ``data-node-id``가 있고, 브라우저 DOM bbox와
심의 결과를 같은 좌표계에서 바로 연결할 수 있다.
"""
from __future__ import annotations

import json
import html as html_lib
import re
import subprocess
from pathlib import Path
from typing import Any


_STYLE = """
<style id="nh-review-surface-style">
:root {
  --nh-page-width: __PAGE_WIDTH__pt;
  --nh-page-height: __PAGE_HEIGHT__pt;
}
.document-page {
  box-sizing: border-box !important;
  width: var(--nh-page-width) !important;
  height: var(--nh-page-height) !important;
  min-height: var(--nh-page-height) !important;
  overflow: hidden !important;
}
[data-node-id][data-nh-highlight="true"] {
  outline: 3px solid #ff9800 !important;
  outline-offset: 2px;
  background-color: rgba(255, 235, 59, 0.34) !important;
}
@media print {
  @page { size: __PAGE_WIDTH__pt __PAGE_HEIGHT__pt; margin: 0; }
  html, body { margin: 0 !important; padding: 0 !important; background: white !important; }
  .document-page {
    margin: 0 !important;
    box-shadow: none !important;
    border: 0 !important;
    break-after: page;
    page-break-after: always;
  }
  .document-page:last-child { break-after: auto; page-break-after: auto; }
}
</style>
"""


_SCRIPT = r"""
<script id="nh-review-surface-api">
(() => {
  const elements = () => Array.from(document.querySelectorAll('[data-node-id]'));
  const pageOf = (element) => element.closest('.document-page');
  const fitPages = () => {
    Array.from(document.querySelectorAll('.document-page')).forEach(page => {
      const content = page.querySelector('.document-page__content');
      if (!content) return;
      content.style.transform = '';
      content.style.transformOrigin = 'top left';
      const widthScale = page.clientWidth / Math.max(content.scrollWidth, 1);
      const heightScale = page.clientHeight / Math.max(content.scrollHeight, 1);
      const scale = Math.min(1, widthScale, heightScale);
      page.dataset.nhScale = String(scale);
      if (scale < 0.999) content.style.transform = `scale(${scale})`;
    });
  };
  const rectFor = (element) => {
    const rect = element.getBoundingClientRect();
    const page = pageOf(element);
    const pageRect = page ? page.getBoundingClientRect() : {left: 0, top: 0};
    return {
      node_id: element.dataset.nodeId,
      page_no: page ? Number(page.dataset.pageNumber || page.dataset.page || 1) : 1,
      bbox: [
        rect.left - pageRect.left,
        rect.top - pageRect.top,
        rect.right - pageRect.left,
        rect.bottom - pageRect.top,
      ].map(value => Math.round(value * 100) / 100),
      text: (element.innerText || element.textContent || '').trim(),
    };
  };
  const rowBoxes = () => {
    const rows = [];
    const pushValues = (nodeId, page, values) => {
      if (!values.length) return;
      const left = Math.min(...values.map(cell => cell.bbox[0]));
      const top = Math.min(...values.map(cell => cell.bbox[1]));
      const right = Math.max(...values.map(cell => cell.bbox[2]));
      const bottom = Math.max(...values.map(cell => cell.bbox[3]));
      rows.push({
        node_id: nodeId,
        page_no: page ? Number(page.dataset.pageNumber || page.dataset.page || 1) : 1,
        bbox: [left, top, right, bottom],
        text: values.map(cell => cell.text).join('\n'),
        cells: values,
      });
    };
    document.querySelectorAll('.document-page table').forEach((table, tableIndex) => {
      Array.from(table.rows || []).forEach((row, rowIndex) => {
        const cells = Array.from(row.cells || []);
        // 중첩 표의 컨테이너 행은 자식 행의 원문을 다시 포함한다.
        if (cells.some(cell => cell.querySelector(':scope > table'))) return;
        const valueFor = (cell, colIndex) => {
          const rect = rectFor(cell);
          return {
            node_id: cell.dataset.nodeId || '',
            col: colIndex,
            col_span: Number(cell.colSpan || 1),
            row_span: Number(cell.rowSpan || 1),
            bbox: rect.bbox,
            text: (cell.innerText || cell.textContent || '').trim(),
          };
        };
        let values = cells.map(valueFor).filter(cell => cell.text);
        // 조판용 부모 행의 표제어가 왼쪽 셀에 있고 실제 값은 오른쪽 중첩 표에
        // 있는 경우, 자식 행에 부모 표제어 셀을 붙인다. 010의 '원금 및 이자
        // 상환방법'처럼 표제어를 잃지 않으면서 부모/자식 원문 중복도 만들지 않는다.
        const ownerCell = row.closest('td');
        if (ownerCell && ownerCell.parentElement) {
          const context = Array.from(ownerCell.parentElement.cells || [])
            .filter(cell => cell !== ownerCell && !cell.querySelector(':scope > table'))
            .map(valueFor)
            .filter(cell => cell.text);
          values = [...context, ...values];
        }
        if (!values.length) return;
        const page = pageOf(row);
        const height = Math.max(...values.map(cell => cell.bbox[3]))
          - Math.min(...values.map(cell => cell.bbox[1]));
        const largeCells = cells.filter(cell =>
          cell.querySelectorAll(':scope > p[data-node-id]').length >= 3
        );
        // 한 셀에 페이지 대부분의 본문을 넣은 LMS형 문서는 행 하나로 내보내지
        // 않는다. 화면에 실제 배치된 문단 bbox를 사용해 의미 단위 후보로 쪼갠다.
        if (page && height > page.clientHeight * 0.30 && largeCells.length) {
          const headerValues = cells
            .filter(cell => !largeCells.includes(cell))
            .flatMap((cell, colIndex) => {
              const paragraphs = Array.from(
                cell.querySelectorAll(':scope > p[data-node-id]')
              ).map(paragraph => valueFor(paragraph, colIndex)).filter(value => value.text);
              return paragraphs.length ? paragraphs : [valueFor(cell, colIndex)];
            })
            .filter(cell => cell.text);
          pushValues(
            `domrow:${table.dataset.nodeId || tableIndex}:${rowIndex}:heading`,
            page,
            headerValues,
          );
          largeCells.forEach((cell, cellIndex) => {
            Array.from(cell.querySelectorAll(':scope > p[data-node-id]'))
              .map((paragraph, paragraphIndex) => ({
                ...valueFor(paragraph, cellIndex),
                col_span: Number(cell.colSpan || 1),
                row_span: 1,
                paragraph_index: paragraphIndex,
              }))
              .filter(value => value.text)
              .forEach(value => pushValues(
                `domparagraph:${value.node_id || cellIndex}:${value.paragraph_index}`,
                page,
                [value],
              ));
          });
          return;
        }
        pushValues(
          `domrow:${table.dataset.nodeId || tableIndex}:${rowIndex}`,
          page,
          values,
        );
      });
    });
    return rows;
  };
  const publish = () => {
    let target = document.getElementById('nh-review-surface-data');
    if (!target) {
      target = document.createElement('script');
      target.id = 'nh-review-surface-data';
      target.type = 'application/json';
      document.body.appendChild(target);
    }
    target.textContent = JSON.stringify({rows: rowBoxes()});
  };
  window.nhReviewSurface = {
    fitPages,
    rows: rowBoxes,
    boxes() { return elements().map(rectFor); },
    highlight(nodeIds) {
      const selected = new Set(nodeIds || []);
      elements().forEach(element => {
        if (selected.has(element.dataset.nodeId)) element.dataset.nhHighlight = 'true';
        else delete element.dataset.nhHighlight;
      });
      return this.boxes().filter(item => selected.has(item.node_id));
    },
    clear() { return this.highlight([]); },
  };
  fitPages();
  publish();
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => {
    fitPages();
    publish();
  });
})();
</script>
"""


def inject_review_surface_api(
    html: str, *, page_width_pt: float = 595.28, page_height_pt: float = 841.89,
) -> str:
    output = html
    style = _STYLE.replace("__PAGE_WIDTH__", str(round(page_width_pt, 2))).replace(
        "__PAGE_HEIGHT__", str(round(page_height_pt, 2))
    )
    output = output.replace("</head>", f"{style}\n</head>", 1)
    output = output.replace("</body>", f"{_SCRIPT}\n</body>", 1)
    return output


def render_hwp_review_surface(path: Path, output_dir: Path) -> dict[str, Any]:
    """사내 파서의 clean review HTML과 표시 방식 메타데이터를 생성한다."""
    from document_processor import DocIR, DocumentInput, render_review_html

    docir = DocIR.from_file(str(path))
    rendered = render_review_html(
        document=DocumentInput(doc_ir=docir), annotations=[], title=path.stem,
    )
    if not rendered.ok:
        raise RuntimeError(f"HWP review HTML 생성 실패: {rendered.issues}")

    output_dir.mkdir(parents=True, exist_ok=True)
    html_path = output_dir / f"{path.stem}.review.html"
    first_page = next(iter(getattr(docir, "pages", None) or []), None)
    page_width_pt = float(getattr(first_page, "width_pt", None) or 595.28)
    page_height_pt = float(getattr(first_page, "height_pt", None) or 841.89)
    html_path.write_text(
        inject_review_surface_api(
            rendered.html,
            page_width_pt=page_width_pt,
            page_height_pt=page_height_pt,
        ),
        encoding="utf-8",
    )
    manifest = {
        "source_file": path.name,
        "surface": "reconstructed_html",
        "coordinate_system": "browser_css_pixels_relative_to_document_page",
        "fidelity": "structural_reconstruction",
        "page_count": len(getattr(docir, "pages", None) or []) or 1,
        "page_css_size": [round(page_width_pt * 4 / 3, 2), round(page_height_pt * 4 / 3, 2)],
        "html": html_path.name,
        "bbox_api": "window.nhReviewSurface.boxes()",
        "highlight_api": "window.nhReviewSurface.highlight(nodeIds)",
        "limitations": [
            "원본 한컴 화면의 픽셀 동일 렌더가 아님",
            "floating table/image placement와 wrapping은 document-processor 지원 범위에 따름",
            "사용자 화면과 bbox 판정에는 반드시 같은 HTML을 사용해야 함",
        ],
    }
    manifest_path = output_dir / f"{path.stem}.review.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return {**manifest, "html_path": str(html_path), "manifest_path": str(manifest_path)}


def read_dom_rows(html_path: Path, browser: str, *, timeout: int = 300) -> list[dict[str, Any]]:
    """headless Chromium이 실제 배치한 표 행과 셀 bbox를 읽는다."""
    completed = subprocess.run(
        [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--dump-dom",
            html_path.resolve().as_uri(),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown error").strip()[-1500:]
        raise RuntimeError(f"HTML DOM bbox 추출 실패(exit={completed.returncode}): {detail}")
    match = re.search(
        r'<script[^>]+id=["\']nh-review-surface-data["\'][^>]*>(.*?)</script>',
        completed.stdout,
        re.DOTALL | re.IGNORECASE,
    )
    if not match:
        raise RuntimeError("HTML DOM bbox 결과를 찾을 수 없습니다")
    payload = json.loads(html_lib.unescape(match.group(1)))
    return list(payload.get("rows") or [])


def dom_rows_to_blocks(
    rows: list[dict[str, Any]],
    *,
    page_no: int,
    canvas: tuple[int, int],
    page_css_size: tuple[float, float],
) -> list[dict[str, Any]]:
    """표시 HTML의 CSS 좌표를 파이프라인 캔버스 좌표의 행 Region으로 바꾼다."""
    css_width, css_height = page_css_size
    scale_x = canvas[0] / max(css_width, 1.0)
    scale_y = canvas[1] / max(css_height, 1.0)

    def pixels(values: Any) -> list[int] | None:
        if not isinstance(values, list) or len(values) != 4:
            return None
        x0, y0, x1, y1 = (float(value) for value in values)
        box = [round(x0 * scale_x), round(y0 * scale_y), round(x1 * scale_x), round(y1 * scale_y)]
        box[0] = max(0, min(canvas[0], box[0]))
        box[2] = max(0, min(canvas[0], box[2]))
        box[1] = max(0, min(canvas[1], box[1]))
        box[3] = max(0, min(canvas[1], box[3]))
        return box if box[2] > box[0] and box[3] > box[1] else None

    blocks: list[dict[str, Any]] = []
    for row in rows:
        if int(row.get("page_no") or 1) != page_no:
            continue
        bbox = pixels(row.get("bbox"))
        cells = []
        for cell in row.get("cells") or []:
            text = str(cell.get("text") or "").strip()
            cell_bbox = pixels(cell.get("bbox"))
            if not text or cell_bbox is None:
                continue
            cells.append({
                "row": 0,
                "col": int(cell.get("col") or 0),
                "row_span": max(1, int(cell.get("row_span") or 1)),
                "col_span": max(1, int(cell.get("col_span") or 1)),
                "is_header": len(cells) == 0 and len(row.get("cells") or []) > 1,
                "text": text,
                "bbox": cell_bbox,
                "node_id": str(cell.get("node_id") or ""),
            })
        text = "\n".join(cell["text"] for cell in cells)
        if bbox is None or not text:
            continue
        is_table = len(cells) >= 2
        block: dict[str, Any] = {
            "bbox": bbox,
            "label": "table" if is_table else "text",
            "kind": "table" if is_table else "text",
            "content": text,
            "text_source": "document_processor_html",
            "bbox_source": "reconstructed_html_dom",
            "bbox_quality": "display_exact",
            "structured": {
                "source": "document_processor_html",
                "node_kind": "dom_table_row",
                "node_ids": [cell["node_id"] for cell in cells if cell["node_id"]],
                "surface_node_id": str(row.get("node_id") or ""),
            },
        }
        if is_table:
            cols = max(cell["col"] + cell["col_span"] for cell in cells)
            block["table"] = {
                "source": "document_processor",
                "grid": {"rows": 1, "cols": max(1, cols)},
                "cells": cells,
                "notes": ["reconstructed_html_dom"],
            }
        blocks.append(block)
    blocks.sort(key=lambda block: (block["bbox"][1], block["bbox"][0]))
    for order, block in enumerate(blocks, start=1):
        block["order"] = order
        block["piece"] = 0
    return blocks
