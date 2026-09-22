"""HWP/HWPX를 기존 PDF 파이프라인에 연결하는 로컬 렌더 어댑터."""
from __future__ import annotations

import json
import os
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


def render_hwp_to_pdf(path: Path, output: Path) -> dict:
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
