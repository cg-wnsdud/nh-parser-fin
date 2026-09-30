# nh-parser-fin

농협 금융 광고물(PDF·PNG/JPG·HWP/HWPX)을 읽어 **심의 단계가 사용할 영역 단위 JSON**으로
바꾸는 파싱 저장소입니다. 각 문구의 페이지 위치와 구분값을 연결하므로, 심의 결과의
`region_ids`로 입력 화면의 실제 위치를 표시할 수 있습니다. 입력 처리·PaddleX 호출·
후처리·VLM 판독·P1/P3 변환 로직을 이 저장소 안에 포함합니다.

이 저장소는 광고 위반 여부를 판정하지 않습니다. 여기서 말하는 `needs_review`는
파싱 결과를 원문과 대조할 필요가 있다는 **품질 신호**입니다.

## 결과물 설명

파이프라인은 문서마다 두 결과를 만듭니다.

- **P1 근거 데이터**: OCR/PDF/HWP 구조/VLM이 본 내용, 후보 텍스트, 좌표 출처, 품질 경고를 보존합니다.
- **P3 심의 입력**: 심의 단계에 필요한 최종 영역만 간결하게 전달합니다.

P1과 P3의 같은 영역은 `region_id`로 연결됩니다. 최종 ID 형식은 페이지별
`pN_rNNN`으로 통일됩니다. 조립 중 사용한 `pN_xNNN` 같은 ID와 영역 출처는 P1의
`source_region_id`, `origin`, `region_id_map`에 남고 P3에는 노출되지 않습니다.

```json
{
  "region_id": "p1_r020",
  "product_id": "product_1",
  "bbox": [243, 518, 1570, 708],
  "selected_text": "가입대상: 개인 및 개인사업자",
  "labels": ["가입대상"],
  "kind": "text",
  "needs_review": false,
  "text_source": "vlm"
}
```

표는 PDF·이미지의 Paddle `table` 영역과 페이지 VLM이 찾은 누락 후보를 다시
검증합니다. 전체 페이지에서 후보 위치를 표시한 이미지, 후보 주변 확대 이미지,
기존 Region ID·bbox·텍스트를 VLM에 함께 전달합니다. 한 심의 항목에 속하는 표로
확인된 경우에만 기존 Region의 `kind`를 `table`로 설정합니다. 여러 Region에 흩어진
표는 기존 bbox의 합집합을 사용해 한 Region으로 묶습니다. 확인된 시각 표는 VLM에
HTML(`rowspan`/`colspan`)로 받아 병합이 풀린 격자로 바꾸고, 행마다 칸 수가 같고 원래
문구를 충분히 담으면 **마크다운 표**를 `selected_text`로 씁니다. 격자가 맞지 않으면
전체 영역의 VLM Judge 판독(`항목 | 값` 줄 나열)으로 되돌리고 검수 대상으로 표시합니다.
원문 조립본과 격자는 P1에 남고, P3에는 별도 표 필드를 추가하지 않습니다.
HWP 원본/HTML에서 확인된 셀은 P1의 `table.cells`에 남습니다. 후보·거부 사유는
P1의 `table_checks`에 남고, P3는 `kind=table`, 텍스트, bbox를 전달합니다.

예를 들어 시각적으로 검증된 금리 표는 P3에서 다음처럼 나옵니다.

```json
{
  "region_id": "p1_r020",
  "bbox": [243, 518, 1570, 708],
  "selected_text": "구분 | 금리\n기본 | 3.0%",
  "kind": "table",
  "text_source": "vlm",
  "labels": ["금리"]
}
```

심의 결과가 `region_ids`를 돌려주면 P3의 `bbox`로 해당 위치를 표시할 수 있습니다.

## 전체 처리 흐름

```text
PDF · 이미지 · HWP/HWPX
  ↓ 1. 파일별 페이지 이미지 생성 + 가능한 원본 텍스트/표 구조 추출
  ↓ 2. 시각 경로는 긴 페이지를 타일로 분할하고 PaddleX 레이아웃·OCR 실행
  ↓ 3. 타일 좌표 복원·중복 제거·원본/Paddle 줄을 Region에 귀속
  ↓ 4. VLM 문서 분류 → 페이지별 상품 소유권·누락 줄·표 후보 판단
  ↓ 5. HWP 원본 셀 보존, 영역별 VLM 판독과 근거 대조
  ↓ 6. Paddle/페이지 VLM 표 후보 검증 → 필요시 표 전체 판독
  ↓ 7. 상품별 템플릿 선택 → 영역별 구분값 라벨링
  ↓ 8. 페이지별 최종 region_id 정규화
P1 근거 데이터 + P3 심의 입력 (+ compact 실행 시 HTML 보고서)
```

### 입력 형식에 따른 분기

| 입력 | 페이지 이미지와 텍스트 근거 | 레이아웃/OCR |
|---|---|---|
| PDF | PDFium으로 렌더하고, 읽을 수 있는 텍스트 레이어가 있으면 PDFium에서 줄과 좌표 추출 | **모든 PDF 페이지**를 PaddleX에 전달. 디지털 텍스트가 있어도 이미지 속 글자를 보완 |
| 이미지 | 입력 이미지를 한 페이지로 사용. 디지털 텍스트 없음 | PaddleX에 전달 |
| HWP/HWPX | `document-processor` 우선·Kordoc 폴백으로 원본 문단/표 파싱. 로컬 HTML/Chromium 우선·LibreOffice 폴백으로 PDF 렌더 | HTML 행과 위치가 있으면 기본 `hybrid`, 필요시 `structured_fast`; 없으면 일반 시각 경로 |

PDF는 텍스트 레이어 상태를 `structured`·`hybrid`·`scan_like`로 기록합니다.
`scan_like`에는 믿을 만한 디지털 줄이 없어 OCR에 의존하지만, 이 판정이 PaddleX를
생략시키지는 않습니다. PDF 표에는 HWP처럼 원본 셀 객체가 없는 경우가 많으므로
셀 관계를 확정했다고 간주하지 않습니다.

HWP의 HTML 행이 충분하면 기본 `hybrid`에서는 원본 행을 기본 Region으로 두고,
PaddleX로 로고·이미지 속 글자 같은 새로운 시각 요소만 보완합니다. HTML에 문단·표
행이 없으면 일반 시각 경로를 사용합니다. `HWP_VISUAL_AUGMENT=off` 또는 빈 구조
페이지의 `structured_fast`에서는 PaddleX를 생략할 수 있습니다. HWP의 실제 원본
표 셀은 P1에 보존합니다. 한컴오피스 COM이나 한컴 서버는 사용하지 않습니다.

### Region과 VLM 판단

일반 시각 경로는 종횡비가 큰 페이지를 글자 밀도를 참고해 타일로 나눕니다. PaddleX가
반환한 레이아웃 영역·OCR 줄을 원래 페이지 좌표로 옮기고 중복을 제거합니다.
OCR 줄을 위치에 따라 Region에 한 번씩 귀속하고, 미배정 줄은 복구 후보로 남깁니다.
PDF 디지털 줄은 정본으로 쓰지 않고 판독이 끝난 뒤 글자 교정 재료로 씁니다(아래 3번).
그다음 VLM은 목적별로 나뉘어 동작합니다.

1. **분류:** 첫 페이지 이미지와 파일명 힌트로 상품군, 광고 유형, 상품명 노출 여부를 판단합니다.
2. **페이지 맥락:** 페이지 이미지와 기존 Region ID·텍스트·bbox를 보고 상품 소유권,
   미배정 줄 복구 여부, PaddleX가 놓친 표 후보를 판단합니다. 긴 페이지는 밴드별로
   확인합니다. 새 좌표 없이 추측한 글자를 P3 Region으로 만들지 않습니다.
3. **영역 판독:** 페이지 Reader가 페이지 이미지와 ID 박스 그림, Region ID·bbox 목록
   (Paddle 문구는 주지 않음)으로 최대 20개 Region을 한 번에 전사합니다. 긴 페이지는
   밴드로 나눕니다. 판독에서 빠졌거나 비었는데 OCR에 글자가 있는 Region만 crop으로
   다시 읽습니다. OCR 문구와 공백·기호를 뺀 글자가 한 자라도 다르면 Judge가 영역
   이미지로 두 후보를 비교합니다. Judge에는 후보 출처를 숨기고 용어를 바꿔 쓰지 말라고
   지시합니다. **HWP 원본 구조와 일치하는 영역**은 VLM이 다르게 읽어도 원문을 보존합니다.
   PDF는 판독이 끝난 뒤 최종 문구의 한글 낱말·숫자를 근처 디지털 글자에 맞춰 교정하고,
   양옆이 맞는 짧은 차이(빠진 각주 표시·덧붙인 말)를 디지털로 메우며, 페이지 어디에도
   없는 디지털 줄은 끼워 넣고 검수 대상으로 표시합니다. 순서·띄어쓰기는 VLM을 따릅니다.
4. **표 검증:** Paddle `table`과 페이지 VLM이 지목한 후보에 대해 전체 페이지의 후보
   위치 이미지, 후보 확대 이미지, Region ID·bbox·문구를 다시 제공합니다. VLM은 실제
   한 심의 항목의 표인지와 포함할 기존 ID만 고릅니다. 확인된 시각 표는 HTML로 다시 읽어
   마크다운 문구를 만들고 `kind=table`로 표시합니다. 거부된 후보는 원래 Region으로 남고
   이유를 P1에 기록합니다. `relation`, `field_list`, `p1_s...`는 만들지 않습니다.
5. **상품별 템플릿과 라벨:** 상품 소유권을 먼저 확정한 뒤 상품군과 상품명 노출 여부를
   교차 확인해 템플릿을 고릅니다. 라벨 VLM에는 그 템플릿의 허용 구분값, 정의,
   긍정·부정 예시와 Region 문구/이미지를 제공합니다. 모델이 각 라벨의 **정확한 근거
   문구**를 제시하도록 하고, 코드가 허용값과 근거를 검증합니다. `가입대상`처럼 명시된
   표제어는 결정론 규칙으로 보완합니다. 한 Region에 복수 라벨이 붙을 수 있습니다.

정형 문서도 현재는 이 공통 흐름을 탑니다. 전체 페이지를 한 번에 P3로 바꾸는
별도 지름길은 없습니다. VLM은 기존 Region과 페이지 이미지를 바탕으로 의미를
판단하며 **새 bbox를 생성하지 않습니다.** 한 영역에 `가입대상`과 `가입금액`이 실제로
함께 있으면 영역을 유지하고 두 라벨을 붙입니다.

세부 단계와 VLM 호출 위치는 [docs/PIPELINE.md](docs/PIPELINE.md), 전체 입출력과
운영 근거는 [docs/HANDOVER.md](docs/HANDOVER.md)를 참고하세요.

## 외부 서비스와 환경변수

이 저장소가 모델을 직접 띄우지는 않습니다. 아래 두 HTTP 서비스가 필요합니다.

1. **PaddleX PP-StructureV3**: 페이지 또는 타일 이미지에서 레이아웃·OCR·표 후보를 반환
2. **이미지 입력 VLM**: OpenAI 호환 `chat/completions`로 분류·소유권·판독·표 검증·라벨링 수행

```bash
cp .env.example .env
```

`.env`에서 다음 값을 실제 환경에 맞게 채웁니다. `.env`는 Git에 포함되지 않습니다.

| 변수 | 용도 |
|---|---|
| `PADDLEX_URL` | PP-StructureV3 `/layout-parsing` 주소 |
| `PADDLEX_TIMEOUT` | PaddleX 요청 제한 시간(초) |
| `GEMMA_URL` | OpenAI 호환 `/v1/chat/completions` 주소 |
| `GEMMA_MODEL` | VLM 서비스에 요청할 모델 이름 |
| `GEMMA_TIMEOUT_S` | VLM 요청 제한 시간(초) |
| `PARSER_V2_ASPECT_LIMIT` | 통짜/타일 분할 기준 종횡비 |
| `PARSER_V2_TILE_SPAN` | 타일 목표 길이(px) |
| `PARSER_V2_ENCODE` | PaddleX 전송 이미지 형식(`jpeg` 또는 `png`) |
| `KORDOC_COMMAND`, `KORDOC_VERSION`, `KORDOC_TIMEOUT` | HWP 구조 파서 폴백 명령·버전·제한 시간 |
| `HWP_RENDER_BACKEND` | HWP 렌더 선택: `auto`(HTML→LibreOffice), `html`, `libreoffice` |
| `HWP_RENDER_TIMEOUT`, `HWP_RENDER_DIR` | HWP→PDF 제한 시간과 선택적 중간 PDF 보존 위치 |
| `NH_OUTPUT_ROOT` | 실행 결과 저장 위치(선택) |
| `NH_MEDIA_DIR` | 렌더한 페이지 이미지 저장 위치(선택) |
| `VLM_CACHE`, `VLM_CACHE_DIR` | 개발용 VLM 응답 캐시 정책과 위치(선택) |

판독 방식은 아래 값으로 바꿀 수 있습니다. 비워 두면 굵게 표시한 기본값을 씁니다.
이전 방식(디지털 줄 정본 + Region별 Reader + `항목 | 값` 표)은
`PARSER_V2_DIGITAL_MODE=primary PARSER_V2_READER_MODE=region PARSER_V2_JUDGE_TRIGGER=ratio
PARSER_V2_JUDGE_PROMPT=default PARSER_V2_TABLE_FORMAT=pipe`입니다.

| 변수 | 값 |
|---|---|
| `PARSER_V2_DIGITAL_MODE` | **`anchor`** PDF 디지털 글자를 교정 재료로 사용 · `primary` 디지털 줄을 정본으로 사용 · `off` 사용 안 함 |
| `PARSER_V2_READER_MODE` | **`page`** 페이지 단위 Reader · `region` Region crop마다 Reader |
| `PARSER_V2_PAGE_READER_TEXT` | **`off`** 페이지 Reader에 ID·bbox만 · `on` Paddle 문구도 참고로 전달 |
| `PARSER_V2_JUDGE_TRIGGER` | **`strict`** 글자·숫자가 한 자라도 다르면 Judge · `ratio` 일치도 0.95 미만일 때만 |
| `PARSER_V2_JUDGE_PROMPT` | **`blind`** 후보 출처를 숨긴 프롬프트 · `default` 이전 프롬프트 |
| `PARSER_V2_TABLE_FORMAT` | **`html`** 시각 표를 HTML→마크다운으로 · `pipe` 표 Judge 줄 나열만 |
| `PARSER_V2_REVIEW_RULES` | **`default`** · `ocr` 디지털 원문 없이 판독 품질 검수 사유 추가(오탐 많음) |

서비스가 터널을 통해서만 접근 가능하다면 실행 환경에서 터널을 열고 `PADDLEX_URL`과
`GEMMA_URL`을 그 터널 주소로 지정합니다. 현재 서비스의 실제 호스트명이나 모델 버전은
코드에 고정되어 있지 않습니다. PaddleX 요청은 이미지와 `fileType=1`만 보내므로
레이아웃 임계값, 표 검출/인식 모듈의 활성화 같은 추론 설정은 **서버의 파이프라인 YAML**이
결정합니다.

VLM 캐시는 기본적으로 꺼져 있습니다. 개발용 `VLM_CACHE=r`은 기존 응답을 재사용하고
새 응답을 기록합니다. `VLM_CACHE=p`는 캐시만 재생하며 없으면 실패합니다. 신규 샘플을
실제 모델로 확인할 때에는 캐시를 비워 실행합니다.

## 설치와 실행

Python 3.11을 기준으로 검증합니다. 3.12와 3.13에서도 같은 결과를 확인했습니다.

```bash
uv sync --dev
# 또는
python -m pip install -e .
python -m pip install pytest
```

HWP 입력에 쓰는 사내 `document-processor`는 `requires-python >=3.13`으로 선언돼 있어
3.11에서는 일반 설치가 거부됩니다. 코드는 3.11에서 동작하므로 버전 검사만 건너뛰어
설치합니다. 명령은 [docs/HWP_INPUT.md](docs/HWP_INPUT.md)를 참고하세요.

전체 파이프라인을 실행하려면(페이지 조립 뒤 VLM 판정과 P1/P3 생성까지 수행):

```bash
uv run python run.py --run-name full-check --compact-output --input "samples/samples-new"
```

PaddleX를 재호출하지 않고 저장된 박스로 Region 조립만 다시 확인하려면 **먼저
`--compact-output` 없이 실행해** `boxes/`를 저장한 뒤:

```bash
uv run python replay.py --from full-check-debug --run-name replay-check
```

`--input`에는 파일 또는 폴더를 여러 개 줄 수 있습니다. 폴더를 지정하면 그 폴더의
**바로 아래 파일** 중 지원 형식을 수집합니다. `--exclude`로 이름에 특정 문자열이
포함된 입력을 제외할 수도 있습니다. 기본 `--sizing asis`는
이미지의 원본 픽셀을 유지하고 PDF를 설정된 크기 정책으로 렌더합니다.
`--sizing maxside --max-side 2500`은 긴 변을 제한하는 비교 실험용입니다.

`--compact-output`의 최종 파일은 다음 위치에 생성됩니다. 여러 입력을 한 번에 실행하면
`report.html`에서 모든 파일의 P3 영역과 페이지 이미지를 함께 확인할 수 있습니다.

```text
outputs/<run-name>/final/<문서명>.p1.json
outputs/<run-name>/final/<문서명>.p3.json
outputs/<run-name>/images/*.png
outputs/<run-name>/report.html
outputs/<run-name>/manifest.json
```

`--compact-output`을 빼면 중간 확인용 `raw/`, `boxes/`, `pages/`와 전체 묶음
`05-p1.json`·`06-p3.json`, Label Studio 자료도 저장합니다. `replay.py`는 저장된
`boxes/`에서 **Region 조립만** 다시 검사하며 VLM까지 전체 재실행하는 명령은 아닙니다.

## P3 인계 계약

P3의 `pages[].regions[]`가 심의의 기본 근거입니다.

| 필드 | 의미 |
|---|---|
| `region_id` | P1 재조회와 화면 하이라이트에 쓰는 `pN_rNNN` ID |
| `product_id` | 여러 상품이 한 페이지에 있을 때의 상품 소유권 |
| `bbox` | 렌더된 페이지 픽셀 좌표 `[x1, y1, x2, y2]` |
| `selected_text` | OCR/PDF/HWP 구조와 VLM 판독을 거쳐 선택된 최종 텍스트 |
| `labels` | 해당 영역의 템플릿 구분값 목록. 복수 허용 |
| `kind` | `text` 또는 `table` |
| `needs_review` | 파싱 품질상 원문 대조가 필요한지 여부 |
| `text_source` | 최종 텍스트 출처. `hwp`, `digital`, `ocr`, `vlm` 중 하나 |

`needs_review`는 광고의 위반 여부가 아닙니다. OCR/VLM 불일치, 낮은 판독
확신도 같은 **파싱 품질 신호**입니다. 구체적인 사유는 같은 `region_id`의 P1
`review_reasons`에서 확인합니다. 실제 심의 결과는 `위반`, `판정불가`, `충족`과 그 근거
`region_ids`를 별도로 반환합니다.

## 저장소 구조

```text
run.py                    전체 실행 진입점
replay.py                 저장된 PaddleX 박스로 Region 조립만 재실행
nh_parser_fin/
  config.py               엔드포인트, 렌더·타일·판독 임계값
  ingest/                 PDF·이미지·HWP 입력과 페이지 렌더
  ocr/                    PaddleX HTTP 호출, 타일 분할·좌표 복원
  parse/                  Region 조립, 복구, 표, 판독, 라벨링, P1/P3
  review/                 금융광고 템플릿 카탈로그와 선택 규칙
  templates/              19개 광고 템플릿 JSON
  vlm/                    OpenAI 호환 VLM 호출과 응답 캐시
tests/                    외부 서버 없이 실행하는 단위 테스트
docs/PIPELINE.md           단계별 데이터 흐름과 설계 근거
docs/HWP_INPUT.md          HWP 렌더·구조 결합 경로와 실행 환경
```

HWP/HWPX 입력 경로는 현재 `main`에 포함되어 있습니다. `document-processor`의 원본
구조와 HTML/Chromium 렌더를 우선 사용하고, 구조 파싱에는 Kordoc, 렌더에는
LibreOffice를 각각 폴백으로 사용합니다. `document-processor`는 별도의 사내 패키지라
공개 필수 의존성에 포함되지 않습니다. 렌더된 페이지의 bbox는 PDFium/HTML 위치와
필요시 PaddleX 시각 경로에 맞춰 만듭니다. 설치형 한컴오피스는 호출하지 않습니다.
HWP 환경이 없어도 PDF와 이미지 경로는 동작합니다.
세부 흐름과 제약은 [docs/HWP_INPUT.md](docs/HWP_INPUT.md)를 참고하세요.

## 검증

```bash
uv run pytest -q
```

테스트는 외부 PaddleX/VLM 서버 없이 Region 귀속, 타일 경계 중복 제거, 표 후보 검증,
OCR/VLM 텍스트 선택, 복수 라벨, 상품 소유권, 최종 ID와 P1/P3 계약을 확인합니다.
**실제 인식 품질**은 모델 서비스에 연결해 샘플을 실행한 뒤 파일별 P1/P3,
`report.html`, `needs_review`와 원본 페이지를 함께 대조해야 합니다.
