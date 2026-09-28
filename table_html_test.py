"""[실험] 확정된 시각 표를 VLM에 HTML(rowspan/colspan)로 추출시키고 마크다운으로 바꾼다.

기존 실행 결과(P1)의 `kind=table` Region bbox를 그대로 쓰고, 파이프라인은 다시 돌리지
않는다. 현재 표 Judge 문구(`항목 | 값`)와 HTML→격자→마크다운 결과를 표 이미지 옆에
놓아 비교한다.

    python table_html_test.py --runs <실행1> <실행2> ... --out <폴더>
"""
from __future__ import annotations

import argparse
import base64
import html
import io
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from PIL import Image

from nh_parser_fin.config import OUTPUT_ROOT
from nh_parser_fin.parse.reading import _crop
from nh_parser_fin.vlm import client as vlm_client
from report import media_index

SCHEMA = {
    "type": "object",
    "properties": {
        "analysis": {"type": "string"},
        "title": {"type": "string"},
        "html": {"type": "string"},
        "notes": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
    "required": ["analysis", "title", "html", "notes", "confidence"],
    "additionalProperties": False,
}

PROMPT = """첨부 이미지는 금융광고에서 확인된 표 하나의 전체 영역입니다.
표를 HTML <table>로 옮기세요.

- 머리글 칸은 <th>, 값 칸은 <td>로 적고 행마다 <tr>을 쓰세요.
- 한 칸이 여러 행에 걸치면 rowspan, 여러 열에 걸치면 colspan을 쓰세요.
  걸친 자리에 같은 값을 반복해 적지 마세요.
- 칸 안의 줄바꿈은 <br>로, 글자·숫자·단위·기호는 보이는 그대로 적으세요.
- 표 위의 제목은 title에, 표 밖의 ※·- 로 시작하는 각주는 notes에 한 줄씩 넣으세요.
  표 밖 문구를 <table> 안에 넣지 마세요.
- 표 이미지에 없는 행·열·숫자를 만들지 말고, 읽을 수 없는 곳은 추측하지 마세요.
- 속성은 rowspan, colspan만 쓰고 style·class 등은 쓰지 마세요.
- analysis에는 표 구조를 한 문장으로 설명하세요.
"""


class _TableParser(HTMLParser):
    """<table> 안의 행·칸과 rowspan/colspan만 읽는다. 다른 태그는 무시한다."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[dict[str, Any]]] = []
        self._cell: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.rows.append([])
        elif tag in ("td", "th"):
            if not self.rows:
                self.rows.append([])
            values = dict(attrs)

            def span(name: str) -> int:
                try:
                    return max(1, min(50, int(values.get(name) or 1)))
                except ValueError:
                    return 1

            self._cell = {"header": tag == "th", "rowspan": span("rowspan"),
                          "colspan": span("colspan"), "text": ""}
        elif tag == "br" and self._cell is not None:
            self._cell["text"] += "\n"

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None:
            self._cell["text"] = re.sub(r"[ \t]+", " ", self._cell["text"]).strip()
            self.rows[-1].append(self._cell)
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell["text"] += data


def to_grid(markup: str) -> dict[str, Any]:
    """HTML 표를 병합이 풀린 격자로 바꾼다.

    각 자리는 원래 칸의 값과 `origin`(병합 시작 좌표)을 가진다. 병합으로 채워진 자리는
    `spanned=True`다. 격자 폭이 행마다 다르면 구조 오류로 본다.
    """
    parser = _TableParser()
    parser.feed(markup)
    grid: list[list[dict[str, Any] | None]] = []
    for r, row in enumerate(parser.rows):
        while len(grid) <= r:
            grid.append([])
        c = 0
        for cell in row:
            while c < len(grid[r]) and grid[r][c] is not None:
                c += 1
            for dr in range(cell["rowspan"]):
                while len(grid) <= r + dr:
                    grid.append([])
                target = grid[r + dr]
                for dc in range(cell["colspan"]):
                    while len(target) <= c + dc:
                        target.append(None)
                    target[c + dc] = {
                        "text": cell["text"], "header": cell["header"],
                        "origin": [r, c], "spanned": bool(dr or dc),
                        "span": "row" if dr else "col" if dc else None,
                    }
            c += cell["colspan"]
    width = max((len(row) for row in grid), default=0)
    ragged = sorted({len(row) for row in grid}) if grid else []
    holes = sum(1 for row in grid for value in row if value is None) + sum(
        width - len(row) for row in grid
    )
    for row in grid:
        row.extend([None] * (width - len(row)))
    return {"grid": grid, "width": width, "rows": len(grid),
            "ragged_widths": ragged, "holes": holes}


def to_markdown(table: dict[str, Any]) -> str:
    """격자 → GFM 마크다운.

    행 병합(rowspan)은 각 행에 값을 반복한다 — 그 값이 행마다 적용된다는 뜻이라
    반복해도 의미가 같다. 열 병합(colspan)은 첫 칸에만 값을 두고 나머지를 비운다 —
    반복하면 다른 열의 값처럼 읽힌다(예: 조건 없는 우대항목에 공통 조건이 붙어 보임).
    """
    grid = table["grid"]
    if not grid:
        return ""

    def cell_text(value: dict[str, Any] | None) -> str:
        if value is None:
            return ""
        if value["spanned"] and value["span"] == "col":
            return ""
        return value["text"].replace("\n", "<br>").replace("|", "\\|")

    header_rows = 0
    for row in grid:
        if all(value is None or value["header"] for value in row):
            header_rows += 1
        else:
            break
    header_rows = max(1, header_rows)
    head = grid[header_rows - 1]
    lines = ["| " + " | ".join(cell_text(v) for v in head) + " |",
             "|" + "|".join("---" for _ in head) + "|"]
    for row in grid[header_rows:]:
        lines.append("| " + " | ".join(cell_text(v) for v in row) + " |")
    return "\n".join(lines)


def _numbers(text: str) -> list[str]:
    return sorted(re.findall(r"\d[\d,.]*\d|\d", str(text or "")))


def _collect(runs: list[str]) -> list[dict[str, Any]]:
    """여러 실행의 표 Region을 모은다. 같은 문서에서 bbox가 거의 같은 표는 하나로 본다."""
    found: list[dict[str, Any]] = []
    for run_name in runs:
        run = OUTPUT_ROOT / run_name
        media = media_index(run)
        for path in sorted((run / "final").glob("*.p1.json")):
            doc = json.loads(path.read_text(encoding="utf-8"))
            if doc.get("file_type") == "hwp" or str(doc.get("source_file", "")).lower().endswith((".hwp", ".hwpx")):
                continue
            for page in doc.get("pages") or []:
                for region in page.get("regions") or []:
                    if region.get("kind") != "table" or region.get("table_status") != "verified_visual":
                        continue
                    item = {
                        "run": run_name, "source_file": doc["source_file"],
                        "page_no": page["page_no"], "region_id": region["region_id"],
                        "bbox": region["bbox"], "current_text": region.get("text") or "",
                        "image": media.get((doc["source_file"], int(page["page_no"]))),
                    }
                    duplicate = False
                    for kept in found:
                        if kept["source_file"] != item["source_file"]:
                            continue
                        a, b = kept["bbox"], item["bbox"]
                        inter = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
                        area = lambda x: (x[2] - x[0]) * (x[3] - x[1])
                        if inter / max(1, min(area(a), area(b))) >= 0.8:
                            duplicate = True
                            break
                    if not duplicate:
                        found.append(item)
    return found


def _data_uri(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _grid_html(table: dict[str, Any]) -> str:
    """격자를 다시 HTML 표로 그린다. VLM이 준 HTML을 그대로 넣지 않고 파싱 결과만 쓴다."""
    rows = []
    for r, row in enumerate(table["grid"]):
        cells = []
        for c, value in enumerate(row):
            if value is None:
                cells.append("<td class='hole'>∅</td>")
                continue
            if value["spanned"]:
                continue
            rowspan = sum(1 for rr in table["grid"][r:] if rr[c] and rr[c]["origin"] == [r, c])
            colspan = sum(1 for v in row[c:] if v and v["origin"] == [r, c])
            tag = "th" if value["header"] else "td"
            cells.append(
                f"<{tag} rowspan='{rowspan}' colspan='{colspan}'>"
                f"{html.escape(value['text']).replace(chr(10), '<br>')}</{tag}>"
            )
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return "<table class='grid'>" + "".join(rows) + "</table>"


def _from_run(run_name: str, base_name: str | None) -> list[dict[str, Any]]:
    """`PARSER_V2_TABLE_FORMAT=html`로 처음부터 돌린 실행의 표 결과를 읽는다(VLM 재호출 없음).

    같은 문서에서 bbox가 겹치는 기준 실행의 표 문구를 `current_text`로 붙인다.
    """
    from nh_parser_fin.parse import table_html

    run = OUTPUT_ROOT / run_name
    media = media_index(run)
    base_tables = _collect([base_name]) if base_name else []
    results = []
    for path in sorted((run / "final").glob("*.p1.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        for page in doc.get("pages") or []:
            image = Image.open(media[(doc["source_file"], int(page["page_no"]))]).convert("RGB")
            for region in page.get("regions") or []:
                attempt = region.get("table_html_attempt")
                if region.get("kind") != "table" or not attempt:
                    continue
                table = to_grid(attempt.get("html") or "")
                visual = region.get("visual_table") or {}
                base_text = "(기준 실행에서 같은 위치의 표 없음)"
                for item in base_tables:
                    if item["source_file"] != doc["source_file"]:
                        continue
                    a, b = item["bbox"], region["bbox"]
                    inter = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
                    if inter / max(1, min((a[2]-a[0])*(a[3]-a[1]), (b[2]-b[0])*(b[3]-b[1]))) >= 0.5:
                        base_text = item["current_text"]
                        break
                status = attempt.get("status")
                results.append({
                    "source_file": doc["source_file"], "region_id": region["region_id"],
                    "bbox": region["bbox"],
                    "error": "" if status == "selected" else (
                        f"HTML 결과 미채택({status}) → 기존 `|` 문구 사용: " + str(attempt.get("error") or "")
                    ),
                    "response": {"title": visual.get("title") or "", "notes": visual.get("notes") or [],
                                 "confidence": attempt.get("confidence"), "html": attempt.get("html") or ""},
                    "markdown": (region.get("text") if status == "selected" else table_html.to_markdown(table)),
                    "grid": table,
                    "checks": {"rows": table["rows"], "cols": table["width"],
                               "ragged_widths": table["ragged_widths"], "holes": table["holes"],
                               "numbers_only_in_current": sorted(set(_numbers(base_text)) - set(_numbers(region.get("text")))),
                               "numbers_only_in_html": sorted(set(_numbers(region.get("text"))) - set(_numbers(base_text)))},
                    "current_text": base_text,
                    "crop_uri": _data_uri(_crop(image, region["bbox"])),
                })
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="표 HTML→마크다운 추출 실험")
    parser.add_argument("--runs", nargs="+", default=[])
    parser.add_argument("--from-run", default=None, help="HTML 표 모드로 처음부터 돌린 실행")
    parser.add_argument("--base", default=None, help="--from-run과 비교할 기준 실행")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out = OUTPUT_ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)
    if args.from_run:
        results = _from_run(args.from_run, args.base)
        (out / "table-html-test.html").write_text(_page(results), encoding="utf-8")
        print(f"완료: {out / 'table-html-test.html'} ({len(results)}개 표)")
        return
    results = []
    for item in _collect(args.runs):
        page = Image.open(item["image"]).convert("RGB")
        crop = _crop(page, item["bbox"])
        try:
            response = vlm_client.chat_json(
                [{"type": "text", "text": PROMPT},
                 vlm_client.image_part(crop, box=(2400, 1800), quality=94)],
                schema_name="experiment_table_html", schema=SCHEMA, max_tokens=9000,
            )
            error = ""
        except Exception as exc:  # noqa: BLE001
            response, error = {}, str(exc)[:300]
        table = to_grid(str(response.get("html") or ""))
        markdown = to_markdown(table)
        grid_numbers = _numbers(" ".join(v["text"] for row in table["grid"] for v in row if v and not v["spanned"]))
        note_numbers = _numbers(" ".join(response.get("notes") or []) + " " + str(response.get("title") or ""))
        current_numbers = _numbers(item["current_text"])
        results.append({
            **{k: v for k, v in item.items() if k != "image"},
            "error": error, "response": response, "markdown": markdown,
            "grid": table, "checks": {
                "rows": table["rows"], "cols": table["width"],
                "ragged_widths": table["ragged_widths"], "holes": table["holes"],
                "numbers_only_in_current": sorted(set(current_numbers) - set(grid_numbers) - set(note_numbers)),
                "numbers_only_in_html": sorted(set(grid_numbers + note_numbers) - set(current_numbers)),
            },
            "crop_uri": _data_uri(crop),
        })
        print(f"· {item['source_file']} {item['region_id']}: {table['rows']}×{table['width']} "
              f"holes={table['holes']} {error[:80]}")
    (out / "table-html-results.json").write_text(json.dumps(
        [{k: v for k, v in r.items() if k != "crop_uri"} for r in results],
        ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "table-html-test.html").write_text(_page(results), encoding="utf-8")
    print(f"완료: {out / 'table-html-test.html'}")


def _page(results: list[dict[str, Any]]) -> str:
    blocks = []
    for index, r in enumerate(results, start=1):
        checks = r["checks"]
        warn = []
        if r["error"]:
            warn.append(f"VLM 실패: {html.escape(r['error'])}")
        if len(checks["ragged_widths"]) > 1 or checks["holes"]:
            warn.append(f"격자 불일치: 행 폭 {checks['ragged_widths']}, 빈 자리 {checks['holes']}")
        if checks["numbers_only_in_current"]:
            warn.append("현재 문구에만 있는 숫자: " + html.escape(", ".join(checks["numbers_only_in_current"])))
        if checks["numbers_only_in_html"]:
            warn.append("HTML에만 있는 숫자: " + html.escape(", ".join(checks["numbers_only_in_html"])))
        response = r["response"]
        notes = "".join(f"<li>{html.escape(n)}</li>" for n in response.get("notes") or [])
        blocks.append(f"""
<section class='card'>
<h2>{index}. {html.escape(r['source_file'])} · {html.escape(r['region_id'])}
<small>bbox {r['bbox']} · {checks['rows']}행×{checks['cols']}열 · 확신도 {response.get('confidence', '—')}</small></h2>
{''.join(f"<div class='warn'>{w}</div>" for w in warn) or "<div class='ok'>자동 점검 통과(격자 폭 일정, 숫자 집합 일치)</div>"}
<div class='cols'>
<div><h3>표 이미지 (VLM 입력)</h3><img src='{r['crop_uri']}'></div>
<div>
<h3>HTML 추출 → 격자 재구성</h3>
<div class='meta'>제목: {html.escape(str(response.get('title') or '—'))}</div>
{_grid_html(r['grid'])}
{f"<div class='meta'>각주</div><ul>{notes}</ul>" if notes else ""}
<h3>마크다운 (P3 selected_text 후보)</h3><pre>{html.escape(r['markdown'])}</pre>
<h3>현재 파이프라인 문구 (표 Judge, `항목 | 값`)</h3><pre>{html.escape(r['current_text'])}</pre>
<details><summary>VLM 원본 HTML</summary><pre>{html.escape(str(response.get('html') or ''))}</pre></details>
</div></div></section>""")
    return f"""<!doctype html><html lang='ko'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'><title>표 HTML 추출 실험</title>
<style>
:root {{ --line:#d8dde3; --bg:#f6f7f9; --ink:#1c2430; --muted:#68727f; }}
body {{ margin:0; padding:16px; font:13px/1.55 -apple-system,"Segoe UI","Malgun Gothic",sans-serif; color:var(--ink); background:var(--bg); }}
h1 {{ font-size:16px; }} h2 {{ font-size:14px; margin:0 0 6px; }} h2 small {{ color:var(--muted); font-weight:400; }}
h3 {{ font-size:12px; color:var(--muted); margin:10px 0 4px; }}
.card {{ background:#fff; border:1px solid var(--line); border-radius:8px; padding:12px; margin-bottom:14px; }}
.cols {{ display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:14px; }}
.cols img {{ width:100%; border:1px solid var(--line); }}
table.grid {{ border-collapse:collapse; width:100%; font-size:12px; }}
table.grid th, table.grid td {{ border:1px solid #9aa5b1; padding:4px 6px; vertical-align:top; }}
table.grid th {{ background:#eef1f4; }} td.hole {{ background:#FFEBEE; color:#B71C1C; }}
pre {{ white-space:pre-wrap; word-break:break-word; background:#f3f5f7; padding:8px; border-radius:6px; font-size:12px; }}
.warn {{ color:#B71C1C; background:#FFEBEE; padding:4px 8px; border-radius:4px; margin:3px 0; }}
.ok {{ color:#1B5E20; background:#E8F5E9; padding:4px 8px; border-radius:4px; }}
.meta {{ color:var(--muted); font-size:12px; }}
@media (max-width:900px) {{ .cols {{ grid-template-columns:1fr; }} }}
</style></head><body><h1>표 HTML(rowspan/colspan) 추출 → 마크다운 실험</h1>
<p class='meta'>기존 실행의 확정 시각 표 bbox를 그대로 잘라 표 Judge와 같은 크기(2400×1800 안, JPEG 94)로 보냈다. 격자는 VLM HTML을 코드가 파싱해 다시 그린 것이다. 마크다운은 행 병합을 반복하고 열 병합은 첫 칸에만 값을 둔다.</p>
{''.join(blocks)}</body></html>"""


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
