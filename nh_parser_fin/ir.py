"""AdPageIR — 광고물 파싱 산출물 스키마 (설계서 7절).

모든 bbox 는 [x0, y0, x1, y1] 원본 캔버스 픽셀 좌표.
HWP 디지털 추출처럼 캔버스가 없는 경우 bbox 는 None.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# 실제로 대입되는 값 셋뿐이다 (2026-08-06 out/json 전수: ocr 448 · digital 37 · vlm_sweep 15).
# 예전에는 "vlm"·"vlm_region" 도 있었는데 B안 전환(VLM 판독이 정본을 덮지 않고 후보로만
# 붙는다) 이후 라인 출처로 대입되는 코드가 없어졌다 — 지우기 전 전수 확인함.
Source = Literal["digital", "ocr", "vlm_sweep"]
ParseRoute = Literal["digital", "ocr", "hybrid"]
ParseStatus = Literal["ok", "partial", "unreadable"]


# 크기 값이 어떻게 얻어진 것인가. 둘은 **서로 다른 물리량**이므로 섞어 비교하면 안 된다.
#   declared — 문서가 선언한 글자 크기 그대로 (HWP `RunIR.run_style.size_pt`)
#   fontbox  — 글꼴칸(loose charbox) 높이를 pt 로 환산한 값 (PDF). 실제 선언 크기보다
#              크다(글꼴 메트릭의 ascent+descent 를 포함). PDF 는 선언 크기를 못 믿는다 —
#              실측(2026-08-21, 광고 PDF 3건 전수): `FPDFText_GetFontSize` 가 모든 글자에
#              1.0pt 를 돌려준다. 크기를 텍스트 행렬로 주는 문서라 명목값이 무의미하다.
# 같은 문서 안에서는 basis 가 일정하므로 **상대 비교(제목 대 유의사항)는 두 경우 다 유효**하다.
SizeBasis = Literal["declared", "fontbox"]


class TextStyle(BaseModel):
    """라인의 시인성 — 글자 크기·굵기·색.

    **왜 담나.** 심의 항목 `DEP-FMT-01`/`LOAN-FMT-01`("글자 색깔·크기 또는 음성 속도·크기
    등이 혜택과 불이익을 균형 있게 전달하는가")은 텍스트만으로 판정이 불가능하다. 실제
    심의사례에도 `유의사항 문구 글자크기 작음`(대출성 #2) 지적이 있다. 파서가 주는 값을
    버리지 않고 담기만 한다 — **어느 크기부터 위반인가(판정)는 여기서 하지 않는다.**

    **왜 대표값과 범위를 함께 담나.** 한 라인에 여러 스타일이 섞인다. 실측(2026-08-21,
    `NH농협은행-2026_004-대출성.hwp`): 같은 표 행에 35pt 헤드라인(`공무원`)과 10pt 본문이
    같이 오고, 8.5pt 심의필 문구가 따로 있다(최대/최소 4.1배). 대표값 하나로 접으면 그
    격차가 사라진다.
    """

    size_pt: Optional[float] = None       # 대표 크기 — 글자수가 가장 많은 값
    size_pt_min: Optional[float] = None
    size_pt_max: Optional[float] = None
    size_basis: Optional[SizeBasis] = None
    bold: Optional[bool] = None           # 라인에 굵은 글자가 하나라도 있으면 True
    # 글꼴 굵기 **원값**. bold 판정을 하류가 다시 할 수 있게 담는다.
    # ⚠️ PDF 는 이 값이 **pdfium 빌드에 따라 다르다** — 실측(2026-08-21, 같은 파일 1,081자):
    #   pypdfium2 5.12.0 → 260·360·440·520·600 (글꼴이 선언한 실제 weight)
    #   pypdfium2 5.13.0 → 400·700 두 값만    (CSS 관례로 뭉갠 값)
    # 같은 글자가 5.12 에서 360(보통), 5.13 에서 700(굵음)으로 나온다. 그래서 이 값만으로
    # bold 를 정하지 않고 글꼴명을 함께 본다(text_style.is_bold). HWP 는 파서가 bool 을 준다.
    font_weight: Optional[int] = None
    color: Optional[str] = None           # 대표 색 `#RRGGBB` (글자수 최다)
    colors: list[str] = Field(default_factory=list)  # 라인에 등장한 색 전부 (대표 색 포함)
    font: Optional[str] = None            # 대표 글꼴명. PDF 는 서브셋 접두어(`ABCDEF+`)를 뗀다
    style_source: Optional[str] = None    # hwp_run | pdf_char — 어디서 얻었는지


class Line(BaseModel):
    text: str
    bbox: Optional[list[int]] = None
    confidence: Optional[float] = None
    source: Source
    # 시인성. OCR 라인은 pt·색을 알 수 없어 항상 None 이다(글자 이미지만 있다) —
    # 값이 없는 것과 "스타일이 없는 문서"를 구분해야 하므로 style_source 로 출처를 남긴다.
    style: Optional[TextStyle] = None
    # 라인 단위 VLM 재판독 후보 — Region.vlm_reading 과 같은 원칙(B안)을 라인에도 적용한다.
    # 원칙은 원래 Region 에만 지켜지고 라인에서는 두 단계가 정본을 갈아치우고 있었다:
    # 스윕-OCR 중복 심판(pipeline)과 저신뢰 재판독(vlm_direct). 두 단계 다 비결정
    # (스윕이 회수하는 문구가 실행마다 다름)이라 정본이 실행마다 흔들렸다 — 같은 코드·같은
    # 입력 2회 실측(2026-08-03): 정본 484줄 중 2줄이 갈렸고 그 2줄이 전부 이 경로였다
    # (002 '이벤트 기간' ↔ '이벤트 기간內', 올원e '20.2%p:...' ↔ '② 0.2%p : ...').
    # 이제 정본은 그대로 두고 후보만 붙인다. 최종 텍스트 선택은 하류(STAGE_3/심의)가 한다.
    vlm_reading: Optional[str] = None
    vlm_reading_conf: Optional[float] = None
    vlm_reading_stage: Optional[str] = None  # sweep_dedupe | lowconf_reread


class TableCell(BaseModel):
    """표 한 칸. `row`/`col` 은 0부터, 병합셀은 `row_span`/`col_span` 으로 나타낸다."""
    row: int
    col: int
    row_span: int = 1
    col_span: int = 1
    bbox: Optional[list[int]] = None
    # PaddleX 표 인식이 읽은 셀 텍스트. **정본이 아니다** — 표 안 글자에도 OCR 오류가
    # 있어서(실측 'BD', '2 295,000원') 정본은 Region.lines 쪽이다. 이 값은 격자 해석의
    # 근거로 남기고, 정본 줄과의 연결은 좌표로 짝지운다(ad_export._table_out).
    text: str = ""


class RegionTable(BaseModel):
    """표 영역의 행·열 구조 (PaddleX `table_res_list`).

    2026-08-24 이전에는 이 정보가 통째로 사라졌다. PaddleX 는 `pred_html` 과
    `cell_box_list` 를 정확히 돌려주는데(실측: 우대조건↔우대금리 짝이 맞다)
    `paddlex_client` 가 `table_res_list` 를 읽지 않았다. 그래서 표가 줄로 흩어지고
    `'-(가입월부터…)입출식05%p'` 처럼 소수점이 문장에 붙는 일까지 생겼다.
    """
    n_rows: int = 0
    n_cols: int = 0
    # PaddleX 원본 HTML. 우리 격자 해석이 틀렸을 때 대조할 유일한 근거라 버리지 않는다.
    html: str = ""
    cells: list[TableCell] = Field(default_factory=list)
    # 격자를 온전히 못 만든 이유 (셀 개수 불일치 등). 조용히 비우지 않는다.
    note: Optional[str] = None


class ReadingAdjudication(BaseModel):
    """영역별 OCR/PDF 정본과 독립 VLM 판독의 shadow 교차검증 기록.

    ``proposed_text``는 Judge가 픽셀 근거로 고른 검수용 제안이며 Region.lines를 바꾸지
    않는다. ``uncertain``이어도 Judge가 A/B 중 하나를 골랐다면 그 후보와 출처를 남긴다.
    다만 이는 VLM 문자열을 정본으로 승격했다는 뜻이 아니며, P2의 제한된 시험 선택은
    영역 전체가 하나의 심의 문구일 때만 이 근거를 사용할 수 있다.
    """

    mode: Literal["shadow"] = "shadow"
    crop_bbox: list[int]
    target_bbox: list[int]
    crop_policy: Literal["masked_outside_region"] = "masked_outside_region"
    # 목표 bbox가 다른 StructureV3 텍스트 영역을 통째로 품은 경우, 그 하위 영역은
    # Reader 입력에서 별도로 마스킹한다. 빈 배열은 bbox 밖 마스킹만 적용했다는 뜻이다.
    excluded_overlap_region_ids: list[str] = Field(default_factory=list)
    selection_reasons: list[str] = Field(default_factory=list)
    canonical_source: Literal["digital", "ocr", "hybrid"]
    reader_text: Optional[str] = None
    reader_confidence: Optional[float] = None
    relation: Optional[str] = None
    status: Literal["reader_failed", "agreed", "judge_selected", "uncertain", "judge_failed"]
    judge_decision: Optional[Literal["candidate_a", "candidate_b", "merge", "uncertain"]] = None
    proposed_text: Optional[str] = None
    proposed_source: Optional[Literal["canonical", "element_vlm", "merged"]] = None
    confidence: Optional[float] = None
    reason: Optional[str] = None
    error: Optional[str] = None


class TableVlmReading(BaseModel):
    """StructureV3가 찾은 표 영역을 VLM이 픽셀만 보고 읽은 관측값.

    PaddleX의 HTML/셀 좌표는 광고 파이프라인의 표 정본으로 쓰지 않는다. 표 영역을
    찾는 역할은 StructureV3에만 맡기고, 행/셀 관계는 이 관측값으로만 보존한다. 이 역시
    OCR 정본을 바꾸지 않는 shadow 근거다.
    """

    text: str = ""
    rows: list[list[str]] = Field(default_factory=list)
    confidence: Optional[float] = None
    structure_confidence: Optional[float] = None
    status: Literal["observed", "unreadable"] = "observed"


class RecoveryCandidate(BaseModel):
    """StructureV3가 만들지 못한 문구를 페이지 sweep이 발견했을 때의 관측값.

    이 값은 기존 ``Line`` 이 아니다. 영역/라인 OCR 정본에 섞으면 VLM의 근사 bbox와
    문자열이 원문 사실처럼 승격된다. 다음 단계는 필요하면 별도 검수 대상으로만 사용한다.
    """

    text: str
    bbox: Optional[list[int]] = None
    confidence: Optional[float] = None
    source: Literal["page_sweep"] = "page_sweep"
    status: Literal["unverified"] = "unverified"


class Region(BaseModel):
    region_id: str
    bbox: Optional[list[int]] = None
    label: str = "unknown"          # 엔진 원 라벨 (PP-Structure label 등)
    # StructureV3 가 이 블록 판정에 붙인 확신도(LayoutBlock.score). 판정에는 안 쓴다 —
    # 역할 판정이 갈릴 때 "레이아웃 엔진도 확신이 없던 자리인가"를 보려고 남긴다.
    # 2026-08-06 이전에는 받을 칸이 없어 파싱 직후 사라졌다 (walkthrough §9-⑤).
    layout_score: Optional[float] = None
    role: str = "본문"               # 제목|본문|유의사항|각주|버튼|고지문구|이미지|표|기타
    role_confidence: Optional[float] = None
    role_source: Optional[str] = None  # vlm | rules (VLM 실패 시 폴백)
    # 규칙(_refine_role)이 내린 판정 — VLM 이 role 을 통째로 덮기 **전** 값이다.
    # 판정에는 안 쓴다. 지금까지는 VLM 이 성공하면 규칙 판정이 흔적 없이 사라져
    # "규칙과 VLM 이 어디서 몇 건 갈리는가"를 잴 수가 없었다(총계 97.3% 일치만 알았다).
    # 이 두 필드가 그 측정의 재료다 — walkthrough §9-⑤ 고도화 1단계.
    role_rule: Optional[str] = None
    role_rule_confidence: Optional[float] = None
    card_no: Optional[int] = None      # 카드-분할(§D): 1..N=카드(위→아래·좌→우), 0=페이지 공통(배너/헤더). 스크롤/미적용은 None
    # 예시/장식(앱화면 예시·지폐 그림 등) 격리용. ⚠️ **어디서도 True 로 대입되지 않는다** —
    # 2026-08-03 섹션 제거 때 설정 코드가 같이 빠졌다. 읽는 곳 3군데(vlm_direct·pipeline·
    # applicability)가 전부 항상 False 로 돈다. 되살릴 조건은 walkthrough §9-⑥ 참조.
    is_illustrative: bool = False
    # 표로 인식된 영역의 행·열 격자. 표가 아니거나 인식이 안 되면 None.
    # 실측(2026-08-24): `13. 대출성상품.pdf` p1 은 표 블록 1개(요율표)만 이걸 받는다 —
    # 왼쪽 '대출대상/대출한도/대출기간' 2열 항목표는 PaddleX 가 label="text" 로 보고
    # 표로 검출하지 않으므로 여기 안 담긴다. 그 짝짓기는 별개 문제다.
    table: Optional[RegionTable] = None
    lines: list[Line] = Field(default_factory=list)
    # 영역별 VLM 통독(§6) — B안: OCR 정본을 덮어쓰지 않고 '후보'로만 보존한다. VLM 이
    # 이 영역 크롭을 통독한 clean text(회전·장식 교정 포함)이며, ocr_score(정밀도)로
    # OCR 과 교차검증된다. 최종 텍스트 선택은 judge/STAGE_3(스키마)가 수행 — 파싱 단계는
    # 정본(OCR)과 후보(vlm_reading)를 둘 다 남길 뿐 조용히 대체하지 않는다.
    vlm_reading: Optional[str] = None
    vlm_reading_score: Optional[float] = None   # 통독 후보의 OCR 대조 정밀도 (0~1)
    vlm_reading_coverage: Optional[float] = None # 통독 후보의 OCR 내용 커버리지 (재현율, 0~1)
    # 정본 대비 후보의 관계 (truncation.classify_reading). 위 두 점수는 토큰 겹침이라
    # 순서를 못 봐서 '뒤가 잘린 판독'이 정밀도 만점을 받는다 — 그 사각을 메우는 라벨.
    # same | tail_cut | head_drop | expanded | diverged
    vlm_reading_relation: Optional[str] = None
    # 개별 영역만 마스킹 crop으로 읽은 독립 VLM 관측. 밴드 통독 후보와 출처가 달라
    # 서로 덮지 않고 비교한다.
    element_vlm_reading: Optional[str] = None
    element_vlm_confidence: Optional[float] = None
    # 표일 때만 채운다. PaddleX 격자와 달리 VLM이 같은 영역 crop을 직접 보고 낸
    # 행/셀 관측이며, 정본 Line.text를 덮어쓰지 않는다.
    table_vlm_reading: Optional[TableVlmReading] = None
    reading_adjudication: Optional[ReadingAdjudication] = None

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


# 2026-08-06 제거: `ExtractedField` 와 `Section` 모델, `AdPage.extracted_fields` ·
# `AdPage.sections` 필드.
#
#   · Section       — 섹션(의미 묶음) 생성을 2026-08-03 에 파이프라인에서 없앴다.
#                     그 뒤로 항상 빈 리스트였고 채우는 코드가 없다.
#   · ExtractedField — 필드 추출은 STAGE_3(out/extracted)가 단일 창구다. 파싱 단계는
#                     필드를 안 뽑으므로 이 모델도 항상 빈 리스트였다.
#
# 산출물(out/json)에서 두 키가 사라진다. 읽던 곳은 러너의 진단 출력(항상 아무것도 안
# 찍혔다)과 삭제된 도구 둘(dump_text·make_gold_draft)뿐이었다. 되살릴 일이 생기면
# git 이력에 남아 있다.


class AdPage(BaseModel):
    page_no: int
    canvas_w: int = 0
    canvas_h: int = 0
    dpi: Optional[int] = None       # PDF 렌더 시 기록. PNG 원본은 None
    parse_route: ParseRoute
    parse_status: ParseStatus = "ok"
    triage: Optional[dict] = None   # PDF 페이지 triage 근거 (감사 추적용)
    regions: list[Region] = Field(default_factory=list)
    unassigned_lines: list[Line] = Field(default_factory=list)
    # OCR/디지털 정본과 분리된 페이지 전체 누락문구 탐색 결과. StructureV3 영역이 없어서
    # Region Reader가 볼 수 없는 장식·벡터 텍스트의 후보이며 자동으로 정본에 합치지 않는다.
    recovery_candidates: list[RecoveryCandidate] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class AdDocument(BaseModel):
    doc_id: str
    source_file: str
    file_type: str                  # pdf|image|hwp|hwpx
    product_group: Optional[str] = None
    ad_type: Optional[str] = None
    # 고유 상품명이 광고에 적혀 있나 (노출|미노출). 템플릿 12종이 이 값으로 갈린다 —
    # 같은 대출성인데 노출형 12항목 · 미노출형 3항목이다. VLM 이 판단불가면 None.
    product_name_shown: Optional[str] = None
    # 분류가 어느 경로로 정해졌나 (gemma_client.classify 주석 참조). prior 와 VLM 이
    # 둘 다 있을 때: filename_and_vlm(합의) | vlm_overrode_filename(충돌, VLM 채택).
    # VLM 만: vlm | vlm_abstained. 파일명만: filename_vlm_abstained(VLM 이 반대) |
    # filename_vlm_failed(호출 실패) | filename_no_vlm(HWP — 안 부름). 없으면 none.
    category_source: Optional[str] = None
    classification_confidence: Optional[float] = None
    pages: list[AdPage] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
