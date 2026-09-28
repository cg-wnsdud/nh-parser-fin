"""현재 파이프라인의 로컬 임계값, VLM 엔드포인트, PaddleX 실행 프로필.

PaddleX 추론 모듈과 세부 파라미터는 서버 YAML이 결정한다.

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
    """현재 PDF 추출, VLM 호출, 분류에 사용되는 공통 설정.

    PaddleX 요청 옵션과 타일 기준은 ``Profile`` 및 서버 YAML이 담당한다.
    """

    gemma_url: str = os.environ.get(
        "GEMMA_URL", "http://YOUR_GEMMA_HOST:4000/v1/chat/completions"
    )
    gemma_model: str = os.environ.get("GEMMA_MODEL", "YOUR_MODEL_NAME")
    gemma_timeout_s: int = int(os.environ.get("GEMMA_TIMEOUT_S", "120"))

    min_readable_chars: int = 20
    max_fffd_ratio: float = 0.3
    min_fffd_count: int = 8
    hybrid_image_area_ratio: float = 0.5

    pdf_render_dpi: int = int(os.environ.get("PDF_RENDER_DPI", "200"))
    tile_overlap_px: int = int(os.environ.get("TILE_OVERLAP_PX", "200"))
    vlm_band_ratio: float = 2.0
    vlm_band_min_height_px: int = 900
    vlm_band_snap_px: int = 160

    product_group_keywords: dict = field(default_factory=lambda: {
        "카드": ["카드상품", "카드"],
        "투자성": ["투자성", "ISA", "개인종합자산관리계좌", "IRP", "퇴직연금", "펀드"],
        "예금성": ["예금성", "예금", "적금", "입출금"],
        "대출성": ["대출성", "대출", "신용대출", "담보"],
    })


SETTINGS = Settings()


# ──────────────────────────────────────────────────────────────────────────
# 실행 프로필 — 한 번의 실행을 재현하는 데 필요한 값만 모은다.
#
# `Settings` 와 나눠 둔 이유: 아래 값들은 **실행마다 달라질 수 있는** 것들이라 `manifest.json` 에
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
