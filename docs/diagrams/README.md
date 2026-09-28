# 파이프라인 흐름도

팀 설명용 파이프라인 전체 흐름도입니다. 손으로 그리지 않고 스크립트가 생성합니다.

| 파일 | 용도 |
|---|---|
| `make_pipeline_excalidraw.py` | 흐름도 정의와 생성기. 내용을 고칠 곳은 여기 하나뿐입니다. |
| `nh-parser-fin-pipeline.excalidraw.md` | Obsidian Excalidraw 플러그인용 |
| `nh-parser-fin-pipeline.excalidraw` | 순수 JSON. excalidraw.com 이나 VS Code 확장에서 엽니다. |

## Obsidian 에서 보기

1. `nh-parser-fin-pipeline.excalidraw.md` 를 볼트 안 아무 폴더에나 복사합니다.
2. Excalidraw 플러그인이 설치돼 있으면 파일을 여는 순간 그림으로 열립니다.
   마크다운으로 열리면 More options `⋯` → **Open as Excalidraw Drawing**.

파일 앞머리가 `excalidraw-plugin: raw` 인 이유는 그림의 정본을 아래 JSON 하나로
두기 위해서입니다. `parsed` 모드는 글자를 `## Text Elements` 절과 동기화하는데,
생성물에서 두 곳이 어긋나면 글자가 비어 보입니다.

## 다시 그리기

단계가 바뀌면 `make_pipeline_excalidraw.py` 의 `STAGE_ROW1` / `STAGE_ROW2` /
`HWP_CARDS` 문자열만 고치고 다시 실행합니다. 좌표와 화살표는 전부 다시 계산됩니다.

```bash
uv run python docs/diagrams/make_pipeline_excalidraw.py
```

난수 시드가 고정돼 있어 내용이 같으면 파일도 같습니다. 즉 diff 에 실제 변경만 남습니다.

## 그림이 담고 있는 것

- ①~⑦ — 입력 정규화부터 Region 조립과 복구 후보까지의 기본 단계.
- HWP-1 ~ HWP-5 — `feat/hwp-input-render` 에서 추가한 HWP/HWPX 경로. 원본 구조 파싱,
  사용 가능한 렌더 백엔드로 PDF 페이지 생성, 실제 렌더 페이지 재분배와 구조 정렬.
- ⑧~⑮ — 항상 이어지는 VLM 의미 판정과 P1/P3 계약 생성.
- 하단 — 단계별 실패 정책, `needs_review` 사유 코드, VLM 호출량, 산출물 파일.

내용의 근거는 [HANDOVER.md](../HANDOVER.md) · [PIPELINE.md](../PIPELINE.md) ·
[HWP_INPUT.md](../HWP_INPUT.md) 와 `run.py` /
`nh_parser_fin/parse/pipeline.py::run_full_pipeline()` 의 실제 호출 순서입니다.
