# nh-parser-fin

농협 금융 광고물(PDF·PNG/JPG·HWP/HWPX)을 읽어 **심의 단계가 사용할 영역 단위 JSON**으로
바꾸는 독립 파싱 저장소입니다. 다른 프로젝트의 Python 코드를 import하지 않으며, 필요한
입력 처리·PaddleX 호출·후처리·VLM 판독·P1/P3 변환 로직을 이 저장소 안에 포함합니다.

## 결과를 먼저 이해하기

파이프라인은 문서마다 두 결과를 만듭니다.

- **P1 근거 데이터**: OCR/PDF/VLM이 본 내용, 후보 텍스트, 좌표 출처, 품질 경고를 보존합니다.
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

표 영역은 같은 객체에 `table.grid`, `table.cells`, `table.notes`가 추가됩니다. 심의 결과는
`region_ids`를 돌려주면 화면에서 P3의 `bbox`를 사용해 해당 위치를 표시할 수 있습니다.

## 전체 처리 흐름

```text
입력 파일
  ↓ 1. 페이지 렌더 및 PDF 텍스트 추출
페이지 이미지 + 디지털 텍스트 줄
  ↓ 2. 일반 페이지는 통째로, 세로로 긴 페이지는 글자 밀도 경계로 타일 분할
  ↓ 3. spark-1118 PP-StructureV3 호출
레이아웃 영역 + OCR 줄
  ↓ 4. 타일 좌표를 페이지 좌표로 복원하고 경계 중복 제거
  ↓ 5. OCR/PDF 줄을 영역에 한 번만 귀속하고 미배정 줄을 복구 후보로 보존
  ↓ 6. VLM 문서 분류 및 상품 소유권 판정
  ↓ 7. 표 구조 복원
  ↓ 8. 영역별 VLM 전사와 OCR 대조, 불일치 시 Judge가 최종 텍스트 선택
  ↓ 9. 상품별 심의 템플릿 결정 및 영역별 복수 구분값 라벨링
  ↓ 10. 최종 region_id 정규화
P1 근거 데이터 + P3 심의 입력
```

영역을 구분값마다 다시 자르지 않습니다. 한 영역에 `가입대상`과 `가입금액`이 함께 있으면
그 영역을 유지하고 `labels`에 두 값을 붙입니다. 좌표는 PaddleX 레이아웃 또는 OCR/PDF
텍스트 줄에서만 만들며 VLM이 좌표를 새로 생성하지 않습니다.

세부 단계와 VLM 호출 위치는 [docs/PIPELINE.md](docs/PIPELINE.md)를 참고하세요.

## 외부 서비스와 환경변수

이 저장소가 모델을 직접 띄우지는 않습니다. 아래 두 HTTP 서비스가 필요합니다.

1. **PaddleX PP-StructureV3**: spark-1118의 PaddleX 3.6.1 / PaddleOCR 3.6.0
2. **Gemma VLM**: fc87의 OpenAI 호환 `chat/completions` 엔드포인트

```bash
cp .env.example .env
```

`.env`에서 다음 값을 실제 환경에 맞게 채웁니다. `.env`는 Git에 포함되지 않습니다.

| 변수 | 용도 |
|---|---|
| `PADDLEX_URL` | PP-StructureV3 `/layout-parsing` 주소 |
| `PADDLEX_TIMEOUT` | PaddleX 요청 제한 시간(초) |
| `GEMMA_URL` | OpenAI 호환 `/v1/chat/completions` 주소 |
| `GEMMA_MODEL` | fc87에서 서빙하는 모델 이름 |
| `GEMMA_TIMEOUT_S` | VLM 요청 제한 시간(초) |
| `PARSER_V2_ASPECT_LIMIT` | 통짜/타일 분할 기준 종횡비 |
| `PARSER_V2_TILE_SPAN` | 타일 목표 길이(px) |
| `PARSER_V2_ENCODE` | PaddleX 전송 이미지 형식(`jpeg` 또는 `png`) |
| `NH_OUTPUT_ROOT` | 실행 결과 저장 위치(선택) |
| `NH_MEDIA_DIR` | 렌더한 페이지 이미지 저장 위치(선택) |
| `VLM_CACHE`, `VLM_CACHE_DIR` | VLM 응답 캐시 정책과 위치(선택) |

spark-1118의 8081 포트가 서버 내부 loopback에만 열려 있다면 실행 PC에서 터널을 유지합니다.

```bash
ssh -N -L 18081:127.0.0.1:8081 spark-1118
```

이 상태에서 `PADDLEX_URL=http://127.0.0.1:18081/layout-parsing`을 사용합니다. OCR 요청은
`fileType`만 보내므로 레이아웃 threshold, merge 모드, 모듈 on/off 같은 추론 설정은
spark-1118의 파이프라인 YAML이 결정합니다.

## 설치와 실행

Python 3.13을 기준으로 검증합니다.

```bash
uv sync --dev
# 또는
python -m pip install -e .
python -m pip install pytest
```

PaddleX까지만 실행해 영역 조립을 확인하려면:

```bash
uv run python run.py --run-name ocr-check --input "samples/sample.pdf"
```

VLM 후처리와 P1/P3까지 실행하려면:

```bash
uv run python run.py --run-name full-check --with-vlm --input "samples/sample.pdf"
```

`--input`에는 파일 또는 폴더를 여러 개 줄 수 있습니다. 기본 `--sizing asis`는 이미지의
원본 픽셀을 유지하고 PDF를 정해진 DPI로 렌더합니다. `--sizing maxside --max-side 2500`은
긴 변이 지정값을 넘지 않도록 축소하는 비교 실험용입니다.

최종 파일은 다음 위치에 생성됩니다.

```text
outputs/<run-name>/05-p1.json
outputs/<run-name>/06-p3.json
outputs/<run-name>/final/<문서명>.p1.json
outputs/<run-name>/final/<문서명>.p3.json
```

## P3 인계 계약

P3의 `pages[].regions[]`가 심의의 기본 근거입니다.

| 필드 | 의미 |
|---|---|
| `region_id` | P1 재조회와 화면 하이라이트에 쓰는 `pN_rNNN` ID |
| `product_id` | 여러 상품이 한 페이지에 있을 때의 상품 소유권 |
| `bbox` | 렌더된 페이지 픽셀 좌표 `[x1, y1, x2, y2]` |
| `selected_text` | OCR/PDF와 VLM 판독을 거쳐 선택된 최종 텍스트 |
| `labels` | 해당 영역의 템플릿 구분값 목록. 복수 허용 |
| `kind` | `text` 또는 `table` |
| `needs_review` | 파싱 품질상 원문 대조가 필요한지 여부 |
| `text_source` | 최종 텍스트가 `ocr` 계열인지 `vlm`인지 |
| `table` | 표인 경우의 행·열·셀·주석 구조 |

`needs_review`는 광고의 위반 여부가 아닙니다. OCR/VLM 불일치, 표 셀 미배치, 낮은 판독
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
  vlm/                    fc87 Gemma 호출과 응답 캐시
tests/                    외부 서버 없이 실행하는 단위 테스트
docs/PIPELINE.md           단계별 데이터 흐름과 설계 근거
```

HWP/HWPX 입력은 사내 `document-processor` 패키지가 추가로 필요합니다. 이 패키지가 없어도
PDF와 이미지 경로는 동작하며, HWP를 읽을 때만 명시적인 import 오류가 발생합니다.

## 검증

```bash
uv run pytest -q
```

테스트는 외부 PaddleX/VLM 서버를 호출하지 않고 Region 귀속, 타일 경계 중복 제거, 표 복원,
OCR/VLM 텍스트 선택, 복수 라벨, 상품 소유권, 최종 ID와 P1/P3 계약을 확인합니다.
