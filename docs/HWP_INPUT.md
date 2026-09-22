# HWP/HWPX 입력 처리

## 처리 흐름

HWP 입력은 한 가지 추출 결과에 의존하지 않는다.

1. Kordoc이 문단, 표, 병합 셀, 중첩 표를 구조 텍스트로 추출한다.
2. 설치된 한컴오피스의 `HWPFrame.HwpObject` COM Automation이 원본을 로컬 PDF로 저장한다.
3. 기존 PDF 경로가 페이지 이미지와 디지털 텍스트 bbox를 만든다.
4. 기존 PaddleX가 화면 Region bbox와 이미지에만 있는 문구를 찾는다.
5. HWP 구조 노드를 Region에 정렬한다. 내용이 충분히 일치하면 HWP 문자열을 정본으로
   선택하고, bbox는 PDF/PaddleX 좌표를 유지한다.
6. 구조 셀 하나가 여러 Region을 가로지르면 문자열을 한 Region에 덮어쓰지 않고
   PDF 텍스트 검증 근거로만 사용한다.
7. VLM은 상품 소유권, 판독 대조, 템플릿, 구분값을 판정한다. HWP 구조와 PDF 텍스트가
   일치한 문구는 VLM 한 번의 오독 때문에 검수 대상으로 바뀌지 않는다.

구조에 없는 배경 이미지 문구와 로고는 OCR/VLM Region으로 남는다. 따라서 HWP 구조
텍스트는 내용 정본이며, PDF/OCR은 bbox와 시각 전용 문구를 보강하는 역할을 한다.

## 표 처리

HWP에서 `table`은 항상 업무상 데이터 표를 뜻하지 않는다. 문서 전체의 배경, 제목,
여백을 배치하려고 17×7 표를 쓰는 파일도 있다. 이런 큰 희소 병합표와 이미지 중심의
1열 표는 `layout_container`로 기록하고 P3 셀 행렬로 보내지 않는다.

작고 모든 셀 문구가 한 Region에서 확인된 표만 Kordoc 셀 순서를 정본으로 사용한다.
P1에는 전체 셀, 병합 정보와 출처를 보존하고 P3에는 반복 키가 없는 `shape`와 `rows`
행렬만 싣는다. 셀 구조가 확인되지 않은 표는 정제된 본문과 bbox만 전달한다.

## 실행 환경

- `KORDOC_COMMAND`: 기본값 `npx --yes kordoc@4.14.1`. 운영에서는 설치된 고정 버전의
  실행 경로를 지정하는 편이 재현성과 네트워크 통제에 유리하다.
- `KORDOC_VERSION`: P1 provenance에 기록할 버전. 기본값 `4.14.1`.
- `KORDOC_TIMEOUT`: 구조 파싱 제한 시간(초). 기본값 `180`.
- `HWP_AUTOMATION_SECURITY_MODULE`: 한컴 공식 Automation 보안 승인 DLL 경로.
- `HWP_RENDER_TIMEOUT`: HWP→PDF 제한 시간(초). 기본값 `300`.
- `HWP_RENDER_DIR`: 지정하면 중간 PDF를 보존한다. 없으면 임시 디렉터리에서 처리한다.

현재 코드는 한컴 서버나 Hwp SDK API를 호출하지 않는다. 설치형 한컴오피스를 현재
Windows 사용자 세션에서 COM으로 제어하므로 문서 데이터는 로컬에 남는다. 보안 승인
모듈도 한컴 개발자 자료의 로컬 파일 접근 승인 모듈이다.

운영에서 Hwp SDK를 도입할 때는 `HWP/HWPX → PDF` 구현만 SDK 어댑터로 교체하면 된다.
그 뒤의 PDF 렌더, 디지털 bbox, PaddleX, 구조 정렬, VLM, P1/P3 단계는 그대로 재사용한다.

관련 자료:

- [Kordoc](https://github.com/chrisryugj/kordoc)
- [한컴 HwpAutomation 안내](https://developer.hancom.com/hwpautomation)
- [한컴 Hwp SDK](https://online.hancom.com/en/product/sdk/hwpSdk)

## 009·010 실측

- `NH농협은행-2026_009-예금성.hwp`: 한컴 렌더 2쪽. Kordoc 문단 32개와 조판용
  3×1 표를 추출했다. Kordoc이 모든 블록을 논리 1쪽으로 표시했지만 PDF 텍스트층과
  대조해 심의필·수신거부 두 문단을 실제 2쪽으로 재배치했다.
- `NH농협은행-2026_010-대출성.hwp`: 한컴 렌더 1쪽. 17×7 조판 표와 중첩 1×2
  상환방식 표, 1×6 연락처 표를 추출했다. 17×7은 P1 구조 근거로만 유지하고, 한
  Region에서 완전히 확인된 1×2 표만 P3 압축 행렬로 전달했다.
- 010의 `금융의 모든 순간`은 HWP 구조/PDF 텍스트층에 없지만 렌더 이미지에서
  OCR/VLM Region으로 회수됐다.
