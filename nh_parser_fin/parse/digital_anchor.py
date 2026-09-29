"""[실험] PDF 디지털 텍스트를 정본이 아니라 글자 교정 재료로 쓴다.

main은 디지털 줄을 좌표 순서로 이어 붙여 정본으로 확정한다. 그러면 글머리 기호가 줄
끝으로 밀리거나(`…이내\\n●`), 띄어쓰기가 빠지거나(`금융의모든순간`), 옆 박스 글자가
붙는(`…위한NH올원뱅크`) 문구가 VLM이 맞게 읽어도 그대로 나간다.

여기서는 순서·띄어쓰기·줄바꿈은 VLM 최종 문구를 따르고, 디지털에서는 **글자만**
가져온다. VLM 문구의 한글 낱말·숫자가 근처 디지털 글자와 거의 같으면(글자 수가 같고
몇 글자만 다름) 디지털 글자로 바꾼다. 디지털 글자는 한글·숫자만 쓰므로 깨진 문자열이나
PUA·특수문자가 문구에 들어가지 않는다. 바꿀 짝이 없는 낱말과, VLM 문구 어디에도
없는 디지털 줄은 기록하고 검수 대상으로 올린다.

    PARSER_V2_DIGITAL_MODE=primary  main과 같음(디지털 줄이 정본)
    PARSER_V2_DIGITAL_MODE=off      디지털 텍스트를 읽지 않음
    PARSER_V2_DIGITAL_MODE=anchor   이 모듈
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
    value = str(os.environ.get("PARSER_V2_DIGITAL_MODE", "primary")).strip().lower()
    return value if value in MODES else "primary"


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
            flag(region, "digital_text_missing")
            record["missing_lines"] = missing
            stats["missing"] += len(missing)
        if moved:
            record["moved_lines"] = moved
            stats["moved"] += len(moved)
        region["digital_anchor"] = record
    page["digital_anchor_stats"] = stats
    return stats


def _area(bbox: list[int]) -> int:
    return max(0, bbox[2] - bbox[0]) * max(0, bbox[3] - bbox[1])
