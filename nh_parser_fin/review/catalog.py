"""농협 광고 템플릿 카탈로그 로더.

카탈로그는 ``tools/build_template_catalog.py``가 농협 제공 HWPX의 실제 표 셀에서
생성한다. 런타임은 원본 HWPX나 사내 ``document-processor`` 없이도 동작하도록 생성된
JSON만 읽는다.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


CATALOG_PATH = Path(__file__).resolve().parents[1] / "templates" / "ad_templates.json"
CATALOG_VERSION = "nh-ad-template-catalog-v1"


@lru_cache(maxsize=4)
def _load_cached(path_text: str) -> dict[str, Any]:
    path = Path(path_text)
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != CATALOG_VERSION:
        raise ValueError(
            f"지원하지 않는 광고 템플릿 카탈로그: {data.get('version')!r}"
        )
    templates = data.get("templates")
    if not isinstance(templates, dict) or not templates:
        raise ValueError("광고 템플릿 카탈로그에 templates가 없습니다")
    for template_id, template in templates.items():
        items = template.get("items") if isinstance(template, dict) else None
        if not isinstance(items, list) or not items:
            raise ValueError(f"템플릿 {template_id!r}에 items가 없습니다")
        gubuns = [str(item.get("gubun") or "") for item in items]
        if any(not value for value in gubuns) or len(gubuns) != len(set(gubuns)):
            raise ValueError(f"템플릿 {template_id!r}의 구분값이 비었거나 중복됩니다")
    return data


def load_catalog(path: Path | None = None) -> dict[str, Any]:
    """검증된 템플릿 카탈로그를 반환한다."""
    return _load_cached(str((path or CATALOG_PATH).resolve()))


def template_ids(catalog: dict[str, Any] | None = None) -> list[str]:
    return list((catalog or load_catalog())["templates"])


def template_item_map(
    template_id: str, catalog: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    active = catalog or load_catalog()
    try:
        template = active["templates"][template_id]
    except KeyError as exc:
        raise ValueError(f"알 수 없는 광고 템플릿: {template_id!r}") from exc
    return {item["gubun"]: item for item in template["items"]}
