"""문서 내장 이미지(ImageAsset) 공용 헬퍼 — rag 트랙과 광고물 트랙이 공유.

document-processor 의 docir.assets 는 {이름: ImageAsset} dict 이고,
ImageAsset 은 bytes_data/mime_type/filename 을 노출한다 (2026-07-18 실측).
문단 스트림에는 이미지 참조 노드가 없어 문서 내 위치는 알 수 없다.
"""

from __future__ import annotations

import io

from PIL import Image


def asset_bytes(asset) -> bytes | None:
    """ImageAsset(또는 유사 객체)에서 원본 바이트를 방어적으로 꺼낸다."""
    for attr in ("bytes_data", "data", "content"):
        val = getattr(asset, attr, None)
        if callable(val):
            try:
                val = val()
            except Exception:
                val = None
        if isinstance(val, (bytes, bytearray)):
            return bytes(val)
    if isinstance(asset, (bytes, bytearray)):
        return bytes(asset)
    return None


def decode_asset_image(asset) -> Image.Image | None:
    data = asset_bytes(asset)
    if not data:
        return None
    try:
        return Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return None


def is_decorative(width: int, height: int) -> bool:
    """구분선·불릿 같은 장식 이미지 필터.

    내용 판단은 VLM 몫이고 여기서는 크기만 본다 — 664×19 구분선(은행연합회
    HWP 실측)처럼 텍스트가 들어갈 수 없는 크기만 거른다.
    """
    return min(width, height) < 32 or width * height < 20_000


def iter_assets(docir):
    """docir.assets 를 (이름, ImageAsset) 시퀀스로 정규화."""
    assets = getattr(docir, "assets", {}) or {}
    if hasattr(assets, "items"):
        return list(assets.items())
    return list(enumerate(assets))
