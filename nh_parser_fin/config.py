"""실행 설정 한 곳 모음 — 임계값·엔드포인트·서빙 파라미터.

**이 파일의 숫자에는 대부분 실측 근거가 주석으로 붙어 있다.** 값을 바꾸려면 그 근거를
먼저 읽을 것 — 서버 기본값이 광고물 파싱을 망가뜨린 사례가 여러 건 기록돼 있다
(det 해상도 다운스케일, 박스 병합 모드, 방향 분류기).

사내 엔드포인트는 코드에 두지 않는다. `.env`(gitignore 대상)에서 읽고, 여기 기본값은
저장소가 공개돼도 내부망 주소가 드러나지 않는 자리표시자다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_env_file(path: Path) -> None:
    """.env 파일을 읽어 **아직 없는** 환경변수만 채운다(이미 export 된 값이 우선).

    HyundaiHS(orchestrator/config.py::load_env_file)와 같은 방식 — python-dotenv
    의존성 없이 직접 파싱한다. 실제 사내 엔드포인트(PADDLEX_URL/GEMMA_URL 등)를
    코드에 하드코딩하지 않기 위한 장치다(2026-08-01, 저장소가 잠깐 공개돼 있던
    사고 이후 조치). `.env`는 `.gitignore` 대상이라 이 파일을 만들어도 커밋되지 않는다.
    """
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def _find_env_file() -> Path | None:
    """`.env` 를 찾는다 — 현재 작업 디렉터리에서 위로, 그다음 소스 트리 기준으로.

    예전에는 `parents[2]/.env` 하나만 봤다(= 소스 레이아웃의 저장소 루트). 패키지를
    설치해 쓰면 `__file__` 이 site-packages 안이라 그 경로가 존재하지 않는다.
    작업 디렉터리에서 위로 올라가며 찾는 쪽을 먼저 두면 두 경우가 다 된다.
    """
    cwd = Path.cwd().resolve()
    here = Path(__file__).resolve()
    candidates = [cwd, *cwd.parents, *here.parents]
    seen: set[Path] = set()
    for base in candidates:
        if base in seen:
            continue
        seen.add(base)
        env = base / ".env"
        if env.is_file():
            return env
    return None


_ENV_FILE = _find_env_file()
if _ENV_FILE is not None:
    load_env_file(_ENV_FILE)


@dataclass(frozen=True)
class Settings:
    # ── 외부 OCR/VLM 서비스 ─────────────────────────────────────────
    # 실제 엔드포인트는 .env(gitignore 대상)에만 둔다. 아래 값은 공유 저장소용
    # 자리표시자이며 .env.example을 복사해 실행 환경에 맞게 설정한다.
    paddlex_url: str = os.environ.get(
        "PADDLEX_URL", "http://YOUR_PADDLEX_HOST:8081/layout-parsing"
    )
    gemma_url: str = os.environ.get(
        "GEMMA_URL", "http://YOUR_GEMMA_HOST:4000/v1/chat/completions"
    )
    gemma_model: str = os.environ.get("GEMMA_MODEL", "YOUR_MODEL_NAME")
    paddlex_timeout_s: int = int(os.environ.get("PADDLEX_TIMEOUT_S", "180"))
    gemma_timeout_s: int = int(os.environ.get("GEMMA_TIMEOUT_S", "120"))

    # ── PDF triage 임계치 (document-processor probe 기준에서 출발) ──
    min_readable_chars: int = 20          # 이하면 텍스트 레이어 불신
    max_fffd_ratio: float = 0.3           # U+FFFD 비율 초과 시 SCAN_LIKE
    min_fffd_count: int = 8               # 절대 개수 하한 (오탐 방지)
    hybrid_image_area_ratio: float = 0.5  # STRUCTURED여도 이미지 면적비 초과 시 OCR 병행
    # 페이지의 글자 획 픽셀 중 텍스트 레이어가 덮은 비율이 이 값 미만이면 STRUCTURED 를
    # HYBRID 로 내린다 — 글자를 도형·그림으로 그려 텍스트 레이어에 안 들어온 페이지.
    # structured 29쪽 실측(2026-08-14): 2.5% 다음이 56.3% 로 53.8%p 가 비어 있어
    # 5~50% 어디에 그어도 같다. 근거는 bands.ink_coverage 주석.
    min_ink_coverage: float = 0.30

    # ── 렌더/타일링 ─────────────────────────────────────────────
    # 렌더/타일링 3개는 **서버를 안 건드리는 실험 축**이다. 레이아웃 모델이 입력을
    # 800x800 정사각으로 종횡비 무시하고 눌러 넣으므로(PP-DocLayout_plus-L
    # inference.yml `keep_ratio: false`, 2026-09-16 실측), 타일의 모양이 곧 실효
    # 해상도를 정한다. 그래서 환경변수로 이번 실행만 바꿀 수 있게 열어 둔다.
    # 기본값은 그대로이므로 지정하지 않으면 운영 동작은 한 글자도 안 달라진다.
    pdf_render_dpi: int = int(os.environ.get("PDF_RENDER_DPI", "200"))
    tile_max_height_px: int = int(       # 조각 하나의 높이 상한 (자르는 위치는 글자 밀도가 결정)
        os.environ.get("TILE_MAX_HEIGHT_PX", "1600")
    )
    tile_overlap_px: int = int(os.environ.get("TILE_OVERLAP_PX", "200"))
    # 타일링 트리거. 이 값은 이전 프로젝트에서 물려받은 것이고 아래 2500 과 맞춰 계산한
    # 값이 아니다 — 그래서 2500~4000 구간(통짜로 들어가는데 서버는 2500 으로 줄이는 구간)이
    # 검증된 적이 없었다. 2026-07-28 실측으로 확인함: 폭 720px 고정, 높이 2200~6111px 로
    # 늘려가며 002 의 fine-print 를 읽혔을 때 **모든 높이에서 동일하게 검출**됐다(0.41배
    # 축소까지). PP-OCR 구조상 검출은 축소본에서 하지만 인식은 원본 좌표를 다시 크롭해
    # 읽기 때문이다. 즉 이 구간의 위험은 이 데이터에서는 발현하지 않는다.
    # 다만 문서 1건·폰트 1종 실측이라 일반화는 안 되고, 샘플 5개 중 이 구간에 드는 문서가
    # 하나도 없다(1120px 또는 6100px+). 해당 크기 입력이 실제로 들어오면 재확인할 것.
    tile_trigger_height_px: int = int(    # 장변 기준 타일링 트리거
        os.environ.get("TILE_TRIGGER_HEIGHT_PX", "4000")
    )

    # ── PaddleX PP-StructureV3 서빙 파라미터 (공식 predict 파라미터의 camelCase) ──
    # text_det_limit_side_len 서버 기본 960/max 는 타일(장변 2000px)을 절반 이하로
    # 축소해 fine-print 를 깨뜨린다 (2026-07-17 실측: 올원 유의사항 완전 복구).
    paddlex_text_det_limit_side_len: int | str = os.environ.get(
        "PADDLEX_TEXT_DET_LIMIT_SIDE_LEN", "2500"
    )
    paddlex_text_det_limit_type: str = os.environ.get(
        "PADDLEX_TEXT_DET_LIMIT_TYPE", "max"
    )
    # 기본 "large" 는 겹침 박스를 큰 쪽으로 흡수 — 사진형 카드 콜라주(003 p1)가
    # 통짜 1블록이 된 원인. "small" 로 카드별 하위 블록 유지 (3→20블록 실측).
    # A/B 실험에서 코드 자체를 바꾸지 않고 large/small을 갈라 실행할 수 있게 한다.
    # 기본값은 기존 카드 콜라주 회귀를 막은 small을 유지한다.
    # "server" 를 주면 요청에서 이 항목을 빼 서버 기본값을 그대로 쓴다 (대조용).
    paddlex_layout_merge_bboxes_mode: str = os.environ.get(
        "PADDLEX_LAYOUT_MERGE_BBOXES_MODE", "small"
    ).strip().lower()
    # 레이아웃 검출 후처리 두 항목. 기본값 "server" 는 위 merge 모드와 **같은 규약**이다 —
    # 요청에서 키를 아예 빼 서버 PP-StructureV3.yml 기본값을 그대로 쓴다. 즉 이 필드를
    # 추가해도 기존 요청 본문은 한 글자도 달라지지 않는다(공유 서버·타 프로젝트 무영향).
    #
    # 왜 필드를 만드나. 2026-09-08 프로브(tmp/probe_params_table.json)에서 영역 수를 가장
    # 크게 움직인 것이 merge 모드가 아니라 이 둘이었다 (기준 33블록):
    #   layoutThreshold 0.3 → 35블록 / 0.7 → 20블록      (merge=large 는 28블록)
    #   layoutUnclipRatio 1.5 → 25블록
    # 그런데 파이프라인에 연결이 없어 probe 로 서버를 직접 때릴 때만 볼 수 있었다.
    #
    # 주의 1. unclip 은 **순수 영역 축이 아니다.** 1.5 에서 OCR 줄이 119→132, 호출이
    #   7.1→8.9초로 함께 변했다. 영역만 보고 판단하면 안 되고 줄·글자 수를 같이 봐야 한다.
    # 주의 2. 공식 문서는 클래스별 dict(`{"2":0.3}`)를 지원한다고 적었지만, 같은 프로브에서
    #   존재하지 않는 키 `{"999":0.3}` 가 동일 결과를 냈다 — 현재 서버 래퍼에서 dict 가
    #   먹는지 확인되지 않았다. 스칼라(또는 unclip 의 배열)만 쓸 것.
    # 문서화된 기본값은 threshold 0.5 / unclip 1.0 이다(PaddleX 공식). 응답이 이 둘을
    # 되돌려주지 않아 서버 실값은 미확인 — 그래서 "현행"은 0.5/1.0 이 아니라 "미지정"이다.
    paddlex_layout_threshold: str | float | list | dict = os.environ.get(
        "PADDLEX_LAYOUT_THRESHOLD", "server"
    )
    paddlex_layout_unclip_ratio: str | float | list | dict = os.environ.get(
        "PADDLEX_LAYOUT_UNCLIP_RATIO", "server"
    )
    paddlex_use_formula_recognition: bool = False  # 광고물에 수식 없음 — 속도 절약
    # 방향 분류기(기본 True)가 얇은 회색 fine-print 를 180도 회전으로 오판해
    # 거꾸로 인식('링이어니을용은' 사건, 2026-07-17 실측). 디지털 캡처/정방향
    # 렌더 입력에는 회전이 없으므로 비활성화.
    paddlex_use_textline_orientation: bool = False

    # ── 실험용 서빙 파라미터 (전부 기본 "server" = 요청에서 키를 뺀다) ──────────
    # PaddleX 3.6 서빙 스키마(`schemas/pp_structurev3.py` InferRequest)가 받는 필드 중
    # 우리가 그동안 한 번도 안 보내던 것들이다. 기본값이 "server" 이므로 이 필드를
    # 추가해도 요청 본문은 예전과 동일하다 — 실험에서 환경변수로만 켠다.
    #
    # 주의. dict(클래스별 값)는 여기서 다루지 않는다. HTTP JSON 은 key 가 문자열이 되고
    # predictor 는 정수 cls_id 와 비교하므로 매칭되지 않는다(프로브 실측: 존재하지 않는
    # 키 "999" 와 실제 클래스 "2" 가 동일 결과). 클래스별 제어는 서버 YAML 의 정수 key 로만
    # 유효하다.
    paddlex_layout_nms: str | bool = os.environ.get("PADDLEX_LAYOUT_NMS", "server")
    paddlex_use_region_detection: str | bool = os.environ.get(
        "PADDLEX_USE_REGION_DETECTION", "server"
    )
    paddlex_use_table_recognition: str | bool = os.environ.get(
        "PADDLEX_USE_TABLE_RECOGNITION", "server"
    )
    # OCR 검출 3개. 영역만의 축이 아니다 — 줄이 늘고 줄면 Region 조립(미배정 줄 흡수)이
    # 따라 움직여 경계가 바뀐다. 영역 수만 보고 판단하면 안 된다.
    paddlex_text_det_thresh: str | float = os.environ.get(
        "PADDLEX_TEXT_DET_THRESH", "server"
    )
    paddlex_text_det_box_thresh: str | float = os.environ.get(
        "PADDLEX_TEXT_DET_BOX_THRESH", "server"
    )
    paddlex_text_det_unclip_ratio: str | float = os.environ.get(
        "PADDLEX_TEXT_DET_UNCLIP_RATIO", "server"
    )
    paddlex_text_rec_score_thresh: str | float = os.environ.get(
        "PADDLEX_TEXT_REC_SCORE_THRESH", "server"
    )

    # ── OCR 라인 병합/중복 제거 ──────────────────────────────────
    dedupe_iou: float = 0.5
    dedupe_containment: float = 0.7   # 겹침/작은쪽 면적 — 타일 경계 부분 조각 제거

    # ── VLM 전체화면 호출의 밴드 분할 ────────────────────────────
    # VLM 서버는 pan-and-scan 없이 어떤 크기든 고정 예산(이미지 토큰 ~1,050~1,100)으로
    # 리샘플링한다(2026-07-27 실측: 896x896 과 1122x6429 의 토큰이 같음). 세로로 긴
    # 캔버스를 통짜로 보내면 축소율만 커져 작은 글씨가 소실된다 — 같은 문단을 통짜로
    # 주면 3/13, 구간만 잘라 주면 13/13 회수. 그래서 폭 기준 비율로 잘라 보낸다.
    # 비율 2.0 근거: 001/002/올원 A/B 에서 통짜가 일관되게 최악(0.23~0.27)이나 최적
    # 비율은 문서마다 엇갈려(001 은 큰 밴드, 002 는 작은 밴드 유리), 최악값이 가장
    # 높은 2.0 을 택했다. 표본이 늘면 재측정 대상.
    vlm_band_ratio: float = 2.0            # 밴드 높이 = 캔버스 폭 x 이 값
    vlm_band_min_height_px: int = 900      # 좁은 캔버스에서 과분할 방지 하한
    # 산술 절단은 글자 한가운데를 지날 수 있다(001 실측: 헤드라인·112px '20,000원',
    # 002: '농협은행이'). 잘린 글자는 양쪽 밴드 어디서도 안 읽혀 회수에서 빠지므로,
    # 이미 아는 텍스트/블록 bbox 를 피해 이 범위 안에서 절단선을 당긴다.
    vlm_band_snap_px: int = 160
    # 응답이 max_tokens 에서 잘리면 JSON 이 깨져 그 밴드 회수분이 통째로 사라진다
    # (실측: 'Unterminated string' → 회수율 0.95 에서 0.27 로 폭락). 넉넉히 준다.
    sweep_max_tokens: int = 3000

    # ── 저신뢰 라인 VLM 재판독 (2b) ──────────────────────────────
    # 유지 결정(2026-07-27): 한때 장식 기호('¥'→'★')만 고쳐 제거 후보로 봤으나, 다음
    # 실행에서 'M모닝이[신'→'부모님이 대신', '소비승관의 첫결음'→'소비습관의 첫걸음'
    # 처럼 실제 문구를 복원했다. 1회 관측으로 판단하면 안 된다는 실측 사례.
    # 심의 관련 영역의 신뢰도 낮은 OCR 라인을 고해상 크롭으로 다시 읽혀 교정한다.
    # 예시/장식·이미지 영역은 제외(무관·비용). 판단 주체는 VLM(크롭 재판독).
    lowconf_reread_threshold: float = 0.80     # OCR 신뢰도 이 미만 라인이 재판독 후보
    lowconf_reread_min_vlm_conf: float = 0.60  # VLM 재판독 확신도 이 이상일 때만 텍스트 교체
    lowconf_reread_max_per_page: int = 12      # 페이지당 재판독 호출 상한 (비용 가드)

    # ── 영역별 Reader → Judge 교차검증 ────────────────────────────
    # 기본 off: kl_parser의 기존 정본·템플릿 결과를 배포 전에 바꾸지 않는다. shadow를
    # 켜면 기본 파이프라인 안에서 후보와 판정 근거를 통합 JSON에 더하되 Line.text는
    # 절대 수정하지 않는다.
    region_reading_mode: str = os.environ.get("REGION_READING_MODE", "off").strip().lower()
    # 이 브랜치의 Reader 경로는 StructureV3가 반환한 모든 텍스트/표 영역을 독립 판독하는
    # 것이 기본이다. 영역별 후보를 넓은 밴드에서 다시 읽는 이전 구조와 섞지 않는다.
    # 표만 확인할 때는 tables, 표+저신뢰 OCR은 targeted를 비교 실험에서 명시한다.
    region_reading_scope: str = os.environ.get("REGION_READING_SCOPE", "all").strip().lower()
    region_reader_max_per_page: int = int(os.environ.get("REGION_READER_MAX_PER_PAGE", "0"))
    # Region bbox 밖의 픽셀은 흰색으로 마스킹한다. StructureV3 bbox가 글자 끝을 약간
    # 자르는 경우를 위해 이 폭만큼의 안전 고리는 남기고, 파란 테두리로 목표 영역을 VLM에
    # 명시한다. 이웃 문구 혼입을 줄이기 위한 값이며 텍스트 정본에는 영향을 주지 않는다.
    region_crop_padding_px: int = int(os.environ.get("REGION_CROP_PADDING_PX", "16"))
    region_mask_bleed_px: int = int(os.environ.get("REGION_MASK_BLEED_PX", "2"))

    # ── 밴드 단위 통합 판독 (④+ 통독 + ⑧ 스윕을 한 호출로) ─────────────
    # OCR 이 본 것과 **같은 밴드**를 VLM 에도 주고, 그 안의 영역 목록을 함께 실어
    # "이 영역들을 고쳐라 + 목록에 없는 문구를 찾아라"를 한 호출로 묻는다. 예전에는
    # 영역별 통독이 영역을 하나씩(4장 배치) 보고 스윕이 밴드를 따로 봐서, 같은 페이지를
    # 서로 다른 크롭으로 두 번 훑었다 — 그 개별 경로는 이 방식이 완전히 흡수해
    # 죽은 코드가 됐고 2026-07-29 제거했다.
    #
    # 우려했던 것은 해상도였다 — 영역 크롭은 영역 하나가 896x896 을 다 쓰지만 밴드는
    # 영역 5~10개가 나눠 쓴다. A/B 실측(2026-07-28, 5문서)에서 그 손해보다 이득이 컸다:
    #   VLM 호출  122 → 65회 (-47%)      파싱 시간 37.2 → 12.2분 (-67%)
    #   골드 문장 234~235 → 236/242      STAGE_3 골드 필드 44/44 유지
    #   OCR 과 다른 교정 후보 49 → 36개 (73% 유지 — 기각선 50% 통과)
    # 원문자 교정도 살아남았고 오히려 정확해졌다(올원e p1_r017: 기존은 ② 영역에 ③ 내용을
    # 넣는 오정렬이었는데 병합 쪽이 ② 를 제대로 읽음) — 밴드가 주변 맥락을 함께 보기 때문.
    #
    # 밴드 경계가 영역을 반토막 내는 문제는 크롭을 넓혀 해결한다(실측 14개 → 0개).

    # ── 카드-분할 (§D) ────────────────────────────────────────
    # 세로 스크롤(모바일 상품페이지 등)은 박스형 섹션이 있어도 카드 collage 가 아니다.
    # VLM 이 그런 섹션을 "카드"로 오판하면(올원e 스크롤을 4카드로 쪼갠 실측) 섹션이
    # 파편화된다. 캔버스 종횡비(높이/폭)가 이 값 이상인 '긴 스크롤'은 카드-분할 대상에서
    # 제외한다 — 스크롤 vs 슬라이드는 구조적 구분(과적합 아님). 슬라이드(003 ≈0.56)만 대상.
    card_split_max_aspect: float = 2.0         # 높이/폭 이 이상이면 스크롤 → 카드-분할 안 함
    card_split_votes: int = 3                  # 카드 판정 복수관측 횟수 (다수결 안정화)

    # ── 분류 (파일명 prior) ─────────────────────────────────────
    # 순서가 곧 우선순위다. `카드` 가 `대출성` 보다 앞에 있어야 한다 —
    # "카드상품-장·단기카드대출" 처럼 파일명에 '대출' 이 들어간 카드 광고가 실재하고,
    # 대출성이 먼저 걸리면 카드가 대출성으로 새어 나간다.
    #
    # 2026-08-24 `카드` 추가. 89건 중 21건이 카드인데 분류 대상이 아니라
    # 전부 미분류로 떨어졌다 — 오분류가 아니라 **분류 체계에 자리가 없었던 것**이다.
    # 농협 광고 템플릿에 카드 4종이 정의돼 있어 이제 자리가 생겼다.
    product_group_keywords: dict = field(
        default_factory=lambda: {
            "카드": ["카드상품", "카드"],
            "투자성": ["투자성", "ISA", "개인종합자산관리계좌", "IRP", "퇴직연금", "펀드"],
            "예금성": ["예금성", "예금", "적금", "입출금"],
            "대출성": ["대출성", "대출", "신용대출", "담보"],
        }
    )


SETTINGS = Settings()


# ──────────────────────────────────────────────────────────────────────────
# 실행 프로필 — 한 번의 실행을 재현하는 데 필요한 값만 모은다.
#
# `Settings` 와 나눠 둔 이유: `Settings` 는 임계값과 서빙 파라미터까지 담는 큰
# 묶음이고, 아래 값들은 **실행마다 달라질 수 있는** 것들이라 `manifest.json` 에
# 통째로 찍어 남긴다. 어떤 조건으로 돌렸는지는 산출물만 보고도 알아야 한다.
# ──────────────────────────────────────────────────────────────────────────
from dataclasses import asdict


@dataclass(frozen=True)
class Profile:
    paddlex_url: str = "http://127.0.0.1:18081/layout-parsing"
    timeout: int = 300
    aspect_limit: float = 2.0
    tile_span: int = 1600
    encode: str = "jpeg"

    @classmethod
    def from_env(cls) -> "Profile":
        return cls(
            paddlex_url=os.environ.get("PADDLEX_URL", cls.paddlex_url),
            timeout=int(os.environ.get("PADDLEX_TIMEOUT", cls.timeout)),
            aspect_limit=float(os.environ.get("PARSER_V2_ASPECT_LIMIT", cls.aspect_limit)),
            tile_span=int(os.environ.get("PARSER_V2_TILE_SPAN", cls.tile_span)),
            encode=os.environ.get("PARSER_V2_ENCODE", cls.encode),
        )

    @property
    def request_payload(self) -> dict:
        # 실행 옵션은 모두 PaddleX 서버의 파이프라인 YAML 이 정한다. 선택 옵션을
        # HTTP 로 보내면 YAML 의 클래스별 dict 와 모듈 on/off 를 요청 스칼라가
        # 덮어쓸 수 있다 — 서버 한 곳에서만 관리한다.
        return {"fileType": 1}

    def manifest(self) -> dict:
        return {
            **asdict(self),
            "request_payload": self.request_payload,
            "paddlex_options_source": "server_pipeline_yaml",
        }


# ──────────────────────────────────────────────────────────────────────────
# 산출물 경로. 한 곳에서 정해 `run.py` 와 `report.py` 가 같은 자리를 보게 한다.
# 페이지 이미지는 P1/P3 와 따로 둔다 — 여러 실행이 같은 렌더를 공유하고,
# 리포트가 base64 로 묻을 원본을 여기서 찾는다.
# ──────────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = Path(os.environ.get("NH_OUTPUT_ROOT") or PROJECT_ROOT / "outputs")
MEDIA_DIR = Path(os.environ.get("NH_MEDIA_DIR") or PROJECT_ROOT / ".media")
