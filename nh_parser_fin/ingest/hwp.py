"""HWP/HWPX 트랙 — 사내 파서(document-processor) 연동. 설계서 5절/6.8절.

텍스트·표는 디지털 정본으로 추출한다. 내장 이미지(BinData)는 호출측이 넘긴
image_page_processor(PNG 입력과 동일한 OCR/VLM 경로)로 별도 페이지로 처리한다
— DocIR 문단 스트림에 이미지 참조 노드가 없어 문서 내 위치는 알 수 없고,
bbox 는 이미지-로컬 좌표다 (2026-07-18 실측).

HWP 렌더링(시각 검토용 캔버스)은 프로토타입 범위 밖이며, 렌더 갭은
AdDocument.notes 에 명시한다. 사내 파서 실패 시(예: Java 미가용)
parse_status="unreadable" 로 조용한 실패를 남기지 않는다.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from PIL import Image

from .assets import decode_asset_image, is_decorative, iter_assets
from ..vlm.client import filename_prior
from ..ir import AdDocument, AdPage, Line, Region, TextStyle
from .text_style import fold

_ANCHOR_RE = re.compile(r"^\[tbl:[^\]]+\]$")   # 표 뒤에 붙는 중첩표 앵커 라인(단독)
_TOKEN_RE = re.compile(r"\[tbl:[^\]]+\]")      # 셀 안에 박힌 중첩표 참조 토큰


def _squash(text: str) -> str:
    return re.sub(r"\s{2,}", " ", (text or "").replace("\n", " ")).strip()


def _cell_render(cell, depth: int) -> str:
    """셀 하나를 문단 순서대로 렌더. 중첩표는 그 자리에 구조로 인라인한다.

    중첩표를 품은 문단은 `.text` 가 **이미 그 표의 평문판**이다(004 실측: p2.text =
    '구분\\n고정금리\\n변동금리\\n적용요율\\n0.01%\\n0.01%'). 그래서 문단 텍스트와
    구조 렌더를 둘 다 넣으면 같은 표가 두 번 들어간다. 표가 있는 문단은 텍스트를
    버리고 구조 렌더로 대체한다 — 열 구분(`|`)이 살아 '적용요율 | 0.01% | 0.01%' 로
    읽히므로, 어느 열의 값인지 알 수 있다.
    """
    parts: list[str] = []
    for para in getattr(cell, "paragraphs", None) or []:
        tables = [
            n for n in (getattr(para, "content", None) or [])
            if type(n).__name__ == "TableIR"
        ]
        if tables:
            for table in tables:
                sub = _struct_table_rows(table, depth + 1)
                if sub:
                    parts.append(" / ".join(sub))
                else:  # 구조를 못 읽으면 문단이 들고 있는 평문이라도 살린다
                    parts.append(_squash(getattr(para, "text", "")))
        else:
            parts.append(_squash(getattr(para, "text", "")))
    joined = " ".join(p for p in parts if p)
    return joined or _squash(getattr(cell, "text", ""))


def _run_atoms(runs) -> list[tuple]:
    """RunIR 목록 → `text_style.StyleAtom` 목록. 글자수만큼 반복해 담는다.

    **왜 글자수만큼인가.** 대표값을 글자수 최다로 뽑으므로(text_style.fold) run 하나를
    한 표로 세면 35pt 한 글자가 10pt 서른 글자를 이긴다. 실측(`004-대출성.hwp`):
    `『`(30pt) · `공무원`(35pt) · `을 위한`(24pt) 이 각각 별 run 이다.
    """
    atoms: list[tuple] = []
    for run in runs or []:
        text = getattr(run, "text", None) or ""
        if not text.strip():
            continue
        style = getattr(run, "run_style", None)
        if style is None:
            continue
        color = getattr(style, "color", None)
        atom = (
            getattr(style, "size_pt", None),
            getattr(style, "bold", None),
            color.upper() if isinstance(color, str) and color else None,
            getattr(style, "font_family", None),
            None,  # HWP 는 파서가 bold 를 bool 로 준다 — 굵기 원값이 없다
        )
        atoms.extend([atom] * len(text.strip()))
    return atoms


def _cell_runs(cell) -> list:
    """셀 하나가 품은 모든 run. 중첩표 안의 run 도 포함된다.

    `paragraph.iter_all_runs()` 가 문단이 품은 표 셀까지 내려간다 — 실측(`004-대출성.hwp`):
    문단 1개(content=TableIR 하나)에서 run 141개가 나오고 그중 138개가 표 안이다.
    그래서 중첩표를 따로 훑을 필요가 없다.
    """
    runs: list = []
    for para in getattr(cell, "paragraphs", None) or []:
        try:
            runs.extend(para.iter_all_runs())
        except Exception:
            continue
    return runs


def _paragraph_style(para) -> TextStyle | None:
    """문단이 직접 들고 있는 run(표 밖)의 시인성. 표는 행 단위로 따로 붙인다."""
    runs = [
        node for node in getattr(para, "content", None) or []
        if type(node).__name__ != "TableIR" and getattr(node, "run_style", None) is not None
    ]
    return fold(_run_atoms(runs), basis="declared", source="hwp_run")


def _struct_table_rows(table, depth: int = 0) -> list[str] | None:
    """TableIR 구조에서 행 문자열을 만든다 (markdown 문자열 파싱 대신).

    **왜 구조를 쓰나.** markdown() 은 병합 셀을 걸친 칸 수만큼 반복 출력한다. 그래서
    예전에는 '연속으로 같은 값이면 병합셀' 이라는 텍스트 규칙으로 접었는데, **값이
    진짜로 같은 이웃 칸**까지 삼켰다 — 실측(004 부대비용 중첩표):

        원본 구조: (2,2) colspan=1 '0.01%' / (2,3) colspan=1 '0.01%'   ← 별개 셀
        옛 출력  : '적용요율 0.01%'                                     ← 하나 소실

    고정금리와 변동금리 중 어디에 요율이 붙는지 알 수 없게 된다. 금리·수수료 표에는
    같은 값이 나란히 오는 일이 흔해서 이 규칙은 계속 값을 잃는다.

    iter_cell_positions() 는 병합 셀을 **논리 셀 하나로 한 번만** 돌려주므로 추측이
    필요 없다. 규칙을 손보는 게 아니라 없애는 수정이다.

    파서가 이 API 를 제공하지 않으면 None 을 돌려 markdown 경로로 되돌아간다.
    """
    styled = _struct_table_rows_styled(table, depth)
    return None if styled is None else [text for text, _ in styled]


def _struct_table_rows_styled(table, depth: int = 0) -> list[tuple[str, TextStyle | None]] | None:
    """`_struct_table_rows` 의 본체. 행 문자열과 **그 행의 시인성**을 함께 돌려준다.

    행 텍스트와 스타일을 한 함수에서 같이 만드는 이유는 **정렬을 보장**하기 위해서다.
    행 목록과 스타일 목록을 따로 만들면 빈 셀 걸러내기 조건이 갈리는 순간 한 칸씩
    밀려 엉뚱한 행에 크기가 붙는다.
    """
    if depth > 3 or not hasattr(table, "iter_cell_positions"):
        return None
    try:
        positions = list(table.iter_cell_positions())
    except Exception:
        return None

    by_row: dict[int, list[tuple[int, str]]] = {}
    runs_by_row: dict[int, list] = {}
    for row, col, cell in positions:
        text = _cell_render(cell, depth)
        if text:
            by_row.setdefault(row, []).append((col, text))
            runs_by_row.setdefault(row, []).extend(_cell_runs(cell))

    rows: list[tuple[str, TextStyle | None]] = []
    for row in sorted(by_row):
        cells = [t for _, t in sorted(by_row[row])]
        if cells:
            style = fold(_run_atoms(runs_by_row.get(row)), basis="declared", source="hwp_run")
            rows.append((" | ".join(cells), style))
    return rows


def _md_cell_rows(md_lines: list[str]) -> list[list[str]]:
    """markdown 표 라인들 → 행별 셀 목록 (병합 셀 = 연속 중복 접기).

    폴백 전용 — 구조 API(_struct_table_rows)가 없는 파서 버전에서만 쓴다. 여기의
    '연속 중복 접기'는 같은 값의 이웃 칸을 잃는 알려진 한계가 있다.
    """
    rows: list[list[str]] = []
    for raw in md_lines:
        raw = raw.strip()
        if not raw.startswith("|") or set(raw) <= {"|", "-", " ", ":"}:
            continue
        cells = [c.strip().replace("<br>", " ").strip() for c in raw.strip("|").split("|")]
        collapsed: list[str] = []
        for cell in cells:
            if cell and (not collapsed or collapsed[-1] != cell):
                collapsed.append(cell)
        if collapsed:
            rows.append(collapsed)
    return rows


def _tables_to_lines(table) -> list[str]:
    """`_tables_to_rows` 의 텍스트만. rag 트랙(rag_ingest)과 기존 시험이 이 형태를 쓴다."""
    return [text for text, _ in _tables_to_rows(table)]


def _tables_to_rows(table) -> list[tuple[str, TextStyle | None]]:
    """TableIR → 행 단위 (텍스트, 시인성). 중첩 표(표 안의 표)를 부모 셀 자리에 인라인한다.

    사내 파서 markdown 은 중첩표 셀을 ``[tbl:<경로>]`` 토큰으로 남기고, 표 뒤에
    같은 토큰을 앵커로 붙여 중첩표 markdown 을 나열하는 각주 방식이다(004 실측).
    토큰 문자열이 정확히 일치하므로 경로 파싱 없이 매칭해 인라인한다:
    ① 단독 앵커 라인으로 본 표/중첩표 구획을 나누고 ② 각 중첩표를 평문으로 렌더해
    경로별 맵을 만든 뒤 ③ 본 표 셀 안의 토큰을 그 평문으로 치환한다. 중첩표가
    없는 일반 표(대다수 rag 문서)는 토큰이 없어 기존 동작과 동일하다.

    구조 API 가 있으면 그쪽을 쓴다 — markdown 문자열은 병합 셀을 반복 출력해서
    '값이 같은 이웃 칸'과 구분이 안 된다(_struct_table_rows 주석 참조).
    """
    structural = _struct_table_rows_styled(table)
    if structural is not None:
        return structural

    try:
        md = table.markdown() if callable(table.markdown) else table.markdown
    except Exception:
        return []
    if not md:
        return []

    # ① 단독 [tbl:경로] 앵커로 (본 표) + (중첩표 구획들) 분리
    segments: list[tuple[str | None, list[str]]] = []
    cur_path: str | None = None
    cur: list[str] = []
    for ln in md.splitlines():
        if _ANCHOR_RE.match(ln.strip()):
            segments.append((cur_path, cur))
            cur_path = ln.strip()[5:-1]  # "[tbl:PATH]" → "PATH"
            cur = []
        else:
            cur.append(ln)
    segments.append((cur_path, cur))

    main_lines = segments[0][1]

    # ② 중첩표 경로 → 평문 렌더 맵 (행: '셀 셀', 여러 행은 ' / ' 로 연결)
    nested: dict[str, str] = {}
    for path, seg_lines in segments[1:]:
        if path is None:
            continue
        flat = " / ".join(" ".join(row) for row in _md_cell_rows(seg_lines))
        nested[path] = flat
    # 중첩표 안의 중첩표 해소 (깊이 상한)
    for _ in range(3):
        changed = False
        for key, val in list(nested.items()):
            resolved = _TOKEN_RE.sub(lambda m: nested.get(m.group(0)[5:-1], ""), val)
            if resolved != val:
                nested[key] = resolved
                changed = True
        if not changed:
            break

    # ③ 본 표 셀의 토큰을 중첩표 평문으로 치환 후 행 문자열 생성
    # 폴백 경로는 스타일을 못 붙인다 — markdown 문자열에 run 정보가 없다. None 으로 둔다.
    rows: list[tuple[str, TextStyle | None]] = []
    for cells in _md_cell_rows(main_lines):
        replaced = [
            _TOKEN_RE.sub(lambda m: nested.get(m.group(0)[5:-1], ""), c).strip()
            for c in cells
        ]
        collapsed: list[str] = []
        for cell in replaced:
            cell = re.sub(r"\s{2,}", " ", cell).strip()
            if cell and (not collapsed or collapsed[-1] != cell):
                collapsed.append(cell)
        if collapsed:
            rows.append((" | ".join(collapsed), None))
    return rows


def _paragraph_text(para) -> str:
    parts: list[str] = []
    for node in getattr(para, "content", []) or []:
        text = getattr(node, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def ingest_hwp(
    path: Path,
    image_page_processor: Callable[[Image.Image, int], AdPage] | None = None,
) -> AdDocument:
    prior = filename_prior(path.name)
    doc = AdDocument(
        doc_id=path.stem,
        source_file=path.name,
        file_type=path.suffix.lstrip(".").lower(),
        product_group=prior,
        # "filename" 이 아니라 "filename_no_vlm" 이다 — HWP 는 캔버스가 없어 분류 VLM 을
        # **애초에 부르지 않는다**(gemma_client.classify 주석의 3번). VLM 이 실패한 것도
        # 반대한 것도 아니고 물어보지 않은 것이라, 같은 값으로 적으면 실측을 세는 순간
        # 틀린다. ad_type 이 None 인 것도 같은 이유다.
        category_source="filename_no_vlm" if prior else None,
    )
    try:
        from document_processor import DocIR

        docir = DocIR.from_file(str(path))
    except Exception as exc:
        doc.notes.append(f"사내 파서 HWP 추출 실패: {exc}")
        doc.pages.append(
            AdPage(page_no=1, parse_route="digital", parse_status="unreadable")
        )
        return doc

    lines: list[Line] = []
    for para in docir.paragraphs:
        text = _paragraph_text(para)
        if text.strip():
            lines.append(
                Line(text=text.strip(), source="digital", style=_paragraph_style(para))
            )
        for node in getattr(para, "content", []) or []:
            if type(node).__name__ == "TableIR":
                # 광고 HWP 는 표가 본체다 — 실측(`004-대출성.hwp`): run 141개 중 138개가
                # 표 안이다. 그래서 표 행에 시인성을 못 붙이면 사실상 아무것도 못 붙는다.
                for row_text, row_style in _tables_to_rows(node):
                    lines.append(Line(text=row_text, source="digital", style=row_style))

    page = AdPage(
        page_no=1,
        parse_route="digital",
        parse_status="ok" if lines else "partial",
        notes=[
            "HWP 디지털 추출 — bbox 없음. 시각 검토(F-011/F-013)용 렌더링은 "
            "설계서 6.8 옵션(R1: LibreOffice 변환 등) 확정 후 별도 처리"
        ],
    )
    page.regions.append(
        Region(region_id="p1_r000", label="hwp_body", role="본문", lines=lines)
    )
    # ⑥-4 필드추출 제거(2026-07-28) — 필드는 STAGE_3(스키마 기반) 하나로 일원화.
    # 이미지 라우트와 같은 이유다(pipeline._apply_vlm_judgments 주석 참조).
    doc.pages.append(page)

    # 내장 이미지 → PNG 입력과 동일한 OCR/VLM 트랙 (별도 페이지, 조용한 스킵 금지)
    skipped = 0
    next_no = 2
    for name, asset in iter_assets(docir):
        img = decode_asset_image(asset)
        if img is None:
            doc.notes.append(f"내장 이미지 {name}: 바이트 추출/디코딩 실패")
            continue
        if is_decorative(*img.size):
            skipped += 1
            continue
        if image_page_processor is None:
            doc.notes.append(f"내장 이미지 {name}: 이미지 프로세서 미연결 — 미처리")
            continue
        try:
            img_page = image_page_processor(img, next_no)
        except Exception as exc:
            doc.notes.append(f"내장 이미지 {name}: OCR/VLM 처리 실패: {exc}")
            continue
        # RunIR 텍스트의 "[image:<asset키>]" 마커가 문서 내 위치 앵커 (실측)
        anchor = next(
            (i for i, ln in enumerate(lines) if f"[image:{name}]" in ln.text), None
        )
        img_page.notes.append(
            f"HWP 내장 이미지 {name} ({img.size[0]}x{img.size[1]}px) — "
            "bbox 는 이미지-로컬 좌표"
            + (f", 본문 앵커: p1 라인 {anchor}" if anchor is not None else ", 본문 마커 없음")
        )
        doc.pages.append(img_page)
        next_no += 1
    if skipped:
        doc.notes.append(f"장식 내장 이미지 {skipped}개 스킵 (크기 필터)")
    return doc


def table_to_html(table, depth: int = 0) -> str | None:
    """TableIR → 농협 KL 이 요구하는 HTML `<table>` 문자열.

    **왜 필요한가.** 농협 KL 규격은 `table` item 의 `value` 가 HTML `<table>` 이어야
    한다고 못박고, 정답 샘플도 `<table cols=2 rows=7><tr><td>용어</td>…` 형식이다
    (`out_sample_hrc.jsonl` 실물). 그런데 우리는 `_tables_to_lines()` 로 표를 **행 단위
    평문**(`'용어 | 설명'`)으로 눌러 왔다. 구조를 이미 갖고 있는데 버리고 있었던 것이라,
    표를 표로 넘기려면 이 함수가 필요하다.

    `iter_cell_positions()` 를 쓰는 이유는 `_struct_table_rows()` 와 같다 — `markdown()`
    은 병합 셀을 걸친 칸 수만큼 반복 출력해서 '값이 진짜로 같은 이웃 칸'과 구분이 안 된다.
    구조 API 는 병합 셀을 **논리 셀 하나로 한 번만** 돌려준다.

    `cols`/`rows` 속성값은 정답 샘플의 형식을 그대로 따른다. 실제 셀이 없는 행은 건너뛰되
    `rows` 는 파서가 준 `row_count` 를 쓴다 — 원본 표의 크기를 신고하는 값이라 우리가
    센 것으로 바꾸면 뜻이 달라진다.

    구조 API 가 없거나 셀이 하나도 없으면 None 을 돌려준다. 호출측은 그때 평문 경로로
    되돌아가면 된다 — **표가 아닌 것을 표로 신고하지 않는 것**이 이 함수의 계약이다.
    """
    if depth > 3 or not hasattr(table, "iter_cell_positions"):
        return None
    try:
        positions = list(table.iter_cell_positions())
    except Exception:
        return None
    if not positions:
        return None

    by_row: dict[int, list[tuple[int, str]]] = {}
    for row, col, cell in positions:
        by_row.setdefault(row, []).append((col, _cell_render(cell, depth)))

    body: list[str] = []
    for row in sorted(by_row):
        cells = [text for _, text in sorted(by_row[row])]
        if not any(c.strip() for c in cells):
            continue
        body.append("<tr>" + "".join(f"<td>{_html_escape(c)}</td>" for c in cells) + "</tr>")
    if not body:
        return None

    col_count = int(getattr(table, "col_count", 0) or max(len(v) for v in by_row.values()))
    row_count = int(getattr(table, "row_count", 0) or len(by_row))
    return f"<table cols={col_count} rows={row_count}>" + "".join(body) + "</table>"


def _html_escape(text: str) -> str:
    """셀 안의 `<`·`&` 가 태그로 오해되지 않게 한다. 줄바꿈은 `<br>` 로 살린다.

    셀 안에 줄바꿈이 실재한다(중첩표를 인라인한 셀). 그냥 두면 HTML 에서 공백 하나로
    접혀 '한 줄이었던 것'과 구분이 안 된다.
    """
    out = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return out.replace("\n", "<br>")
