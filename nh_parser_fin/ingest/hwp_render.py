"""HWP/HWPX를 기존 PDF 파이프라인에 연결하는 로컬 렌더 어댑터."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


def _powershell_script(source: Path, output: Path, security_module: Path | None) -> str:
    # 경로는 JSON 문자열로 넣어 PowerShell 인용/명령 치환을 차단한다.
    source_json = json.dumps(str(source.resolve()), ensure_ascii=False)
    output_json = json.dumps(str(output.resolve()), ensure_ascii=False)
    module_json = json.dumps(str(security_module.resolve()), ensure_ascii=False) if security_module else "$null"
    return rf"""
$ErrorActionPreference = 'Stop'
$source = ConvertFrom-Json @'
{source_json}
'@
$output = ConvertFrom-Json @'
{output_json}
'@
$module = {module_json if security_module is None else "ConvertFrom-Json @'" + chr(10) + module_json + chr(10) + "'@"}
$registry = 'HKCU:\Software\HNC\HwpAutomation\Modules'
$oldValue = $null
$hadValue = $false
$hwp = $null
try {{
    if ($module) {{
        New-Item -Path $registry -Force | Out-Null
        try {{ $oldValue = (Get-ItemProperty -Path $registry -Name 'FilePathCheckerModuleExample' -ErrorAction Stop).'FilePathCheckerModuleExample'; $hadValue = $true }} catch {{}}
        Set-ItemProperty -Path $registry -Name 'FilePathCheckerModuleExample' -Value $module
    }}
    $hwp = New-Object -ComObject HWPFrame.HwpObject
    if ($module) {{ [void]$hwp.RegisterModule('FilePathCheckDLL', 'FilePathCheckerModuleExample') }}
    [void]$hwp.SetMessageBoxMode(0x00211411)
    $opened = $hwp.Open($source, 'HWP', 'lock:false;forceopen:true;suspendpassword:true;')
    if (-not $opened) {{ throw '한글 문서를 열지 못했습니다' }}
    $saved = $hwp.SaveAs($output, 'PDF', '')
    if (-not $saved) {{ throw 'PDF 저장 요청이 실패했습니다' }}
}} finally {{
    if ($hwp) {{
        try {{ $hwp.Clear(1) }} catch {{}}
        try {{ $hwp.Quit() }} catch {{}}
        try {{ [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($hwp) }} catch {{}}
    }}
    if ($module) {{
        if ($hadValue) {{ Set-ItemProperty -Path $registry -Name 'FilePathCheckerModuleExample' -Value $oldValue }}
        else {{ Remove-ItemProperty -Path $registry -Name 'FilePathCheckerModuleExample' -ErrorAction SilentlyContinue }}
    }}
}}
"""


def _render_hancom(path: Path, output: Path) -> dict:
    """설치된 한컴오피스 Automation으로 로컬 PDF를 만든다.

    외부 서버 호출은 없다. 무인 실행에서 파일 접근 확인창을 없애려면 한컴이 제공한
    보안 승인 모듈 경로를 ``HWP_AUTOMATION_SECURITY_MODULE``로 지정해야 한다.
    """
    if os.name != "nt":
        raise RuntimeError("한컴 HwpAutomation 렌더러는 Windows에서만 사용할 수 있습니다")
    raw_module = os.environ.get("HWP_AUTOMATION_SECURITY_MODULE", "").strip()
    module = Path(raw_module) if raw_module else None
    if module and not module.is_file():
        raise RuntimeError(f"HWP 보안 승인 모듈을 찾을 수 없습니다: {module}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    timeout = int(os.environ.get("HWP_RENDER_TIMEOUT", "300"))
    with tempfile.TemporaryDirectory(prefix="nh-hwp-render-") as directory:
        script = Path(directory) / "render.ps1"
        script.write_text(_powershell_script(path, output, module), encoding="utf-8-sig")
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown error").strip()[-1500:]
        raise RuntimeError(f"한컴 HWP→PDF 변환 실패(exit={completed.returncode}): {detail}")

    deadline = time.monotonic() + 30
    previous = -1
    stable = 0
    while time.monotonic() < deadline:
        size = output.stat().st_size if output.exists() else 0
        if size > 1000 and size == previous:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        previous = size
        time.sleep(0.5)
    if not output.exists() or output.stat().st_size <= 1000 or output.read_bytes()[:5] != b"%PDF-":
        raise RuntimeError("한컴 변환 프로세스는 끝났지만 유효한 PDF가 생성되지 않았습니다")
    return {
        "engine": "hancom_hwpautomation",
        "transport": "local_com",
        "security_module": bool(module),
        "pdf_bytes": output.stat().st_size,
    }


def _find_chromium() -> str | None:
    configured = os.environ.get("HWP_CHROMIUM", "").strip()
    if configured:
        return configured
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome", "msedge"):
        if found := shutil.which(name):
            return found
    for candidate in (
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def _render_document_processor_html(path: Path, output: Path) -> dict:
    from .hwp_html import read_dom_rows, render_hwp_review_surface

    browser = _find_chromium()
    if not browser:
        raise RuntimeError("headless Chromium/Chrome/Edge 실행 파일을 찾을 수 없습니다")
    configured = os.environ.get("HWP_REVIEW_DIR", "").strip()
    review_dir = Path(configured) if configured else output.parent / "review-html"
    surface = render_hwp_review_surface(path, review_dir)
    html_path = Path(surface["html_path"]).resolve()
    timeout = int(os.environ.get("HWP_RENDER_TIMEOUT", "300"))
    surface["dom_rows"] = read_dom_rows(html_path, browser, timeout=timeout)
    Path(surface["manifest_path"]).write_text(
        json.dumps(
            {key: value for key, value in surface.items() if key != "html_path"},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-pdf-header-footer",
            f"--print-to-pdf={output.resolve()}",
            html_path.as_uri(),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown error").strip()[-1500:]
        raise RuntimeError(f"HTML→PDF 렌더 실패(exit={completed.returncode}): {detail}")
    return {
        "engine": "document_processor_html_chromium",
        "transport": "local_process",
        "review_surface": surface,
    }


def _find_soffice() -> str | None:
    configured = os.environ.get("HWP_SOFFICE", "").strip()
    if configured:
        return configured
    for name in ("soffice", "libreoffice"):
        if found := shutil.which(name):
            return found
    for candidate in (
        Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
        Path(r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def _render_libreoffice(path: Path, output: Path) -> dict:
    soffice = _find_soffice()
    if not soffice:
        raise RuntimeError("LibreOffice soffice 실행 파일을 찾을 수 없습니다")
    timeout = int(os.environ.get("HWP_RENDER_TIMEOUT", "300"))
    with tempfile.TemporaryDirectory(prefix="nh-hwp-libreoffice-") as directory:
        converted = Path(directory) / f"{path.stem}.pdf"
        completed = subprocess.run(
            [
                soffice,
                "--headless",
                "--nologo",
                "--nodefault",
                "--nolockcheck",
                "--convert-to",
                "pdf:writer_pdf_Export",
                "--outdir",
                directory,
                str(path.resolve()),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0 or not converted.is_file():
            detail = (completed.stderr or completed.stdout or "출력 파일 없음").strip()[-1500:]
            raise RuntimeError(f"LibreOffice HWP→PDF 실패(exit={completed.returncode}): {detail}")
        shutil.copy2(converted, output)
    return {
        "engine": "libreoffice_writer_pdf",
        "transport": "local_process",
    }


def backend_order(requested: str | None = None, *, platform: str | None = None) -> list[str]:
    value = (requested or os.environ.get("HWP_RENDER_BACKEND", "auto")).strip().lower()
    aliases = {"html": "document_processor_html", "document-processor-html": "document_processor_html"}
    value = aliases.get(value, value)
    allowed = {"auto", "hancom", "document_processor_html", "libreoffice"}
    if value not in allowed:
        raise RuntimeError(f"지원하지 않는 HWP_RENDER_BACKEND: {value}")
    if value != "auto":
        return [value]
    current = platform or os.name
    if current == "nt":
        return ["hancom", "document_processor_html", "libreoffice"]
    return ["document_processor_html", "libreoffice"]


def _validate_pdf(output: Path) -> None:
    deadline = time.monotonic() + 30
    previous = -1
    stable = 0
    while time.monotonic() < deadline:
        size = output.stat().st_size if output.exists() else 0
        if size > 1000 and size == previous:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        previous = size
        time.sleep(0.5)
    if not output.exists() or output.stat().st_size <= 1000 or output.read_bytes()[:5] != b"%PDF-":
        raise RuntimeError("렌더 프로세스는 끝났지만 유효한 PDF가 생성되지 않았습니다")


def render_hwp_to_pdf(path: Path, output: Path) -> dict:
    """환경에 맞는 로컬 렌더러를 선택해 HWP/HWPX를 PDF로 만든다.

    ``auto``는 Windows에서 한컴을 먼저 사용하고, Linux/Spark에서는 구조 HTML과
    headless Chromium을 먼저 사용한다. LibreOffice는 공식 HWP 97 필터의 호환 범위가
    맞는 문서를 위한 폴백이다. 어떤 경로도 문서를 외부 서버로 전송하지 않는다.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    renderers = {
        "hancom": _render_hancom,
        "document_processor_html": _render_document_processor_html,
        "libreoffice": _render_libreoffice,
    }
    order = backend_order()
    for backend in order:
        if output.exists():
            output.unlink()
        try:
            info = renderers[backend](path, output)
            _validate_pdf(output)
            return {
                **info,
                "backend": backend,
                "attempted_backends": [*order[: order.index(backend) + 1]],
                "pdf_bytes": output.stat().st_size,
            }
        except Exception as exc:
            errors.append(f"{backend}: {exc}")
            if len(order) == 1:
                raise
    raise RuntimeError("HWP 렌더러가 모두 실패했습니다: " + " | ".join(errors))
