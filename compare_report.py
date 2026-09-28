"""두 실행 결과(P1/P3)를 같은 문서끼리 나란히 비교하는 HTML.

`report.py`는 한 실행을 읽는 화면이다. 실험(예: PDF 디지털 텍스트 제거)의 효과를 보려면
같은 문서의 기준 실행과 실험 실행을 한 화면에서 대조해야 한다. Region ID는 실행마다
다시 매겨지므로 bbox 겹침(IoU)으로 짝을 맞춘 뒤 문구·출처·라벨 차이를 보여준다.

    python compare_report.py --base <기준 실행> --exp <실험 실행> [--only 파일명 ...]
"""
from __future__ import annotations

import argparse
import difflib
import html
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from nh_parser_fin.config import OUTPUT_ROOT
from report import _color, _thumb, media_index

MATCH_IOU = 0.3


def _load(run: Path) -> dict[str, dict[str, Any]]:
    docs: dict[str, dict[str, Any]] = {}
    for path in sorted((run / "final").glob("*.p3.json")):
        p3 = json.loads(path.read_text(encoding="utf-8"))
        p1_path = path.with_name(path.name.replace(".p3.json", ".p1.json"))
        p1 = json.loads(p1_path.read_text(encoding="utf-8")) if p1_path.exists() else {}
        docs[str(p3["document"]["source_file"])] = {"p3": p3, "p1": p1}
    return docs


def _iou(a: list[float], b: list[float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    area = lambda r: max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])
    union = area(a) + area(b) - inter
    return inter / union if union > 0 else 0.0


def _norm(text: str) -> str:
    return "".join(str(text or "").split())


def _similarity(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def _match(base: list[dict], exp: list[dict]) -> tuple[list[tuple[dict, dict, float]], list[dict], list[dict]]:
    """IoU가 큰 쌍부터 1:1로 묶는다."""
    candidates = sorted(
        (
            (_iou(b["bbox"], e["bbox"]), i, j)
            for i, b in enumerate(base) for j, e in enumerate(exp)
            if b.get("bbox") and e.get("bbox")
        ),
        reverse=True,
    )
    used_b, used_e, pairs = set(), set(), []
    for score, i, j in candidates:
        if score < MATCH_IOU:
            break
        if i in used_b or j in used_e:
            continue
        used_b.add(i)
        used_e.add(j)
        pairs.append((base[i], exp[j], score))
    order = {id(r): k for k, r in enumerate(base)}
    pairs.sort(key=lambda item: order[id(item[0])])
    return (
        pairs,
        [r for i, r in enumerate(base) if i not in used_b],
        [r for j, r in enumerate(exp) if j not in used_e],
    )


def _diff_html(a: str, b: str) -> tuple[str, str]:
    """글자 단위 차이. 공백·줄바꿈은 원문대로 두고 비교는 그대로 한다."""
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    left, right = [], []
    for op, i0, i1, j0, j1 in matcher.get_opcodes():
        sa, sb = html.escape(a[i0:i1]), html.escape(b[j0:j1])
        if op == "equal":
            left.append(sa)
            right.append(sb)
        else:
            if sa:
                left.append(f"<del>{sa}</del>")
            if sb:
                right.append(f"<ins>{sb}</ins>")
    fix = lambda parts: "".join(parts).replace("\n", "<br>")
    return fix(left), fix(right)


def _p1_regions(doc: dict, page_no: int) -> dict[str, dict]:
    for page in (doc.get("p1") or {}).get("pages") or []:
        if int(page["page_no"]) == page_no:
            return {str(r["region_id"]): r for r in page.get("regions") or []}
    return {}


def _p1_page(doc: dict, page_no: int) -> dict:
    for page in (doc.get("p1") or {}).get("pages") or []:
        if int(page["page_no"]) == page_no:
            return page
    return {}


def _page_stats(doc: dict, page: dict) -> dict[str, Any]:
    regions = page["regions"]
    p1_page = _p1_page(doc, int(page["page_no"]))
    p1_regions = p1_page.get("regions") or []
    digital_lines = sum(
        1 for r in p1_regions for line in r.get("lines") or [] if line.get("source") == "digital"
    )
    diagnostics = p1_page.get("diagnostics") or {}
    return {
        "Region": len(regions),
        "표(kind=table)": sum(1 for r in regions if r.get("kind") == "table"),
        "라벨 부착 Region": sum(1 for r in regions if r.get("labels")),
        "라벨 없음": sum(1 for r in regions if not r.get("labels")),
        "라벨 총수": sum(len(r.get("labels") or []) for r in regions),
        "검수 필요": sum(1 for r in regions if r.get("needs_review")),
        "P3 출처": dict(Counter(str(r.get("text_source")) for r in regions)),
        "P1 출처": dict(Counter(str(r.get("text_source")) for r in p1_regions)),
        "디지털 줄(Region 귀속)": digital_lines,
        "미배정 줄": len(p1_page.get("unassigned_lines") or []),
        "digital_text_route": diagnostics.get("digital_text_route"),
        "triage": (p1_page.get("origin") or {}).get("triage"),
    }


def _fmt(value: Any) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{k} {v}" for k, v in sorted(value.items(), key=lambda kv: -kv[1])) or "—"
    return "—" if value is None else str(value)


def _stats_table(base: dict, exp: dict) -> str:
    rows = []
    for key in base:
        b, e = base[key], exp.get(key)
        changed = _fmt(b) != _fmt(e)
        delta = ""
        if isinstance(b, int) and isinstance(e, int) and b != e:
            delta = f" <span class='delta'>({e - b:+d})</span>"
        rows.append(
            f"<tr class='{'chg' if changed else ''}'><th>{html.escape(key)}</th>"
            f"<td>{html.escape(_fmt(b))}</td><td>{html.escape(_fmt(e))}{delta}</td></tr>"
        )
    return (
        "<table class='meta stats'><tr><th>지표</th><th>기준(디지털 사용)</th>"
        f"<th>실험(디지털 미사용)</th></tr>{''.join(rows)}</table>"
    )


def _label_diff(base: list[dict], exp: list[dict]) -> str:
    cb = Counter(label for r in base for label in r.get("labels") or [])
    ce = Counter(label for r in exp for label in r.get("labels") or [])
    lost, gained = cb - ce, ce - cb
    chips = lambda c, cls: "".join(
        f"<span class='chip {cls}'>{html.escape(k)}{' ×' + str(v) if v > 1 else ''}</span>"
        for k, v in sorted(c.items())
    ) or "<span class='muted'>없음</span>"
    return (
        "<div class='labeldiff'>"
        f"<div><b>기준에만 있는 라벨</b> {chips(lost, 'lost')}</div>"
        f"<div><b>실험에만 있는 라벨</b> {chips(gained, 'gain')}</div>"
        "</div>"
    )


def _svg(regions: list[dict], width: int, height: int, side: str) -> str:
    parts = [f"<svg viewBox='0 0 {width} {height}' preserveAspectRatio='none' class='overlay'>"]
    font = max(11.0, min(width, height) / 55)
    for region in regions:
        box = region.get("bbox")
        if not box:
            continue
        x0, y0, x1, y1 = (float(v) for v in box)
        rid = html.escape(f"{side}:{region['region_id']}")
        color = _color(region.get("product_id"))
        parts.append(
            f"<g class='box' data-rid='{rid}'><rect x='{x0}' y='{y0}' "
            f"width='{max(1.0, x1 - x0)}' height='{max(1.0, y1 - y0)}' stroke='{color}' fill='{color}'/>"
            f"<text x='{x0 + 3}' y='{y0 + font}' fill='{color}' font-size='{font}'>"
            f"{html.escape(str(region['region_id']))}</text></g>"
        )
    parts.append("</svg>")
    return "".join(parts)


def _labels(region: dict | None) -> str:
    if not region:
        return ""
    labels = region.get("labels") or []
    return "".join(f"<span class='chip lab'>{html.escape(str(x))}</span>" for x in labels) or (
        "<span class='chip none'>라벨 없음</span>"
    )


def _cell(region: dict, p1: dict, text_html: str) -> str:
    source = html.escape(str(region.get("text_source")))
    p1_source = html.escape(str((p1 or {}).get("text_source") or ""))
    flags = []
    if region.get("kind") == "table":
        flags.append("<span class='chip alt'>표</span>")
    if region.get("needs_review"):
        flags.append("<span class='chip rev'>검수</span>")
    return (
        f"<div class='cellhead'><span class='rid'>{html.escape(str(region['region_id']))}</span>"
        f"<span class='chip src'>{source}</span><span class='p1src'>{p1_source}</span>"
        f"{''.join(flags)}{_labels(region)}</div><div class='text'>{text_html or '<i>(빈 텍스트)</i>'}</div>"
    )


def _rows(pairs, only_base, only_exp, base_p1, exp_p1) -> tuple[str, dict[str, int]]:
    rows, counts = [], Counter()
    for b, e, iou in pairs:
        bt, et = str(b.get("selected_text") or ""), str(e.get("selected_text") or "")
        sim = _similarity(bt, et)
        same_labels = sorted(b.get("labels") or []) == sorted(e.get("labels") or [])
        text_same = _norm(bt) == _norm(et)
        status = "same" if text_same and same_labels else "chg"
        counts["문구 동일" if text_same else "문구 변경"] += 1
        counts["라벨 동일" if same_labels else "라벨 변경"] += 1
        left, right = _diff_html(bt, et)
        tags = []
        if not text_same:
            tags.append(f"<span class='tag t'>문구 {sim:.0%}</span>")
        if not same_labels:
            tags.append("<span class='tag l'>라벨</span>")
        if b.get("text_source") != e.get("text_source"):
            tags.append("<span class='tag s'>출처</span>")
        rows.append(
            f"<tr class='pair {status}' data-b='b:{html.escape(str(b['region_id']))}' "
            f"data-e='e:{html.escape(str(e['region_id']))}' data-sim='{sim:.4f}'>"
            f"<td class='meta-col'>IoU {iou:.2f}<br>{''.join(tags) or '<span class=tag ok>동일</span>'}</td>"
            f"<td>{_cell(b, base_p1.get(str(b['region_id'])), left)}</td>"
            f"<td>{_cell(e, exp_p1.get(str(e['region_id'])), right)}</td></tr>"
        )
    for b in only_base:
        counts["기준에만"] += 1
        rows.append(
            f"<tr class='pair chg gone' data-b='b:{html.escape(str(b['region_id']))}' data-e='' data-sim='0'>"
            "<td class='meta-col'><span class='tag gone'>기준에만</span></td>"
            f"<td>{_cell(b, base_p1.get(str(b['region_id'])), html.escape(str(b.get('selected_text') or '')).replace(chr(10), '<br>'))}</td>"
            "<td class='empty'>—</td></tr>"
        )
    for e in only_exp:
        counts["실험에만"] += 1
        rows.append(
            f"<tr class='pair chg new' data-b='' data-e='e:{html.escape(str(e['region_id']))}' data-sim='0'>"
            "<td class='meta-col'><span class='tag new'>실험에만</span></td><td class='empty'>—</td>"
            f"<td>{_cell(e, exp_p1.get(str(e['region_id'])), html.escape(str(e.get('selected_text') or '')).replace(chr(10), '<br>'))}</td></tr>"
        )
    return "".join(rows), dict(counts)


def build(base_run: Path, exp_run: Path, only: list[str] | None, title: str, note: str) -> str:
    base_docs, exp_docs = _load(base_run), _load(exp_run)
    base_media, exp_media = media_index(base_run), media_index(exp_run)
    names = [n for n in exp_docs if n in base_docs and (not only or n in only)]
    tabs, panes, overview = [], [], []

    for index, name in enumerate(names):
        bdoc, edoc = base_docs[name], exp_docs[name]
        bpages = {int(p["page_no"]): p for p in bdoc["p3"]["pages"]}
        for epage in edoc["p3"]["pages"]:
            page_no = int(epage["page_no"])
            bpage = bpages.get(page_no)
            if bpage is None:
                continue
            key = f"d{index}_p{page_no}"
            bst, est = _page_stats(bdoc, bpage), _page_stats(edoc, epage)
            pairs, only_b, only_e = _match(bpage["regions"], epage["regions"])
            rows, counts = _rows(
                pairs, only_b, only_e, _p1_regions(bdoc, page_no), _p1_regions(edoc, page_no),
            )
            full_b = "\n".join(str(r.get("selected_text") or "") for r in bpage["regions"])
            full_e = "\n".join(str(r.get("selected_text") or "") for r in epage["regions"])
            page_sim = _similarity(full_b, full_e)
            overview.append((name, page_no, bst, est, counts, page_sim))

            imgs = []
            for side, media, page in (("b", base_media, bpage), ("e", exp_media, epage)):
                path = media.get((name, page_no))
                if path is None:
                    imgs.append("<div class='warn'>렌더 이미지 없음</div>")
                    continue
                uri, w, h = _thumb(path)
                imgs.append(
                    f"<div class='canvas'><img src='{uri}' alt=''>{_svg(page['regions'], w, h, side)}</div>"
                )
            summary = "".join(
                f"<span class='pill'>{html.escape(k)} <b>{v}</b></span>" for k, v in counts.items()
            )
            tabs.append(f"<button class='tab' data-key='{key}'>{html.escape(name)}</button>")
            panes.append(
                f"<section class='pane' data-key='{key}'>"
                f"<h2>{html.escape(name)} <small>p{page_no} · triage {html.escape(str(est['triage']))}"
                f" · 전체 문구 유사도 {page_sim:.1%}</small></h2>"
                f"{_stats_table(bst, est)}{_label_diff(bpage['regions'], epage['regions'])}"
                f"<div class='pills'>{summary}</div>"
                "<div class='filters'><label><input type='checkbox' class='onlychg'> 변경된 행만</label></div>"
                "<div class='split'>"
                "<div class='left'><div class='imgs'>"
                f"<div><div class='cap'>기준</div>{imgs[0]}</div>"
                f"<div><div class='cap'>실험</div>{imgs[1]}</div>"
                "</div></div>"
                "<div class='right'><table class='cmp'><thead><tr><th></th><th>기준(디지털 사용)</th>"
                f"<th>실험(디지털 미사용)</th></tr></thead><tbody>{rows}</tbody></table></div>"
                "</div></section>"
            )

    # 개요 탭
    head = (
        "<tr><th>문서</th><th>triage</th><th>Region 기준→실험</th><th>디지털 줄</th>"
        "<th>P3 출처(실험)</th><th>문구 변경/매칭</th><th>라벨 변경/매칭</th><th>기준에만/실험에만</th>"
        "<th>라벨 총수</th><th>검수</th><th>전체 문구 유사도</th></tr>"
    )
    body = []
    for name, page_no, bst, est, counts, sim in overview:
        matched = counts.get("문구 동일", 0) + counts.get("문구 변경", 0)
        body.append(
            f"<tr><td>{html.escape(name)}</td><td>{html.escape(str(est['triage']))}</td>"
            f"<td>{bst['Region']} → {est['Region']}</td>"
            f"<td>{bst['디지털 줄(Region 귀속)']} → {est['디지털 줄(Region 귀속)']}</td>"
            f"<td>{html.escape(_fmt(est['P3 출처']))}</td>"
            f"<td>{counts.get('문구 변경', 0)}/{matched}</td>"
            f"<td>{counts.get('라벨 변경', 0)}/{matched}</td>"
            f"<td>{counts.get('기준에만', 0)}/{counts.get('실험에만', 0)}</td>"
            f"<td>{bst['라벨 총수']} → {est['라벨 총수']}</td>"
            f"<td>{bst['검수 필요']} → {est['검수 필요']}</td>"
            f"<td>{sim:.1%}</td></tr>"
        )
    overview_html = (
        "<section class='pane' data-key='overview'><h2>개요</h2>"
        f"<div class='note'>{note}</div>"
        f"<table class='meta ov'>{head}{''.join(body)}</table>"
        "<div class='note'>Region ID는 실행마다 다시 매겨지므로 bbox IoU ≥ "
        f"{MATCH_IOU}로 1:1 매칭했다. '문구 변경'은 공백을 제거한 selected_text가 다른 경우이며, "
        "유사도는 글자 단위 SequenceMatcher 비율이다. 정답 라벨이 없으므로 여기 수치는 "
        "'기준 대비 차이'이지 정확도가 아니다.</div></section>"
    )
    tabs.insert(0, "<button class='tab' data-key='overview'>개요</button>")
    return _SHELL.format(
        title=html.escape(title), tabs="".join(tabs), panes=overview_html + "".join(panes),
    )


_SHELL = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ --line:#d8dde3; --bg:#f6f7f9; --ink:#1c2430; --muted:#68727f; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font:13px/1.55 -apple-system,"Segoe UI","Malgun Gothic",sans-serif;
  color:var(--ink); background:var(--bg); }}
header {{ position:sticky; top:0; z-index:5; background:#fff; border-bottom:1px solid var(--line);
  padding:10px 16px; display:flex; gap:10px; align-items:center; flex-wrap:wrap; }}
h1 {{ font-size:15px; margin:0; }}
h2 {{ font-size:15px; margin:0 0 8px; }} h2 small {{ color:var(--muted); font-weight:400; }}
.tab {{ font:inherit; font-size:12px; padding:5px 10px; border:1px solid var(--line);
  background:#fff; border-radius:6px; cursor:pointer; color:var(--muted); }}
.tab.on {{ background:var(--ink); color:#fff; border-color:var(--ink); }}
.pane {{ display:none; padding:12px 16px 40px; }} .pane.on {{ display:block; }}
table.meta {{ border-collapse:collapse; background:#fff; font-size:12px; margin-bottom:10px; }}
table.meta th, table.meta td {{ border:1px solid var(--line); padding:4px 10px; text-align:left; vertical-align:top; }}
table.meta tr:first-child th {{ background:#eef1f4; }}
table.meta tr.chg td {{ background:#FFF8E1; }}
.delta {{ color:#B71C1C; font-weight:600; }}
.note {{ color:var(--muted); font-size:12px; margin:8px 0; max-width:1100px; }}
.labeldiff {{ display:flex; flex-direction:column; gap:4px; margin:6px 0 10px; }}
.pills {{ display:flex; gap:6px; flex-wrap:wrap; margin-bottom:6px; }}
.pill {{ background:#fff; border:1px solid var(--line); border-radius:12px; padding:2px 10px; font-size:12px; }}
.filters {{ margin-bottom:8px; font-size:12px; }}
.split {{ display:grid; grid-template-columns:minmax(0,5fr) minmax(0,7fr); gap:14px; align-items:start; }}
.left {{ position:sticky; top:60px; max-height:calc(100vh - 70px); overflow:auto; }}
.imgs {{ display:grid; grid-template-columns:1fr 1fr; gap:8px; }}
.cap {{ font-size:12px; font-weight:600; color:var(--muted); margin-bottom:2px; }}
.canvas {{ position:relative; background:#fff; border:1px solid var(--line); border-radius:6px; overflow:hidden; }}
.canvas img {{ display:block; width:100%; }}
svg.overlay {{ position:absolute; inset:0; width:100%; height:100%; }}
svg.overlay rect {{ fill-opacity:.05; stroke-width:4; }}
svg.overlay text {{ font-weight:700; paint-order:stroke; stroke:#fff; stroke-width:3; }}
svg.overlay .box.hot rect {{ fill-opacity:.35; stroke-width:12; stroke:#EF6C00; }}
table.cmp {{ width:100%; border-collapse:collapse; background:#fff; table-layout:fixed; }}
table.cmp th {{ background:#eef1f4; position:sticky; top:52px; z-index:1; }}
table.cmp th, table.cmp td {{ border:1px solid var(--line); padding:6px 8px; vertical-align:top; }}
table.cmp th:first-child, table.cmp td.meta-col {{ width:82px; font-size:11px; color:var(--muted); }}
tr.pair {{ cursor:pointer; }} tr.pair.hot td {{ background:#FFF3E0; }}
tr.pair.same td:not(.meta-col) {{ color:#56606b; }}
body.onlychg tr.pair.same {{ display:none; }}
td.empty {{ color:#b0b8c1; text-align:center; }}
.cellhead {{ display:flex; flex-wrap:wrap; gap:4px; align-items:center; margin-bottom:3px; }}
.rid {{ font:11px ui-monospace,Consolas,monospace; color:var(--muted); }}
.p1src {{ font:10px ui-monospace,Consolas,monospace; color:#8a94a0; }}
.chip {{ font-size:11px; color:#fff; border-radius:4px; padding:1px 6px; }}
.chip.lab {{ background:#2E7D32; }} .chip.src {{ background:#455A64; }}
.chip.none {{ background:#fff; color:#9aa5b1; border:1px dashed #c3cbd4; }}
.chip.alt {{ background:#6D4C41; }} .chip.rev {{ background:#EF6C00; }}
.chip.lost {{ background:#C62828; }} .chip.gain {{ background:#1565C0; }}
.muted {{ color:var(--muted); }}
.tag {{ display:inline-block; font-size:10px; border-radius:3px; padding:0 5px; margin:2px 2px 0 0; color:#fff; }}
.tag.t {{ background:#C62828; }} .tag.l {{ background:#6A1B9A; }} .tag.s {{ background:#455A64; }}
.tag.ok {{ background:#9aa5b1; }} .tag.gone {{ background:#B71C1C; }} .tag.new {{ background:#1565C0; }}
.text {{ white-space:pre-wrap; word-break:break-word; }}
del {{ background:#FFCDD2; color:#B71C1C; text-decoration:line-through; }}
ins {{ background:#C8E6C9; color:#1B5E20; text-decoration:none; }}
.warn {{ color:#B71C1C; }}
@media (max-width:1000px) {{ .split {{ grid-template-columns:1fr; }} .left {{ position:static; max-height:none; }} }}
</style></head><body>
<header><h1>{title}</h1>{tabs}</header>
{panes}
<script>
const tabs=[...document.querySelectorAll('.tab')], panes=[...document.querySelectorAll('.pane')];
function show(k){{tabs.forEach(t=>t.classList.toggle('on',t.dataset.key===k));panes.forEach(p=>p.classList.toggle('on',p.dataset.key===k));}}
tabs.forEach(t=>t.addEventListener('click',()=>show(t.dataset.key)));
show('overview');
document.querySelectorAll('.onlychg').forEach(cb=>cb.addEventListener('change',e=>{{
  document.querySelectorAll('.onlychg').forEach(o=>o.checked=e.target.checked);
  document.body.classList.toggle('onlychg',e.target.checked);}}));
panes.forEach(root=>{{
  const boxes=id=>id?root.querySelectorAll(`.box[data-rid="${{CSS.escape(id)}}"]`):[];
  const left=root.querySelector('.left');
  root.querySelectorAll('tr.pair').forEach(tr=>{{
    const ids=[tr.dataset.b,tr.dataset.e];
    const on=v=>{{tr.classList.toggle('hot',v);ids.forEach(i=>boxes(i).forEach(b=>b.classList.toggle('hot',v)));}};
    tr.addEventListener('mouseenter',()=>on(true)); tr.addEventListener('mouseleave',()=>on(false));
    tr.addEventListener('click',()=>{{const b=[...boxes(ids[0]),...boxes(ids[1])][0]; if(!b||!left)return;
      const r=b.getBoundingClientRect(),p=left.getBoundingClientRect();
      left.scrollTo({{top:left.scrollTop+(r.top-p.top)-p.height/2,behavior:'smooth'}});}});
  }});
  root.querySelectorAll('.box').forEach(g=>g.addEventListener('click',()=>{{
    const id=g.dataset.rid, key=id.startsWith('b:')?'b':'e';
    const tr=root.querySelector(`tr.pair[data-${{key}}="${{CSS.escape(id)}}"]`);
    if(tr){{tr.scrollIntoView({{behavior:'smooth',block:'center'}});tr.classList.add('hot');setTimeout(()=>tr.classList.remove('hot'),1500);}}
  }}));
}});
</script></body></html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="두 실행의 P1/P3 비교 HTML")
    parser.add_argument("--base", required=True)
    parser.add_argument("--exp", required=True)
    parser.add_argument("--only", nargs="*", default=None)
    parser.add_argument("--title", default="PDF 디지털 텍스트 제거 실험 비교")
    parser.add_argument("--note", default="")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    base, exp = OUTPUT_ROOT / args.base, OUTPUT_ROOT / args.exp
    out = args.out or (exp / "compare.html")
    out.write_text(build(base, exp, args.only, args.title, args.note), encoding="utf-8")
    print(f"완료: {out}  ({out.stat().st_size / 1_048_576:.1f} MB)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
