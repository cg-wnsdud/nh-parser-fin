"""[실험] 한 실행의 검수 대상 Region을 원본 이미지 조각과 함께 모아 보는 HTML.

현재 파이프라인이 켠 `needs_review` 사유와, 디지털 텍스트 없이 돌릴 때를 위해 제안한
기준(R1~R3)을 같은 화면에 놓는다. 제안 기준은 P1 값만으로 계산하며 파이프라인은
바꾸지 않는다.

    python review_report.py --run <실행 이름>
"""
from __future__ import annotations

import argparse
import base64
import html
import io
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

from PIL import Image

from nh_parser_fin.config import OUTPUT_ROOT
from report import media_index

RULES = {
    "R1": "빈 판독인데 Paddle 문구에 글자·숫자가 4자 이상",
    "R2": "Judge가 두 후보를 모두 고쳤고(corrected) 최종 문구가 Reader 판독과 다름",
    "R3": "최종 문구와 Reader 판독의 숫자(금리·금액·날짜)가 다름",
}
REASON_NAMES = {
    "ownership_unknown": "상품 소유권 미상",
    "ownership_low_confidence": "상품 소유권 확신도 낮음",
    "label_low_confidence": "라벨 확신도 낮음",
    "template_unresolved": "템플릿 미확정",
    "vlm_read_failed": "VLM 판독 실패",
    "ocr_vlm_disagreement": "OCR·VLM 불일치(Judge 없음)",
    "digital_text_vlm_disagreement": "디지털 원문·VLM 불일치",
    "vlm_only_text": "VLM만 읽음(OCR 없음)",
    "vlm_judge_low_confidence": "Judge 확신도 낮음",
    "table_html_invalid": "표 HTML 무효(기존 문구 사용)",
    "digital_text_missing": "디지털 줄이 문구에 없음(누락)",
    "vlm_not_in_digital": "디지털에 없는 낱말",
    "vlm_blank_with_ocr_text": "빈 판독(OCR엔 글자 있음)",
    "page_reader_fallback": "페이지 판독 누락(crop으로 대체)",
    "unsupported_number": "근거 없는 숫자",
    "judge_new_word": "Judge가 만든 낱말",
    "table_verification_failed": "표 검증 실패",
    "recovery_action_uncertain": "복구 후보 처리 불확실",
    "recovery_low_confidence": "복구 후보 확신도 낮음",
}


def _alnum(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return "".join(char for char in text if char.isalnum()).casefold()


def _numbers(value: Any) -> list[str]:
    return re.findall(r"\d+(?:[.,]\d+)*", str(value or ""))


def proposed(region: dict[str, Any]) -> list[str]:
    if region.get("reading_status") is None:
        return []
    candidates = region.get("text_candidates") or {}
    parser_text = candidates.get("parser_selected") or ""
    reader_text = (region.get("vlm_reading") or {}).get("text") or ""
    judge = region.get("vlm_judge") or {}
    hits = []
    if region.get("reading_status") == "vlm_blank" and len(_alnum(parser_text)) >= 4:
        hits.append("R1")
    if judge.get("text") and judge.get("source") == "corrected" and _alnum(judge["text"]) != _alnum(reader_text):
        hits.append("R2")
    if reader_text and region.get("kind") != "table" and _numbers(region.get("text")) != _numbers(reader_text):
        hits.append("R3")
    return hits


def _crop_uri(image: Image.Image, bbox: list[int]) -> str:
    pad = 16
    x0, y0, x1, y1 = (int(v) for v in bbox)
    crop = image.crop((max(0, x0 - pad), max(0, y0 - pad), min(image.width, x1 + pad), min(image.height, y1 + pad)))
    crop.thumbnail((900, 700))
    buffer = io.BytesIO()
    crop.convert("RGB").save(buffer, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _text(value: Any) -> str:
    value = str(value or "")
    return html.escape(value).replace("\n", "<br>") if value.strip() else "<i class='muted'>(없음)</i>"


def build(run_name: str) -> tuple[str, dict[str, int]]:
    run = OUTPUT_ROOT / run_name
    media = media_index(run)
    cards, counts = [], {"current": 0, **{k: 0 for k in RULES}}
    nav = []
    total = 0
    for path in sorted((run / "final").glob("*.p1.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc_cards = []
        for page in doc.get("pages") or []:
            image_path = media.get((doc["source_file"], int(page["page_no"])))
            image = Image.open(image_path).convert("RGB") if image_path else None
            for region in page.get("regions") or []:
                total += 1
                current = list(region.get("review_reasons") or []) if region.get("needs_review") else []
                rules = proposed(region)
                if not current and not rules:
                    continue
                if current:
                    counts["current"] += 1
                for rule in rules:
                    counts[rule] += 1
                tags = "".join(
                    f"<span class='tag cur'>{html.escape(REASON_NAMES.get(code, code))}</span>" for code in current
                ) + "".join(f"<span class='tag new' title='{html.escape(RULES[r])}'>{r} {html.escape(RULES[r])}</span>" for r in rules)
                candidates = region.get("text_candidates") or {}
                judge = region.get("vlm_judge") or {}
                img = f"<img src='{_crop_uri(image, region['bbox'])}'>" if image is not None and region.get("bbox") else ""
                data_rules = " ".join(["cur"] if current else []) + " " + " ".join(rules)
                doc_cards.append(f"""
<div class='card' data-rules='{data_rules.strip()}'>
<div class='head'><b>{html.escape(region['region_id'])}</b> p{page['page_no']} · bbox {region.get('bbox')} ·
상태 {html.escape(str(region.get('reading_status')))} · 출처 {html.escape(str(region.get('text_source')))}
· 라벨 {html.escape(', '.join(region.get('semantic_labels') or []) or '없음')}</div>
<div class='tags'>{tags}</div>
<div class='cols'><div class='img'>{img}</div><table>
<tr><th>최종 문구</th><td>{_text(region.get('text'))}</td></tr>
<tr><th>Paddle 문구</th><td>{_text(candidates.get('parser_selected'))}</td></tr>
<tr><th>Reader 판독</th><td>{_text((region.get('vlm_reading') or {}).get('text'))}</td></tr>
<tr><th>Judge 판독</th><td>{_text(judge.get('text'))}{f" <span class='muted'>(source={html.escape(str(judge.get('source')))})</span>" if judge else ''}</td></tr>
</table></div></div>""")
        if doc_cards:
            anchor = f"d{len(nav)}"
            nav.append(f"<a href='#{anchor}'>{html.escape(doc['source_file'])} ({len(doc_cards)})</a>")
            cards.append(f"<h2 id='{anchor}'>{html.escape(doc['source_file'])} <small>{len(doc_cards)}건</small></h2>{''.join(doc_cards)}")
    counts["total_regions"] = total
    buttons = "".join(
        f"<label><input type='checkbox' class='f' value='{key}' checked> {label}</label>"
        for key, label in [("cur", f"현재 검수 표시 ({counts['current']})"),
                           *[(k, f"{k} {v} ({counts[k]})") for k, v in RULES.items()]]
    )
    page_html = f"""<!doctype html><html lang='ko'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'><title>검수 대상 목록</title>
<style>
:root {{ --line:#d8dde3; --bg:#f6f7f9; --ink:#1c2430; --muted:#68727f; }}
body {{ margin:0; padding:16px; font:13px/1.55 -apple-system,"Segoe UI","Malgun Gothic",sans-serif; color:var(--ink); background:var(--bg); }}
h1 {{ font-size:16px; margin:0 0 6px; }} h2 {{ font-size:14px; margin:18px 0 8px; }} h2 small {{ color:var(--muted); font-weight:400; }}
.bar {{ position:sticky; top:0; background:#fff; border:1px solid var(--line); border-radius:8px; padding:8px 12px; margin-bottom:10px; z-index:2; }}
.bar label {{ margin-right:14px; }} .nav a {{ margin-right:10px; font-size:12px; }}
.card {{ background:#fff; border:1px solid var(--line); border-radius:8px; padding:10px; margin-bottom:10px; }}
.card.hide {{ display:none; }}
.head {{ font-size:12px; color:var(--muted); }} .head b {{ color:var(--ink); }}
.tags {{ margin:6px 0; }} .tag {{ display:inline-block; font-size:11px; border-radius:4px; padding:1px 7px; margin:0 4px 3px 0; color:#fff; }}
.tag.cur {{ background:#EF6C00; }} .tag.new {{ background:#6A1B9A; }}
.cols {{ display:grid; grid-template-columns:minmax(0,2fr) minmax(0,3fr); gap:12px; }}
.img img {{ max-width:100%; border:1px solid var(--line); }}
table {{ border-collapse:collapse; width:100%; }} th, td {{ border:1px solid var(--line); padding:4px 8px; vertical-align:top; text-align:left; }}
th {{ width:90px; background:#eef1f4; font-weight:600; font-size:12px; }} td {{ word-break:break-word; }}
.muted {{ color:var(--muted); }}
@media (max-width:900px) {{ .cols {{ grid-template-columns:1fr; }} }}
</style></head><body>
<h1>검수 대상 목록 — {html.escape(run_name)}</h1>
<p class='muted'>Region {total}개 중 현재 검수 표시 {counts['current']}개. 보라색 R1~R3은 디지털 텍스트 없이 돌릴 때를 위해 제안한 기준으로, P1 값으로 계산했으며 파이프라인에는 아직 반영되지 않았다.</p>
<div class='bar'>{buttons}<div class='nav'>{''.join(nav)}</div></div>
{''.join(cards)}
<script>
const boxes=[...document.querySelectorAll('.f')];
function apply(){{const on=new Set(boxes.filter(b=>b.checked).map(b=>b.value));
document.querySelectorAll('.card').forEach(c=>{{const r=c.dataset.rules.split(' ').filter(Boolean);c.classList.toggle('hide',!r.some(x=>on.has(x)));}});}}
boxes.forEach(b=>b.addEventListener('change',apply));
</script></body></html>"""
    return page_html, counts


def main() -> None:
    parser = argparse.ArgumentParser(description="검수 대상 Region HTML")
    parser.add_argument("--run", required=True)
    args = parser.parse_args()
    page, counts = build(args.run)
    out = OUTPUT_ROOT / args.run / "review.html"
    out.write_text(page, encoding="utf-8")
    print(f"완료: {out}", counts)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
