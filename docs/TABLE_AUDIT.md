# Paddle 표 후보 검출 실험 (2026-09-26)

범위: `samples/` 20개 파일, 21페이지. 일반 P1/P3 파이프라인과 `main` 병합은 이 실험에서 실행하지 않았다.

## 경로

- PDF: PDFium 렌더링·디지털 텍스트와 Paddle OCR. PDF `document-processor` 구조 추론은 기본 비활성화.
- HWP: `document-processor` 문단·표 셀과 HTML 렌더링.
- 이미지: 원본 이미지와 Paddle OCR.
- 모든 페이지 이미지를 타일링 없이 Paddle에 한 번 입력하여 `table` 박스를 수집한다. 박스의 전체 페이지 오버레이 및 개별 크롭을 저장한다.
- 각 후보는 VLM이 `table` / `field_list` / `not_table`로 판단한다. 의미 관계는 기존 텍스트 줄 ID만 선택하게 하고, 텍스트·좌표는 그 줄에서 결정론적으로 만든다. `verified`는 자동 점검 통과일 뿐 사람 검수의 정답 판정이 아니다.
- PDFium 텍스트층이 OCR에 비해 한글 본문을 현저히 적게 담으면 OCR로 폴백한다.

실행 예시 (`document-processor` 사설 패키지 접근 필요):

```powershell
uv run --no-sync --with ../document-processor python table_audit.py --run-name paddle-table-audit-samples --input samples
```

결과는 `outputs/paddle-table-audit-samples/report.html`, `results.json`, `summary.json`, `images/`에 있다. 기존 파일별 결과가 있으면 재사용하며, `--force '파일명 일부'`로 선택 재실행한다.

## 관찰

Paddle 표 후보 17개, 자동 관계 점검 통과 15개, 부분 결과 1개, 시각적 표 판정만 있는 결과 1개, 실행 오류 0개. `4. 예금성상품(적립식).pdf`는 표 2개를 잡고 기본금리와 우대금리 조건–금리 관계를 연결했다. `20. 대출성상품.pdf`는 대출기간 표 1개에서 운전/시설자금과 상환방식–기간의 5개 관계를 연결했다.

`2. 예금성상품(입출식).pdf`는 표 후보 하나가 본문 대부분과 각주까지 과도하게 덮었다. PDFium 원문은 수치 조각만 추출되었고, OCR 폴백 후 VLM은 `field_list`를 골랐지만 여러 독립 금리값을 한 관계에 묶어 `partial`이다. `4. 대출성상품.pdf`와 `NH농협은행-2026_010-대출성.hwp`에는 눈에 보이는 소표가 있지만 Paddle 후보는 0개다. 따라서 Paddle 단독 검출은 표 누락을 허용하며, 후보가 없다고 표가 없다고 판단할 수 없다. 과대 박스는 전체를 한 의미 구조로 전달하지 말고 후속 분할·검수 대상으로 두어야 한다.

`NH농협은행-2026_004-대출성.hwp`에서는 Paddle이 대출대상과 대출금리 값을 한 박스로 묶었다. VLM은 이를 `field_list`로 분류했지만 서로 다른 행의 값 둘을 한 관계로 잘못 연결했다. `NH농협은행-2026_009-예금성.hwp`에서는 LMS 본문 대부분을 한 박스로 잡았고, 문단까지 VLM에 전달한 재검증 결과 `table` 판정만 있고 유효한 의미 관계는 0개였다. 자동 `verified` 또는 VLM 자신도만으로 심의 전달 정확도를 보장할 수 없다.

일반 파이프라인은 PDF·이미지 `visual` 및 HWP `hybrid` 경로에서 Paddle을 실행한다. HWP `structured_fast`는 원본 구조만으로 처리하므로 Paddle을 호출하지 않는다. 이 실험 스크립트만은 모든 페이지에 대해 전체 페이지 Paddle 호출을 강제했다.
