"""NH 광고물 파싱 파이프라인.

입력(PDF·이미지·HWP) → PaddleX 레이아웃/OCR → VLM 의미 판정 → P1(전체 근거) ·
P3(심의 입력). 단계별 책임은 `README.md` 의 "파이프라인 흐름" 절에 있다.
"""
__all__ = ["config"]
