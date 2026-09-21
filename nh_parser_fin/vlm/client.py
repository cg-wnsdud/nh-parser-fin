"""Gemma(OpenAI 호환 VLM) 클라이언트 — 설계서 6.2/6.5절.

- chat_json(): 모든 VLM 태스크가 공유하는 strict json_schema 호출 헬퍼 (재시도 포함)
- classify(): 문서 분류 — 파일명 prior + VLM 관측 결합, category_source 로 합의/충돌 기록
  (이전 프로젝트 검증 패턴)
"""

from __future__ import annotations

import base64
import io
import json
import re
import sys
import time
from dataclasses import dataclass

import requests
from PIL import Image

from . import cache as vlm_cache
from ..config import SETTINGS

# 단계별 VLM 호출 수·시간 집계 (schema_name 기준). 어느 단계가 비용을 쓰는지 재려면
# 호출 지점마다 세는 수밖에 없었는데, 그때그때 로그를 눈으로 세다 보니 매번 값이 달랐다.
# 운영에 영향 없는 순수 카운터라 항상 켜 둔다.
STATS: dict[str, dict[str, float]] = {}


def reset_stats() -> None:
    STATS.clear()


def _record(schema_name: str, seconds: float, cached: bool,
            attempts: int = 1, timeouts: int = 0) -> None:
    """한 `chat_json` 호출의 비용을 단계별로 적는다.

    `attempts`·`timeouts` 를 따로 세는 이유(2026-08-25). `seconds` 만 보면 "모델이 느리다"와
    "타임아웃 나서 재시도했다"를 구분할 수 없다. `chat_json` 은 실패 시 3회까지 시도하고
    (`retries=2`) 사이에 2초·4초를 쉬므로, **한 호출이 최악 366초**
    (120+2+120+4+120, `gemma_timeout_s=120`)까지 가는데 그게 `calls=1` 로 집계된다.
    실측: `13. 대출성상품.pdf` 이 "VLM 195초 11회 (캐시 10)" 로 찍혀 미캐시 1회가
    195초처럼 보였다 — 이 두 칸이 없으면 원인을 좁힐 수가 없다.
    """
    s = STATS.setdefault(
        schema_name,
        {"calls": 0, "cached": 0, "seconds": 0.0, "attempts": 0, "timeouts": 0},
    )
    s["calls"] += 1
    s["seconds"] += seconds
    s["attempts"] += attempts
    s["timeouts"] += timeouts
    if cached:
        s["cached"] += 1


def stats_table() -> str:
    """단계별 호출 수·시도 수·타임아웃·누적 시간 표 (시간 많이 쓴 순)."""
    if not STATS:
        return "(VLM 호출 없음)"
    rows = sorted(STATS.items(), key=lambda kv: -kv[1]["seconds"])
    out = [f"{'단계(schema_name)':30s} {'호출':>4s} {'캐시':>4s} {'시도':>4s} "
           f"{'타임아웃':>7s} {'초':>8s} {'초/시도':>8s}"]
    for name, s in rows:
        live = max(1, int(s["attempts"]) - int(s["cached"]))
        out.append(
            f"{name:30s} {int(s['calls']):4d} {int(s['cached']):4d} "
            f"{int(s['attempts']):4d} {int(s['timeouts']):7d} "
            f"{s['seconds']:8.1f} {s['seconds'] / live:8.1f}"
        )
    tot = {k: sum(s[k] for _, s in rows) for k in ("calls", "cached", "attempts", "timeouts", "seconds")}
    out.append(
        f"{'합계':30s} {int(tot['calls']):4d} {int(tot['cached']):4d} "
        f"{int(tot['attempts']):4d} {int(tot['timeouts']):7d} {tot['seconds']:8.1f}"
    )
    return "\n".join(out)


def chat_json(
    content_parts: list[dict],
    schema_name: str,
    schema: dict,
    max_tokens: int = 1500,
    retries: int = 2,
) -> dict:
    """VLM 에 멀티모달 메시지를 보내고 strict json_schema 로 강제된 JSON 을 받는다.

    strict json_schema 만 사용하는 이유: LiteLLM→vLLM 경로에서 실제로 강제되는
    유일한 구조화 출력 모드 (이전 프로젝트 실전 검증). 파싱은 방어적으로 수행.
    """
    # 개발용 결정론 캐시(기본 off). 같은 호출이면 저장된 응답을 재생해 A/B 에서
    # '내가 바꾼 단계'만 달라지게 한다 — vlm_cache 모듈 설명 참조.
    import time as _time
    _t0 = _time.time()
    cache_key = None
    if vlm_cache.enabled():
        cache_key = vlm_cache.key_for(
            content_parts, schema_name, schema, max_tokens, SETTINGS.gemma_model
        )
        cached = vlm_cache.load(cache_key)
        if cached is not None:
            vlm_cache.note("hit")
            _record(schema_name, _time.time() - _t0, cached=True)
            return cached
        vlm_cache.note("miss")
        if vlm_cache.replay_only():
            raise RuntimeError(
                f"VLM 캐시 미스(replay 모드): {schema_name} — 먼저 VLM_CACHE=r 로 기록하세요"
            )

    payload = {
        "model": SETTINGS.gemma_model,
        "temperature": 0,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content_parts}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        },
    }
    last_exc: Exception | None = None
    last_text: str | None = None
    timeouts = 0
    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                SETTINGS.gemma_url, json=payload, timeout=SETTINGS.gemma_timeout_s
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            text = _strip_fences(content)
            last_text = text
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                repaired = _repair_trailing_escape(text)
                if repaired is None:
                    # 잘린 응답은 같은 payload 로 다시 물어도 똑같이 잘린다.
                    # 재시도가 의미를 가지려면 출력 예산을 늘려야 한다. 한글은
                    # 토큰당 글자 수가 적어 영문 기준 예산에서 자주 잘린다.
                    payload["max_tokens"] = min(16000, int(payload["max_tokens"] * 1.6))
                    raise
                parsed = repaired
            if cache_key is not None:
                vlm_cache.store(cache_key, schema_name, parsed)
                vlm_cache.note("stored")
            _record(schema_name, _time.time() - _t0, cached=False,
                    attempts=attempt + 1, timeouts=timeouts)
            return parsed
        except Exception as exc:  # 연결 오류·JSON 파싱 실패 모두 재시도
            last_exc = exc
            # 타임아웃은 따로 센다 — 재시도 사유가 '서버가 안 끝냈다'인지
            # '응답이 깨졌다'인지에 따라 손볼 곳이 다르다(타임아웃 값 vs 스키마·토큰).
            if isinstance(exc, requests.Timeout):
                timeouts += 1
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    # 실패도 시간·호출은 썼다
    _record(schema_name, _time.time() - _t0, cached=False,
            attempts=retries + 1, timeouts=timeouts)
    raise RuntimeError(
        f"VLM 호출 실패({retries + 1}회, 타임아웃 {timeouts}회): "
        f"{last_exc}{_failure_excerpt(last_exc, last_text)}"
    )


def _failure_excerpt(exc: Exception | None, text: str | None, window: int = 60) -> str:
    """JSON 파싱 실패 시 문제 지점 원문 조각을 메시지에 붙인다.

    **왜 필요한가.** 실패 원인이 잘림인지 오발행 이스케이프인지 다른 것인지 로그만
    보고는 가를 수 없어서 추측으로 고치게 된다(2026-08-14 실측: 오발행이 여러 개라고
    보고 반복 제거를 넣었는데 17 → 14 건으로 3건만 줄었다 — 원문이 없어 진짜 원인을
    확인할 수 없었다). 조용한 실패 금지 원칙의 연장이다.

    원문 전체는 수천 자라 로그를 덮으므로 문제 지점 앞뒤만 잘라 붙인다.
    """
    if not isinstance(exc, json.JSONDecodeError) or not text:
        return ""
    pos = max(exc.pos, 0)
    start, end = max(0, pos - window), min(len(text), pos + window)
    return (
        f" | 응답 {len(text)}자, 오류 위치 {pos}"
        f", 앞뒤 원문: ...{text[start:end]!r}..."
    )


def _repair_trailing_escape(text: str, max_fixes: int = 8) -> dict | None:
    """guided-decoding 서빙 결함 보정 — 불필요한 역슬래시를 내보내고 그대로
    생성을 멈추는 경우가 실측됨(gemma-4-26b-NVFP4-MTP, finish_reason=stop,
    temperature=0 에서도 호출마다 미묘하게 다른 위치/길이로 재현).

    **문제 지점의 역슬래시를 먼저 지운다**(2026-08-12 수정). 예전에는 텍스트
    전체의 마지막 역슬래시만 지웠는데, 오발행이 문자열 **중간**에서 나고 그
    뒤에 정상 역슬래시(`\\n` 등)가 더 있으면 엉뚱한 것을 지워 복구에 실패했다.
    실측(new-sample-data 실행): `Invalid \\uXXXX escape: line 1 column 3244` 가
    3회 재시도 뒤 밴드 통독 실패로 떨어졌다.

    파싱 오류 위치(JSONDecodeError.pos) 이하에서 가장 가까운 역슬래시를 지워
    보고, 안 되면 예전 방식(마지막 역슬래시)으로 폴백한다. 둘 다 실패하면
    None(원래 예외 유지) — 여기서 만든 값은 호출측의 형식 가드로 다시 검증된다.

    **한 응답에 오발행이 여러 개 나온다**(2026-08-14 수정). 예전에는 한 개만
    지우고 끝냈다 — 합성 입력으로 확인: 오발행 1개는 살리고 2개부터 못 살린다.
    응답이 길수록 오발행이 겹칠 확률이 오르는데, 파싱 개선으로 밴드 통독에
    들어가는 텍스트가 늘면서 실측(new-sample-data 89건 재실행) 밴드 통독 실패가
    6 → 17건으로 늘었고 그중 16건이 `Invalid \\uXXXX escape` 였다. 그래서 살아날
    때까지 **반복해서** 지운다.

    상한(max_fixes)을 두는 이유: 응답이 escape 문제가 아니라 통째로 잘린 경우
    끝없이 지워도 안 살아난다. 그때는 None 을 돌려 호출측 재시도·분할에 맡긴다
    (truncation 대응은 이 함수 몫이 아니다).
    """
    cur = text
    for n in range(max_fixes):
        try:
            parsed = json.loads(cur)
            if n > 1:
                # 여러 개를 지워야 살아났다 = 정상 이스케이프까지 갉아먹었을 수 있다.
                # 조용히 넘기면 텍스트가 망가진 채 하류로 흘러가므로 소리를 낸다.
                print(
                    f"[VLM] 역슬래시 {n}개를 지워야 JSON 이 살아났다 — 텍스트 손상 가능",
                    file=sys.stderr,
                )
            return parsed
        except json.JSONDecodeError as exc:
            # exc.pos 는 깨진 이스케이프의 바로 뒤 문자를 가리킨다(`\u00` 이면 'u').
            idx = cur.rfind("\\", 0, max(exc.pos, 0) + 1)
            if idx == -1:
                break
            cur = cur[:idx] + cur[idx + 1:]
    # 위치 기반으로 못 살리면 예전 방식(맨 뒤 역슬래시 하나)으로 폴백
    idx = text.rfind("\\")
    if idx == -1:
        return None
    try:
        return json.loads(text[:idx] + text[idx + 1:])
    except Exception:
        return None


def image_part(
    image: Image.Image,
    box: tuple[int, int] = (896, 2400),
    quality: int = 85,
    image_format: str = "JPEG",
) -> dict:
    """캔버스를 VLM 입력용 축소 이미지 파트로 변환한다(비율 유지).

    기존 전체·밴드 호출은 JPEG 85를 유지한다. 숫자·미세 글자를 비교하는 영역 Reader와
    Judge는 ``image_format=\"PNG\"``로 손실 압축 없이 같은 crop을 보낼 수 있다.
    """
    img = image.copy()
    img.thumbnail(box)
    buf = io.BytesIO()
    fmt = image_format.strip().upper()
    if fmt == "PNG":
        img.save(buf, format="PNG")
        mime = "image/png"
    elif fmt in {"JPEG", "JPG"}:
        img.save(buf, format="JPEG", quality=quality)
        mime = "image/jpeg"
    else:
        raise ValueError(f"지원하지 않는 VLM 이미지 형식: {image_format}")
    url = f"data:{mime};base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": url}}


def _strip_fences(text: str) -> str:
    text = text.strip()
    match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    return match.group(1) if match else text


# ──────────────────────────── 문서 분류 ────────────────────────────

_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        # `analysis` 를 선두에 둔다 — 배열/열거가 스키마 앞에 오면 모델이 최소 유효 출력으로
        # 조기 종료하는 퇴행이 실측됐다(2026-07-16). 먼저 서술하게 하고 값을 뒤에 받는다.
        "analysis": {"type": "string"},
        "product_group": {
            "type": "string",
            "enum": ["예금성", "대출성", "카드", "투자성", "기타", "판단불가"],
        },
        "ad_type": {
            "type": "string",
            "enum": ["상세페이지", "안내장", "배너", "이벤트페이지", "기타"],
        },
        # 상품명이 광고에 드러나 있는가. 농협 광고 템플릿이 "상품명 노출/미노출"로
        # 갈리는데 그 갈림에서 필수항목 수가 12개 ↔ 3개로 바뀐다 — 템플릿 판정의
        # 핵심 갈림길이라 분류 단계에서 같이 관측한다(호출을 늘리지 않는다).
        "product_name_shown": {"type": "string", "enum": ["노출", "미노출", "판단불가"]},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": [
        "analysis", "product_group", "ad_type", "product_name_shown", "confidence", "reason",
    ],
    "additionalProperties": False,
}

_PROMPT = """당신은 금융상품 광고물 분류기입니다. 이미지를 보고 다음을 판정하세요.

1. analysis: 이미지에서 무엇이 보이는지 두 문장 이내로 먼저 서술하세요.
2. product_group: 광고하는 상품의 성격
   - 예금성 : 예금·적금·입출금 통장
   - 대출성 : 대출 상품
   - 카드   : 신용카드·체크카드, 카드사가 파는 장·단기 카드대출
   - 투자성 : ISA·퇴직연금·IRP·펀드·집합투자증권
   - 기타 / 판단불가
   ※ '카드대출'은 카드 상품이지 대출성이 아닙니다.
3. ad_type: 광고물 형태 — 상세페이지(세로 스크롤 웹/모바일), 안내장(인쇄물 형태), 배너, 이벤트페이지, 기타
4. product_name_shown: 특정 상품의 **고유 상품명**이 광고에 적혀 있는지
   - 노출   : "올원e적금", "NH직장인대출" 처럼 상품 이름이 보인다
   - 미노출 : 은행/카드사 브랜드만 있고 개별 상품명이 없다 (브랜드 홍보·이벤트 등)
   - 판단불가
   ※ "NH농협은행" 같은 회사명은 상품명이 아닙니다.

파일명 힌트: "{filename}"
파일명 힌트는 강한 사전확률입니다. 이미지에서 명확히 반대되는 증거가 보일 때만 뒤집으세요.
reason 은 한 문장으로."""


def filename_prior(filename: str) -> str | None:
    for group, keywords in SETTINGS.product_group_keywords.items():
        if any(kw in filename for kw in keywords):
            return group
    return None


@dataclass
class Classification:
    product_group: str | None
    ad_type: str | None
    confidence: float | None
    category_source: str
    reason: str = ""
    # 상품명 노출 여부. 템플릿 판정이 쓰지만 **여기서 템플릿을 정하지는 않는다** —
    # 관측만 넘기고 12종 중 무엇인지는 ad_template.resolve_template() 이 정한다.
    product_name_shown: str | None = None


def classify(canvas: Image.Image, filename: str) -> Classification:
    """파일명 prior 와 VLM 관측을 결합한다. **어느 경로로 정해졌는지를 값으로 남긴다.**

    2026-08-06: `category_source="filename"` 이 서로 다른 세 상황을 뭉치고 있었다.
    셋은 신뢰도가 완전히 다른데 값이 같아서 산출물만 보고 가를 수가 없었다:

      1. VLM 호출이 실패했다 (서버 죽음·타임아웃) → `filename_vlm_failed`
         파일명 말고는 근거가 없다. 재실행하면 달라질 수 있다.
      2. VLM 이 봤는데 "기타/판단불가"라고 답했다 → `filename_vlm_abstained`
         **VLM 이 반대 의견을 낸 것**이지 정보가 없는 게 아니다. 실측(003): VLM 이
         확신도 0.9 로 "특정 금융상품 가입 유도가 아니라 '올원모임' 서비스 이벤트"라고
         답했는데 파일명의 '예금성' 이 그 판단을 덮었다. 스키마 선택이 걸린 자리라
         (예금성 팩으로 심의) 검수자가 이 케이스를 알아야 한다.
      3. VLM 을 애초에 안 불렀다 (HWP — 캔버스가 없다) → `filename_no_vlm`
         hwp_ingest.py 가 붙인다. 여기 코드가 도는 경로가 아니다.

    §9-③ 의 '규칙폴백 2건 중 1건은 안 부른 것' 과 같은 종류의 혼동이다 —
    "폴백했다"와 "물어보지도 않았다"를 한 값에 담으면 실측을 세는 순간 틀린다.
    """
    prior = filename_prior(filename)
    try:
        data = chat_json(
            [
                {"type": "text", "text": _PROMPT.format(filename=filename)},
                image_part(canvas),
            ],
            schema_name="ad_classification",
            schema=_CLASSIFY_SCHEMA,
            max_tokens=300,
        )
    except Exception as exc:
        # VLM 실패 시 파일명 prior 로 안전 기본값 (이전 프로젝트 default_classification 패턴)
        return Classification(
            product_group=prior,
            ad_type=None,
            confidence=None,
            category_source="filename_vlm_failed" if prior else "none",
            reason=f"VLM 분류 실패, 파일명 prior 사용: {exc}",
        )

    raw_group = data.get("product_group")          # 기타/판단불가 를 지우기 전 원값
    vlm_group = None if raw_group in ("기타", "판단불가") else raw_group
    abstained = raw_group in ("기타", "판단불가")
    reason = str(data.get("reason", ""))

    if prior and vlm_group:
        source = "filename_and_vlm" if prior == vlm_group else "vlm_overrode_filename"
        group = vlm_group
    elif vlm_group:
        source, group = "vlm", vlm_group
    elif prior and abstained:
        source, group = "filename_vlm_abstained", prior
        # VLM 이 무엇을 근거로 반대했는지를 노트에 그대로 남긴다. 이 문장이 없으면
        # 산출물에서 "VLM 도 동의했다"와 구분이 안 된다 (003 이 그렇게 보였다).
        reason = (
            f"VLM 은 '{raw_group}' 로 판단(확신도 {data.get('confidence')})했고 "
            f"파일명 prior '{prior}' 를 적용했다. VLM 사유: {reason}"
        )
    elif prior:
        source, group = "filename_vlm_failed", prior
    elif abstained:
        source, group = "vlm_abstained", None
    else:
        source, group = "none", None
    shown = data.get("product_name_shown")
    return Classification(
        product_group=group,
        ad_type=data.get("ad_type"),
        confidence=data.get("confidence"),
        category_source=source,
        reason=reason,
        product_name_shown=None if shown == "판단불가" else shown,
    )
