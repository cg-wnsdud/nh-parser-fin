"""PDF 페이지 단위 triage [B1] — 설계서 4.2절.

document-processor 의 probe 기준(정상문자 비율, U+FFFD, 이미지 면적비)을
pypdfium2 로 재현한다. 프로토타입에서는 이중 렌더를 피하려고 인라인 구현했고,
프로덕션에서는 사내 파서 probe 를 그대로 감싸도 된다.

핵심 원칙(조용한 실패 금지): 모든 페이지는 반드시 structured / scan_like /
hybrid 중 하나로 판정되고 판정 근거가 AdPage.triage 에 남는다.
"""

from __future__ import annotations

import ctypes
import unicodedata
from dataclasses import dataclass, field

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from ..config import SETTINGS
from ..ir import Line
from .text_style import fold, is_bold, rgb_hex, strip_subset_prefix

# FPDFText_GetFontInfo 버퍼. 실측 글꼴명이 `TRDBGG+SDGothicNeoa-fSm`(23자) 수준이라 넉넉하다.
_FONT_NAME_BUF = 128


@dataclass
class PageTriage:
    verdict: str                      # structured | scan_like | hybrid
    char_count: int
    korean_count: int
    fffd_count: int
    image_area_ratio: float
    path_count: int = 0               # 벡터 도형(PATH) 개수 — 판정에는 안 쓰고 기록만
    path_area_ratio: float = 0.0      # 겹침 미보정이라 1.0 을 넘을 수 있다(배경+패널 중첩)
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "char_count": self.char_count,
            "korean_count": self.korean_count,
            "fffd_count": self.fffd_count,
            "image_area_ratio": round(self.image_area_ratio, 3),
            "path_count": self.path_count,
            "path_area_ratio": round(self.path_area_ratio, 3),
            "reasons": self.reasons,
        }


def _object_census(page: pdfium.PdfPage) -> tuple[float, int, float]:
    """이미지/벡터 오브젝트를 한 번에 센다 → (이미지 면적비, PATH 개수, PATH 면적비).

    **왜 PATH 도 세나.** `_image_area_ratio` 는 FPDF_PAGEOBJ_IMAGE 만 센다. 그런데
    글자를 **벡터 윤곽선(PATH)으로 그려 넣은** 페이지가 있고, 그런 글자는 두 신호
    어디에도 안 잡힌다 — 텍스트 레이어에도 없고(문자가 아니라 도형이므로)
    이미지 면적비도 0 이다. 그래서 `structured` 로 판정돼 OCR 을 통째로 건너뛴다.

    실측 (003 p3, 2026-08-06 좌표 대조): 오브젝트 TEXT 173 · PATH 11 · **IMAGE 0**.
    PATH 11개 중 8개가 낱말 크기였고 원본 크롭과 좌표가 일치했다 —

        [100,91,147,107] '10/20'      [155,90,240,108] '인스타그램'
        [98,119,216,150] '올원모임'    [227,120,345,151] '소문내기'
        [359,120,448,151] '이벤트'     [457,120,513,150] '멘션'
        [1776,79,1910,101] 'Last Updated'  [1825,114,1911,128] '2025.10.15'

    나머지 3개만 배경(2000x1125)·패널(1854x808)·주황띠(1854x44)였다. 이 헤더 문구는
    디지털 레이어에 한 글자도 안 들어왔고(첫 디지털 줄이 y=190 의 '멘션'), ⑦-2
    통짜 스윕이 우연히 3건을 건졌다. 스윕이 안 걸렸으면 조용히 사라졌을 문구다.

    **그래도 판정은 안 바꾼다.** 벡터 글자와 벡터 장식(테두리·구분선·배경 도형)을
    개수나 면적으로 가를 방법이 이 표본에는 없다 — 대부분의 PDF 는 장식 PATH 를
    가지고 있고, 여기서 임계를 만들면 그건 샘플 1건에 맞춘 규칙이다. 그래서 세어서
    남기기만 한다. 판정을 바꾸려면 벡터 글자가 있는 문서를 더 모아 정답을 만드는
    것이 선행이다.
    """
    w, h = page.get_size()
    page_area = w * h
    if page_area <= 0:
        return 0.0, 0, 0.0
    img_total = path_total = 0.0
    path_n = 0
    try:
        for obj in page.get_objects(max_depth=2):
            if obj.type == pdfium_c.FPDF_PAGEOBJ_IMAGE:
                left, bottom, right, top = obj.get_bounds()  # pypdfium2 v5 (v4: get_pos)
                img_total += max(0.0, right - left) * max(0.0, top - bottom)
            elif obj.type == pdfium_c.FPDF_PAGEOBJ_PATH:
                path_n += 1
                left, bottom, right, top = obj.get_bounds()
                path_total += max(0.0, right - left) * max(0.0, top - bottom)
    except Exception:
        return 0.0, 0, 0.0
    # 이미지 면적비는 hybrid 판정에 쓰이므로 기존대로 1.0 에서 자른다. PATH 면적비는
    # 판정에 안 쓰고 진단용이라 자르지 않는다 — 배경과 패널이 겹쳐 1.0 을 넘는 것
    # 자체가 "이건 장식 도형이 깔린 페이지"라는 정보다 (003 p3 = 1.71).
    return min(1.0, img_total / page_area), path_n, path_total / page_area


def triage_page(page: pdfium.PdfPage) -> PageTriage:
    textpage = page.get_textpage()
    text = textpage.get_text_bounded() or ""
    stripped = "".join(ch for ch in text if not ch.isspace())
    char_count = len(stripped)
    korean = sum(1 for c in stripped if "가" <= c <= "힣")
    fffd = stripped.count("�")
    img_ratio, path_n, path_ratio = _object_census(page)

    reasons: list[str] = []
    verdict = "structured"
    if char_count <= SETTINGS.min_readable_chars:
        verdict = "scan_like"
        reasons.append(f"텍스트 {char_count}자 이하 — 텍스트 레이어 불신")
    elif fffd >= SETTINGS.min_fffd_count and fffd / max(char_count, 1) >= SETTINGS.max_fffd_ratio:
        verdict = "scan_like"
        reasons.append(f"U+FFFD {fffd}개({fffd / char_count:.0%}) — 인코딩 깨짐")
    elif char_count >= 50 and _normal_ratio(stripped) <= 0.1:
        verdict = "scan_like"
        reasons.append("정상 문자 비율 10% 이하 — PUA/난독 의심")
    elif img_ratio >= SETTINGS.hybrid_image_area_ratio:
        verdict = "hybrid"
        reasons.append(
            f"텍스트 레이어 정상이지만 이미지 면적비 {img_ratio:.0%} — 이미지 영역 OCR 병행"
        )

    # 경고만 — 판정은 위에서 이미 끝났고 아래 줄은 verdict 를 건드리지 않는다.
    # structured 일 때만 남긴다: OCR 을 건너뛰는 경로에서만 벡터 글자가 유실되기 때문이다
    # (scan_like/hybrid 는 어차피 OCR 이 도니 위험이 없다). 조용한 실패 금지 원칙에서,
    # reasons 가 빈 배열이면 "확인할 게 없다"로 읽히던 자리다 — 003 p3 이 그랬다.
    if verdict == "structured" and path_n:
        reasons.append(
            f"[주의] 벡터 도형 {path_n}개(면적비 {path_ratio:.2f}) — 글자를 벡터로 그렸으면 "
            f"텍스트 레이어에도 이미지 면적비에도 안 잡혀 OCR 생략 시 유실된다 "
            f"(실측 003 p3: 헤더 8낱말). 판정은 유지 — 스윕 회수 결과를 확인할 것"
        )
    return PageTriage(verdict, char_count, korean, fffd, img_ratio, path_n, path_ratio, reasons)


def _normal_ratio(stripped: str) -> float:
    if not stripped:
        return 0.0
    normal = 0
    for c in stripped:
        if "가" <= c <= "힣" or "ㄱ" <= c <= "ㆎ" or c.isascii():
            normal += 1
            continue
        cat = unicodedata.category(c)
        if cat.startswith(("L", "N", "P", "S")) and cat != "Co":  # Co = 사설영역
            normal += 1
    return normal / len(stripped)


_LINE_GAP_MULTIPLIER = 4.0  # 평균 글자 폭의 이 배수 이상 벌어지면 다른 칸(줄)으로 본다
_LINE_GAP_MIN_PX = 6.0      # 위 배수가 너무 작아지는 것을 막는 하한


def _split_by_column_gap(
    group: list[tuple[str, tuple[float, float, float, float]]],
) -> list[list[tuple[str, tuple[float, float, float, float]]]]:
    """세로 겹침만으로 묶인 한 그룹을, 가로로 비정상 벌어진 지점에서 다시 쪼갠다.

    표 칸("가입금액" | "100만원이상" | "조건" | "금리(%p)")은 세로 위치가 같아 위 군집
    에서 한 줄로 합쳐진다. 실측(2026-08-13, `1. 예금성상품(거치식).pdf`): 정상 낱말
    사이 간격은 평균 글자 폭의 최대 2.2배인데, 표 칸 경계 간격은 5.6~19배였다 — 그
    사이 어디에도 정상 문장이 없어 4배를 경계로 쓴다. 이 경계를 못 넘으면 한 줄이
    옆 칸 내용까지 끌고 들어가 영역 배정 때 엉뚱한 영역에 붙는다(p1_r006 실측).
    """
    if len(group) < 2:
        return [group]
    widths = [item[1][2] - item[1][0] for item in group]
    avg_w = sum(widths) / len(widths)
    gap_limit = max(avg_w * _LINE_GAP_MULTIPLIER, _LINE_GAP_MIN_PX)
    segments: list[list[tuple[str, tuple[float, float, float, float]]]] = [[group[0]]]
    # 항목을 재조립하지 않고 **그대로** 넣는다. 2-튜플만 다루던 예전 코드는 `(ch, box)` 로
    # 다시 만들어 3번째 원소(시인성)를 잘라 버렸다. 여기서 필요한 건 [0]=글자·[1]=칸뿐이므로
    # 길이에 관여하지 않는 편이 호출자를 안 묶는다.
    for item in group[1:]:
        box = item[1]
        prev_right = segments[-1][-1][1][2]
        if box[0] - prev_right > gap_limit:
            segments.append([])
        segments[-1].append(item)
    return segments


# ── PDF 자체 구조로 줄을 정하는 재료 ─────────────────────────────────────────
# 왜 이걸 쓰나. 종전에는 글자를 세로 겹침으로 군집해 줄을 만들었다. 그러면 **큰 글자가
# 멀리 있는 작은 글자를 빨아들인다** — 겹침 분모가 작은 쪽이라 24pt 글꼴칸(높이 67px)
# 안에 든 10.4pt 글자는 겹침이 100% 가 된다. 가로로 640px 떨어져 있어도 같은 줄이 됐다.
# 실측(2026-08-21, `1. 예금성상품(적립식).pdf`): `기본금리 연 2.25%` 가 오른쪽 칸
# 예금자보호 문구 **두 줄**을 끌어와 한 줄이 되고, x 정렬 때 글자가 교차해
# `1이인예당금“1은억예원금까자지보”…` 가 됐다(판독 불가). 89개 PDF 전수로 4줄/4파일.
# 같은 원인의 다른 얼굴이 칼럼 넘김이다 — 좌우 2단이 한 줄로 붙어 엉뚱한 영역에 배정됐다.
#
# PDF 는 답을 이미 갖고 있다. 두 가지를 읽는다.
#   ① TEXT 런  — PDF 가 선언한 텍스트 조각. 실측 143개 전부 높이 48.8px 이하로
#                **한 런은 두 줄에 걸치지 않는다.** 런의 `matrix.f` 가 베이스라인이며
#                같은 시각 줄은 1pt 안쪽, 다른 줄은 7pt 이상 떨어진다(같은 파일 실측:
#                예금자보호 1줄 f=135.60 · 2줄 f=124.10).
#   ② 세로 구분선 — 여백에 그려진 얇고 긴 벡터 경로. 위 파일의 좌우 2단 점선이
#                `x=711.5..714.3, y=1236..2018` 로 실제 객체다. 임계값이 아니라
#                파일에 그려진 사실이라 문서마다 흔들리지 않는다.
_DIVIDER_MIN_H_PT = 36.0      # 이보다 짧으면 칸 구분선이 아니라 장식으로 본다(≈3줄)
_DIVIDER_MAX_W_PT = 6.0       # 실측 구분선 폭 0.4~3.0pt. 그보다 두꺼우면 막대/아이콘
_DIVIDER_MIN_RATIO = 6.0      # 세로/가로 비. 가로 괘선을 걸러낸다

# 베이스라인 허용차 = **글자 크기**의 이 비율. 잉크 높이를 쓰면 안 된다 — 마침표는
# 잉크가 1.2pt 뿐이라 허용차가 0 으로 눌리고, 같은 베이스라인인데도 탈락한다(실측:
# `1.0%p` 의 `.` 이 f=327.40 으로 `1`·`0%` 와 같은데 떨어져 나가 `10%p` 가 됐다).
# 글자 크기는 `matrix.a` 로 얻는다 — `FPDFTextObj_GetFontSize` 는 이 문서들에서
# 전부 1.0 을 돌려줘 못 쓴다(ir.SizeBasis 주석과 같은 사정).
# 같은 줄 안의 흔들림은 첨자성 보정에서 온다(실측: `p` 가 본문보다 0.98pt 높다).
_BASELINE_TOL_RATIO = 0.20

# 문장부호·글머리 기호는 광학적으로 가운데 맞춰 그려서 베이스라인이 어긋난다 —
# 실측: `■`(4pt)이 본문(8pt) 베이스라인보다 1.25pt 높다. 이런 조각은 베이스라인으로
# 못 붙이므로, **줄이 세로로 감싸고 가로로 맞붙어 있으면** 그 줄에 넣는다.
#
# 대상을 **글자 하나짜리 런**으로 한정한다. 글자 크기(pt)로 자르면 안 된다 — `●` 는
# 글리프가 작아도 글꼴 크기는 본문과 같아서(실측 `1. 대출성상품.pdf`) 크기 상한에
# 걸리지 않는다. 한 글자로 한정하면 낱말을 옆 칸으로 빨아들일 수가 없다.
# 간격 한계는 **조각 자신의 폭**을 잣대로 쓴다. 실측: 기호와 본문 사이는 0~10px 인데
# 표 칸 사이는 93~120px 로 한 자리 수 배 차이가 난다.
_MARK_GAP_RATIO = 2.0         # 가로 간격 한계 = 조각 폭의 이 배수
_MARK_MIN_GAP_PT = 2.0        # 폭이 아주 좁은 기호(마침표)를 위한 하한
_MARK_CONTAIN_RATIO = 0.8     # 줄의 y 범위가 조각의 이 비율 이상을 덮어야 한다


def _obj_key(obj) -> int:
    """페이지 객체의 동일성 열쇠. 핸들 주소를 쓴다 — 글자→런 대응에만 쓰는 값이다."""
    return ctypes.cast(obj.raw, ctypes.c_void_p).value


def _text_runs(page: pdfium.PdfPage) -> list[dict]:
    """TEXT 객체를 (베이스라인, 좌표, 열쇠) 로 모은다. 못 읽으면 빈 리스트."""
    runs: list[dict] = []
    try:
        objects = list(page.get_objects(max_depth=6))
    except Exception:  # noqa: BLE001 — 구조를 못 읽으면 글자 군집으로 되돌아간다
        return []
    for obj in objects:
        if obj.type != pdfium_c.FPDF_PAGEOBJ_TEXT:
            continue
        try:
            left, bottom, right, top = obj.get_bounds()
            matrix = obj.get_matrix()
        except Exception:  # noqa: BLE001
            continue
        if right - left <= 0 or top - bottom <= 0:
            continue  # 폭·높이 0 인 런이 실제로 있다(빈 문자열 자리)
        runs.append({
            "key": _obj_key(obj),
            "baseline": matrix.f,
            # 실효 글자 크기. 텍스트 행렬의 가로 배율이 곧 pt 크기다(실측: 본문 8.00 ·
            # `가입기간` 12.62 · `■` 4.00). 행렬을 못 읽으면 잉크 높이로 대신한다.
            "size": abs(matrix.a) or (top - bottom),
            "box": (left, bottom, right, top),
        })
    return runs


def _vertical_dividers(page: pdfium.PdfPage, chars) -> list[tuple[float, float, float]]:
    """칸을 가르는 세로 구분선 → [(x중심, y하, y상)]. PDF pt 좌표.

    **여백 조건이 핵심이다.** 얇고 긴 경로라는 모양만 보면 막대그래프·아이콘 조각까지
    걸린다 — 실측(2026-08-21, 89개 PDF): 모양만으로 242개가 잡히고 그중 폭이 13pt 인
    것도 있었다. 진짜 칸 구분선은 **글자가 없는 자리에** 그려져 있으므로, 자기 x 띠
    안에 글자 잉크가 겹치면 구분선으로 안 본다. 이 조건은 문서별 조정이 필요 없다.
    """
    try:
        objects = list(page.get_objects(max_depth=6))
    except Exception:  # noqa: BLE001
        return []
    out: list[tuple[float, float, float]] = []
    for obj in objects:
        if obj.type != pdfium_c.FPDF_PAGEOBJ_PATH:
            continue
        try:
            left, bottom, right, top = obj.get_bounds()
        except Exception:  # noqa: BLE001
            continue
        w, h = right - left, top - bottom
        if h < _DIVIDER_MIN_H_PT or w > _DIVIDER_MAX_W_PT:
            continue
        if h / max(w, 0.01) < _DIVIDER_MIN_RATIO:
            continue
        if any(
            tight[2] > left and tight[0] < right and tight[3] > bottom and tight[1] < top
            for _, tight, _, _, _ in chars
        ):
            continue  # 글자가 올라타 있다 → 여백의 구분선이 아니다
        out.append(((left + right) / 2.0, bottom, top))
    return out


def _crosses_divider(box_a, box_b, dividers) -> bool:
    """두 상자 사이에 구분선이 놓여 있는가. 구분선의 y 범위 안에서만 막는다."""
    lo = min(box_a[2], box_b[2])
    hi = max(box_a[0], box_b[0])
    if lo >= hi:
        return False  # 가로로 겹친다 — 사이에 뭐가 들어갈 자리가 없다
    y_lo = min(box_a[1], box_b[1])
    y_hi = max(box_a[3], box_b[3])
    return any(lo <= x <= hi and bottom < y_hi and top > y_lo for x, bottom, top in dividers)


def _place_orphan_spaces(pending, runs, buckets, dividers) -> None:
    """소속 런을 모르는 공백을 가장 가까운 런에 넣는다. 구분선은 넘지 않는다.

    베이스라인이 겹치는 런을 먼저 보고, 없으면 세로 거리까지 포함해 가장 가까운
    런에 붙인다. 공백도 정본의 일부라 버리지 않는다 — 통째로 지웠다가
    `공무원연금공단에서확인된`처럼 띄어쓰기를 잃은 전례가 있다.
    """
    for entry in pending:
        tight = entry[1]
        center = (tight[1] + tight[3]) / 2.0
        best: int | None = None
        best_key: tuple[int, float] | None = None
        for slot, run in enumerate(runs):
            box = run["box"]
            if _crosses_divider(tight, box, dividers):
                continue
            on_baseline = box[1] <= center <= box[3]
            dx = 0.0 if box[0] <= tight[0] <= box[2] else min(
                abs(tight[0] - box[2]), abs(box[0] - tight[2])
            )
            dy = 0.0 if on_baseline else min(abs(center - box[1]), abs(center - box[3]))
            key = (0 if on_baseline else 1, dx + dy)
            if best_key is None or key < best_key:
                best, best_key = slot, key
        if best is not None:
            buckets[best].append(entry)


def _lines_from_runs(chars, runs, dividers) -> list[list] | None:
    """런의 베이스라인으로 줄을 정하고 글자를 그 줄에 담는다.

    반환은 '줄 = 글자 리스트' 의 리스트. 런에 못 붙은 글자가 하나라도 있으면 None 을
    돌려 호출자가 종전 방식(글자 군집)으로 되돌아가게 한다 — **텍스트를 잃지 않는
    쪽이 먼저다.**
    """
    if not runs:
        return None
    index = {r["key"]: i for i, r in enumerate(runs)}
    buckets: list[list] = [[] for _ in runs]
    pending: list = []
    for entry in chars:
        slot = index.get(entry[4])
        if slot is None:
            # **공백은 기하학으로 붙이고 이 경로를 계속 쓴다.** 예전에는 소속 모르는
            # 글자가 하나라도 있으면 통째로 폴백했는데, pdfium 이 홀로 놓인 공백
            # 글리프의 TEXT 객체를 돌려주지 않는 일이 잦아 **거의 모든 PDF 가
            # 폴백으로 떨어졌다** — 실측(2026-09-20, 43페이지): 42페이지가 폴백,
            # 소속 불명 글자 828개가 **전부 공백**(비공백 0개)이었다.
            # 폴백인 글자 군집에는 구분선 인식이 없어 2단 편집물의 좌우 칸이 한 줄로
            # 붙는다(`1. 예금성상품(적립식)`: `만기후 금리중도해지 및 만기후가입혜택
            # 가입한 모든 고객에게` — 세로 구분선을 넘어 오른쪽 상품까지 삼킴).
            # 공백 1~2%를 위해 나머지 98% 의 구조 정보를 버리던 셈이다.
            if entry[0].isspace():
                pending.append(entry)
                continue
            return None  # 글자가 섞이면 종전대로 폴백한다 — 텍스트를 잃지 않는 쪽이 먼저다
        buckets[slot].append(entry)
    _place_orphan_spaces(pending, runs, buckets, dividers)

    live = [(runs[i], buckets[i]) for i in range(len(runs)) if buckets[i]]
    if not live:
        return None
    # **큰 런부터** 처리한다. 줄의 기준 베이스라인을 본문이 잡아야 하기 때문이다 —
    # 작은 조각이 먼저 그룹을 만들면 그 조각의 어긋난 베이스라인이 줄의 기준이 된다.
    live.sort(key=lambda rb: (-rb[0]["size"], -rb[0]["baseline"], rb[0]["box"][0]))

    groups: list[dict] = []
    for run, items in live:
        box, size = run["box"], run["size"]
        part = {"box": box, "items": items, "baseline": run["baseline"], "size": size}
        for g in groups:
            # **그룹의 첫 런이 아니라 이미 들어간 런들과 각각 비교한다(연쇄).** 첫 런에
            # 기준을 고정하면 아치형(곡선) 제목이 끊긴다 — 실측(`1. 예금성상품(입출식·
            # 적립식 통합).pdf`): `NH비대면전용입출식및적립식` 이 글자마다 베이스라인이
            # 달라(647.81→640.79→645.92, 폭 7pt) 4조각으로 갈렸다. 이웃끼리는 1.6pt
            # 안쪽이라 연쇄로 보면 한 줄로 이어진다.
            if not any(
                abs(run["baseline"] - p["baseline"]) <= max(size, p["size"]) * _BASELINE_TOL_RATIO
                and not _crosses_divider(box, p["box"], dividers)
                for p in g["parts"]
            ):
                continue
            g["parts"].append(part)
            g["box"] = (min(g["box"][0], box[0]), min(g["box"][1], box[1]),
                        max(g["box"][2], box[2]), max(g["box"][3], box[3]))
            break
        else:
            groups.append({"box": box, "parts": [part]})

    _attach_marks(groups, dividers)
    groups.sort(key=lambda g: (-g["box"][3], g["box"][0]))
    return [[it for p in g["parts"] for it in p["items"]] for g in groups]


def _attach_marks(groups: list[dict], dividers) -> None:
    """떠 있는 한 글자 조각(마침표·`■`·`●` 등)을 그것을 감싸는 줄에 넣는다.

    베이스라인만으로는 못 붙는다 — 이런 기호는 광학적으로 가운데 맞춰 그려서
    베이스라인이 본문과 다르다(실측: `■` 이 본문보다 1.25pt 높다). 대신 **줄이 세로로
    감싸고 가로로 맞붙어 있다**는 기하 사실을 쓴다.

    **런 단위로 옮긴다.** 그룹 단위로 하면 같은 베이스라인의 기호 여럿이 한 그룹으로
    묶여 빠져나간다 — 실측(`1. 대출성상품.pdf`): 좌우 칼럼의 `●` 두 개가 497px,
    `■` 두 개가 838px 떨어져 있는데 베이스라인이 같아 한 그룹이었다.

    **한 글자 런 전부를 대상으로 보되, 자기 줄에 붙어 있으면 안 건드린다.** 기호가 다른
    칼럼의 본문과 베이스라인이 같으면 그 그룹에 들어가고, 뒤이은 가로 공백 분리가 그
    기호를 다시 떼어 낸다 — 실측(`17. 대출성상품.pdf`): `●근저당설정비…` 가 `●` + 본문
    으로 갈렸다. 그렇다고 한 글자 런을 무조건 재배치하면 **한 글자씩 그려진 슬로건이
    흩어진다** — 실측(`1. 예금성상품(거치식·적립식 통합).pdf`): `모든순간` 이 `모든` +
    낱자로 깨졌다. 그래서 기준은 하나다: **자기 그룹의 나머지와 맞붙어 있으면 제자리다.**
    """
    singles = [(g, p) for g in groups for p in g["parts"] if len(p["items"]) == 1]
    for src, part in singles:
        box = part["box"]
        height = box[3] - box[1]
        limit = max((box[2] - box[0]) * _MARK_GAP_RATIO, _MARK_MIN_GAP_PT)

        def _abuts(obox, _box=box, _h=height, _lim=limit) -> float | None:
            """세로로 감싸고 가로로 맞붙으면 그 간격, 아니면 None."""
            covered = min(_box[3], obox[3]) - max(_box[1], obox[1])
            if _h > 0 and covered / _h < _MARK_CONTAIN_RATIO:
                return None
            gap = max(obox[0] - _box[2], _box[0] - obox[2], 0.0)
            if gap > _lim or _crosses_divider(_box, obox, dividers):
                return None
            return gap

        rest = [p for p in src["parts"] if p is not part]
        if rest:
            rest_box = (min(p["box"][0] for p in rest), min(p["box"][1] for p in rest),
                        max(p["box"][2] for p in rest), max(p["box"][3] for p in rest))
            if _abuts(rest_box) is not None:
                continue  # 이미 자기 줄에 붙어 있다 — 슬로건 낱자·`1.0%p` 의 `1` 등
        best, best_gap = None, None
        for other in groups:
            if other is src:
                continue
            gap = _abuts(other["box"])
            if gap is None:
                continue
            if best_gap is None or gap < best_gap:
                best, best_gap = other, gap
        if best is None:
            continue
        best["parts"].append(part)
        best["box"] = (min(best["box"][0], box[0]), min(best["box"][1], box[1]),
                       max(best["box"][2], box[2]), max(best["box"][3], box[3]))
        src["parts"] = [p for p in src["parts"] if p is not part]
    groups[:] = [g for g in groups if g["parts"]]


def _char_style(textpage, index: int):
    """글자 하나의 (크기pt, 굵음, 색, 글꼴명, 굵기원값). 못 얻는 항목은 None.

    크기는 여기서 안 채운다 — 호출자가 글꼴칸 높이로 넣는다(`ir.SizeBasis` 주석 참조).
    굵음 판정은 `text_style.is_bold` 에 맡긴다 — 굵기 원값이 pdfium 빌드마다 달라
    글꼴명을 1차 근거로 써야 한다.
    """
    weight = pdfium_c.FPDFText_GetFontWeight(textpage.raw, index)
    weight = weight if weight and weight > 0 else None

    color: str | None = None
    r, g, b, a = (ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint())
    if pdfium_c.FPDFText_GetFillColor(
        textpage.raw, index, ctypes.byref(r), ctypes.byref(g), ctypes.byref(b), ctypes.byref(a)
    ):
        color = rgb_hex(r.value, g.value, b.value)

    font: str | None = None
    buf = ctypes.create_string_buffer(_FONT_NAME_BUF)
    flags = ctypes.c_int()
    used = pdfium_c.FPDFText_GetFontInfo(
        textpage.raw, index, buf, _FONT_NAME_BUF, ctypes.byref(flags)
    )
    if used:
        name = buf.raw[: max(0, used - 1)].decode("utf-8", "replace").strip()
        if name:
            font = strip_subset_prefix(name)

    return None, is_bold(weight, font), color, font, weight


def extract_digital_lines(page: pdfium.PdfPage, px_per_pt: float) -> list[Line]:
    """텍스트 레이어 → 라인. 좌표·시인성은 글자 단위, **줄 소속은 PDF 구조**로 정한다.

    두 단계다.
      1) 줄 소속 — `_lines_from_runs`: TEXT 런의 베이스라인 + 세로 구분선. 임계값이
         아니라 파일에 적힌 사실이라 문서마다 흔들리지 않는다(위 주석 블록 참조).
      2) 줄 안의 글자 순서·좌표·시인성 — 종전 그대로 글자 단위. 한 줄에 두 시각 줄이
         섞이지 않게 된 뒤라 x 정렬이 안전하다.

    구조를 못 읽는 PDF(TEXT 객체가 없거나 글자-런 대응이 끊긴 경우)는 **아래 글자 군집
    으로 되돌아간다** — 조용히 텍스트를 잃지 않는 쪽이 먼저다.

    ── 폴백(글자 군집)에 대하여 ────────────────────────────────────────────
    **줄 묶기는 글꼴칸(loose charbox), 좌표는 잉크칸(tight charbox)을 쓴다.** 잉크칸은
    글자마다 높이가 제각각이라(같은 줄에서 `ㆍ` 5.3px · `임` 25.8px 실측) 겹침 비율이
    글자 모양에 좌우된다. 그래서 키 큰 글자가 윗줄 글자와 임계값을 아슬하게 넘겨
    **윗줄에 흡수되고 형제 글자만 남아 라벨이 쪼개졌다** — 실측(2026-08-14,
    `7. 대출성상품.pdf` p1): `필`(높이 28.7)이 윗줄 `임`(25.8)과 51.6% 겹쳐 흡수되고
    `류`·`요`는 46~49%로 탈락해 `필요서류` → `필서` + `요류` 로 갈렸다. 같은 원인으로
    `대출한도` → `대출한` + `도`, `대출용도` → `대출용` + `도`(6. 대출성상품).

    글꼴칸은 글꼴 메트릭이 정하므로 **같은 줄 글자가 전부 같은 상자**가 된다(위 실측에서
    `필요서류` 넷 다 1916.3~1948.9, `임대차계약서` 전부 1903.6~1933.0) — 글자 모양에
    흔들리지 않는다. 다만 글꼴칸은 실제 획보다 크므로 화면 하이라이트·영역 배정이 쓰는
    bbox 는 종전대로 잉크칸으로 낸다.
    """
    textpage = page.get_textpage()
    _, page_h = page.get_size()
    n = textpage.count_chars()
    # (글자, 잉크칸, 글꼴칸, 시인성, 소속 런 열쇠)
    chars: list[tuple[str, tuple[float, ...], tuple[float, ...], tuple, int | None]] = []
    for i in range(n):
        ch = textpage.get_text_range(i, 1)
        # PDF 텍스트 레이어 안의 일반 공백은 정본의 일부다. 예전에는 ``isspace()`` 를
        # 통째로 건너뛰어 `공무원연금공단에서확인된`처럼 **PDFium 이 이미 읽은**
        # 띄어쓰기까지 우리 쪽에서 지웠다. 줄바꿈(CR/LF)만 제외하고, 공백은 같은 TEXT
        # 런의 글자로 보존한다. PDFium 공간 글리프의 높이는 0에 가까울 수 있으므로 줄
        # 소속은 아래의 TEXT 런 기준 로직이 계속 결정한다.
        if not ch or ch in ("\r", "\n"):
            continue
        try:
            tight = textpage.get_charbox(i)
        except Exception:
            continue
        try:
            loose = textpage.get_charbox(i, loose=True)
        except Exception:
            loose = tight  # 글꼴칸을 못 얻는 글자는 종전(잉크칸) 동작으로 되돌아간다
        if loose[3] - loose[1] <= 0:
            loose = tight
        try:
            _, bold, color, font, weight = _char_style(textpage, i)
        except Exception:
            bold = color = font = weight = None  # 스타일 실패가 텍스트 추출을 멈추지 않는다
        # 글자가 속한 TEXT 런. pdfium 이 직접 알려주므로 좌표로 추측하지 않는다.
        try:
            owner = pdfium_c.FPDFText_GetTextObject(textpage.raw, i)
            owner_key = ctypes.cast(owner, ctypes.c_void_p).value if owner else None
        except Exception:  # noqa: BLE001
            owner_key = None
        # 크기는 글꼴칸 높이(pt). 선언값(FPDFText_GetFontSize)은 못 쓴다 — ir.SizeBasis 주석.
        size = round(loose[3] - loose[1], 1)
        chars.append((ch, tight, loose, (size, bold, color, font, weight), owner_key))
    if not chars:
        return []

    # ① 우선 PDF 구조로 줄을 정한다. 실패하면 ②로 되돌아간다.
    lines_raw = _lines_from_runs(chars, _text_runs(page), _vertical_dividers(page, chars))
    if lines_raw is None:
        # ② 폴백 — 글꼴칸 top 기준 정렬 후 수직 겹침 50% 이상이면 같은 라인으로 군집
        chars.sort(key=lambda c: (-c[2][3], c[1][0]))
        lines_raw = []
        for entry in chars:
            box = entry[2]
            placed = False
            for group in lines_raw:
                gbox = group[0][2]
                overlap = min(box[3], gbox[3]) - max(box[1], gbox[1])
                height = min(box[3] - box[1], gbox[3] - gbox[1])
                if height > 0 and overlap / height >= 0.5:
                    group.append(entry)
                    placed = True
                    break
            if not placed:
                lines_raw.append([entry])

    lines: list[Line] = []
    for group in lines_raw:
        group.sort(key=lambda c: c[1][0])
        # 가로 공백 분리와 최종 좌표는 잉크칸 기준 (글꼴칸은 좌우로도 넉넉해 칸 경계가 뭉갠다)
        # 3번째 원소로 시인성을 함께 실어 보낸다 — _split_by_column_gap 은 [0]·[1]만 본다.
        tight_group = [(ch, tight, atom) for ch, tight, _, atom, _ in group]
        for segment in _split_by_column_gap(tight_group):
            # 줄 안 공백은 보존하되, PDF TEXT 객체가 줄 첫머리에 넣는 장식용 공백은
            # 내용이 아니므로 경계에서만 제거한다.
            text = "".join(item[0] for item in segment).strip()
            if not text.strip():
                continue
            left = min(item[1][0] for item in segment)
            bottom = min(item[1][1] for item in segment)
            right = max(item[1][2] for item in segment)
            top = max(item[1][3] for item in segment)
            # PDF pt(원점 좌하단) → 렌더 픽셀(원점 좌상단)
            bbox = [
                int(left * px_per_pt),
                int((page_h - top) * px_per_pt),
                int(right * px_per_pt),
                int((page_h - bottom) * px_per_pt),
            ]
            style = fold(
                # 공백은 텍스트에는 남기되, 대표 글자 스타일을 정할 때는 제외한다.
                # 공간 글리프는 0-height box를 갖는 PDF가 있어 스타일 대표값을 흐릴 수 있다.
                (item[2] for item in segment if len(item) > 2 and not item[0].isspace()),
                basis="fontbox",
                source="pdf_char",
            )
            lines.append(
                Line(text=text, bbox=bbox, confidence=None, source="digital", style=style)
            )
    from ..ocr.reading_order import sort_reading_order

    return sort_reading_order(lines)
