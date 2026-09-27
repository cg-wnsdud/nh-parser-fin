"""P3 결과를 한 장짜리 HTML로 본다.

Label Studio 는 라벨을 **고치는** 도구다. 지금 필요한 것은 파이프라인이 무엇을
내놨는지 빠르게 **읽는** 것이라 화면이 과하다. 여기서는 왼쪽에 렌더된 페이지와
영역 박스를, 오른쪽에 읽기 순서대로 나열한 최종 결과를 놓는다.

이미지는 base64 로 묻어 파일 하나로 끝낸다. 경로가 깨지지 않아 그대로 전달할 수 있다.
"""
from __future__ import annotations

import argparse
import base64
import html
import io
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from PIL import Image

from nh_parser_fin.config import MEDIA_DIR, OUTPUT_ROOT

# 화면에서 읽을 수 있으면 충분하다. 원본을 그대로 묻으면 파일이 수십 MB가 된다.
VIEW_MAX_SIDE = 1500
JPEG_QUALITY = 78

PRODUCT_COLORS = {
    "product_1": "#1E88E5",
    "product_2": "#8E24AA",
    "product_3": "#00897B",
    "product_4": "#F4511E",
    "page_common": "#546E7A",
    "unknown": "#C62828",
}


def _color(product_id: str | None) -> str:
    return PRODUCT_COLORS.get(str(product_id or "unknown"), "#C62828")


def _thumb(path: Path) -> tuple[str, int, int]:
    """페이지 이미지를 화면용 크기로 줄여 data URI 로 만든다."""
    with Image.open(path) as image:
        page = image.convert("RGB")
        width, height = page.size
        scale = min(1.0, VIEW_MAX_SIDE / max(page.size))
        if scale < 1.0:
            page = page.resize(
                (max(1, round(page.width * scale)), max(1, round(page.height * scale))),
                Image.LANCZOS,
            )
        buffer = io.BytesIO()
        page.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    # 박스는 원본 canvas 좌표라 원본 크기를 함께 돌려준다.
    return f"data:image/jpeg;base64,{encoded}", width, height


def media_index(run: Path) -> dict[tuple[str, int], Path]:
    """(파일명, 페이지) → 렌더 이미지 경로.

    파일명에서 경로를 다시 만들지 않는다. `run.py`가 `doc_id`(확장자를 뗀 이름)로
    파일명을 만들기 때문에 여기서 복원하면 어긋난다. 이미 정확한 매핑을 가진
    `label-studio.json`을 그대로 읽는다.
    """
    compact_index = run / "media-index.json"
    if compact_index.exists():
        entries = json.loads(compact_index.read_text(encoding="utf-8"))
        index: dict[tuple[str, int], Path] = {}
        for entry in entries:
            name = unquote(str(entry["image_name"]))
            path = run / "images" / name
            if not path.exists():
                path = MEDIA_DIR / name
            if path.exists():
                index[(str(entry["source_file"]), int(entry["page_no"]))] = path
        return index
    tasks = json.loads((run / "label-studio.json").read_text(encoding="utf-8"))
    index: dict[tuple[str, int], Path] = {}
    for task in tasks:
        data = task["data"]
        name = unquote(str(data["image"]).split("pages/", 1)[-1])
        path = MEDIA_DIR / name
        if path.exists():
            index[(str(data["source_file"]), int(data["page_no"]))] = path
    return index


def _region_html(region: dict[str, Any]) -> str:
    product = str(region.get("product_id") or "unknown")
    entries = region.get("labels") or []
    chips = [
        f"<span class='chip' style='background:{_color(product)}'>{html.escape(product)}</span>",
    ]
    # 한 영역이 구분값을 여럿 가질 수 있다. 대표 하나만 보이면 나머지가 화면에서
    # 사라져, 영역을 쪼개지 않기로 한 판단을 검증할 수 없다.
    if entries:
        chips += [
            f"<span class='chip lab'>{html.escape(str(label))}</span>"
            for label in entries
        ]
    else:
        chips.append("<span class='chip none'>라벨 없음</span>")
    if region.get("kind") == "table":
        chips.append("<span class='chip alt'>표</span>")
    if region.get("needs_review"):
        chips.append("<span class='chip rev'>검수</span>")

    text = html.escape(str(region.get("selected_text") or "")).replace("\n", "<br>")
    body = f"<div class='text'>{text or '<i>(빈 텍스트)</i>'}</div>"

    return (
        f"<li class='region' id='r-{html.escape(str(region['region_id']))}' "
        f"data-rid='{html.escape(str(region['region_id']))}'>"
        f"<div class='head'><span class='rid'>{html.escape(str(region['region_id']))}</span>"
        f"{''.join(chips)}</div>{body}</li>"
    )


def _paddle_preview_html(preview: dict[str, Any] | None) -> str:
    if not preview:
        return ""
    count = int(preview.get("paddle_table_count") or 0)
    mode = "전체 페이지" if preview.get("whole_page_input") else f"입력 방식: {preview.get('tiling') or '미상'}"
    full = html.escape(str(preview.get("full_image") or ""))
    crops = "".join(
        f"<a href='paddle-tables/{html.escape(str(name))}' target='_blank'>표 후보 {index} 확대</a> "
        for index, name in enumerate(preview.get("crops") or [], start=1)
    )
    return (
        "<details class='paddle-preview'><summary>"
        f"Paddle 표 검출: {count}개 · {html.escape(mode)}</summary>"
        f"<a href='paddle-tables/{full}' target='_blank'>전체 크기 PNG 열기</a> {crops}"
        f"<img src='paddle-tables/{full}' alt='Paddle 표 검출 위치'></details>"
    )


def _boxes_svg(page: dict[str, Any], width: int, height: int) -> str:
    parts = [
        f"<svg viewBox='0 0 {width} {height}' preserveAspectRatio='none' class='overlay'>"
    ]
    for region in page["regions"]:
        box = region.get("bbox")
        if not box:
            continue
        x0, y0, x1, y1 = (float(v) for v in box)
        color = _color(region.get("product_id"))
        rid = html.escape(str(region["region_id"]))
        font = max(11.0, min(width, height) / 55)
        parts.append(
            f"<g class='box' data-rid='{rid}'>"
            f"<rect x='{x0}' y='{y0}' width='{max(1.0, x1 - x0)}' "
            f"height='{max(1.0, y1 - y0)}' stroke='{color}' fill='{color}' />"
            f"<text x='{x0 + 3}' y='{y0 + font}' fill='{color}' "
            f"font-size='{font}'>{rid}</text></g>"
        )
    parts.append("</svg>")
    return "".join(parts)


def _summary_html(document: dict[str, Any], page: dict[str, Any]) -> str:
    regions = page["regions"]
    rows = [
        ("Region", len(regions)),
        ("표", sum(1 for r in regions if r.get("kind") == "table")),
        ("복수 라벨", sum(1 for r in regions if len(r.get("labels") or []) > 1)),
        ("라벨 없음", sum(1 for r in regions if not r.get("labels"))),
        ("검수 필요", sum(1 for r in regions if r.get("needs_review"))),
    ]
    cells = "".join(
        f"<div class='stat'><b>{value}</b><span>{html.escape(name)}</span></div>"
        for name, value in rows
    )
    templates = []
    for item in document.get("review_units") or []:
        product_id = str(item.get("product_id") or "unknown")
        name = item.get("product_name")
        templates.append(
            f"<tr><td><span class='chip' style='background:{_color(product_id)}'>"
            f"{html.escape(product_id)}</span></td>"
            f"<td>{html.escape(str(name or '—'))}</td>"
            f"<td>{html.escape(str(item.get('template_id') or '—'))}</td>"
            f"<td>{len(item.get('region_ids') or [])}개</td></tr>"
        )
    table = (
        "<table class='meta'><tr><th>상품</th><th>이름</th><th>템플릿</th>"
        f"<th>Region</th></tr>{''.join(templates)}</table>"
        if templates else ""
    )
    return f"<div class='stats'>{cells}</div>{table}"


def build(run: Path, title: str) -> str:
    aggregate = run / "06-p3.json"
    p3 = (
        json.loads(aggregate.read_text(encoding="utf-8"))
        if aggregate.exists()
        else [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((run / "final").glob("*.p3.json"))
        ]
    )
    media = media_index(run)
    preview_path = run / "paddle-table-index.json"
    previews = {
        (str(item["source_file"]), int(item["page_no"])): item
        for item in json.loads(preview_path.read_text(encoding="utf-8"))
    } if preview_path.exists() else {}
    tabs, panes = [], []
    missing = []

    for doc_index, document in enumerate(p3):
        source_file = str(document["document"]["source_file"])
        for page in document["pages"]:
            page_no = int(page["page_no"])
            key = f"p{doc_index}_{page_no}"
            image_path = media.get((source_file, page_no))
            if image_path is None:
                missing.append(f"{source_file} p{page_no}")
                continue
            uri, width, height = _thumb(image_path)
            label = html.escape(source_file)
            if len(document["pages"]) > 1:
                label += f" <small>p{page_no}</small>"
            tabs.append(
                f"<button class='tab' data-key='{key}'>{label}</button>"
            )
            regions = "".join(_region_html(region) for region in page["regions"])
            panes.append(
                f"<section class='pane' data-key='{key}'>"
                f"{_summary_html(document, page)}"
                f"{_paddle_preview_html(previews.get((source_file, page_no)))}"
                f"<div class='split'>"
                f"<div class='left'><div class='canvas'>"
                f"<img src='{uri}' alt='{html.escape(source_file)}'>"
                f"{_boxes_svg(page, width, height)}</div></div>"
                f"<div class='right'><ol class='regions'>{regions}</ol></div>"
                f"</div></section>"
            )

    warn = (
        f"<div class='warn top'>렌더 이미지를 찾지 못한 페이지: {html.escape(', '.join(missing))}</div>"
        if missing else ""
    )
    return _SHELL.format(
        title=html.escape(title),
        warn=warn,
        tabs="".join(tabs),
        panes="".join(panes),
    )


_SHELL = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ --line:#d8dde3; --bg:#f6f7f9; --ink:#1c2430; --muted:#68727f; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font:14px/1.6 -apple-system,"Segoe UI","Malgun Gothic",sans-serif;
  color:var(--ink); background:var(--bg); }}
header {{ position:sticky; top:0; z-index:5; background:#fff;
  border-bottom:1px solid var(--line); padding:10px 16px; }}
h1 {{ font-size:15px; margin:0; flex:none; }}
.bar {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}
/* 문서가 수십 건이면 탭 목록만으로 화면 절반을 덮는다. 기본은 접고, 지금 보고 있는
   문서 이름을 버튼에 띄워 접힌 상태에서도 위치를 알 수 있게 한다. */
.toggle {{ font:inherit; font-size:12px; padding:5px 10px; border:1px solid var(--line);
  background:#fff; border-radius:6px; cursor:pointer; color:var(--ink);
  display:flex; align-items:center; gap:8px; max-width:min(70vw, 720px); }}
.toggle:hover {{ border-color:#9aa5b1; }}
.toggle .who {{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.toggle .count {{ color:var(--muted); flex:none; }}
.toggle .caret {{ color:var(--muted); flex:none; transition:transform .15s; }}
.toggle[aria-expanded="true"] .caret {{ transform:rotate(180deg); }}
.tabs {{ display:flex; gap:6px; flex-wrap:wrap; margin-top:8px;
  max-height:42vh; overflow:auto; }}
.tabs[hidden] {{ display:none; }}
.tab {{ font:inherit; font-size:12px; padding:5px 10px; border:1px solid var(--line);
  background:#fff; border-radius:6px; cursor:pointer; color:var(--muted); }}
.tab:hover {{ border-color:#9aa5b1; }}
.tab.on {{ background:var(--ink); color:#fff; border-color:var(--ink); }}
.tab small {{ opacity:.7; }}
.pane {{ display:none; padding:12px 16px 40px; }}
.pane.on {{ display:block; }}
.stats {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:8px; }}
.stat {{ background:#fff; border:1px solid var(--line); border-radius:6px;
  padding:6px 12px; min-width:82px; }}
.stat b {{ display:block; font-size:17px; }}
.stat span {{ font-size:11px; color:var(--muted); }}
table.meta {{ border-collapse:collapse; background:#fff; font-size:12px;
  margin-bottom:10px; }}
table.meta th, table.meta td {{ border:1px solid var(--line); padding:4px 10px;
  text-align:left; }}
table.meta th {{ background:#eef1f4; font-weight:600; }}
.split {{ display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:14px;
  align-items:start; }}
/* 자체 스크롤을 준다. 페이지가 세로로 길면 sticky 만으로는 아래쪽 Region 이
   화면 밖으로 잘려, 오른쪽에서 눌러도 보여줄 자리가 없다. */
.left {{ position:sticky; top:86px; max-height:calc(100vh - 100px);
  overflow:auto; scrollbar-width:thin; }}
.canvas {{ position:relative; background:#fff; border:1px solid var(--line);
  border-radius:6px; overflow:hidden; }}
.canvas img {{ display:block; width:100%; }}
svg.overlay {{ position:absolute; inset:0; width:100%; height:100%; }}
svg.overlay rect {{ fill-opacity:.06; stroke-width:4; }}
svg.overlay text {{ font-weight:700; paint-order:stroke; stroke:#fff; stroke-width:3; }}
svg.overlay .box {{ cursor:pointer; }}
svg.overlay .box.hot rect {{ fill-opacity:.3; stroke-width:9; }}
svg.overlay .box.sel rect {{ fill-opacity:.34; stroke-width:12; }}
svg.overlay rect.orphan {{ fill:#00000000; stroke:#C62828; stroke-width:3;
  stroke-dasharray:10 7; }}
svg.overlay rect.span {{ fill:#00000000; stroke:#2E7D32; stroke-width:3;
  stroke-dasharray:6 5; }}
ol.regions {{ list-style:none; margin:0; padding:0; }}
.region {{ background:#fff; border:1px solid var(--line); border-radius:6px;
  padding:8px 10px; margin-bottom:6px; scroll-margin-top:100px; cursor:pointer; }}
.region.hot {{ border-color:var(--ink); box-shadow:0 0 0 2px rgba(28,36,48,.14); }}
.region.sel {{ border-color:#EF6C00; box-shadow:0 0 0 2px rgba(239,108,0,.28); }}
.head {{ display:flex; align-items:center; gap:6px; flex-wrap:wrap; margin-bottom:4px; }}
.seq {{ min-width:22px; height:22px; border-radius:11px; background:var(--ink);
  color:#fff; font-size:11px; display:grid; place-items:center; }}
.rid {{ font:11px ui-monospace,Menlo,Consolas,monospace; color:var(--muted); }}
.chip {{ font-size:11px; color:#fff; border-radius:4px; padding:1px 7px; }}
.chip.lab {{ background:#2E7D32; }}
.chip.none {{ background:#fff; color:#9aa5b1; border:1px dashed #c3cbd4; }}
.chip.alt {{ background:#455A64; }}
.chip.rev {{ background:#EF6C00; }}
.text {{ white-space:pre-wrap; word-break:break-word; }}
.span {{ border-left:3px solid var(--line); padding-left:8px; margin:4px 0; }}
.span b {{ font-size:12px; color:#2E7D32; }}
.span i {{ font-size:12px; color:var(--muted); }}
.note {{ font-size:11px; color:var(--muted); margin-top:4px; }}
.paddle-preview {{ background:#fff; border:1px solid var(--line); border-radius:6px;
  padding:7px 10px; margin-bottom:10px; }}
.paddle-preview summary {{ cursor:pointer; font-weight:600; }}
.paddle-preview a {{ margin-right:10px; font-size:12px; }}
.paddle-preview img {{ display:block; max-width:100%; max-height:70vh; margin-top:8px; }}
.warn {{ color:#B71C1C; font-size:12px; margin-top:4px; }}
.warn.top {{ padding:8px 16px; background:#FFEBEE; }}
h3 {{ font-size:12px; color:var(--muted); margin:14px 0 4px; }}
ul.orphans {{ margin:0; padding-left:18px; }}
.orphan-row {{ font-size:12px; color:#B71C1C; }}
@media (max-width:900px) {{
  .split {{ grid-template-columns:1fr; }}
  .left {{ position:static; }}
}}
</style></head><body>
<header>
<div class="bar"><h1>{title}</h1>
<button type="button" id="toggle" class="toggle" aria-expanded="false" aria-controls="tabs">
<span class="who" id="current">문서 선택</span>
<span class="count" id="count"></span><span class="caret">&#9662;</span></button></div>
<div class="tabs" id="tabs" hidden>{tabs}</div></header>
{warn}
{panes}
<script>
const tabs = [...document.querySelectorAll('.tab')];
const panes = [...document.querySelectorAll('.pane')];
const tabsBox = document.getElementById('tabs');
const toggle = document.getElementById('toggle');
const current = document.getElementById('current');
document.getElementById('count').textContent = tabs.length + '개';

function setOpen(open) {{
  tabsBox.hidden = !open;
  toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
}}
function show(key) {{
  tabs.forEach(t => t.classList.toggle('on', t.dataset.key === key));
  panes.forEach(p => p.classList.toggle('on', p.dataset.key === key));
  const active = tabs.find(t => t.dataset.key === key);
  if (active) current.textContent = active.textContent.trim();
}}
// 탭을 고르면 목록은 닫는다 — 고르자마자 내용이 보여야 한다.
tabs.forEach(t => t.addEventListener('click', () => {{ show(t.dataset.key); setOpen(false); }}));
toggle.addEventListener('click', () => {{
  const opening = tabsBox.hidden;
  setOpen(opening);
  if (opening) tabsBox.querySelector('.tab.on')?.scrollIntoView({{block: 'nearest'}});
}});
document.addEventListener('keydown', e => {{ if (e.key === 'Escape') setOpen(false); }});
// 문서가 몇 건뿐이면 접을 이유가 없다.
setOpen(tabs.length <= 12);
if (tabs.length) show(tabs[0].dataset.key);

// 박스와 오른쪽 항목을 양방향으로 연결한다. 어느 쪽을 봐도 짝을 찾을 수 있어야 한다.
function link(root) {{
  const left = root.querySelector('.left');
  const pick = (rid) => root.querySelectorAll(`[data-rid="${{CSS.escape(rid)}}"]`);
  const mark = (rid, cls, on) => pick(rid).forEach(el => el.classList.toggle(cls, on));

  // 왼쪽은 자체 스크롤이라 창을 건드리지 않고 패널 안에서만 옮긴다. scrollIntoView 를
  // 쓰면 창까지 따라 움직여 sticky 로 고정해 둔 페이지가 제자리를 벗어난다.
  const revealBox = (box) => {{
    if (!left || !box) return;
    const b = box.getBoundingClientRect(), p = left.getBoundingClientRect();
    left.scrollTo({{
      top: left.scrollTop + (b.top - p.top) - (p.height - b.height) / 2,
      behavior: 'smooth',
    }});
  }};

  let chosen = null;
  root.querySelectorAll('[data-rid]').forEach(el => {{
    el.addEventListener('mouseenter', () => mark(el.dataset.rid, 'hot', true));
    el.addEventListener('mouseleave', () => mark(el.dataset.rid, 'hot', false));
    el.addEventListener('click', () => {{
      const rid = el.dataset.rid;
      if (chosen) mark(chosen, 'sel', false);
      mark(rid, 'sel', true);
      chosen = rid;
      // 누른 쪽이 아니라 **반대쪽**을 움직인다. 누른 자리를 스크롤하면 아무 일도
      // 일어나지 않는데, 이전 코드가 양쪽 모두 오른쪽 목록으로 보내고 있었다.
      if (el.closest('.left')) {{
        const item = root.querySelector(`li[data-rid="${{CSS.escape(rid)}}"]`);
        if (item) item.scrollIntoView({{behavior:'smooth', block:'center'}});
      }} else {{
        revealBox(root.querySelector(`.left [data-rid="${{CSS.escape(rid)}}"]`));
      }}
    }});
  }});

}}
panes.forEach(link);
</script></body></html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="P3 결과 HTML 리포트")
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--title", default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    run = OUTPUT_ROOT / args.run_name
    if not (run / "06-p3.json").exists() and not list((run / "final").glob("*.p3.json")):
        raise SystemExit(f"P3 JSON이 없습니다: {run}")
    out = args.out or (run / "report.html")
    out.write_text(
        build(run, args.title or f"NH 광고물 파싱 — {args.run_name}"), encoding="utf-8",
    )
    size = out.stat().st_size / 1_048_576
    print(f"완료: {out}  ({size:.1f} MB)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
