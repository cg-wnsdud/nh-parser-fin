"""VLM 응답 캐시 — A/B 를 결정론적으로 만들기 위한 개발용 장치.

**왜 필요한가.** 파이프라인에는 비결정적 VLM 호출이 문서당 30여 개 있다. 그 상태로
한 단계만 바꿔 전체를 돌려 골드로 채점하면, 바꾼 것의 효과보다 실행 간 변동이 커서
매번 "변동성 때문에 판정 불가"로 끝난다(실측: 값 교체가 0건인 단계를 껐는데도 문장
2건·필드 2건이 달라졌고, 달라진 항목은 전부 그 단계와 무관한 헤드라인이었다).

캐시를 켜면 (프롬프트 + 이미지 + 스키마)가 같은 호출은 저장된 응답을 그대로 재생하므로,
A/B 에서 달라지는 것은 '내가 바꾼 단계'뿐이 된다. 덤으로 재실행이 25분에서 수십 초로
줄어 반복 검증이 가능해진다.

**운영에서는 끈다.** 기본 off 이고 환경변수로만 켠다 — 캐시된 응답을 실서비스에 쓰면
광고가 바뀌어도 옛 판독을 재생하게 된다.

    VLM_CACHE=r  기록(record): 실제 호출하고 응답을 저장. 캐시에 있으면 재사용
    VLM_CACHE=p  재생(replay): 캐시에 있으면 재생, 없으면 실패 — 완전 결정론
    (미설정)     캐시 없음 (기본, 운영)
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

_MODE = os.environ.get("VLM_CACHE", "").lower()[:1]  # '' | 'r' | 'p'
_DIR = Path(os.environ.get("VLM_CACHE_DIR", ".vlm_cache"))


def enabled() -> bool:
    return _MODE in ("r", "p")


def replay_only() -> bool:
    return _MODE == "p"


def key_for(content_parts: list[dict], schema_name: str, schema: dict,
            max_tokens: int, model: str) -> str:
    """호출을 특정하는 해시. 이미지는 base64 원문까지 포함해야 크롭이 1px 달라도 구분된다."""
    h = hashlib.sha256()
    for part in content_parts:
        if part.get("type") == "text":
            h.update(b"T"); h.update(part["text"].encode("utf-8"))
        else:  # image_url — data URI 전체가 곧 픽셀 내용
            h.update(b"I"); h.update(part["image_url"]["url"].encode("utf-8"))
    h.update(b"S"); h.update(schema_name.encode())
    h.update(json.dumps(schema, sort_keys=True, ensure_ascii=False).encode())
    h.update(f"|{max_tokens}|{model}".encode())
    return h.hexdigest()


def _path(key: str) -> Path:
    return _DIR / key[:2] / f"{key}.json"


def load(key: str) -> dict | None:
    p = _path(key)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))["response"]
    except Exception:
        return None  # 깨진 캐시는 없는 것으로 취급하고 다시 호출한다


def store(key: str, schema_name: str, response: dict) -> None:
    p = _path(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"schema_name": schema_name, "response": response},
                   ensure_ascii=False),
        encoding="utf-8",
    )


# 2026-08-06 제거: `stats()`. "캐시 적중/저장 현황 확인용" 이었으나 부르는 곳이 없었다.
# 같은 정보가 gemma_client.STATS 의 `cached` 열로 러너 표에 이미 찍힌다.
_STATS = {"hit": 0, "miss": 0, "stored": 0}


def note(kind: str) -> None:
    if kind in _STATS:
        _STATS[kind] += 1
