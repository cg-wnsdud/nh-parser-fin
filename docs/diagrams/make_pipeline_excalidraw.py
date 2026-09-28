# -*- coding: utf-8 -*-
"""파이프라인 흐름도를 Obsidian Excalidraw 파일로 생성한다.

    uv run python docs/diagrams/make_pipeline_excalidraw.py

생성물
    docs/diagrams/nh-parser-fin-pipeline.excalidraw.md   Obsidian 플러그인용
    docs/diagrams/nh-parser-fin-pipeline.excalidraw      순수 JSON (excalidraw.com)

Obsidian 사용법
    1. 위 두 파일 중 `.excalidraw.md` 를 볼트 안으로 복사한다.
    2. Excalidraw 플러그인이 설치돼 있으면 파일을 여는 순간 그림으로 열린다.
       마크다운으로 열리면 More options ... → Open as Excalidraw Drawing.

왜 스크립트로 그리나
    흐름도가 코드보다 먼저 낡는다. 단계가 바뀌면 아래 STAGES/NOTES 의 문자열만
    고치고 다시 실행하면 좌표·화살표가 전부 다시 계산된다. 손으로 옮긴 박스를
    다시 정렬할 일이 없다.

내용의 근거
    docs/HANDOVER.md · docs/PIPELINE.md · docs/HWP_INPUT.md 와
    run.py / nh_parser_fin/parse/pipeline.py::run_full_pipeline() 의 실제 호출 순서.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent
NAME = "nh-parser-fin-pipeline"

# ──────────────────────────────────────────────────────────────────────────
# 팔레트 — (테두리, 채움). 범례와 1:1 로 맞춘다.
# ──────────────────────────────────────────────────────────────────────────
STYLES = {
    "code": ("#1e1e1e", "#ffffff"),   # 손수 코드 (모델 호출 없음)
    "data": ("#343a40", "#f1f3f5"),   # 입력·중간 데이터
    "ocr":  ("#1971c2", "#a5d8ff"),   # PaddleX PP-StructureV3
    "vlm":  ("#9c36b5", "#eebefa"),   # Gemma VLM
    "hwp":  ("#e8590c", "#ffec99"),   # HWP 전용 (이번 브랜치 작업)
    "out":  ("#2f9e44", "#b2f2bb"),   # 산출물 파일
    "warn": ("#c92a2a", "#ffc9c9"),   # 실패·예외 정책
    "soft": ("#9c36b5", "#f8f0fc"),   # 보조 설명 (VLM 계열)
}

TITLE_SIZE = 14
BODY_SIZE = 11
LINE_H = 1.25

_rng = random.Random(20260923)   # 재실행해도 같은 파일이 나오게 고정한다.
_elements: list[dict] = []
_seq = 0


def _uid(kind: str) -> str:
    global _seq
    _seq += 1
    return f"{kind}{_seq:04d}{_rng.randrange(16**6):06x}"


def _base(kind: str, x: float, y: float, w: float, h: float) -> dict:
    return {
        "id": _uid(kind[:2]),
        "type": kind,
        "x": round(x, 2), "y": round(y, 2),
        "width": round(w, 2), "height": round(h, 2),
        "angle": 0,
        "strokeColor": "#1e1e1e",
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": 2,
        "strokeStyle": "solid",
        "roughness": 1,
        "opacity": 100,
        "groupIds": [],
        "frameId": None,
        "roundness": {"type": 3},
        "seed": _rng.randrange(2**31),
        "version": 1,
        "versionNonce": _rng.randrange(2**31),
        "isDeleted": False,
        "boundElements": None,
        "updated": 1758585600000,
        "link": None,
        "locked": False,
    }


def rect(x, y, w, h, style="code", dashed=False, stroke_width=2, radius=True) -> dict:
    stroke, fill = STYLES[style]
    el = _base("rectangle", x, y, w, h)
    el.update(
        strokeColor=stroke,
        backgroundColor=fill,
        strokeStyle="dashed" if dashed else "solid",
        strokeWidth=stroke_width,
        roundness={"type": 3} if radius else None,
    )
    _elements.append(el)
    return el


def _measure(lines: list[str], size: int) -> tuple[float, float]:
    """한글은 전각, 나머지는 반각으로 잡은 대략치. 선택 박스 크기에만 쓰인다."""
    widest = 0.0
    for line in lines:
        w = sum(size * (1.0 if ord(ch) > 0x2000 else 0.56) for ch in line)
        widest = max(widest, w)
    return widest, len(lines) * size * LINE_H


def text(x, y, body: str, size=BODY_SIZE, color="#1e1e1e", align="left") -> dict:
    lines = body.split("\n")
    w, h = _measure(lines, size)
    el = _base("text", x, y, w, h)
    el.update(
        strokeColor=color,
        fontSize=size,
        fontFamily=2,          # Helvetica 계열 — 한글 폴백이 가장 안정적이다.
        text=body,
        originalText=body,
        textAlign=align,
        verticalAlign="top",
        containerId=None,
        autoResize=True,
        lineHeight=LINE_H,
        roundness=None,
    )
    _elements.append(el)
    return el


def card(x, y, w, h, title: str, body: str, style="code", dashed=False) -> dict:
    stroke, _ = STYLES[style]
    box = rect(x, y, w, h, style=style, dashed=dashed)
    text(x + 14, y + 12, title, size=TITLE_SIZE, color=stroke)
    if body:
        text(x + 14, y + 12 + TITLE_SIZE * LINE_H + 8, body, size=BODY_SIZE, color="#1e1e1e")
    return box


def arrow(points: list[tuple[float, float]], color="#1e1e1e", dashed=False,
          stroke_width=2, head=True) -> dict:
    x0, y0 = points[0]
    rel = [[round(px - x0, 2), round(py - y0, 2)] for px, py in points]
    xs = [p[0] for p in rel]
    ys = [p[1] for p in rel]
    el = _base("arrow", x0, y0, max(xs) - min(xs), max(ys) - min(ys))
    el.update(
        strokeColor=color,
        backgroundColor="transparent",
        strokeStyle="dashed" if dashed else "solid",
        strokeWidth=stroke_width,
        points=rel,
        lastCommittedPoint=None,
        startBinding=None,
        endBinding=None,
        startArrowhead=None,
        endArrowhead="arrow" if head else None,
        elbowed=False,
        roundness={"type": 2},
    )
    _elements.append(el)
    return el


# ──────────────────────────────────────────────────────────────────────────
# 레이아웃 상수
# ──────────────────────────────────────────────────────────────────────────
CW, PITCH = 340, 380            # 카드 폭 / 카드 간격
R1_Y, R1_H = 290, 160           # 1행: 입력 → Region 조립
HELP_Y, HELP_H = 520, 250       # 1행 보조 설명
HWP_Y, HWP_H = 830, 330         # HWP 전용 밴드
R2_Y, R2_H = 1260, 230          # 2행: VLM 의미 판정 → P1/P3
NOTE_Y, NOTE_H = 1560, 300      # 2행 하단 주석·산출물

r1x = [60 + PITCH * i for i in range(7)]
r2x = [60 + PITCH * i for i in range(9)]


# ──────────────────────────────────────────────────────────────────────────
# 머리말 · 범례 · 설계 원칙
# ──────────────────────────────────────────────────────────────────────────
text(60, 40, "NH 광고심의 파싱 파이프라인", size=34, color="#1e1e1e")
text(60, 92, "run.py 기준 실제 실행 경로 · 기반 커밋 663899e · HWP 경로는 feat/hwp-input-render (main 미병합) · 2026-09-23",
     size=15, color="#495057")

LEGEND = [
    ("code", "손수 코드 (모델 호출 없음)"),
    ("ocr",  "PaddleX PP-StructureV3"),
    ("vlm",  "Gemma VLM"),
    ("hwp",  "HWP 전용 (이번 브랜치 작업)"),
    ("out",  "산출물 파일"),
    ("data", "입력·중간 데이터"),
    ("warn", "실패·예외 정책"),
]
_lx = 60
for _style, _label in LEGEND:
    rect(_lx, 136, 16, 16, style=_style, radius=False)
    text(_lx + 24, 137, _label, size=12, color="#343a40")
    _lx += 24 + _measure([_label], 12)[0] + 26

text(60, 166, "실선 = 주 흐름   ·   주황 점선 = HWP 입력이 본류에 합류하는 지점   ·   회색 점선 = 보조 설명 연결",
     size=12, color="#868e96")

card(
    2440, 40, 1000, 200,
    "설계 원칙 — 좌표와 의미의 책임 분리",
    "· bbox 는 PaddleX 레이아웃 또는 OCR/PDF 줄에서만 만든다.\n"
    "· VLM 은 좌표를 만들지 않는다. 주어진 ID 를 고르고 의미(상품 소유권 · 표 여부 ·\n"
    "  구분값)만 판정한다.\n"
    "· VLM 이 화면에서 봤지만 좌표가 없는 누락 문구는 P1 의 coarse_missing_candidates\n"
    "  에만 남고 P3 Region 으로 승격하지 않는다.\n"
    "→ 심의 결과의 region_ids 를 원본 화면 픽셀에 정확히 하이라이트하기 위한 조건이다.",
    style="warn", dashed=True,
)


# ──────────────────────────────────────────────────────────────────────────
# 1행 — 입력에서 Region 조립까지
# ──────────────────────────────────────────────────────────────────────────
STAGE_ROW1 = [
    ("① 광고 파일 입력", "data",
     "run.py::iter_inputs()\n"
     "· PDF / PNG·JPG / HWP·HWPX\n"
     "· 폴더는 바로 아래 파일만 (재귀 없음)\n"
     "· --exclude 로 파일명 부분일치 제외\n"
     "· samples/ : PDF 9 · PNG 4 · HWP 2"),
    ("② 페이지 정규화", "code",
     "ingest/loader.py::load_pages()\n"
     "· 입력 종류별로 LabPage 한 장씩 생성\n"
     "· PDF 200 DPI, scan_like·hybrid 는\n"
     "  내장 래스터 기준 native DPI\n"
     "· --sizing asis 기본, 확대는 하지 않음\n"
     "→ 이후 모든 bbox 의 기준 좌표계"),
    ("③ 타일 분할 판정", "code",
     "ocr/tiling.py::plan() · ocr/bands.py\n"
     "· 종횡비 ≤ 2.0 이면 통짜로 전송\n"
     "· 넘으면 긴 축으로 1600px 목표 분할\n"
     "· 절단선은 글자 획 밀도가 낮은 곳으로\n"
     "· 타일 간 15% (80~200px) 오버랩"),
    ("④ PaddleX PP-StructureV3", "ocr",
     "ocr/paddlex.py::call()\n"
     "· /layout-parsing 에 base64, fileType=1\n"
     "· 추론 옵션은 서버 파이프라인 YAML 소유\n"
     "· 응답 3종을 모두 사용한다\n"
     "· 재시도 래퍼 없음 → 실패 시 실행 중단"),
    ("⑤ 좌표 복원 · 중복 제거", "code",
     "tiling.shift() / tiling.dedupe()\n"
     "adapters.dedupe_ocr_lines()\n"
     "· 타일 오프셋을 더해 페이지 좌표로 복원\n"
     "· IoU 0.5 / 작은 박스 포함비 0.7 기준\n"
     "· 점수가 높은 판독을 남긴다\n"
     "· 버린 판독은 tile_alternates 에 보존"),
    ("⑥ Region 조립", "code",
     "parse/adapters.py::build_page_evidence()\n"
     "· parsing_res_list 로 초기 Region 생성\n"
     "· 줄은 Region 하나에만 귀속 (겹침 50%↑)\n"
     "· canonical line 에 pN_lNNNN 부여\n"
     "· 정본 + text_candidates 를 함께 기록\n"
     "· line_id 무결성 깨지면 ValueError"),
    ("⑦ 미배정 줄 복구 후보", "code",
     "parse/recovery.py\n"
     "· 어느 Region 에도 못 들어간 줄을 보존\n"
     "· 가까운 줄끼리 pN_xNNN 후보로 묶는다\n"
     "· bbox 는 원래 줄 bbox 의 합집합만\n"
     "· 의미 판단은 여기서 하지 않는다\n"
     "· 원본은 raw_unassigned_lines 에 복사"),
]
for i, (t, s, b) in enumerate(STAGE_ROW1):
    card(r1x[i], R1_Y, CW, R1_H, t, b, style=s)
    if i:
        arrow([(r1x[i] - 40, R1_Y + R1_H / 2), (r1x[i] - 6, R1_Y + R1_H / 2)])

# 1행 산출물
card(
    2740, R1_Y, CW, 420,
    "중간 산출물 — ①~⑦",
    "outputs/<run-name>/\n"
    "  manifest.json    실행 프로필 · 시간\n"
    "  documents.json   Region 조립 전체\n"
    "  raw/    타일별 PaddleX 원응답\n"
    "  boxes/  페이지 좌표로 복원한 박스\n"
    "  pages/  페이지별 canonical 증거\n"
    "  label-studio.json\n"
    "  labeling-config.xml\n"
    ".media/  렌더한 페이지 PNG\n"
    "         (여러 실행이 공유)\n\n"
    "replay.py 가 boxes/ 만 읽어\n"
    "PaddleX 재호출 없이 조립 로직을\n"
    "다시 돌린다.\n\n"
    "report.py 는 P3 와 페이지 이미지를\n"
    "묶어 HTML 리포트를 만든다.",
    style="out",
)
arrow([(r1x[6] + CW, R1_Y + R1_H / 2), (2740 - 6, R1_Y + R1_H / 2)])


# ──────────────────────────────────────────────────────────────────────────
# 1행 보조 설명 — 각 단계 바로 아래
# ──────────────────────────────────────────────────────────────────────────
HELPERS = [
    (1, "② 보조 · PDF triage", "data",
     "ingest/triage.py::triage_page()\n"
     "· structured  텍스트 레이어를 신뢰\n"
     "· scan_like   읽을 글자 20자 미만 또는\n"
     "              U+FFFD 비율 0.3 초과\n"
     "· hybrid      이미지 면적비 0.5 초과 또는\n"
     "              ink_coverage 0.3 미만\n\n"
     "판정은 렌더 DPI 보조 신호와 origin 기록용\n"
     "이다. triage 결과와 무관하게 모든 페이지가\n"
     "PaddleX 를 거친다. 디지털 텍스트는 PaddleX\n"
     "를 대체하는 별도 Region 이 아니라 같은\n"
     "좌표계의 더 정확한 텍스트 후보다."),
    (2, "③ 보조 · 왜 픽셀이 아니라 종횡비인가", "code",
     "레이아웃 모델이 입력을 800x800 정사각으로\n"
     "종횡비 무시하고 눌러 넣는다\n"
     "(PP-DocLayout_plus-L keep_ratio: false).\n\n"
     "→ 픽셀 수가 아니라 타일의 '모양'이\n"
     "  실효 해상도를 정한다. 세로로 긴 페이지를\n"
     "  통짜로 보내면 작은 글자가 소실된다.\n\n"
     "PARSER_V2_ASPECT_LIMIT / _TILE_SPAN 으로\n"
     "실행 단위로 조정한다."),
    (3, "④ 보조 · 응답 3종의 쓰임", "ocr",
     "layout_det_res\n"
     "  원시 bbox · label · score.\n"
     "  진단 근거로만 보존한다.\n"
     "parsing_res_list\n"
     "  Region 초기 bbox · 본문 ·\n"
     "  부분 읽기 순서. 현재 Region 생성 기준.\n"
     "overall_ocr_res\n"
     "  줄 텍스트 · 인식 점수 · 줄 bbox.\n\n"
     "요청 본문은 {\"fileType\": 1} 뿐이다.\n"
     "threshold · merge 모드 · 모듈 on/off 는\n"
     "서버 한 곳에서만 관리한다."),
    (4, "⑤ 보조 · 중복 제거 기준", "code",
     "· 레이아웃 영역: 같은 label + 높은 x/y 겹침\n"
     "  → 합집합 bbox 로 병합\n"
     "· OCR 줄: 문자열 일치가 아니라\n"
     "  IoU 또는 작은 박스 기준 포함비\n"
     "· 같은 타일 안의 중첩 박스는 부모·자식\n"
     "  구조일 수 있으므로 제거하지 않는다\n\n"
     "타일 경계에서 잘린 조각이 두 번 세어지는\n"
     "것을 막는 단계다."),
    (5, "⑥ 보조 · 줄 통합과 정본 텍스트", "code",
     "1  디지털 PDF 줄을 먼저 넣는다\n"
     "2  디지털 bbox 가 50% 이상 덮은 OCR 줄 제외\n"
     "3  나머지 OCR 줄 보충 → 읽기 순서 정렬\n\n"
     "정본 우선순위\n"
     "  디지털 줄 > 줄 조립본 > block_content\n"
     "  > 빈 문자열\n\n"
     "block_content 와 줄 조립본의 유사도\n"
     "  0.8 ↑        sources_agree\n"
     "  0.5 ~ 0.8    minor_difference\n"
     "  0.5 ↓        conflict_pending_vlm\n"
     "선택되지 않은 후보도 모두 남긴다."),
    (6, "⑦ 보조 · 복구 후보 정책", "code",
     "· 상품 소유권을 알기 전에 같은 높이의 줄을\n"
     "  표로 합치지 않는다. 좌우에 다른 상품이\n"
     "  있는 페이지에서 두 표가 하나로 붙는다.\n"
     "· 세로 팽창을 작게 줘 서로 다른 항목 행이\n"
     "  한 덩어리로 뭉치는 것을 막는다.\n"
     "· 여기서는 아무것도 버리지 않는다.\n"
     "  채택 여부는 ⑨ 의 VLM 이 판정한다."),
]
for idx, t, s, b in HELPERS:
    card(r1x[idx], HELP_Y, CW, HELP_H, t, b, style=s)
    arrow([(r1x[idx] + CW / 2, R1_Y + R1_H), (r1x[idx] + CW / 2, HELP_Y - 6)],
          color="#adb5bd", dashed=True, stroke_width=1, head=False)


# ──────────────────────────────────────────────────────────────────────────
# HWP / HWPX 밴드 — 이번 브랜치 작업
# ──────────────────────────────────────────────────────────────────────────
rect(60, HWP_Y, 2160, HWP_H, style="hwp", dashed=True, stroke_width=1.5)
_elements[-1]["backgroundColor"] = "#fff9db"
text(80, HWP_Y + 14,
     "HWP / HWPX 입력 경로 — feat/hwp-input-render (main 미병합). ②의 한 갈래이며, 변환 PDF 를 만든 뒤 ③ 이후 본류에 합류한다.",
     size=16, color="#e8590c")

HWP_CARDS = [
    (100, "HWP-1  문서 구조 파싱", "hwp",
     "ingest/hwp_structure.py\n"
     "  ::parse_hwp_structure()\n"
     "· document-processor 우선, Kordoc 폴백\n"
     "· 문단 · 표 · 병합 셀 · 중첩 표를 추출\n"
     "· 문자열과 문서 순서는 정확하지만\n"
     "  화면 좌표가 전혀 없다\n"
     "· Kordoc 폴백은 고정 버전 실행 권장"),
    (520, "HWP-2  로컬 PDF 렌더", "hwp",
     "ingest/hwp_render.py::render_hwp_to_pdf()\n"
     "· 모든 OS: HTML/Chromium → LibreOffice\n"
     "· 설치형 한컴오피스 호출 없음\n"
     "· HTML DOM 행은 구조 bbox 근거로 사용\n"
     "· 모든 변환은 로컬, Hwp SDK 호출 없음\n"
     "· HWP_RENDER_DIR 지정 시 중간 PDF 보존"),
    (940, "HWP-3  실제 렌더 페이지로 재분배", "hwp",
     "hwp_structure.repartition_by_rendered_text()\n"
     "· 구조 파서의 논리 pageNumber 는 자동\n"
     "  쪽나눔을 반영하지 못한다\n"
     "· 변환 PDF 의 페이지별 텍스트층과 대조해\n"
     "  구조 노드를 실제 쪽으로 다시 분배\n"
     "· 009 실측: 구조가 전부 1쪽이라 했지만\n"
     "  심의필 · 수신거부 문단을 2쪽으로 이동\n"
     "· 결과는 page.hwp_structure 로 동행"),
    (1360, "HWP-4  화면 Region 생성", "data",
     "· 변환 PDF → 페이지 이미지 · 디지털 bbox\n"
     "  · triage. HTML 행 충분 → structured_fast\n"
     "  또는 hybrid, 나머지는 Paddle 시각 경로\n"
     "· 최종 bbox 는 사용자가 보는 페이지 좌표계\n"
     "· 구조 텍스트는 HWP-5 에서 시각 Region 과 정렬\n\n"
     "역할 분담\n"
     "  HWP 구조 = 내용 정본\n"
     "  변환 PDF · OCR = 좌표와 시각 전용 문구\n"
     "  (배경 이미지 · 로고 문구는 구조에 없어도\n"
     "   OCR/VLM Region 으로 회수된다)"),
]
for hx, t, s, b in HWP_CARDS:
    card(hx, HWP_Y + 60, 380, 230, t, b, style=s)
for hx in (520, 940, 1360):
    arrow([(hx - 40, HWP_Y + 60 + 115), (hx - 6, HWP_Y + 60 + 115)], color="#e8590c")

card(
    1780, HWP_Y + 60, 380, 230,
    "HWP 실패 정책 · 남은 제약",
    "· 구조 파서 전체 또는 렌더러 전체가\n"
    "  실패하면 그 입력을 중단한다.\n"
    "  예전 내장 이미지 경로로 폴백하지 않는다.\n"
    "· 예전 ingest/hwp.py · assets.py 는 제거.\n"
    "· boxes/*.json 에 구조 원본이 없어\n"
    "  replay.py 만으로 HWP-5 까지 재현할 수 없다.\n"
    "· Spark/Linux에서는 Chromium 또는\n"
    "  LibreOffice가 설치되어야 한다.",
    style="warn",
)

# ② → HWP 밴드 진입
arrow([(r1x[1] + 40, R1_Y + R1_H), (r1x[1] + 40, 490), (230, 490), (230, HWP_Y - 6)],
      color="#e8590c", dashed=True)
text(250, 462, "HWP · HWPX 이면", size=12, color="#e8590c")

# HWP-4 → ③ 합류
arrow([(1560, HWP_Y + 60), (1560, 490), (900, 490), (900, R1_Y + R1_H + 6)],
      color="#e8590c", dashed=True)
text(1130, 462, "변환 PDF 페이지 = 이후 일반 PDF 와 같은 경로", size=12, color="#e8590c")


# ──────────────────────────────────────────────────────────────────────────
# 2행 — VLM 의미 판정과 계약 생성
# ──────────────────────────────────────────────────────────────────────────
STAGE_ROW2 = [
    ("⑧ VLM 문서 분류", "vlm",
     "vlm/client.py::classify()\n"
     "· 문서 첫 페이지 1장, 문서당 1회\n"
     "· product_group / ad_type /\n"
     "  product_name_shown\n"
     "· 파일명 prior 를 쓰되 이미지에 명확한\n"
     "  반대 근거가 있으면 VLM 판단이 우선\n"
     "· category_source 로 합의 / 역전 /\n"
     "  실패 폴백을 구분해 남긴다\n\n"
     "실패 → 파일명 prior 로 폴백하고 계속"),
    ("⑨ 페이지 상품 소유권", "vlm",
     "parse/semantic.py::analyze_page_context()\n"
     "· ID 오버레이 + 페이지 전체 문맥을 한 번에\n"
     "· Region → product_1~4 / page_common /\n"
     "  unknown\n"
     "· 복구 후보 action 5종과 표 후보\n"
     "  table_areas 를 같은 호출에서 받는다\n"
     "· 긴 페이지는 2~4개 의미 밴드로 분할.\n"
     "  ID 는 중심 기준 한 밴드에만, crop 만 겹침\n"
     "· 어떤 action 이어도 텍스트·bbox 를 지우지\n"
     "  않는다. decorative 는 vlm_excluded 표시만\n"
     "· 확신도 0.7 미만 또는 unknown → needs_review\n\n"
     "실패 → 실행 중단"),
    ("⑩ HWP 원본 셀 보존", "hwp",
     "parse/pipeline.py::_place_tables()\n"
     "· document-processor/HTML 이 제공한 원본 셀만\n"
     "  P1 의 table 에 보존한다\n"
     "· PDF 는 PDFium 텍스트와 Paddle 시각 경로\n"
     "· 시각적 셀 행렬은 추정하지 않는다"),
    ("HWP-5  HWP 구조 정렬", "hwp",
     "parse/hwp_alignment.py::align_hwp_structure()\n"
     "HWP 페이지에서만 · 원본 셀 보존(⑩) 뒤, Reader(⑪) 앞\n"
     "· 좌표 없는 Kordoc 노드 ↔ bbox 있는 Region\n"
     "  정규화 문자열 · 문서 순서 · 포함 관계로 대조\n"
     "· 앞뒤 문단이 같은 Region 이면 순서로 보완\n"
     "· 충분히 일치할 때만 text_source=hwp_structure\n"
     "· 한 셀이 여러 Region 을 가로지르면 덮어쓰지\n"
     "  않고 hwp_structure_corroboration 근거로만\n"
     "· 17x7 조판표 · 1열 이미지 표 → layout_container\n"
     "  (P3 표로 승격하지 않는다)\n"
     "· 하나도 안 맞으면 hwp_structure_page_unmatched"),
    ("⑪ Region Reader · 표 검증", "vlm",
     "parse/reading.py\n"
     "· Region 을 crop 으로 독립 전사한다\n"
     "  (Reader 에게 OCR 텍스트를 보여주지 않는다)\n"
     "· crop: 12px 여백 + 이웃을 흰색 마스킹 +\n"
     "  짧은 변 320px 까지 최대 4배 확대\n"
     "· 공백 제거 후 일치도 0.95 미만이면 Judge 가\n"
     "  같은 crop 과 두 후보를 보고 정본을 고른다\n"
     "· PDF 디지털/OCR/VLM 후보를 Judge 가 대조\n"
     "· HWP PUA 글리프는 나머지 문맥이 구조와\n"
     "  맞을 때만 vlm_structure_verified 로 정규화\n"
     "· 그 뒤 visual_tables.py 가 Paddle/VLM 표\n"
     "  후보를 페이지+확대 이미지로 확인\n"
     "· 기존 ID 만 병합, 표 전체를 다시 Judge 판독\n"
     "· P3 kind=table/평문, text_source=vlm\n"
     "· PARSER_V2_READING_SCOPE  all / targeted / off"),
    ("⑫ 상품별 심의 템플릿", "vlm",
     "parse/templates.py · review/resolution.py\n"
     "templates/ad_templates.json (19종)\n"
     "· 문서당 하나가 아니라 상품별로 결정한다\n"
     "· 상품군 · 상품명 노출 · 본문 키워드로\n"
     "  결정론 규칙을 먼저 적용\n"
     "· VLM 이 보고한 상품명이 실제 해당 상품\n"
     "  Region 텍스트에 있는지 상호 검증\n"
     "· 후보가 남으면 VLM 이 하나를 선택\n"
     "· page_common 은 단일 상품이면 그 템플릿,\n"
     "  복수 상품이면 19종 교집합 라벨만 허용\n\n"
     "실패 → unresolved · template_unresolved"),
    ("⑬ 상품별 복수 구분값 라벨링", "vlm",
     "parse/semantic.py::analyze_product_labels()\n"
     "· product_id 별로 나눠 호출한다.\n"
     "  다른 템플릿의 라벨이 섞이지 않게.\n"
     "· Region 15개씩 청크. 전체 오버레이와\n"
     "  확대 contact sheet 를 함께 준다\n"
     "· 검증: 허용 라벨인가 / 근거 quote 가 실제\n"
     "  Region 원문에 있는가 / 모순 응답인가\n"
     "· 줄 머리의 '가입대상:' 같은 표제어는\n"
     "  결정론 규칙으로 먼저 붙인다\n"
     "· 한 Region 에 라벨 여러 개를 붙일 수 있다\n\n"
     "실패 → 실행 중단"),
    ("⑭ 읽기 순서 · region_id 정규화", "code",
     "pipeline._assign_reading_order() · parse/ids.py\n"
     "· PaddleX 엔진 순서를 유지한다.\n"
     "  단순 y/x 정렬은 2단 문서의 좌우를 섞는다\n"
     "· attach_context 로 붙은 복구 Region 만\n"
     "  대상 Region 바로 뒤로 옮긴다\n"
     "· 모든 판단이 끝난 뒤 pN_r001 부터 재부여\n"
     "· parent_id / child_ids / related_region_id /\n"
     "  채택된 표 Region ID 도 함께 갱신\n"
     "· 원래 ID 는 source_region_id 와\n"
     "  페이지의 region_id_map 에 남는다"),
    ("⑮ P1 / P3 내보내기", "code",
     "parse/export.py\n"
     "P1  nh-ad-parse-evidence-v4\n"
     "  재현 · 원인 분석용 근거 원장\n\n"
     "P3  nh-ad-region-review-input-v9\n"
     "  region_id · product_id · bbox ·\n"
     "  selected_text · labels · kind ·\n"
     "  needs_review · text_source\n\n"
     "· text_source 는 hwp / digital / ocr / vlm\n"
     "  네 값으로 축약한다\n"
     "· 표는 kind=table + selected_text 평문\n"
     "· 원본 셀과 검증 근거는 P1 에만"),
]
HWP_STEP = 3   # HWP-5 는 HWP 페이지에서만 도는 조건부 단계 — 들고 나는 화살표를 구분한다.
for i, (t, s, b) in enumerate(STAGE_ROW2):
    card(r2x[i], R2_Y, CW, R2_H, t, b, style=s)
    if i:
        conditional = i in (HWP_STEP, HWP_STEP + 1)
        arrow([(r2x[i] - 40, R2_Y + R2_H / 2), (r2x[i] - 6, R2_Y + R2_H / 2)],
              color="#e8590c" if conditional else "#1e1e1e", dashed=conditional)
# 들고 나는 화살표를 주황 점선으로 둔 것이 곧 "HWP 입력일 때만 돈다"는 표시다.
# 카드 사이 40px 에 글자를 더 넣으면 이웃 카드를 침범하므로, 조건은 카드 본문에만 적는다.

# ⑦ → ⑧ 줄바꿈 연결
arrow([(r1x[6] + 20, R1_Y + R1_H), (r1x[6] + 20, 490), (2320, 490),
       (2320, 1210), (230, 1210), (230, R2_Y - 6)], color="#1971c2")
text(1180, 1180, "기본 실행에서 이 단계가 이어지고 P1/P3를 생성한다.",
     size=13, color="#1971c2")


# ──────────────────────────────────────────────────────────────────────────
# 하단 — 실패 정책 · 품질 경고 · 호출량 · 산출물
# ──────────────────────────────────────────────────────────────────────────
card(
    60, NOTE_Y, 700, NOTE_H,
    "단계별 실패 정책 — vlm/client.py::chat_json()",
    "공통: OpenAI 호환 chat/completions · temperature 0 · strict JSON Schema ·\n"
    "기본 1회 + 재시도 2회(2초 → 4초 대기) · 잘린 JSON 으로 보이면 다음 시도의\n"
    "max_tokens 를 최대 16,000 까지 올린다.\n\n"
    "재시도 후에도 실패했을 때\n"
    "  PaddleX 호출        재시도 래퍼 없음 → 실행 중단\n"
    "  ⑧ 문서 분류         파일명 prior 로 폴백하고 계속\n"
    "  ⑨ 페이지 소유권     실행 중단\n"
    "  ⑩ HWP 원본 셀      구조에 있을 때만 보존\n"
    "  ⑪ Reader / 표 검증 실패 해당 Region 에 경고만 남기고 계속\n"
    "  ⑫ 템플릿 선택       unresolved 로 남기고 계속\n"
    "  ⑬ 상품별 라벨링     실행 중단\n\n"
    "재실행 정책을 만들 때는 vlm-stats.json · 마지막 산출물 · 캐시 사용 여부를 함께 본다.",
    style="warn",
)

card(
    800, NOTE_Y, 560, NOTE_H,
    "needs_review 는 위반이 아니라 파싱 품질 신호",
    "ownership_unknown / ownership_low_confidence\n"
    "recovery_action_uncertain / recovery_low_confidence\n"
    "table_verification_failed / table_region_overlap /\n"
    "table_reading_order_uncertain\n"
    "digital_text_vlm_disagreement\n"
    "ocr_vlm_disagreement / vlm_judge_low_confidence\n"
    "vlm_only_text / vlm_read_failed\n"
    "hwp_structure_page_unmatched      ← HWP 전용\n"
    "template_unresolved / label_low_confidence\n\n"
    "권장 검수 순서\n"
    "  1  소유권 · 템플릿 (틀리면 라벨에 연쇄 영향)\n"
    "  2  숫자 · 금리 · 날짜 불일치\n"
    "  3  표 구조 (완전 표인지, 행·열이 맞는지)\n"
    "  4  라벨 신뢰도\n\n"
    "상세 사유는 P1 의 같은 Region review_reasons.",
    style="warn",
)

card(
    1400, NOTE_Y, 520, NOTE_H,
    "VLM 호출 수는 고정이 아니다",
    "    문서 분류 수\n"
    "  + 페이지 또는 긴 페이지의 의미 밴드 수\n"
    "  + 표 후보 수\n"
    "  + Reader 대상 Region 수\n"
    "  + OCR / Reader 불일치 Judge 수\n"
    "  + 규칙으로 못 정한 상품 템플릿 수\n"
    "  + 상품별 라벨링 청크 수\n\n"
    "실행별 실제 호출 · 재시도 · 타임아웃 · 누적\n"
    "시간은 schema 단위로 기록된다.\n"
    "  outputs/<run>/vlm-stats.json\n"
    "  manifest.json 의 semantic_pipeline\n\n"
    "VLM_CACHE / VLM_CACHE_DIR 로 개발용\n"
    "결정론 캐시를 켤 수 있다.",
    style="soft",
)

card(
    1960, NOTE_Y, 440, NOTE_H,
    "사람이 대조하는 산출물",
    "03-ownership.json\n"
    "  상품 소유권 · 템플릿 · review unit 축약\n"
    "04-vlm-evidence.json\n"
    "  분류 · Reader/Judge · 표 · 라벨 판정 축약\n\n"
    "label-studio.json\n"
    "  1-raw parsing / 2-text source /\n"
    "  3-unassigned 탭 + P3 semantic 탭\n"
    "labeling-config.xml\n\n"
    "report.py\n"
    "  P3 와 페이지 이미지를 한 파일로 묶은\n"
    "  HTML 리포트",
    style="out",
)

card(
    2440, NOTE_Y, 440, NOTE_H,
    "P1 — 근거 원장",
    "outputs/<run>/05-p1.json\n"
    "outputs/<run>/final/<문서>.p1.json\n"
    "계약 nh-ad-parse-evidence-v4\n\n"
    "· OCR / PDF 원문 줄과 좌표\n"
    "· HWP 구조 근거 (좌표 없음)\n"
    "· PaddleX 영역 관측과 내부 출처\n"
    "· VLM 소유권 · 판독 · 라벨 판정\n"
    "· 선택 전 텍스트 후보와 선택 이유\n"
    "· HWP 원본 셀 및 시각 표 검증 근거\n"
    "· needs_review 의 구체적 사유\n"
    "· source_region_id · region_id_map",
    style="out",
)

card(
    2920, NOTE_Y, 520, NOTE_H,
    "P3 — 심의 입력",
    "outputs/<run>/06-p3.json\n"
    "outputs/<run>/final/<문서>.p3.json\n"
    "계약 nh-ad-region-review-input-v9\n\n"
    "{ region_id, product_id, bbox, selected_text,\n"
    "  labels, kind, needs_review, text_source }\n"
    "검증된 시각 표는 kind=table · VLM 평문.\n"
    "읽기 순서와 역색인은 싣지 않는다. 근거 참조는\n"
    "순번이 아니라 region_id 를 쓴다.\n\n"
    "후속 심의 결과 계약은 별도다.\n"
    "  result   위반 / 판정불가 / 충족\n"
    "  reason   판정 사유\n"
    "  region_ids  P3 에 실제 존재하는 근거 ID",
    style="out",
)

arrow([(r2x[8] + 80, R2_Y + R2_H), (r2x[8] + 80, NOTE_Y - 6)], color="#2f9e44")
arrow([(r2x[8] + 20, R2_Y + R2_H), (r2x[8] + 20, 1520), (2660, 1520), (2660, NOTE_Y - 6)],
      color="#2f9e44")

text(60, 1890,
     "이 그림은 docs/HANDOVER.md · docs/PIPELINE.md · docs/HWP_INPUT.md 를 run.py 와 nh_parser_fin/parse/pipeline.py::run_full_pipeline() 의 실제 호출 순서와 대조해 그렸다.  "
     "다시 그리려면:  uv run python docs/diagrams/make_pipeline_excalidraw.py",
     size=13, color="#868e96")


# ──────────────────────────────────────────────────────────────────────────
# 출력
# ──────────────────────────────────────────────────────────────────────────
scene = {
    "type": "excalidraw",
    "version": 2,
    "source": "nh-parser-fin/docs/diagrams/make_pipeline_excalidraw.py",
    "elements": _elements,
    "appState": {
        "gridSize": None,
        "gridStep": 5,
        "gridModeEnabled": False,
        "viewBackgroundColor": "#ffffff",
    },
    "files": {},
}
payload = json.dumps(scene, ensure_ascii=False, indent=2)

# Obsidian Excalidraw 플러그인용 마크다운.
#   excalidraw-plugin: raw  — 그림의 정본은 아래 JSON 하나다. parsed 모드는 텍스트를
#   `## Text Elements` 절과 동기화하는데, 생성물에서 두 곳이 어긋나면 글자가 비어
#   보인다. raw 로 두면 플러그인이 JSON 을 그대로 읽는다.
markdown = f"""---
excalidraw-plugin: raw
tags: [excalidraw, nh-parser-fin]
---
==⚠  Switch to EXCALIDRAW VIEW in the MORE OPTIONS menu of this document. ⚠==

# Excalidraw Data

## Text Elements

%%
## Drawing
```json
{payload}
```
%%
"""

(OUT_DIR / f"{NAME}.excalidraw.md").write_text(markdown, encoding="utf-8")
(OUT_DIR / f"{NAME}.excalidraw").write_text(payload, encoding="utf-8")
print(f"elements: {len(_elements)}")
print(f"wrote: {OUT_DIR / (NAME + '.excalidraw.md')}")
print(f"wrote: {OUT_DIR / (NAME + '.excalidraw')}")
