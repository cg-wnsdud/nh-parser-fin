# -*- coding: utf-8 -*-
"""PaddleX `/layout-parsing` 호출 — 한 장 보내고 응답을 그대로 받는다.

`SETTINGS` 를 읽지 않는다. 보내는 값은 전부
인자로 들어오고 그대로 기록된다. "이 실행이 무엇을 보냈는지"를 기억이나 환경변수에
의존해 추적하지 않기 위해서다.

**`"server"` 규약**: 값이 `"server"` 면 그 키를 **안 보낸다**. 그래야 서버
`PP-StructureV3.yml` 의 값이 쓰인다. 스칼라를 하나라도 보내면 그 항목의 **클래스별
dict 가 통째로 무력화**되므로, 클래스별 설정을 시험할 때는 반드시 `server` 여야 한다.
"""
from __future__ import annotations

import base64
import io
import time

import requests
from PIL import Image

SENTINEL = ("", "server", "none", None)

# 광고물 파싱에서 늘 끄는 것들. 값을 바꾸고 싶으면 CLI 로 덮어쓴다.
FIXED = {
    "fileType": 1,
    "useDocOrientationClassify": False,
    "useDocUnwarping": False,
    "useFormulaRecognition": False,
    "useTextlineOrientation": False,
}

NUMERIC_KEYS = {
    "layout_threshold": "layoutThreshold",
    "layout_unclip_ratio": "layoutUnclipRatio",
    "text_det_limit_side_len": "textDetLimitSideLen",
    "text_det_thresh": "textDetThresh",
    "text_det_box_thresh": "textDetBoxThresh",
    "text_det_unclip_ratio": "textDetUnclipRatio",
    "text_rec_score_thresh": "textRecScoreThresh",
}
BOOL_KEYS = {
    "layout_nms": "layoutNms",
    "use_region_detection": "useRegionDetection",
    "use_table_recognition": "useTableRecognition",
}
STRING_KEYS = {
    "layout_merge_bboxes_mode": "layoutMergeBboxesMode",
    "text_det_limit_type": "textDetLimitType",
}
INT_KEYS = {"textDetLimitSideLen"}


def _number(value):
    text = str(value).strip()
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"숫자 또는 'server' 여야 합니다: {value!r}") from exc


def _boolean(value):
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"true/false 또는 'server' 여야 합니다: {value!r}")


def build_payload(**knobs) -> dict:
    """보낼 본문을 만든다(파일 바이트 제외). 지정하지 않은 항목은 서버 값을 쓴다."""
    payload = dict(FIXED)
    for name, key in NUMERIC_KEYS.items():
        raw = knobs.get(name)
        if raw in SENTINEL:
            continue
        value = _number(raw)
        payload[key] = int(value) if key in INT_KEYS else value
    for name, key in BOOL_KEYS.items():
        raw = knobs.get(name)
        if raw in SENTINEL:
            continue
        payload[key] = _boolean(raw)
    for name, key in STRING_KEYS.items():
        raw = knobs.get(name)
        if raw in SENTINEL:
            continue
        payload[key] = str(raw).strip()
    return payload


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
