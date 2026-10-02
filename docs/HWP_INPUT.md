# HWP/HWPX 입력 처리

현재 개발 브랜치 `feat/hwp-input-render`의 실행 경로를 설명합니다. 진입점은 PDF·이미지와 같은 `run.py`입니다.

## 페이지와 구조 텍스트

1. `ingest/hwp_structure.py`가 사내 `document-processor`로 문단·원본 표 셀을 읽습니다. 사용할 수 없거나 파싱에 실패하면 Kordoc으로 폴백합니다. `HWP_STRUCTURE_ENGINE`으로 한 경로만 지정할 수도 있습니다.
2. `ingest/hwp_render.py`가 화면 좌표의 기준이 될 PDF를 로컬에서 만듭니다. `auto`는 모든 운영체제에서 document-processor HTML/Chromium → LibreOffice 순서입니다. `HWP_RENDER_BACKEND`로 둘 중 하나를 지정할 수 있습니다.
3. PDFium이 변환 PDF를 페이지 이미지로 렌더하고 디지털 텍스트 줄과 bbox를 읽습니다. 구조 텍스트는 렌더 페이지의 텍스트와 대조해 페이지별로 다시 배분합니다.
4. HTML 경로가 DOM 행을 제공하면 `structured_fast` 또는 `hybrid`로 처리합니다. `structured_fast`는 구조 행이 충분할 때 Paddle 호출을 생략하고, `hybrid`는 구조 행에 Paddle 시각 요소를 보완합니다. 이때 구조 표 행 안의 새 문구는 OCR 대신 원본 구조 줄(렌더 글자로 위치 확인)로 채우고, 렌더 글자가 없는 이미지 속 글자만 별도 Region으로 둡니다. 그 밖에는 기존 Paddle/OCR 시각 경로로 갑니다.
5. `parse/hwp_alignment.py`가 좌표 없는 원본 문구·셀을 화면 Region과 대조합니다. 충분히 맞는 문구만 최종 텍스트로 사용하고, 보이지 않는 이미지 글자는 OCR/VLM 결과로 보완합니다.

따라서 구조 파싱은 **문구·셀의 근거**, 렌더 이미지와 PDF/Paddle 결과는 **사용자 화면의 bbox 근거**입니다. 변환 PDF는 화면 좌표를 얻기 위한 중간 산출물이며, PDF 입력에 사내 구조 파서를 적용하는 분기는 없습니다.

## 표와 심의 입력

원본 구조의 표 객체가 있다고 해서 전부 금융상품 데이터 표는 아닙니다. 배치용 큰 표는 `layout_container`로 분류합니다. 화면 Region에 정확히 대응하는 원본/HTML 셀만 P1 `table.cells`에 보존하고, P3에는 확정된 표 Region의 `kind: "table"`, `selected_text`, `bbox`, `labels`만 보냅니다.

Paddle이 표라고 표시한 시각 후보와 페이지 VLM이 찾은 누락 후보는 `parse/visual_tables.py`에서 페이지·확대 이미지를 함께 보고 검증합니다. 서로 다른 상품이나 독립 심의 항목을 한 표로 합치지 않습니다. 여러 Region으로 나뉜 한 표는 기존 원문과 bbox의 합집합으로 한 Region에 담으며, VLM 셀 격자나 항목·값 관계를 새로 만들지 않습니다. 거부한 후보도 원래 텍스트 Region으로 남습니다.

## 실행 환경과 실패 정책

- 사내 `document-processor`가 설치되면 구조 파싱과 HTML 렌더에서 우선 사용합니다. Kordoc 폴백에는 Node.js와 고정 버전 `KORDOC_COMMAND`가 필요합니다.
- 이 저장소의 기본 Python 설치에는 사설 `document-processor`가 포함되지 않습니다. HWP HTML 경로를 사용할 환경이나 컨테이너에는 접근 가능한 사내 저장소를 별도로 설치해야 합니다. 컨테이너에서는 같은 패키지를 빌드 단계에 설치해야 합니다.
- `document-processor`는 `requires-python >=3.13`으로 선언돼 있어 Python 3.11에서는 `pip`와 `uv` 모두 설치를 거부합니다. 3.11에서 테스트와 입력 단계 출력(페이지 픽셀·디지털 텍스트·HWP 구조)이 3.13과 같음을 확인했으므로 저장소를 고치지 않고 버전 검사만 건너뜁니다. `--ignore-requires-python`이 다른 패키지의 검사까지 끄지 않도록 의존성은 먼저 따로 설치하고 `--no-deps`로 설치합니다. 그래야 `pypdfium2`도 lock의 5.12.0으로 유지됩니다. Windows 개발 환경 예(Linux는 `.venv/bin/python`):

  ```bash
  uv sync --dev
  uv pip install --python .venv/Scripts/python.exe "jpype1>=1.7.1" "pypdf>=6.2,<7" "python-docx>=1.2" pip
  .venv/Scripts/python.exe -m pip install --ignore-requires-python --no-deps -e ../document-processor
  ```

- 이렇게 설치한 패키지는 `uv.lock`에 없으므로 기본 `uv sync`가 제거합니다. 이후 동기화는 `uv sync --dev --inexact`로 합니다. `uv run`은 제거하지 않습니다.
- HTML 렌더에는 Chromium/Chrome/Edge가 필요하며 `HWP_CHROMIUM`으로 지정할 수 있습니다. LibreOffice 폴백에는 `soffice`가 필요합니다. 설치형 한컴오피스는 사용하지 않습니다.
- `HWP_RENDER_DIR`을 지정하면 중간 PDF를 보존합니다. `HWP_REVIEW_DIR`을 지정하면 HTML 검토 화면을 보존합니다.
- 구조 파서 또는 모든 렌더 백엔드가 실패하면 HWP 입력은 명시적으로 중단합니다. 예전 내장 이미지 자산을 가상 페이지로 만드는 경로는 제거했습니다.
- HTML/LibreOffice 조판은 한컴 원본 화면과 다를 수 있습니다. 심의 화면의 bbox는 **실제로 선택된 렌더 결과 이미지**를 기준으로 표시해야 합니다.

HWP를 외부 한컴 서버로 보내는 SDK 호출은 사용하지 않습니다. PaddleX/VLM 호출에는 렌더된 페이지 이미지가 전송됩니다.
