"""[실험] 시각 표를 VLM에 HTML(rowspan/colspan)로 받아 격자·마크다운으로 바꾼다.

`항목 | 값` 줄 나열은 열이 셋 이상이거나 병합 칸이 있는 표에서 구조를 잃는다.
HTML은 병합을 표현할 수 있으므로 모델에는 HTML을 받고, 코드가 결정론으로
격자(병합 해제)와 마크다운(심의 입력 문구)을 만든다. 격자 폭이 행마다 다르면
구조를 잘못 읽은 것으로 보고 호출측이 기존 경로로 되돌린다.
"""
from __future__ import annotations

import os
import re
import unicodedata
from collections import Counter
from html.parser import HTMLParser
from typing import Any

from PIL import Image

from ..vlm import client as vlm_client

FORMATS = ("pipe", "html")

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
  걸친 자리에 같은 값을 반복해 적지 마세요. 모든 행의 칸 수(colspan 포함)가 같아야 합니다.
- 칸 안의 줄바꿈은 <br>로, 글자·숫자·단위·기호는 보이는 그대로 적으세요.
- title은 이미지 안에 표 제목 글자가 **실제로 인쇄돼 있을 때만** 그 글자를 그대로 적고,
  없으면 빈 문자열로 두세요. 제목을 지어내지 마세요.
- 표 밖의 ※·- 로 시작하는 각주는 notes에 한 줄씩 넣으세요. 표 밖 문구를 <table> 안에 넣지 마세요.
- 표 이미지에 없는 행·열·숫자를 만들지 말고, 읽을 수 없는 곳은 추측하지 마세요.
- 속성은 rowspan, colspan만 쓰고 style·class 등은 쓰지 마세요.
- analysis에는 표 구조를 한 문장으로 설명하세요.
"""


def format_from_env() -> str:
    value = str(os.environ.get("PARSER_V2_TABLE_FORMAT", "pipe")).strip().lower()
    return value if value in FORMATS else "pipe"


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

    각 자리는 원래 칸의 값과 `origin`(병합 시작 좌표)을 가진다. 병합으로 채워진
    자리는 `spanned=True`이고 `span`이 `row`/`col`이다.
    """
    parser = _TableParser()
    parser.feed(markup)
    grid: list[list[dict[str, Any] | None]] = []
    merged: list[dict[str, int]] = []
    for r, row in enumerate(parser.rows):
        while len(grid) <= r:
            grid.append([])
        c = 0
        for cell in row:
            while c < len(grid[r]) and grid[r][c] is not None:
                c += 1
            if cell["rowspan"] > 1 or cell["colspan"] > 1:
                merged.append({"row": r, "col": c, "rowspan": cell["rowspan"],
                               "colspan": cell["colspan"]})
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
    return {"grid": grid, "width": width, "rows": len(grid), "merged": merged,
            "ragged_widths": ragged, "holes": holes}


def is_valid(table: dict[str, Any]) -> bool:
    return bool(table["rows"]) and table["width"] >= 2 and len(table["ragged_widths"]) == 1 and not table["holes"]


def header_rows(table: dict[str, Any]) -> int:
    """맨 위부터 모든 칸이 <th>인 행 수. 머리글 행이 없는 표(좌측 머리글형)는 0이다."""
    count = 0
    for row in table["grid"]:
        if all(value is None or value["header"] for value in row):
            count += 1
        else:
            break
    return count


def to_markdown(table: dict[str, Any]) -> str:
    """격자 → GFM 마크다운.

    행 병합은 각 행에 값을 반복한다 — 그 값이 행마다 적용된다는 뜻이라 반복해도
    의미가 같다. 열 병합은 첫 칸에만 값을 두고 나머지를 비운다 — 반복하면 다른 열의
    값처럼 읽힌다. 머리글 행이 없으면 빈 머리글 행을 둔다(마크다운 표는 머리글 행이
    필수라 첫 데이터 행을 머리글로 올리면 의미가 바뀐다).
    """
    grid = table["grid"]
    if not grid:
        return ""

    def cell_text(value: dict[str, Any] | None) -> str:
        if value is None or (value["spanned"] and value["span"] == "col"):
            return ""
        return value["text"].replace("\n", "<br>").replace("|", "\\|")

    heads = header_rows(table)
    if heads:
        head = [cell_text(v) for v in grid[heads - 1]]
        body = grid[heads:]
    else:
        head = [""] * table["width"]
        body = grid
    lines = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    lines += ["| " + " | ".join(cell_text(v) for v in row) + " |" for row in body]
    return "\n".join(lines)


def compose_text(title: str, markdown: str, notes: list[str]) -> str:
    parts = [title.strip()] if title.strip() else []
    parts.append(markdown)
    parts += [note.strip() for note in notes if note.strip()]
    return "\n".join(parts)


def _alnum(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return "".join(char for char in normalized if char.isalnum()).casefold()


# 표로 묶이기 전 Region 문구의 글자가 HTML 결과(표·제목·각주)에 이 비율 이상 있어야 한다.
# 기존 문구에는 OCR 잡음이 섞여 있어 1.0을 요구하지 않는다. 실측: 카드 GS 표에서 큰
# 제목 한 줄(약 22자)이 빠졌을 때 약 0.77이었다.
MIN_COVERAGE = 0.85


def coverage(source_text: str, output_text: str) -> float:
    """source의 글자·숫자가 output에 얼마나 들어 있는지(문자 다중집합 기준)."""
    source, output = _alnum(source_text), _alnum(output_text)
    if not source:
        return 1.0
    shared = sum((Counter(source) & Counter(output)).values())
    return shared / len(source)


def extract(crop: Image.Image, feedback: str = "") -> dict[str, Any]:
    prompt = PROMPT + (f"\n{feedback}\n" if feedback else "")
    response = vlm_client.chat_json(
        [{"type": "text", "text": prompt},
         vlm_client.image_part(crop, box=(2400, 1800), quality=94)],
        schema_name="parser_v2_visual_table_html", schema=SCHEMA, max_tokens=9000,
    )
    table = to_grid(str(response.get("html") or ""))
    return {"response": response, "table": table}


def p3_table(table: dict[str, Any], title: str, notes: list[str]) -> dict[str, Any]:
    """심의 입력용 표 구조. 병합이 풀린 격자 텍스트와 병합 위치만 남긴다."""
    return {
        "format": "grid",
        "title": title.strip() or None,
        "header_rows": header_rows(table),
        "rows": [[(value or {}).get("text", "") for value in row] for row in table["grid"]],
        "merged": table["merged"],
        "notes": [note.strip() for note in notes if note.strip()],
    }
