"""Discover local Strata host and inference-server endpoints without changing config."""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

HOST_URL_RE = re.compile(r"strata host[^\r\n]*?http://127\.0\.0\.1:(\d+)", re.IGNORECASE)
_PROCESS_CONFIG_CACHE = (0.0, [])


def _running_server_configs():
    """Get only config paths/ports from the local server process command lines.

    This avoids trying API keys from unrelated saved model profiles that happen
    to use the same configured port. The short PowerShell lookup is cached.
    """
    global _PROCESS_CONFIG_CACHE
    cached_at, cached = _PROCESS_CONFIG_CACHE
    if time.monotonic() - cached_at < 60:
        return cached
    script = r"""
$rows = @(Get-CimInstance Win32_Process -Filter "name = 'python.exe' OR name = 'pythonw.exe'" |
  Where-Object { $_.CommandLine -match 'serve\.server' } | ForEach-Object {
    $cmd = $_.CommandLine
    $port = 0
    $config = ''
    if ($cmd -match '--port\s+(\d+)') { $port = [int]$Matches[1] }
    if ($cmd -match '--config(?:=|\s+)(?:"([^"]+)"|(\S+))') {
      $config = if ($Matches[1]) { $Matches[1] } else { $Matches[2] }
    }
    if ($port -gt 0 -and $config) { [pscustomobject]@{ port = $port; config = $config } }
  })
$rows | ConvertTo-Json -Compress
"""
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, text=True, timeout=4,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
        rows = json.loads(result.stdout) if result.returncode == 0 and result.stdout.strip() else []
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            rows = []
        valid = [row for row in rows if isinstance(row, dict) and isinstance(row.get("port"), int)
                 and isinstance(row.get("config"), str)]
        _PROCESS_CONFIG_CACHE = (time.monotonic(), valid)
        return valid
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        _PROCESS_CONFIG_CACHE = (time.monotonic(), [])
        return []


def discover_host_url():
    """Find the desktop host port from Strata's recent log; use the local proxy as fallback."""
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "Strata" / "logs"
    try:
        logs = sorted(root.glob("desktop-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in logs[:3]:
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 1024 * 1024))
                text = stream.read().decode("utf-8", errors="replace")
            matches = HOST_URL_RE.findall(text)
            if matches:
                return f"http://127.0.0.1:{matches[-1]}"
    except OSError:
        pass
    return "http://127.0.0.1:8090"


def discover_engine_servers():
    """Return ready local inference instances as (base URL, API key) pairs.

    API keys are read only from the matching local native-instance config and are
    sent solely to that loopback server. They are never written to logs/settings.
    """
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    profile = Path(os.environ.get("USERPROFILE", str(Path.home())))
    runtime = local / "Strata" / "runtime"
    configs = profile / ".strata" / "data" / "native-instances"
    try:
        files = sorted(runtime.glob("*.ready.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    found = []
    process_configs = _running_server_configs()
    for path in files:
        try:
            ready = json.loads(path.read_text(encoding="utf-8"))
            base = ready.get("api_url")
            instance_id = ready.get("instance_id")
            parsed = urlsplit(base) if isinstance(base, str) else None
            if (parsed is None or parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                    or not isinstance(parsed.port, int)):
                continue
            config_paths = [Path(row["config"]) for row in process_configs if row["port"] == parsed.port]
            if isinstance(instance_id, str) and re.fullmatch(r"[A-Za-z0-9-]{1,80}", instance_id):
                ready_config = configs / f"{instance_id}.json"
                if not config_paths:
                    config_paths.append(ready_config)
            keys = []
            seen_paths = set()
            for config_path in config_paths:
                if config_path in seen_paths:
                    continue
                seen_paths.add(config_path)
                # Only read Strata's own per-instance config, never an arbitrary
                # path supplied by a process that happens to use the same port.
                try:
                    config_path.resolve().relative_to(configs.resolve())
                except (OSError, ValueError):
                    continue
                try:
                    config = json.loads(config_path.read_text(encoding="utf-8"))
                    if str(config.get("port")) != str(parsed.port):
                        continue
                    configured = config.get("api_key")
                    if isinstance(configured, str) and configured:
                        keys.append(configured)
                    elif isinstance(configured, list):
                        keys.extend(key for key in configured if isinstance(key, str) and key)
                except (OSError, json.JSONDecodeError):
                    pass
            if not keys:
                keys = [""]
            for key in keys:
                pair = (base.rstrip("/"), key)
                if pair not in found:
                    found.append(pair)
        except (OSError, json.JSONDecodeError, AttributeError, ValueError):
            continue
    return found
