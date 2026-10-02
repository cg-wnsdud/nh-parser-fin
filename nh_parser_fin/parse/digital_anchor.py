"""PDF 디지털 텍스트를 정본이 아니라 글자 교정 재료로 쓴다.

primary 모드(이전 방식)는 디지털 줄을 좌표 순서로 이어 붙여 정본으로 확정한다. 그러면
글머리 기호가 줄 끝으로 밀리거나(`…이내\\n●`), 띄어쓰기가 빠지거나(`금융의모든순간`),
옆 박스 글자가 붙는(`…위한NH올원뱅크`) 문구가 VLM이 맞게 읽어도 그대로 나간다.

여기서는 순서·띄어쓰기·줄바꿈은 VLM 최종 문구를 따르고, 디지털에서는 **글자만**
가져온다. 세 단계다.
  1) 낱말 교정(snap_text): 한글 낱말·숫자가 근처 디지털 글자와 거의 같으면 바꾼다.
  2) 문장 정렬(align_spans): 양옆이 맞는 짧은 차이(빠진 각주 표시, 덧붙인 말, 끝 기호)를
     디지털로 메운다.
  3) 줄 확인: 페이지 어디에도 없는 소속 디지털 줄은 끼워 넣고, 옆 박스 줄이 들어오면 표시한다.
깨진 문자열·PUA·제어문자는 문구에 들어가지 않는다. 고친 곳과 의심되는 곳은 Region의
`digital_anchor`에 남고, 끼워 넣기·의심 낱말·중복 줄은 검수 대상으로 올린다.

    PARSER_V2_DIGITAL_MODE=anchor   이 모듈(기본)
    PARSER_V2_DIGITAL_MODE=primary  이전 방식(디지털 줄이 정본)
    PARSER_V2_DIGITAL_MODE=off      디지털 텍스트를 읽지 않음
"""
from __future__ import annotations

import os
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

from .adapters import line_overlap_ratio
from .quality import flag

MODES = ("primary", "off", "anchor")

# 교정 후보를 모을 때 Region 박스를 넓히는 폭(px). 박스가 줄을 반쯤 걸친 경우를 받는다.
NEAR_MARGIN = 16
NEAR_OVERLAP = 0.3
# main의 줄 배정과 같은 기준. 이 줄들은 "이 Region 소속"으로 보고 누락을 확인한다.
OWN_OVERLAP = 0.5
# 이보다 짧은 줄(한글·숫자 기준)은 누락 판정에서 뺀다. `●`, `1`, `만원` 같은 조각.
MISSING_MIN_CHARS = 6
MISSING_COVERAGE = 0.6

_HANGUL_RUN = re.compile(r"[가-힣]+")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_TOKEN = re.compile(r"[가-힣]+|\d+(?:[.,]\d+)*")


def mode_from_env() -> str:
    value = str(os.environ.get("PARSER_V2_DIGITAL_MODE", "anchor")).strip().lower()
    return value if value in MODES else "anchor"


def _hangul(text: str) -> str:
    return "".join(_HANGUL_RUN.findall(unicodedata.normalize("NFKC", text or "")))


def _content(text: str) -> str:
    """누락 비교용: 한글·숫자·영문만."""
    normalized = unicodedata.normalize("NFKC", text or "")
    return "".join(
        char for char in normalized
        if char.isalnum() and unicodedata.category(char) != "Co"
    ).casefold()


def _expand(bbox: list[int], margin: int) -> list[int]:
    return [bbox[0] - margin, bbox[1] - margin, bbox[2] + margin, bbox[3] + margin]


def _mismatch(a: str, b: str) -> int:
    return sum(1 for x, y in zip(a, b) if x != y)


def _hangul_allowance(length: int) -> int:
    # 3자 이하는 교정하지 않는다. 한 글자만 달라도 전혀 다른 낱말이 흔하다.
    return length // 4 if length >= 4 else 0


def _edit_distance(a: str, b: str, limit: int) -> int:
    """레벤시테인 거리. limit를 넘으면 limit+1을 돌려 계산을 줄인다."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, start=1):
        current = [i] + [0] * len(b)
        for j, y in enumerate(b, start=1):
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y))
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def _fix_hangul(word: str, pool: str, runs: set[str] = frozenset()) -> str | None:
    """pool(디지털 한글 연속열)에서 편집 거리가 가장 작은 유일한 짝.

    글자 수가 한 개 다른 짝도 본다. 실측(20. 대출성상품): 인쇄 `농어업전문교육이수자`를
    VLM이 `농업전문교육이수자`로 읽었을 때 같은 길이만 보면 `어업전문교육이수자`로
    잘못 바꾼다. 거리가 같은 후보가 여럿이면 디지털에서 기호 없이 이어진 한 덩어리(runs)와
    같은 후보를 먼저 고른다 — 실측(1. 예금성상품) `⑦번동의서`를 VLM이 `⑦변동의서`로
    읽었을 때 비율만 보면 `동의서`가 이긴다. 그다음 SequenceMatcher 비율로 가리고, 그래도
    같으면 바꾸지 않는다.
    """
    allowed = _hangul_allowance(len(word))
    if not allowed:
        return None
    scored: dict[str, int] = {}
    for size in (len(word) - 1, len(word), len(word) + 1):
        if size < 2:
            continue
        for start in range(len(pool) - size + 1):
            window = pool[start:start + size]
            if window in scored:
                continue
            distance = _edit_distance(word, window, allowed)
            if distance <= allowed:
                scored[window] = distance
    if not scored:
        return None
    best = min(scored.values())
    tied = [window for window, distance in scored.items() if distance == best]
    ranked = sorted(
        ((window in runs, SequenceMatcher(None, word, window).ratio(), window) for window in tied),
        reverse=True,
    )
    if len(ranked) > 1 and ranked[0][:2] == ranked[1][:2]:
        return None
    return ranked[0][2]


def _wrapped(word: str, line_words: list[str]) -> bool:
    """표 칸 안에서 줄이 나뉜 낱말(`운전` / `자금`)인지. 디지털 줄 순서는 칸끼리 섞인다."""
    return any(
        any(piece.endswith(word[:k]) for piece in line_words)
        and any(piece.startswith(word[k:]) for piece in line_words)
        for k in range(1, len(word))
    )


def _fix_number(number: str, pool: set[str]) -> str | None:
    """형식(자리·점 위치)이 같고 숫자 하나만 다른 유일한 디지털 숫자."""
    shape = re.sub(r"\d", "0", number)
    found = {
        candidate for candidate in pool
        if len(candidate) == len(number) and re.sub(r"\d", "0", candidate) == shape
        and _mismatch(candidate, number) == 1
    }
    return found.pop() if len(found) == 1 else None


def snap_text(
    text: str, lines: list[dict[str, Any]],
) -> tuple[str, list[dict[str, str]], list[str], list[str]]:
    """VLM 문구의 한글 낱말·숫자를 디지털 글자에 맞춘다. 순서·공백·기호는 그대로 둔다.

    돌려주는 값: (교정 문구, 교정 목록, 짝 없는 낱말, 그중 의심 낱말). 의심 낱말은 앞뒤
    낱말은 디지털에 있는데 그 낱말만 없는 경우다. 짝 없는 낱말이 둘 이상 이어지면
    로고·배너 같은 그림 속 글자로 보고 의심하지 않는다.
    """
    if not text or not lines:
        return text, [], [], []
    joined = "\n".join(str(line.get("text") or "") for line in lines)
    pool_hangul = _hangul(joined)
    runs = set(_HANGUL_RUN.findall(unicodedata.normalize("NFKC", joined)))
    line_words = [_hangul(str(line.get("text") or "")) for line in lines]
    pool_numbers = set(_NUMBER.findall(unicodedata.normalize("NFKC", joined)))
    changes: list[dict[str, str]] = []
    unmatched: list[str] = []
    suspect: list[str] = []

    def resolve(token: str) -> tuple[str, bool | None]:
        """(바꿀 값, 짝 있음). 판단 대상이 아니면 None."""
        if token[0].isdigit():
            if token in pool_numbers:
                return token, True
            fixed = _fix_number(token, pool_numbers)
        else:
            if len(token) < 2:
                return token, None
            if token in pool_hangul or _wrapped(token, line_words):
                return token, True
            fixed = _fix_hangul(token, pool_hangul, runs)
        if fixed is None:
            return token, False
        changes.append({"from": token, "to": fixed})
        return fixed, True

    def snap_line(line: str) -> str:
        pieces, status, cursor = [], [], 0
        for match in _TOKEN.finditer(line):
            pieces.append(line[cursor:match.start()])
            value, found = resolve(match.group(0))
            pieces.append(value)
            if found is not None:
                status.append((match.group(0), found))
            cursor = match.end()
        pieces.append(line[cursor:])
        for index, (token, found) in enumerate(status):
            if found:
                continue
            unmatched.append(token)
            neighbors = [status[i][1] for i in (index - 1, index + 1) if 0 <= i < len(status)]
            if neighbors and all(neighbors):
                suspect.append(token)
        return "".join(pieces)

    snapped = "\n".join(snap_line(line) for line in text.split("\n"))
    return snapped, changes, unmatched, suspect


def _snap_table(table: dict[str, Any], lines: list[dict[str, Any]]) -> None:
    for row in table.get("rows") or []:
        for index, cell in enumerate(row):
            row[index] = snap_text(str(cell or ""), lines)[0] if cell else cell
    for cell in table.get("cells") or []:
        if cell.get("text"):
            cell["text"] = snap_text(str(cell["text"]), lines)[0]
    if table.get("title"):
        table["title"] = snap_text(str(table["title"]), lines)[0]
    table["notes"] = [snap_text(str(note), lines)[0] for note in table.get("notes") or []]


def _covered(line_text: str, region_text: str) -> float:
    line, text = _content(line_text), _content(region_text)
    if not line:
        return 1.0
    if line in text:
        return 1.0
    blocks = SequenceMatcher(None, line, text, autojunk=False).get_matching_blocks()
    return sum(block.size for block in blocks) / len(line)


# 문장 정렬: 앞뒤로 이만큼 같은 글자가 이어져야 그 사이 차이를 디지털로 바꾼다.
ALIGN_ANCHOR = 3
# 한 번에 바꾸는 구간 길이(공백 제외) 상한. 길면 줄 배정·순서 차이일 가능성이 커서 손대지 않는다.
ALIGN_MAX_SPAN = 6
# 글머리·장식 기호. 디지털 쪽 표기(ㆍ·●)를 VLM 표기(•)에 덮어쓰지 않는다.
_BULLETS = set("•·ㆍ●○◦▪▫■□◾◽▶▷►-–—*※")
_MARKUP = set("|<>")
_MARKUP_RE = re.compile(r"<br\s*/?>|\||-{3,}")


def _row_order(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """세로로 절반 이상 겹치는 줄을 한 행으로 보고 행 안에서는 왼쪽부터 놓는다.

    본문 옆에 작게 붙은 `주1)` 같은 줄은 윗변이 본문보다 조금 위라 y 정렬만 하면
    본문 앞에 온다. 같은 행으로 묶어야 `…동의 시` 뒤에 붙는다.
    """
    rows: list[list[dict[str, Any]]] = []
    for line in sorted(lines, key=lambda item: (item["bbox"][1], item["bbox"][0])):
        box = line["bbox"]
        for row in rows:
            ref = row[0]["bbox"]
            overlap = min(box[3], ref[3]) - max(box[1], ref[1])
            height = max(1, min(box[3] - box[1], ref[3] - ref[1]))
            if overlap / height >= 0.5:
                row.append(line)
                break
        else:
            rows.append([line])
    return [line for row in rows for line in sorted(row, key=lambda item: item["bbox"][0])]


def _usable(char: str) -> bool:
    return not char.isspace() and unicodedata.category(char) not in ("Co", "Cc", "Cf", "Cs")


def _meaningful(chars: str) -> bool:
    return any(char.isalnum() or unicodedata.category(char) == "No" for char in chars if char not in _BULLETS)


def align_spans(
    text: str, lines: list[dict[str, Any]], own: list[dict[str, Any]] | None = None,
) -> tuple[str, list[dict[str, str]]]:
    """VLM 문구와 디지털 글자열을 정렬해, 양옆이 맞는 짧은 차이를 디지털로 메운다.

    낱말 교정(snap_text)이 못 하는 세 경우를 다룬다.
      - VLM이 글자를 뺐다: `…동의 시1)` → `…동의 시주1)` (작게 붙은 각주 표시)
      - VLM이 말을 덧붙였다: `자동으로 이체` → `자동이체`
      - VLM이 기호를 뺐다: `최고 1.50%p` → `최고 1.50%p (①+②)` (문구 끝)
    바뀐 구간은 공백을 뺀 길이 ALIGN_MAX_SPAN 이하, 양옆에 ALIGN_ANCHOR 글자 이상
    일치가 있어야 한다(문구 끝은 한쪽만). 표 구분자(`|`)·줄바꿈을 걸치거나 기호만 다른
    차이는 두지 않는다.

    근처 줄(lines)에는 옆 박스 글자도 섞이므로 넣기·지우기는 좁게 허용한다.
      - 문장 중간에 넣기: 숫자 없는 2글자 이하(`주`, `려`). 숫자·낱말을 넣으면 옆 칸 값이 들어온다.
      - 문구 끝에 붙이기: 이 Region 소속 줄(own)의 글자만.
      - 지우기: 한글 3글자 이하(`으로`). 더 길면 칸 순서 차이일 수 있다.
    """
    if not text or not lines:
        return text, []
    own_ids = {id(line) for line in own or []}
    digital_raw, owned = "", []
    for number, line in enumerate(_row_order(lines)):
        piece = ("\n" if number else "") + str(line.get("text") or "")
        digital_raw += piece
        owned += [id(line) in own_ids] * len(piece)
    # 마크다운 표 표기(`|`, `---`, `<br>`)는 정렬에서 뺀다. 그대로 두면 일치 구간이 끊긴다.
    markup = {index for match in _MARKUP_RE.finditer(text) for index in range(match.start(), match.end())}
    t_pos = [index for index, char in enumerate(text) if _usable(char) and index not in markup]
    d_pos = [index for index, char in enumerate(digital_raw) if _usable(char)]
    t_seq = "".join(text[index] for index in t_pos)
    d_seq = "".join(digital_raw[index] for index in d_pos)
    if not t_seq or not d_seq:
        return text, []
    ops = SequenceMatcher(None, t_seq, d_seq, autojunk=False).get_opcodes()
    edits: list[tuple[int, int, str, dict[str, str]]] = []
    for k, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == "equal":
            continue
        t_span, d_span = t_seq[i1:i2], d_seq[j1:j2]
        if max(len(t_span), len(d_span)) > ALIGN_MAX_SPAN:
            continue
        if _MARKUP & set(t_span + d_span):
            continue
        if not _meaningful(t_span) and not _meaningful(d_span):
            continue  # 기호만 다르다(• 와 ㆍ 등). VLM 표기를 둔다.
        before = ops[k - 1] if k > 0 else None
        after = ops[k + 1] if k + 1 < len(ops) else None
        left_ok = before is not None and before[0] == "equal" and before[2] - before[1] >= ALIGN_ANCHOR
        right_ok = after is not None and after[0] == "equal" and after[2] - after[1] >= ALIGN_ANCHOR
        at_end = after is None and i2 == len(t_seq) and j2 == len(d_seq)
        if not (left_ok and (right_ok or at_end)):
            continue
        if tag == "delete" and (len(t_span) > 3 or not re.fullmatch(r"[가-힣]+", t_span)):
            continue  # VLM만 가진 숫자·기호·긴 구간은 지우지 않는다. 틀렸어도 표시(suspect)로 남긴다.
        if tag == "insert" and not at_end and (len(d_span) > 2 or re.search(r"\d", d_span)):
            continue
        if at_end and tag == "insert" and not all(owned[d_pos[j]] for j in range(j1, j2)):
            continue
        # 원문 위치: 왼쪽 일치 끝 글자 다음부터 오른쪽 일치 첫 글자 앞까지(사이 공백 포함).
        start = t_pos[i1 - 1] + 1
        end = t_pos[i2] if i2 < len(t_pos) else len(text)
        if any(mark in text[start:end] for mark in ("|", "\n", "<br", "---")):
            continue  # 표 칸·줄 경계를 걸친 차이는 구조 차이다.
        d_start = d_pos[j1 - 1] + 1
        d_end = d_pos[j2] if j2 < len(d_pos) else (d_pos[j2 - 1] + 1 if j2 else d_start)
        replacement = digital_raw[d_start:d_end]
        if "\n" in replacement:
            # 디지털 줄 경계. VLM이 그 자리를 띄웠으면 한 칸, 붙였으면 붙인다.
            gap = " " if any(char.isspace() for char in text[start:end]) else ""
            replacement = re.sub(r"\s*\n\s*", gap, replacement)
        if at_end:
            replacement = replacement.rstrip()
        replacement = "".join(char for char in replacement if char.isspace() or _usable(char))
        edits.append((start, end, replacement, {"from": text[start:end], "to": replacement}))
    for start, end, replacement, _ in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text, [change for *_, change in edits]


def _insert_missing(text: str, own: list[dict[str, Any]], missing: list[str]) -> str:
    """페이지 어디에도 없는 디지털 줄을 세로 위치에 맞춰 끼워 넣는다.

    문구의 각 줄을 가장 비슷한 소속 디지털 줄에 대응시키고, 빠진 줄은 자기보다 아래에
    있는 첫 줄 앞에 넣는다. 대응되는 줄이 없으면 끝에 붙인다.
    """
    by_text = {str(line.get("text") or ""): line for line in own}
    text_lines = text.split("\n")
    anchors: list[float | None] = []
    for text_line in text_lines:
        best = max(
            ((_covered(str(line.get("text") or ""), text_line), line) for line in own
             if str(line.get("text") or "") not in missing),
            key=lambda item: item[0], default=(0.0, None),
        )
        anchors.append(best[1]["bbox"][1] if best[1] is not None and best[0] >= MISSING_COVERAGE else None)
    for line_text in sorted(missing, key=lambda value: by_text[value]["bbox"][1]):
        y = by_text[line_text]["bbox"][1]
        clean = "".join(char for char in line_text if char.isspace() or _usable(char)).strip()
        position = next((i for i, value in enumerate(anchors) if value is not None and value > y), len(text_lines))
        text_lines.insert(position, clean)
        anchors.insert(position, y)
    return "\n".join(text_lines)


def anchor_page(page: dict[str, Any]) -> dict[str, int]:
    """페이지 Region 문구를 디지털 글자에 맞추고, 빠진 디지털 줄을 찾는다."""
    lines = [
        line for line in page.get("digital_anchor_lines") or []
        if line.get("bbox") and _content(line.get("text"))
    ]
    stats = {"regions": 0, "corrected": 0, "corrections": 0, "unmatched": 0,
             "missing": 0, "moved": 0, "blank_filled": 0}
    if not lines:
        page["digital_anchor_stats"] = stats
        return stats
    regions = [region for region in page.get("regions") or [] if region.get("bbox")]

    # main의 줄 배정 규칙(가장 많이 겹치는 Region 하나)으로 소속을 정한다. 누락 판정용이다.
    owner: dict[int, list[dict[str, Any]]] = {}
    for line in lines:
        best = max(
            ((line_overlap_ratio(line["bbox"], region["bbox"]), -_area(region["bbox"]), index)
             for index, region in enumerate(regions)),
            default=None,
        )
        if best and best[0] >= OWN_OVERLAP:
            owner.setdefault(best[2], []).append(line)

    for index, region in enumerate(regions):
        source = str(region.get("text_source") or "")
        # HWP·구조 파서 원문은 이미 정본이다.
        if source.startswith("document_processor") or source.startswith("hwp"):
            continue
        near_box = _expand(region["bbox"], NEAR_MARGIN)
        near = [line for line in lines if line_overlap_ratio(line["bbox"], near_box) >= NEAR_OVERLAP]
        own = owner.get(index, [])
        if not near and not own:
            continue
        stats["regions"] += 1
        record: dict[str, Any] = {"near_lines": len(near), "own_lines": len(own)}
        text = str(region.get("text") or "")

        if not _content(text) and own:
            # VLM도 OCR도 비었는데 디지털 줄은 있다. main과 같이 디지털 줄을 쓴다.
            text = "\n".join(str(line.get("text") or "").strip() for line in own)
            region["text"] = text
            region["text_source"] = "digital_anchor_fallback"
            record["blank_filled"] = True
            stats["blank_filled"] += 1
        else:
            snapped, changes, unmatched, suspect = snap_text(text, near)
            if changes:
                region.setdefault("text_candidates", {})["pre_digital_anchor"] = text
                region["text"] = snapped
                text = snapped
                stats["corrected"] += 1
                stats["corrections"] += len(changes)
            if region.get("visual_table") and changes:
                _snap_table(region["visual_table"], near)
            record["corrections"] = changes
            record["unmatched"] = unmatched
            record["suspect"] = suspect
            stats["unmatched"] += len(unmatched)
            # 셀 정본이 있는 표의 마크다운은 셀에서 만든 보기다. 머리글 경로 구분자(` / `)와
            # 병합 칸 반복은 디지털 글자와 맞출 차이가 아니므로 차이 메우기를 하지 않는다.
            # 실측: 040 우대금리 표 머리글 `세부조건 / 가입기간`이 `세부조건 우대금리 가입기간`이 됐다.
            grid_table = (region.get("visual_table") or {}).get("cells") is not None
            aligned, spans = (text, []) if grid_table else align_spans(text, near, own)
            if spans:
                region.setdefault("text_candidates", {}).setdefault("pre_digital_anchor", text)
                region["text"] = aligned
                text = aligned
                stats["aligned"] = stats.get("aligned", 0) + len(spans)
                # 정렬로 메운 낱말은 더 이상 의심하지 않는다.
                suspect = [word for word in suspect if word in text]
                record["suspect"] = suspect
            record["aligned"] = spans
            stats["suspect"] = stats.get("suspect", 0) + len(suspect)
            if suspect:
                flag(region, "vlm_not_in_digital")

        missing, moved = [], []
        page_text = "\n".join(str(other.get("text") or "") for other in regions if other is not region)
        for line in own:
            line_text = str(line.get("text") or "")
            if len(_content(line_text)) < MISSING_MIN_CHARS or _covered(line_text, text) >= MISSING_COVERAGE:
                continue
            if _covered(line_text, page_text) >= MISSING_COVERAGE:
                moved.append(line_text)
            else:
                missing.append(line_text)
        if missing:
            # 빠진 줄을 제자리에 끼워 넣고, 사람이 확인하도록 표시는 남긴다.
            region.setdefault("text_candidates", {}).setdefault("pre_digital_anchor", text)
            text = _insert_missing(text, own, missing)
            region["text"] = text
            flag(region, "digital_text_inserted")
            record["missing_lines"] = missing
            stats["missing"] += len(missing)
        # 다른 Region 소속 디지털 줄이 이 문구에 통째로 들어 있고 소속 Region에도 있으면
        # 페이지 Reader가 맞붙은 박스의 줄을 이쪽에도 넣은 것이다(중복).
        duplicated = []
        for other_index, other_lines in owner.items():
            if other_index == index or _nested(region["bbox"], regions[other_index]["bbox"]):
                continue
            other_text = _content(str(regions[other_index].get("text") or ""))
            for line in other_lines:
                content = _content(line.get("text"))
                if (
                    # 줄이 이 박스 안에도 걸쳐 있으면 박스끼리 겹친 것이지 잘못 읽은 게 아니다.
                    line_overlap_ratio(line["bbox"], region["bbox"]) < NEAR_OVERLAP
                    and _adjacent(region["bbox"], line["bbox"])
                    and len(content) >= MISSING_MIN_CHARS
                    and content in _content(text) and content in other_text
                    and not any(content in _content(mine.get("text")) for mine in own)
                ):
                    duplicated.append(str(line.get("text") or ""))
        if duplicated:
            flag(region, "neighbor_line_duplicated")
            record["duplicated_lines"] = duplicated
            stats["duplicated"] = stats.get("duplicated", 0) + len(duplicated)
        if moved:
            record["moved_lines"] = moved
            stats["moved"] += len(moved)
        region["digital_anchor"] = record
    page["digital_anchor_stats"] = stats
    return stats


def _adjacent(box: list[int], line_box: list[int]) -> bool:
    """줄이 Region 바로 위·아래(줄 높이 1.5배 이내)에 붙어 있는가.

    같은 유의사항이 상품마다 반복 인쇄되는 광고가 흔하다(4. 카드상품). 멀리 떨어진
    같은 문구는 중복이 아니라 반복 인쇄이므로, 맞붙은 줄만 본다.
    """
    height = max(1, line_box[3] - line_box[1])
    gap = max(line_box[1] - box[3], box[1] - line_box[3], 0)
    overlap_x = min(box[2], line_box[2]) - max(box[0], line_box[0])
    return overlap_x > 0 and gap <= 1.5 * height


def _nested(a: list[int], b: list[int]) -> bool:
    """한 박스가 다른 박스에 대부분(80%) 들어 있는가 — Paddle 부모·자식 박스."""
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return ix * iy >= 0.8 * max(1, min(_area(a), _area(b)))


def _area(bbox: list[int]) -> int:
    return max(0, bbox[2] - bbox[0]) * max(0, bbox[3] - bbox[1])
