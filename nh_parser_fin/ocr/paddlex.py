# -*- coding: utf-8 -*-
"""PaddleX `/layout-parsing` 호출 — 한 장 보내고 응답을 그대로 받는다.

`SETTINGS` 를 읽지 않는다. 실제 요청 본문은 실행 프로필이 넘긴 값을 그대로 사용한다.
표 검출·인식 모듈의 활성화는 현재 서버 YAML 설정에 따른다.
"""
from __future__ import annotations

import base64
import io
import time

import requests
from PIL import Image

def encode(image: Image.Image, fmt: str = "jpeg", quality: int = 90) -> bytes:
    buffer = io.BytesIO()
    if fmt == "png":
        image.save(buffer, format="PNG", optimize=False)
    else:
        image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def call(image: Image.Image, *, url: str, payload: dict, timeout: int = 300,
         fmt: str = "jpeg") -> dict:
    """한 장을 보내고 `prunedResult` 를 **가공 없이** 돌려준다."""
    body = dict(payload)
    raw = encode(image, fmt)
    body["file"] = base64.b64encode(raw).decode("ascii")

    started = time.time()
    response = requests.post(url, json=body, timeout=timeout)
    seconds = round(time.time() - started, 3)
    response.raise_for_status()
    parsed = response.json()
    if parsed.get("errorCode") != 0:
        raise RuntimeError(f"PaddleX error {parsed.get('errorCode')}: {parsed.get('errorMsg')}")

    results = (parsed.get("result") or {}).get("layoutParsingResults") or []
    if not results:
        raise RuntimeError("layoutParsingResults 가 비어 있습니다")
    return {
        "pruned": results[0].get("prunedResult") or {},
        "seconds": seconds,
        "request_bytes": len(raw),
        "sent_px": list(image.size),
    }


def det_boxes(pruned: dict) -> list[dict]:
    """`layout_det_res` — 검출기 원출력. 확신도(score)와 cls_id 가 붙어 오는 유일한 목록."""
    out = []
    for box in (pruned.get("layout_det_res") or {}).get("boxes") or []:
        coordinate = box.get("coordinate")
        if not coordinate or len(coordinate) != 4:
            continue
        out.append({
            "label": str(box.get("label") or "unknown"),
            "cls_id": box.get("cls_id"),
            "score": box.get("score"),
            "bbox": [int(round(float(v))) for v in coordinate],
        })
    return out


def parsing_boxes(pruned: dict) -> list[dict]:
    """`parsing_res_list` — 엔진이 읽기순서까지 정리한 목록. score 는 없다."""
    out = []
    for order, block in enumerate(pruned.get("parsing_res_list") or []):
        bbox = block.get("block_bbox")
        if not bbox or len(bbox) != 4:
            continue
        out.append({
            "label": str(block.get("block_label") or "unknown"),
            "order": block.get("block_order", order),
            "bbox": [int(round(float(v))) for v in bbox],
            "content": block.get("block_content"),
        })
    return out


def ocr_lines(pruned: dict) -> list[dict]:
    ocr = pruned.get("overall_ocr_res") or {}
    texts = ocr.get("rec_texts") or []
    scores = ocr.get("rec_scores") or []
    boxes = ocr.get("rec_boxes") or []
    out = []
    for index, text in enumerate(texts):
        bbox = boxes[index] if index < len(boxes) else None
        if not bbox or len(bbox) != 4:
            continue
        out.append({
            "text": str(text),
            "score": float(scores[index]) if index < len(scores) else None,
            "bbox": [int(round(float(v))) for v in bbox],
        })
    return out
