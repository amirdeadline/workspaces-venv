#!/usr/bin/env python3
"""Manage Python workspaces under a shared folder (drive letter can differ per PC).

Each workspace lives in <root>/virtual_envs/<name>/ with:
  .venv/              Python virtualenv
  .env                environment variables (never stored in the JSON DB)
  env.cmd             CMD helper generated from .env
  requirements.txt    pinned/declared packages
  activate.ps1        PowerShell activator (venv + .env + workdir)
  activate.cmd        CMD activator (venv + .env + workdir)

The registry is <root>/scripts/venvs.json. Secrets stay in each workspace .env only.
Machine-local install state lives in %USERPROFILE%\\.workspaces (not in the share).
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

if os.name == "nt":
    import winreg
else:  # pragma: no cover
    winreg = None

SCHEMA_VERSION = 2
EXPORT_FORMAT_VERSION = 1
CONFIG_FILE_NAME = "venv.config.json"
SHORTCUT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
SECRET_RE = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|AUTH|CREDENTIAL|PRIVATE)",
    re.IGNORECASE,
)
RESERVED_NAMES = {
    "list",
    "add",
    "delete",
    "var",
    "info",
    "path",
    "doctor",
    "sync",
    "freeze",
    "help",
    "ws",
    "venv",
    "litellm",
    "python",
    "pip",
    "install-shell",
    "alias",
    "export",
    "import",
}
MARKER_BEGIN = "# >>> workspaces-venv >>>"
MARKER_END = "# <<< workspaces-venv <<<"
TENANT_BLOCK_RE = re.compile(
    r"\n?# >>> prisma-tenant-env:[^\n]+ >>>\n.*?\n# <<< prisma-tenant-env:[^\n]+ <<<\n?",
    flags=re.DOTALL,
)
SIMPLE_PRISMA_FN_RE = re.compile(
    r"\n?# Typing \"prisma\".*?\nfunction prisma \{\n(?:    .*\n)*\}\n?"
    r"|\n?function prisma \{\n    & \"[^\"]+Activate\.ps1\"\n\}\n?",
    flags=re.IGNORECASE,
)

USER_BIN = Path.home() / "bin"
LOCAL_STATE_DIR = Path.home() / ".workspaces"
LOCAL_CONFIG = LOCAL_STATE_DIR / "install.json"
USER_ENV_ROOT = "WORKSPACES_ROOT"

# Overwritten by configure_paths(). Defaults assume this file lives in <root>/scripts.
SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parent
VENVS_ROOT = ROOT / "virtual_envs"
DB_PATH = SCRIPTS_DIR / "venvs.json"
HOOKS_DIR = LOCAL_STATE_DIR


def python_for_launchers() -> str:
    return str(Path(sys.executable).resolve())


def _abspath(path: Path | str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def detect_root(explicit: str | Path | None = None) -> Path:
    if explicit:
        return _abspath(explicit)
    env = (os.environ.get(USER_ENV_ROOT) or "").strip()
    if env:
        return _abspath(env)
    if LOCAL_CONFIG.is_file():
        try:
            stored = json.loads(LOCAL_CONFIG.read_text(encoding="utf-8")).get("root")
            if stored:
                return _abspath(stored)
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return Path(__file__).resolve().parent.parent


def normalize_workspaces_root(path: Path) -> Path:
    path = _abspath(path)
    if (path / "scripts" / "venv.py").is_file():
        return path
    if path.name.lower() == "scripts" and (path / "venv.py").is_file():
        return path.parent
    raise SystemExit(
        f"Not a workspaces folder (missing scripts\\venv.py): {path}\n"
        "Pass the folder that contains scripts\\ and virtual_envs\\, for example:\n"
        "  python install.py --path Z:\\workspaces"
    )


def configure_paths(explicit: str | Path | None = None) -> Path:
    """Bind ROOT/SCRIPTS_DIR to this machine's workspaces folder (E:, Z:, UNC, ...)."""
    global ROOT, SCRIPTS_DIR, VENVS_ROOT, DB_PATH, HOOKS_DIR
    ROOT = normalize_workspaces_root(detect_root(explicit))
    SCRIPTS_DIR = ROOT / "scripts"
    VENVS_ROOT = ROOT / "virtual_envs"
    DB_PATH = SCRIPTS_DIR / "venvs.json"
    HOOKS_DIR = LOCAL_STATE_DIR
    os.environ[USER_ENV_ROOT] = str(ROOT)
    load_workspace_config()
    return ROOT


DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": 1,
    "virtual_envs_dir": "virtual_envs",
    "venv_dir_name": ".venv",
    "env_file_name": ".env",
    "requirements_file": "requirements.txt",
    "default_python": "",
    "default_cd_on_activate": True,
    "list_description_max_width": 60,
    "export_include_venv": True,
    "export_include_env": True,
    "export_include_claude_config": False,
}

WORKSPACE_CONFIG: dict[str, Any] = {}


def config_file_path() -> Path:
    return SCRIPTS_DIR / CONFIG_FILE_NAME


def load_workspace_config() -> dict[str, Any]:
    """Load optional venv.config.json from the workspaces scripts folder."""
    global WORKSPACE_CONFIG, VENVS_ROOT
    merged = dict(DEFAULT_CONFIG)
    path = config_file_path()
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                merged.update(raw)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[WARN] Could not read {path.name}: {exc}")
    WORKSPACE_CONFIG = merged
    subdir = str(merged.get("virtual_envs_dir") or "virtual_envs").strip() or "virtual_envs"
    VENVS_ROOT = ROOT / subdir.replace("\\", "/").strip("/")
    return merged


def default_python_executable() -> str:
    configured = str(WORKSPACE_CONFIG.get("default_python") or "").strip()
    if configured and Path(configured).is_file():
        return configured
    defaults_py = ""
    if DB_PATH.exists():
        try:
            data = json.loads(DB_PATH.read_text(encoding="utf-8"))
            defaults_py = str((data.get("defaults") or {}).get("python") or "").strip()
        except (OSError, json.JSONDecodeError):
            pass
    if defaults_py and Path(defaults_py).is_file():
        return defaults_py
    return sys.executable


def user_bin_dir() -> Path:
    configured = str(WORKSPACE_CONFIG.get("user_bin") or "").strip()
    if configured:
        return _abspath(configured)
    return USER_BIN


def relocate_stored_path(value: str) -> Path:
    """Map a DB path onto this machine's ROOT. `{root}/virtual_envs/palo` and old E:\\...\\virtual_envs\\palo both work."""
    raw = (value or "").strip()
    if not raw:
        return Path(raw)
    lowered = raw.replace("\\", "/").lower()
    if lowered.startswith("{root}/") or lowered.startswith("{root}\\"):
        return _abspath(ROOT / raw.split("{root}", 1)[1].lstrip("\\/").replace("/", os.sep))
    parts = Path(raw.replace("/", os.sep)).parts
    lowered_parts = [p.lower() for p in parts]
    if "virtual_envs" in lowered_parts:
        idx = lowered_parts.index("virtual_envs")
        return _abspath(ROOT.joinpath(*parts[idx:]))
    return Path(raw)


def store_path(value: str | Path) -> str:
    path = _abspath(value)
    try:
        rel = path.relative_to(_abspath(ROOT))
        return "{root}/" + rel.as_posix()
    except ValueError:
        return str(path)


POWERSHELL_PROFILES = [
    Path.home() / "Documents" / "WindowsPowerShell" / "profile.ps1",
    Path.home() / "Documents" / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1",
    Path.home() / "Documents" / "PowerShell" / "profile.ps1",
    Path.home() / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1",
]


# ---------------------------------------------------------------------------
# Paths / time / IO
# ---------------------------------------------------------------------------

def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_write(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as handle:
            handle.write(text)
            if not text.endswith("\n"):
                handle.write("\n")
        Path(tmp).replace(path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def run(command: list[str], label: str, check: bool = True) -> int:
    print(f"[RUN] {label}")
    proc = subprocess.run(command, check=False)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{label} failed with exit code {proc.returncode}")
    return proc.returncode


def mask_secret(key: str, value: str) -> str:
    if not value:
        return "<empty>"
    if is_secret_key(key):
        return "<set>"
    return value


def is_secret_key(key: str) -> bool:
    return bool(SECRET_RE.search(key))


# ---------------------------------------------------------------------------
# .env handling — values never go into the JSON database
# ---------------------------------------------------------------------------

def parse_env_text(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            result[key] = value
    return result


def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return parse_env_text(path.read_text(encoding="utf-8"))


def write_env(path: Path, env: dict[str, str]) -> None:
    lines = [f"{key}={_env_file_value(value)}" for key, value in env.items()]
    atomic_write(path, "\n".join(lines) + ("\n" if env else ""))


def _env_file_value(value: str) -> str:
    if any(ch in value for ch in ' \t#"\'') or value == "":
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def parse_cmd_set_lines(text: str) -> dict[str, str]:
    """Parse `set "KEY=value"` lines from a CMD launcher. Does not print values."""
    env: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        match = re.match(r'^set\s+"([^=]+)=(.*)"\s*$', stripped, flags=re.IGNORECASE)
        if match:
            env[match.group(1)] = match.group(2)
    return env


def write_env_cmd(path: Path, env: dict[str, str]) -> None:
    lines = ["@echo off"]
    for key, value in env.items():
        lines.append(f'set "{key}={_cmd_set_value(value)}"')
    atomic_write(path, "\n".join(lines) + "\n", encoding="utf-8")


def _cmd_set_value(value: str) -> str:
    # Inside `set "KEY=value"`, quotes are doubled; % must be doubled or it expands.
    return value.replace("%", "%%").replace('"', '""')


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def empty_db() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "paths": {
            "root": str(ROOT),
            "virtual_envs": str(VENVS_ROOT),
            "scripts": str(SCRIPTS_DIR),
            "hooks": str(HOOKS_DIR),
            "database": str(DB_PATH),
        },
        "defaults": {
            "venv_dir_name": ".venv",
            "env_file_name": ".env",
            "requirements_file": "requirements.txt",
            "python": sys.executable,
        },
        "workspaces": [],
        "aliases": [],
    }


def load_db() -> dict[str, Any]:
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        return empty_db()
    try:
        data = json.loads(DB_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return empty_db()
    if not isinstance(data, dict):
        return empty_db()
    data.setdefault("workspaces", [])
    data.setdefault("aliases", [])
    data.setdefault("paths", empty_db()["paths"])
    data.setdefault("defaults", empty_db()["defaults"])
    data["schema_version"] = SCHEMA_VERSION
    data["paths"] = {
        "root": str(ROOT),
        "virtual_envs": str(VENVS_ROOT),
        "scripts": str(SCRIPTS_DIR),
        "hooks": str(HOOKS_DIR),
        "database": str(DB_PATH),
    }
    for ws in data["workspaces"]:
        name = ws.get("name") or ""
        stored_folder = ws.get("folder")
        if stored_folder:
            ws["folder"] = str(relocate_stored_path(str(stored_folder)))
        elif name:
            ws["folder"] = str(VENVS_ROOT / name)
        venv_meta = ws.setdefault("venv", {})
        venv_dir = str(WORKSPACE_CONFIG.get("venv_dir_name") or ".venv").strip() or ".venv"
        folder_path = Path(ws.get("folder") or (VENVS_ROOT / name))
        if venv_meta.get("path"):
            venv_meta["path"] = str(relocate_stored_path(str(venv_meta["path"])))
        else:
            venv_meta["path"] = str(folder_path / venv_dir)
        activate = ws.get("activate") or {}
        if activate.get("workdir"):
            activate["workdir"] = str(relocate_stored_path(str(activate["workdir"])))
        if activate.get("pythonpath"):
            activate["pythonpath"] = [str(relocate_stored_path(str(p))) for p in activate["pythonpath"]]
    return data


def save_db(db: dict[str, Any]) -> None:
    db["updated_at"] = utc_now()
    db["schema_version"] = SCHEMA_VERSION
    db["paths"] = {
        "root": "{root}",
        "virtual_envs": "{root}/virtual_envs",
        "scripts": "{root}/scripts",
        "hooks": "{root}/scripts/hooks",
        "database": "{root}/scripts/venvs.json",
    }
    for ws in db.get("workspaces") or []:
        name = ws.get("name") or ""
        venv_dir = str(WORKSPACE_CONFIG.get("venv_dir_name") or ".venv").strip() or ".venv"
        if name:
            try:
                default_folder = "{root}/" + VENVS_ROOT.relative_to(ROOT).as_posix() + "/" + name
            except ValueError:
                default_folder = "{root}/virtual_envs/" + name
            current = ws.get("folder") or default_folder
            if str(current).replace("\\", "/") == default_folder.replace("\\", "/"):
                ws["folder"] = default_folder
            else:
                ws["folder"] = store_path(current)
            venv_meta = ws.setdefault("venv", {})
            venv_meta["path"] = ws["folder"].rstrip("/\\") + "/" + venv_dir
        activate = ws.get("activate") or {}
        if activate.get("workdir"):
            activate["workdir"] = store_path(activate["workdir"])
        if activate.get("pythonpath"):
            activate["pythonpath"] = [store_path(p) for p in activate["pythonpath"]]
    atomic_write(DB_PATH, json.dumps(db, indent=2, ensure_ascii=False))


def next_id(db: dict[str, Any]) -> str:
    used: set[int] = set()
    for ws in db["workspaces"]:
        try:
            used.add(int(str(ws.get("id", "0"))))
        except ValueError:
            continue
    candidate = 1
    while candidate in used:
        candidate += 1
    return f"{candidate:03d}"


def find_workspace(db: dict[str, Any], target: str) -> dict[str, Any]:
    needle = (target or "").strip()
    if not needle:
        raise SystemExit("Workspace id or name is required.")
    for ws in db["workspaces"]:
        if str(ws.get("id", "")).lstrip("0") == needle.lstrip("0") and needle.lstrip("0"):
            return ws
        if str(ws.get("id")) == needle:
            return ws
        if str(ws.get("name", "")).lower() == needle.lower():
            return ws
        shortcut = (ws.get("activate") or {}).get("shortcut", "")
        if str(shortcut).lower() == needle.lower():
            return ws
    raise SystemExit(f"No workspace found for '{target}'. Use: python venv.py --list")


def workspace_paths(ws: dict[str, Any]) -> dict[str, Path]:
    name = ws["name"]
    stored = ws.get("folder")
    if stored:
        folder = relocate_stored_path(str(stored))
    else:
        folder = VENVS_ROOT / name
    files = ws.get("files") or {}
    venv_dir = str(WORKSPACE_CONFIG.get("venv_dir_name") or ".venv").strip() or ".venv"
    venv_meta = ws.get("venv") or {}
    if venv_meta.get("path"):
        venv_path = relocate_stored_path(str(venv_meta["path"]))
    else:
        venv_path = folder / venv_dir
    env_name = files.get("env") or WORKSPACE_CONFIG.get("env_file_name") or ".env"
    req_name = files.get("requirements") or WORKSPACE_CONFIG.get("requirements_file") or "requirements.txt"
    return {
        "folder": folder,
        "venv": venv_path,
        "python": venv_path / "Scripts" / "python.exe",
        "env": folder / env_name,
        "env_cmd": folder / (files.get("env_cmd") or "env.cmd"),
        "requirements": folder / req_name,
        "activate_ps1": folder / "activate.ps1",
        "activate_cmd": folder / "activate.cmd",
        "gitignore": folder / ".gitignore",
    }


# ---------------------------------------------------------------------------
# Workspace files
# ---------------------------------------------------------------------------

def generate_activate_ps1(ws: dict[str, Any]) -> str:
    paths = workspace_paths(ws)
    activate = ws.get("activate") or {}
    stored_workdir = activate.get("workdir") or ""
    pythonpath = activate.get("pythonpath") or []
    do_cd = activate.get("cd", True)
    pp_literal = ";".join(pythonpath).replace("'", "''")
    workdir_literal = str(relocate_stored_path(stored_workdir) if stored_workdir else paths["folder"]).replace("'", "''")
    cd_block = ""
    if do_cd:
        cd_block = (
            f"if (Test-Path -LiteralPath '{workdir_literal}') {{\n"
            f"    Set-Location -LiteralPath '{workdir_literal}'\n"
            "}\n"
            "else {\n"
            "    Set-Location -LiteralPath $WorkspaceRoot\n"
            "}\n"
        )
    pp_block = ""
    if pythonpath:
        pp_block = (
            f"$prepend = '{pp_literal}'\n"
            "if ($prepend) {\n"
            "    if ($env:PYTHONPATH) {\n"
            "        $env:PYTHONPATH = $prepend + [IO.Path]::PathSeparator + $env:PYTHONPATH\n"
            "    } else {\n"
            "        $env:PYTHONPATH = $prepend\n"
            "    }\n"
            "}\n"
        )
    name = ws["name"]
    prompt_ps = name.replace("'", "''")
    return f"""# Generated by venv.py - do not edit by hand
$WorkspaceRoot = $PSScriptRoot
$VenvActivate = Join-Path $PSScriptRoot '.venv\\Scripts\\Activate.ps1'
$EnvFile = Join-Path $PSScriptRoot '.env'
$PromptName = '{prompt_ps}'
$env:WORKSPACE_NAME = '{ws["name"]}'
$env:WORKSPACE_ID = '{ws["id"]}'
$env:WORKSPACE_ROOT = $WorkspaceRoot
if (Test-Path -LiteralPath $EnvFile) {{
    Get-Content -LiteralPath $EnvFile | ForEach-Object {{
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith('#')) {{ return }}
        if ($line -match '^export\\s+') {{ $line = $line.Substring(7).Trim() }}
        $eq = $line.IndexOf('=')
        if ($eq -lt 1) {{ return }}
        $name = $line.Substring(0, $eq).Trim()
        $value = $line.Substring($eq + 1).Trim()
        if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {{
            $value = $value.Substring(1, $value.Length - 2)
        }}
        Set-Item -Path "Env:$name" -Value $value
    }}
}}
if ($env:ANTHROPIC_API_KEY) {{
    $env:CLAUDE_CONFIG_DIR = Join-Path $WorkspaceRoot '.claude-code'
    if (-not $env:ANTHROPIC_AUTH_TOKEN) {{
        $env:ANTHROPIC_AUTH_TOKEN = $env:ANTHROPIC_API_KEY
    }}
}}
{pp_block}if (Test-Path -LiteralPath $VenvActivate) {{
    . $VenvActivate
}} else {{
    Write-Error "Python venv not found at $VenvActivate"
}}
$env:VIRTUAL_ENV_PROMPT = $PromptName
if (Get-Variable -Name _PYTHON_VENV_PROMPT_PREFIX -Scope Global -ErrorAction SilentlyContinue) {{
    Remove-Variable -Name _PYTHON_VENV_PROMPT_PREFIX -Scope Global -Force
}}
New-Variable -Name _PYTHON_VENV_PROMPT_PREFIX -Description "Python virtual environment prompt prefix" -Scope Global -Option ReadOnly -Visibility Public -Value $PromptName
function global:litellm_test {{
    if (-not $env:ANTHROPIC_API_KEY) {{
        Write-Error "ANTHROPIC_API_KEY is not set. Run palo first."
        return
    }}
    $base = $env:ANTHROPIC_BASE_URL
    if (-not $base) {{ $base = "https://scaleteam-litellm-eu.paloaltonetworks.com" }}
    $url = $base.TrimEnd('/') + "/v1/models"
    Write-Host "GET $url"
    curl.exe --ssl-no-revoke $url -H "Authorization: Bearer $($env:ANTHROPIC_API_KEY)"
}}
{cd_block}"""


def generate_activate_cmd(ws: dict[str, Any]) -> str:
    paths = workspace_paths(ws)
    activate = ws.get("activate") or {}
    stored_workdir = activate.get("workdir") or ""
    workdir = str(relocate_stored_path(stored_workdir) if stored_workdir else paths["folder"])
    pythonpath = activate.get("pythonpath") or []
    do_cd = activate.get("cd", True)
    lines = [
        "@echo off",
        "set \"WORKSPACE_NAME=" + ws["name"] + "\"",
        "set \"WORKSPACE_ID=" + ws["id"] + "\"",
        'set "WORKSPACE_ROOT=%~dp0"',
        'if "%WORKSPACE_ROOT:~-1%"=="\\" set "WORKSPACE_ROOT=%WORKSPACE_ROOT:~0,-1%"',
        'if exist "%~dp0env.cmd" call "%~dp0env.cmd"',
        'if exist "%~dp0.venv\\Scripts\\activate.bat" call "%~dp0.venv\\Scripts\\activate.bat"',
    ]
    if pythonpath:
        joined = ";".join(pythonpath)
        lines.append(f'set "PYTHONPATH={joined};%PYTHONPATH%"')
    if do_cd:
        lines.append(f'if exist "{workdir}" (cd /d "{workdir}") else (cd /d "%~dp0")')
    lines.append(f'set "VIRTUAL_ENV_PROMPT={ws["name"]}"')
    lines.append("if defined _OLD_VIRTUAL_PROMPT (")
    lines.append(f'    set "PROMPT=({ws["name"]}) %_OLD_VIRTUAL_PROMPT%"')
    lines.append(") else (")
    lines.append(f'    set "PROMPT=({ws["name"]}) %PROMPT%"')
    lines.append(")")
    lines.append('if defined ANTHROPIC_API_KEY set "CLAUDE_CONFIG_DIR=%WORKSPACE_ROOT%\\.claude-code"')
    lines.append("if defined ANTHROPIC_API_KEY if not defined ANTHROPIC_AUTH_TOKEN set \"ANTHROPIC_AUTH_TOKEN=%ANTHROPIC_API_KEY%\"")
    lines.append("")
    return "\n".join(lines)


def set_venv_prompt(venv_path: Path, name: str) -> None:
    cfg = venv_path / "pyvenv.cfg"
    if cfg.exists():
        lines = cfg.read_text(encoding="utf-8").splitlines()
        out: list[str] = []
        found = False
        for line in lines:
            if line.strip().lower().startswith("prompt"):
                out.append(f"prompt = {name}")
                found = True
            else:
                out.append(line)
        if not found:
            out.append(f"prompt = {name}")
        atomic_write(cfg, "\n".join(out) + "\n")
    bat = venv_path / "Scripts" / "activate.bat"
    if bat.exists():
        text = bat.read_text(encoding="utf-8")
        text = re.sub(r"set PROMPT=\([^)]*\) %PROMPT%", f"set PROMPT=({name}) %PROMPT%", text)
        text = re.sub(
            r'set "VIRTUAL_ENV_PROMPT=.*"',
            f'set "VIRTUAL_ENV_PROMPT=({name}) "',
            text,
        )
        bat.write_text(text, encoding="utf-8")


def write_claude_config_dir(folder: Path, env: dict[str, str]) -> None:
    """Give this workspace its own Claude Code config so API-key auth does not mix with /login."""
    if not env.get("ANTHROPIC_API_KEY"):
        return
    cfg_dir = folder / ".claude-code"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    key = env["ANTHROPIC_API_KEY"]
    tail = key[-20:] if len(key) >= 12 else key
    claude_json = {
        "customApiKeyResponses": {
            "approved": [tail],
            "rejected": [],
        }
    }
    atomic_write(cfg_dir / ".claude.json", json.dumps(claude_json, indent=2))
    settings = {
        "env": {
            key: value
            for key, value in env.items()
            if key.startswith("ANTHROPIC_") or key.startswith("CLAUDE_CODE_") or key == "NODE_USE_SYSTEM_CA"
        }
    }
    atomic_write(cfg_dir / "settings.json", json.dumps(settings, indent=2))


def approve_api_key_in_home_claude(env: dict[str, str]) -> None:
    """Claude Code stores a rejected-key suffix in ~/.claude.json; that forces /login."""
    key = env.get("ANTHROPIC_API_KEY")
    if not key:
        return
    home = Path.home() / ".claude.json"
    if not home.exists():
        return
    try:
        data = json.loads(home.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    cap = data.setdefault("customApiKeyResponses", {"approved": [], "rejected": []})
    approved = list(cap.get("approved") or [])
    rejected = list(cap.get("rejected") or [])
    tail = key[-20:] if len(key) >= 12 else key
    new_rejected = []
    for item in rejected:
        if item and (key.endswith(item) or item in key):
            if item not in approved:
                approved.append(item)
        else:
            new_rejected.append(item)
    if tail not in approved:
        approved.append(tail)
    cap["approved"] = approved
    cap["rejected"] = new_rejected
    atomic_write(home, json.dumps(data, indent=2, ensure_ascii=False))


def write_workspace_helpers(ws: dict[str, Any]) -> None:
    paths = workspace_paths(ws)
    paths["folder"].mkdir(parents=True, exist_ok=True)
    atomic_write(paths["activate_ps1"], generate_activate_ps1(ws))
    atomic_write(paths["activate_cmd"], generate_activate_cmd(ws))
    atomic_write(
        paths["gitignore"],
        "\n".join(
            [
                ".venv/",
                ".env",
                "env.cmd",
                ".claude-code/",
                "__pycache__/",
                "*.pyc",
            ]
        )
        + "\n",
    )
    env = read_env(paths["env"])
    write_env_cmd(paths["env_cmd"], env)
    set_venv_prompt(paths["venv"], ws["name"])
    write_claude_config_dir(paths["folder"], env)
    approve_api_key_in_home_claude(env)
    if not paths["requirements"].exists():
        paths["requirements"].touch()


def python_version_of(python: Path) -> str:
    if not python.exists():
        return ""
    proc = subprocess.run(
        [str(python), "-c", "import sys; print(sys.version.split()[0])"],
        check=False,
        capture_output=True,
        text=True,
    )
    return (proc.stdout or "").strip()


def freeze_requirements(python: Path, dest: Path) -> None:
    proc = subprocess.run(
        [str(python), "-m", "pip", "freeze"],
        check=True,
        capture_output=True,
        text=True,
    )
    atomic_write(dest, proc.stdout)


# ---------------------------------------------------------------------------
# Shell integration
# ---------------------------------------------------------------------------

def broadcast_environment_change() -> None:
    if os.name != "nt":
        return
    ctypes.windll.user32.SendMessageTimeoutW(  # type: ignore[attr-defined]
        0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None
    )


def ensure_user_bin_on_path() -> None:
    USER_BIN.mkdir(parents=True, exist_ok=True)
    if winreg is None:
        return
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE
    )
    try:
        try:
            current_path, reg_type = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current_path, reg_type = "", winreg.REG_EXPAND_SZ
        entries = [entry.strip() for entry in current_path.split(";") if entry.strip()]
        normalized = {str(Path(entry)).lower() for entry in entries}
        if str(USER_BIN).lower() not in normalized:
            entries.append(str(USER_BIN))
            winreg.SetValueEx(key, "Path", 0, reg_type, ";".join(entries))
            broadcast_environment_change()
            print(f"[INFO] Added to user PATH: {USER_BIN}")
    finally:
        winreg.CloseKey(key)
    os.environ["PATH"] = str(USER_BIN) + os.pathsep + os.environ.get("PATH", "")


def generate_hooks(db: dict[str, Any]) -> None:
    HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    root_ps = str(ROOT).replace("'", "''")
    install_hint = f"python {SCRIPTS_DIR / 'install.py'} --path {ROOT}"
    install_hint_ps = install_hint.replace("'", "''")
    functions = [
        "# Generated per-machine. Do not copy this file to another PC.",
        f"$script:WorkspacesRoot = if ($env:{USER_ENV_ROOT}) {{ $env:{USER_ENV_ROOT} }} else {{ '{root_ps}' }}",
        "function global:venv {",
        "    $launcher = Join-Path $env:USERPROFILE 'bin\\venv.cmd'",
        "    if (-not (Test-Path -LiteralPath $launcher)) {",
        f"        Write-Error 'venv is not installed. Run: {install_hint_ps}'",
        "        return",
        "    }",
        "    & $launcher @args",
        "}",
        "function global:ws { venv @args }",
        "function global:litellm {",
        "    $launcher = Join-Path $env:USERPROFILE 'bin\\litellm.cmd'",
        "    if (-not (Test-Path -LiteralPath $launcher)) {",
        f"        Write-Error 'litellm is not installed. Run: {install_hint_ps}'",
        "        return",
        "    }",
        "    & $launcher @args",
        "}",
        "function global:Enter-Workspace {",
        "    param([Parameter(Mandatory=$true)][string]$Name)",
        f"    $root = if ($env:{USER_ENV_ROOT}) {{ $env:{USER_ENV_ROOT} }} else {{ $script:WorkspacesRoot }}",
        "    $activate = Join-Path $root ('virtual_envs\\' + $Name + '\\activate.ps1')",
        "    if (-not (Test-Path -LiteralPath $activate)) {",
        "        Write-Error \"Workspace '$Name' not found. Run: venv --list\"",
        "        return",
        "    }",
        "    Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force",
        "    $localDir = Join-Path $env:USERPROFILE '.workspaces'",
        "    if (-not (Test-Path -LiteralPath $localDir)) { New-Item -ItemType Directory -Path $localDir | Out-Null }",
        "    $localActivate = Join-Path $localDir ('activate-' + $Name + '.ps1')",
        "    Copy-Item -LiteralPath $activate -Destination $localActivate -Force",
        "    Unblock-File -LiteralPath $localActivate -ErrorAction SilentlyContinue",
        "    . $localActivate",
        "}",
    ]
    doskey = [
        "@echo off",
        "REM Generated per-machine. Do not copy this file to another PC.",
        f'if not defined {USER_ENV_ROOT} set "{USER_ENV_ROOT}={ROOT}"',
        'doskey venv="%USERPROFILE%\\bin\\venv.cmd" $*',
        "doskey ws=venv $*",
        'doskey litellm="%USERPROFILE%\\bin\\litellm.cmd" $*',
    ]
    for ws in db["workspaces"]:
        shortcut = (ws.get("activate") or {}).get("shortcut") or ws["name"]
        if not SHORTCUT_RE.fullmatch(shortcut):
            continue
        ws_folder = workspace_paths(ws)["folder"]
        activate_cmd = ws_folder / "activate.cmd"
        functions.append(f"function global:{shortcut} {{ Enter-Workspace '{ws['name']}' }}")
        doskey.append(f'doskey {shortcut}=call "{activate_cmd}"')
        launcher = user_bin_dir() / f"{shortcut}.cmd"
        atomic_write(
            launcher,
            "\n".join(
                [
                    "@echo off",
                    f'if not defined {USER_ENV_ROOT} set "{USER_ENV_ROOT}={ROOT}"',
                    f'call "{activate_cmd}"',
                    "",
                ]
            ),
        )
    for alias in db.get("aliases") or []:
        name = str(alias.get("name") or "").strip()
        command = str(alias.get("command") or "").strip()
        if not name or not command or not SHORTCUT_RE.fullmatch(name):
            continue
        launcher = user_bin_dir() / f"{name}.cmd"
        if not launcher.exists():
            write_custom_alias_launcher(name, command)
        doskey.append(f'doskey {name}=call "{launcher}"')
        functions.append(
            f"function global:{name} {{ cmd /c \"{launcher}\" }}"
        )
    atomic_write(HOOKS_DIR / "profile.ps1", "\n".join(functions) + "\n")
    atomic_write(HOOKS_DIR / "autorun.cmd", "\n".join(doskey) + "\n")
    _write_shared_hooks_stub()


def _write_shared_hooks_stub() -> None:
    shared = SCRIPTS_DIR / "hooks"
    shared.mkdir(parents=True, exist_ok=True)
    note = (
        "@echo off\n"
        "REM Machine-local hooks live in %USERPROFILE%\\.workspaces\n"
        "REM Run: python \"%~dp0..\\install.py\" --path <workspaces-root>\n"
        f'if exist "%USERPROFILE%\\.workspaces\\autorun.cmd" call "%USERPROFILE%\\.workspaces\\autorun.cmd"\n'
    )
    ps_note = (
        "# Machine-local hooks live in $env:USERPROFILE\\.workspaces\n"
        "# Run: python <workspaces>\\scripts\\install.py --path <workspaces-root>\n"
        "$local = Join-Path $env:USERPROFILE '.workspaces\\profile.ps1'\n"
        "if (Test-Path -LiteralPath $local) { . $local }\n"
    )
    atomic_write(shared / "autorun.cmd", note)
    atomic_write(shared / "profile.ps1", ps_note)


def upsert_marked_block(path: Path, body: str) -> None:
    new_block = f"{MARKER_BEGIN}\n{body.rstrip()}\n{MARKER_END}\n"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    pattern = re.compile(
        rf"{re.escape(MARKER_BEGIN)}\n.*?\n{re.escape(MARKER_END)}\n?",
        flags=re.DOTALL,
    )
    if pattern.search(existing):
        updated = pattern.sub(lambda _match: new_block, existing)
    else:
        separator = "" if existing.endswith("\n") or not existing else "\n"
        updated = f"{existing}{separator}{new_block}"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, updated if updated.endswith("\n") else updated + "\n")


def scrub_legacy_profile(path: Path) -> None:
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    updated = TENANT_BLOCK_RE.sub("\n", text)
    updated = SIMPLE_PRISMA_FN_RE.sub("\n", updated)
    # Drop a leftover host-specific lab1 that pointed at ~/.virtualenvs
    updated = re.sub(
        r"\n?# Typing \"lab1\".*?\nfunction lab1 \{\n    & \"[^\"]+Activate\.ps1\"\n\}\n?",
        "\n",
        updated,
    )
    if updated != text:
        atomic_write(path, updated)


def _autorun_parts(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"\s*&\s*", str(value or "")) if part.strip()]


def is_workspaces_autorun(part: str) -> bool:
    low = part.lower().replace("/", "\\")
    return "autorun.cmd" in low and (
        "\\.workspaces\\" in low
        or "\\hooks\\autorun.cmd" in low
        or "%userprofile%\\.workspaces\\" in low
    )


def strip_workspaces_autorun(existing: str) -> str:
    kept = [part for part in _autorun_parts(existing) if not is_workspaces_autorun(part)]
    return " & ".join(kept)


def install_cmd_autorun() -> None:
    if winreg is None:
        return
    autorun_script = str(HOOKS_DIR / "autorun.cmd")
    call = f'call "{autorun_script}"'
    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Command Processor")
    try:
        try:
            existing, _ = winreg.QueryValueEx(key, "AutoRun")
        except FileNotFoundError:
            existing = ""
        cleaned = strip_workspaces_autorun(str(existing))
        new_value = f"{cleaned} & {call}" if cleaned else call
        if new_value != str(existing):
            winreg.SetValueEx(key, "AutoRun", 0, winreg.REG_SZ, new_value)
            print("[INFO] CMD AutoRun updated for workspace shortcuts")
        else:
            print("[INFO] CMD AutoRun already includes workspaces autorun")
    finally:
        winreg.CloseKey(key)


def install_shell(db: dict[str, Any]) -> None:
    ensure_user_bin_on_path()
    for ws in db["workspaces"]:
        write_workspace_helpers(ws)
    generate_hooks(db)
    hook = HOOKS_DIR / "profile.ps1"
    body = f'. "{hook}"'
    for profile in POWERSHELL_PROFILES:
        scrub_legacy_profile(profile)
        upsert_marked_block(profile, body)
        print(f"[INFO] PowerShell profile hooked: {profile}")
    install_cmd_autorun()
    py = python_for_launchers()
    launcher_lines = [
        "@echo off",
        f'if not defined {USER_ENV_ROOT} set "{USER_ENV_ROOT}={ROOT}"',
        f'"{py}" "{SCRIPTS_DIR / "venv.py"}" %*',
        "",
    ]
    launcher_body = "\n".join(launcher_lines)
    atomic_write(USER_BIN / "venv.cmd", launcher_body)
    atomic_write(USER_BIN / "ws.cmd", launcher_body)
    atomic_write(
        USER_BIN / "litellm.cmd",
        "\n".join(
            [
                "@echo off",
                f'if not defined {USER_ENV_ROOT} set "{USER_ENV_ROOT}={ROOT}"',
                f'"{py}" "{SCRIPTS_DIR / "litellm.py"}" %*',
                "",
            ]
        ),
    )
    print(f"[INFO] Manager CLI: venv     ->  {SCRIPTS_DIR / 'venv.py'}")
    print(f"[INFO] LiteLLM CLI: litellm  ->  {SCRIPTS_DIR / 'litellm.py'}")
    print("[INFO] Open a NEW terminal, then run: venv --list")
    print("[INFO]                          litellm --models")


# ---------------------------------------------------------------------------
# Aliases (CMD / PowerShell shortcuts)
# ---------------------------------------------------------------------------

def write_custom_alias_launcher(name: str, command: str) -> Path:
    bin_dir = user_bin_dir()
    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher = bin_dir / f"{name}.cmd"
    body = "\n".join(
        [
            "@echo off",
            f'if not defined {USER_ENV_ROOT} set "{USER_ENV_ROOT}={ROOT}"',
            command,
            "",
        ]
    )
    atomic_write(launcher, body)
    return launcher


def validate_alias_name(name: str, db: dict[str, Any], *, allow_existing: str | None = None) -> str:
    name = validate_name(name)
    if name.lower() in {"list", "add", "delete"}:
        raise SystemExit(f"'{name}' is reserved for alias subcommands.")
    for ws in db.get("workspaces") or []:
        shortcut = (ws.get("activate") or {}).get("shortcut") or ws.get("name") or ""
        if shortcut.lower() == name.lower() and (allow_existing or "").lower() != name.lower():
            raise SystemExit(
                f"Alias '{name}' conflicts with workspace shortcut '{shortcut}'. "
                "Choose another name or change the workspace shortcut."
            )
    return name


def alias_rows(db: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for ws in db.get("workspaces") or []:
        shortcut = (ws.get("activate") or {}).get("shortcut") or ws.get("name") or ""
        if not SHORTCUT_RE.fullmatch(shortcut):
            continue
        activate_cmd = workspace_paths(ws)["folder"] / "activate.cmd"
        rows.append(
            {
                "kind": "workspace",
                "alias": shortcut,
                "command": f'call "{activate_cmd}"',
                "detail": f"workspace {ws.get('name')}",
            }
        )
    for item in db.get("aliases") or []:
        name = str(item.get("name") or "").strip()
        command = str(item.get("command") or "").strip()
        if not name:
            continue
        rows.append(
            {
                "kind": "custom",
                "alias": name,
                "command": command,
                "detail": str(item.get("description") or "custom alias"),
            }
        )
    return rows


def find_custom_alias(db: dict[str, Any], target: str) -> tuple[int, dict[str, Any]]:
    needle = (target or "").strip()
    aliases = db.setdefault("aliases", [])
    if needle.isdigit():
        rows = alias_rows(db)
        idx = int(needle)
        if idx < 1 or idx > len(rows):
            raise SystemExit(f"No alias at index {idx}.")
        row = rows[idx - 1]
        if row["kind"] != "custom":
            raise SystemExit(
                f"Index {idx} is a workspace shortcut ({row['alias']}). "
                "Remove the workspace with: venv delete NAME"
            )
        for i, item in enumerate(aliases):
            if str(item.get("name", "")).lower() == row["alias"].lower():
                return i, item
        raise SystemExit(f"Custom alias '{row['alias']}' not found in database.")
    for i, item in enumerate(aliases):
        if str(item.get("name", "")).lower() == needle.lower():
            return i, item
    raise SystemExit(f"No custom alias named '{target}'. Use: venv -A list")


def cmd_alias_list(db: dict[str, Any]) -> int:
    rows = alias_rows(db)
    if not rows:
        print("No aliases registered. Workspace shortcuts appear after: venv install-shell")
        return 0
    headers = ("Item", "Alias", "Command", "Detail")
    widths = [len(h) for h in headers]
    for row in rows:
        values = (
            str(rows.index(row) + 1),
            row["alias"],
            row["command"],
            row["detail"],
        )
        for i, value in enumerate(values):
            widths[i] = max(widths[i], min(len(value), 80 if i == 2 else len(value)))
    widths[2] = min(widths[2], 80)
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print("  ".join("-" * w for w in widths))
    for n, row in enumerate(rows, start=1):
        cmd = row["command"]
        if len(cmd) > widths[2]:
            cmd = cmd[: widths[2] - 3] + "..."
        print(fmt.format(str(n), row["alias"], cmd, row["detail"]))
    return 0


def cmd_alias_add(db: dict[str, Any], name: str, command: str, description: str = "") -> int:
    name = validate_alias_name(name, db)
    command = (command or "").strip()
    if not command:
        raise SystemExit("Command is required. Example: venv -A add --name gs --command \"python C:\\\\tools\\\\gs.py\"")
    aliases = db.setdefault("aliases", [])
    record = {"name": name, "command": command, "description": description or ""}
    replaced = False
    for i, item in enumerate(aliases):
        if str(item.get("name", "")).lower() == name.lower():
            aliases[i] = record
            replaced = True
            break
    if not replaced:
        aliases.append(record)
    write_custom_alias_launcher(name, command)
    save_db(db)
    generate_hooks(db)
    action = "Updated" if replaced else "Added"
    print(f"[OK] {action} alias '{name}'. Open a NEW cmd window, then run: {name}")
    return 0


def cmd_alias_delete(db: dict[str, Any], target: str) -> int:
    idx, item = find_custom_alias(db, target)
    name = str(item.get("name"))
    db["aliases"].pop(idx)
    save_db(db)
    launcher = user_bin_dir() / f"{name}.cmd"
    if launcher.exists():
        launcher.unlink()
        print(f"[DONE] Removed launcher {launcher}")
    generate_hooks(db)
    print(f"[OK] Deleted custom alias '{name}'")
    return 0


# ---------------------------------------------------------------------------
# Export / import
# ---------------------------------------------------------------------------

EXPORT_MANIFEST = "workspace-export.json"
EXPORT_ALL_MANIFEST = "workspaces-export-all.json"


def _export_workspace_into_zip(
    zf: zipfile.ZipFile,
    ws: dict[str, Any],
    path_prefix: str = "",
) -> dict[str, Any]:
    """Write one workspace tree into an open ZipFile. Returns per-workspace manifest."""
    paths = workspace_paths(ws)
    prefix = path_prefix.replace("\\", "/").strip("/")
    if prefix:
        prefix = prefix + "/"
    include_venv = bool(WORKSPACE_CONFIG.get("export_include_venv", True))
    include_env = bool(WORKSPACE_CONFIG.get("export_include_env", True))
    include_claude = bool(WORKSPACE_CONFIG.get("export_include_claude_config", False))
    manifest: dict[str, Any] = {
        "export_format_version": EXPORT_FORMAT_VERSION,
        "exported_at": utc_now(),
        "workspace": json.loads(json.dumps(ws)),
        "config_snapshot": {
            "venv_dir_name": WORKSPACE_CONFIG.get("venv_dir_name", ".venv"),
            "env_file_name": WORKSPACE_CONFIG.get("env_file_name", ".env"),
            "requirements_file": WORKSPACE_CONFIG.get("requirements_file", "requirements.txt"),
        },
        "files_included": [],
    }
    for rel_name, path in (
        ("requirements.txt", paths["requirements"]),
        ("activate.ps1", paths["activate_ps1"]),
        ("activate.cmd", paths["activate_cmd"]),
        ("env.cmd", paths["env_cmd"]),
    ):
        if path.is_file():
            zf.write(path, arcname=f"{prefix}workspace/{rel_name}")
            manifest["files_included"].append(rel_name)
    if include_env and paths["env"].is_file() and paths["env"].stat().st_size > 0:
        zf.write(paths["env"], arcname=f"{prefix}workspace/.env")
        manifest["files_included"].append(".env")
    if include_claude:
        claude_dir = paths["folder"] / ".claude-code"
        if claude_dir.is_dir():
            for child in claude_dir.rglob("*"):
                if child.is_file():
                    arc = prefix + "workspace/.claude-code/" + child.relative_to(claude_dir).as_posix()
                    zf.write(child, arcname=arc)
            manifest["files_included"].append(".claude-code/")
    if include_venv and paths["venv"].is_dir():
        for child in paths["venv"].rglob("*"):
            if child.is_file():
                arc = prefix + "dotvenv/" + child.relative_to(paths["venv"]).as_posix()
                zf.write(child, arcname=arc)
        manifest["files_included"].append(".venv/")
    zf.writestr(f"{prefix}{EXPORT_MANIFEST}", json.dumps(manifest, indent=2))
    return manifest


def cmd_export(db: dict[str, Any], target: str, zip_path: str) -> int:
    if (target or "").strip().lower() == "all":
        return cmd_export_all(db, zip_path)
    ws = find_workspace(db, target)
    dest = _abspath(zip_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        manifest = _export_workspace_into_zip(zf, ws, "")
    print(f"[OK] Exported workspace '{ws['name']}' to {dest}")
    print(f"     Included: {', '.join(manifest['files_included']) or '(manifest only)'}")
    print(
        "     Note: .venv folders often need recreation on another PC "
        "(venv --import --recreate-venv)."
    )
    return 0


def cmd_export_all(db: dict[str, Any], zip_path: str) -> int:
    if not db.get("workspaces"):
        raise SystemExit("No workspaces registered. Nothing to export.")
    dest = _abspath(zip_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, str]] = []
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for ws in db["workspaces"]:
            name = str(ws.get("name") or "")
            if not name:
                continue
            arc_prefix = f"workspaces/{name}"
            _export_workspace_into_zip(zf, ws, arc_prefix)
            entries.append({"name": name, "manifest": f"{arc_prefix}/{EXPORT_MANIFEST}"})
        bundle = {
            "export_format_version": EXPORT_FORMAT_VERSION,
            "kind": "all",
            "exported_at": utc_now(),
            "workspace_count": len(entries),
            "entries": entries,
            "aliases": json.loads(json.dumps(db.get("aliases") or [])),
        }
        zf.writestr(EXPORT_ALL_MANIFEST, json.dumps(bundle, indent=2))
    print(f"[OK] Exported {len(entries)} workspace(s) to {dest}")
    print(f"     Manifest: {EXPORT_ALL_MANIFEST}")
    return 0


def remove_workspace_if_exists(
    db: dict[str, Any], name: str, *, force_folder: bool = False
) -> bool:
    """Remove a workspace from the registry and disk. Returns True if one was removed."""
    needle = name.strip().lower()
    ws = None
    for item in db.get("workspaces") or []:
        if str(item.get("name", "")).lower() == needle:
            ws = item
            break
    if not ws:
        return False
    paths = workspace_paths(ws)
    folder = paths["folder"]
    if folder.exists():
        under_venvs = VENVS_ROOT.resolve() in folder.resolve().parents
        under_root = ROOT.resolve() in folder.resolve().parents
        if under_venvs or under_root or force_folder:
            shutil.rmtree(folder)
            print(f"[DONE] Removed folder {folder}")
        else:
            raise SystemExit(
                f"Refusing to delete folder outside workspaces root: {folder}\n"
                "Use venv delete with confirmation, or import to a path under your root."
            )
    db["workspaces"] = [
        item for item in db["workspaces"] if str(item.get("name", "")).lower() != needle
    ]
    shortcut = (ws.get("activate") or {}).get("shortcut") or ws.get("name")
    launcher = user_bin_dir() / f"{shortcut}.cmd"
    if launcher.exists():
        launcher.unlink()
    return True


def _import_workspace_from_zip(
    db: dict[str, Any],
    zf: zipfile.ZipFile,
    arc_prefix: str,
    src_label: str,
    folder: Optional[str],
    recreate: bool,
    override: bool,
) -> dict[str, Any]:
    prefix = arc_prefix.replace("\\", "/").strip("/")
    if prefix:
        prefix = prefix + "/"
    manifest_path = f"{prefix}{EXPORT_MANIFEST}"
    try:
        manifest = json.loads(zf.read(manifest_path).decode("utf-8"))
    except KeyError:
        raise SystemExit(f"Invalid export (missing {manifest_path})")
    ws = manifest.get("workspace")
    if not isinstance(ws, dict):
        raise SystemExit("Invalid export manifest: missing workspace record")
    name = validate_name(str(ws.get("name") or ""))
    exists = any(
        str(item.get("name", "")).lower() == name.lower() for item in db.get("workspaces") or []
    )
    if exists:
        if not override:
            raise SystemExit(
                f"Workspace '{name}' already exists. Use --override to replace it, or venv delete {name}."
            )
        if remove_workspace_if_exists(db, name, force_folder=True):
            print(f"[OK] Replaced existing workspace '{name}' (--override)")
        save_db(db)

    dest_folder = _abspath(folder) if folder else _abspath(ws.get("folder") or (VENVS_ROOT / name))
    dest_folder.mkdir(parents=True, exist_ok=True)
    venv_dir = str((manifest.get("config_snapshot") or {}).get("venv_dir_name") or ".venv")
    dotvenv_root = dest_folder / venv_dir
    extracted_any = False
    ws_prefix = f"{prefix}workspace/"
    venv_prefix = f"{prefix}dotvenv/"
    for info in zf.infolist():
        if info.filename.startswith(ws_prefix):
            rel = info.filename[len(ws_prefix) :]
            if not rel or rel.endswith("/"):
                continue
            target = dest_folder / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src_fp, open(target, "wb") as out_fp:
                shutil.copyfileobj(src_fp, out_fp)
            extracted_any = True
        elif info.filename.startswith(venv_prefix):
            rel = info.filename[len(venv_prefix) :]
            if not rel:
                continue
            target = dotvenv_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src_fp, open(target, "wb") as out_fp:
                shutil.copyfileobj(src_fp, out_fp)
            extracted_any = True
    if not extracted_any:
        print(f"[WARN] Export for '{name}' contained no workspace/ files; continuing with manifest only.")
    activate = ws.get("activate") or {}
    record = make_workspace_record(
        db,
        name=name,
        description=str(ws.get("description") or ""),
        workdir=str(activate.get("workdir") or dest_folder),
        pythonpath=list(activate.get("pythonpath") or []),
        shortcut=str(activate.get("shortcut") or name),
        do_cd=bool(activate.get("cd", True)),
        source={"kind": "venv.py import", "from": src_label},
        folder=dest_folder,
    )
    record["venv"]["python"] = python_version_of(dotvenv_root / "Scripts" / "python.exe")
    write_workspace_helpers(record)
    python_exe = dotvenv_root / "Scripts" / "python.exe"
    if recreate or not python_exe.exists():
        if dotvenv_root.exists():
            shutil.rmtree(dotvenv_root, ignore_errors=True)
        create_virtualenv(default_python_executable(), dotvenv_root, prompt=name)
        req = dest_folder / "requirements.txt"
        install_packages(dotvenv_root / "Scripts" / "python.exe", req, [])
        if (dotvenv_root / "Scripts" / "python.exe").exists():
            freeze_requirements(dotvenv_root / "Scripts" / "python.exe", req)
        record["venv"]["python"] = python_version_of(dotvenv_root / "Scripts" / "python.exe")
        write_workspace_helpers(record)
    db["workspaces"].append(record)
    return record


def cmd_import(
    db: dict[str, Any],
    zip_path: str,
    folder: Optional[str],
    recreate: bool,
    override: bool,
) -> int:
    src = _abspath(zip_path)
    if not src.is_file():
        raise SystemExit(f"Import file not found: {src}")
    with zipfile.ZipFile(src, "r") as zf:
        names = set(zf.namelist())
        if EXPORT_ALL_MANIFEST in names:
            bundle = json.loads(zf.read(EXPORT_ALL_MANIFEST).decode("utf-8"))
            entries = bundle.get("entries") or []
            if not entries:
                raise SystemExit("Bundle export contains no workspaces.")
            imported: list[str] = []
            for entry in entries:
                entry_name = str(entry.get("name") or "")
                manifest_path = str(entry.get("manifest") or "")
                if not entry_name or not manifest_path:
                    continue
                arc_prefix = manifest_path[: -len("/" + EXPORT_MANIFEST)]
                dest = None
                if folder:
                    dest = str(_abspath(folder) / entry_name)
                record = _import_workspace_from_zip(
                    db,
                    zf,
                    arc_prefix,
                    str(src),
                    dest,
                    recreate,
                    override,
                )
                imported.append(record["name"])
            alias_items = bundle.get("aliases") or []
            if alias_items:
                db.setdefault("aliases", [])
                existing = {str(a.get("name", "")).lower() for a in db["aliases"]}
                for item in alias_items:
                    aname = str(item.get("name") or "").strip()
                    if aname and aname.lower() not in existing:
                        db["aliases"].append(item)
                        existing.add(aname.lower())
            save_db(db)
            generate_hooks(db)
            print(f"[OK] Imported {len(imported)} workspace(s) from bundle: {', '.join(imported)}")
            return 0

        record = _import_workspace_from_zip(
            db, zf, "", str(src), folder, recreate, override
        )
        save_db(db)
        generate_hooks(db)
    shortcut = (record.get("activate") or {}).get("shortcut") or record["name"]
    print(f"[OK] Imported workspace '{record['name']}' into {record.get('folder')}")
    print(f"     Activate in a NEW terminal with: {shortcut}")
    return 0


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def validate_name(name: str) -> str:
    name = (name or "").strip()
    if name.lower() in RESERVED_NAMES:
        raise SystemExit(f"'{name}' is reserved. Choose another workspace name.")
    if not SHORTCUT_RE.fullmatch(name):
        raise SystemExit(
            "Workspace name must start with a letter and contain only letters, "
            "numbers, hyphen, or underscore (max 32 chars)."
        )
    return name


def prompt(label: str, default: str = "", required: bool = True) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    if not value:
        value = default
    if required and not value:
        raise SystemExit(f"{label} is required.")
    return value


def make_workspace_record(
    db: dict[str, Any],
    name: str,
    description: str,
    workdir: Optional[str],
    pythonpath: list[str],
    shortcut: str,
    do_cd: bool,
    source: Optional[dict[str, str]] = None,
    folder: Optional[str | Path] = None,
) -> dict[str, Any]:
    venv_dir = str(WORKSPACE_CONFIG.get("venv_dir_name") or ".venv").strip() or ".venv"
    folder_path = _abspath(folder) if folder else (VENVS_ROOT / name)
    venv_path = folder_path / venv_dir
    now = utc_now()
    return {
        "id": next_id(db),
        "name": name,
        "description": description,
        "folder": str(folder_path),
        "status": "ready",
        "created_at": now,
        "updated_at": now,
        "venv": {
            "path": str(venv_path),
            "python": "",
        },
        "files": {
            "env": ".env",
            "env_cmd": "env.cmd",
            "requirements": "requirements.txt",
        },
        "activate": {
            "shortcut": shortcut,
            "workdir": workdir or str(folder_path),
            "cd": do_cd,
            "pythonpath": pythonpath,
        },
        "source": source or {},
    }


def create_virtualenv(python_exe: str, venv_path: Path, prompt: str = "") -> None:
    if venv_path.exists() and (venv_path / "Scripts" / "python.exe").exists():
        print(f"[INFO] Reusing existing venv at {venv_path}")
        if prompt:
            set_venv_prompt(venv_path, prompt)
        return
    cmd = [python_exe, "-m", "venv"]
    if prompt:
        cmd.extend(["--prompt", prompt])
    cmd.append(str(venv_path))
    run(cmd, f"Create venv at {venv_path}")


def install_packages(python: Path, requirements: Optional[Path], packages: list[str]) -> None:
    run([str(python), "-m", "pip", "install", "--upgrade", "pip"], "Upgrade pip")
    if requirements and requirements.exists() and requirements.stat().st_size > 0:
        run(
            [str(python), "-m", "pip", "install", "-r", str(requirements)],
            f"Install {requirements.name}",
        )
    if packages:
        run(
            [str(python), "-m", "pip", "install", "--upgrade", *packages],
            "Install extra packages",
        )


def cmd_list(db: dict[str, Any], as_json: bool = False) -> int:
    rows = []
    for ws in db["workspaces"]:
        rows.append(
            {
                "id": ws.get("id", ""),
                "name": ws.get("name", ""),
                "folder": ws.get("folder", ""),
                "description": ws.get("description", ""),
            }
        )
    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No workspaces registered. Run: python venv.py --add")
        return 0
    headers = ("ID", "NAME", "FOLDER", "DESCRIPTION")
    widths = [len(h) for h in headers]
    table = []
    for row in rows:
        values = (row["id"], row["name"], row["folder"], row["description"] or "")
        table.append(values)
        for i, value in enumerate(values):
            widths[i] = max(widths[i], len(value))
    widths[3] = min(widths[3], int(WORKSPACE_CONFIG.get("list_description_max_width") or 60))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print("  ".join("-" * w for w in widths))
    for values in table:
        desc = values[3]
        if len(desc) > widths[3]:
            desc = desc[: widths[3] - 3] + "..."
        print(fmt.format(values[0], values[1], values[2], desc))
    return 0


def cmd_info(ws: dict[str, Any]) -> int:
    paths = workspace_paths(ws)
    env = read_env(paths["env"])
    activate = ws.get("activate") or {}
    print(f"ID           : {ws.get('id')}")
    print(f"Name         : {ws.get('name')}")
    print(f"Description  : {ws.get('description')}")
    print(f"Folder       : {ws.get('folder')}")
    print(f"Status       : {ws.get('status')}")
    print(f"Python       : {(ws.get('venv') or {}).get('python') or paths['python']}")
    print(f"Shortcut     : {activate.get('shortcut')}")
    print(f"Workdir      : {activate.get('workdir')}")
    print(f"CD on enter  : {activate.get('cd', True)}")
    print(f"PYTHONPATH   : {';'.join(activate.get('pythonpath') or []) or '(none)'}")
    print(f"Env file     : {paths['env']}  ({len(env)} variable(s))")
    print(f"Requirements : {paths['requirements']}")
    print("Env vars     :")
    for key in env:
        print(f"  - {key}={env[key]}")
    return 0


def cmd_var_list(ws: dict[str, Any]) -> int:
    env = read_env(workspace_paths(ws)["env"])
    if not env:
        print(f"No variables in {ws['name']} (.env is empty).")
        return 0
    width = max(len(k) for k in env)
    for key, value in env.items():
        print(f"{key:<{width}}  {value}")
    return 0


def cmd_var_add(db: dict[str, Any], ws: dict[str, Any], key: str, value: str) -> int:
    key = (key or "").strip()
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
        raise SystemExit(f"Invalid variable name: {key}")
    paths = workspace_paths(ws)
    env = read_env(paths["env"])
    existed = key in env
    env[key] = value
    write_env(paths["env"], env)
    write_workspace_helpers(ws)
    ws["updated_at"] = utc_now()
    save_db(db)
    action = "Updated" if existed else "Added"
    print(f"[OK] {action} {key} on workspace {ws['name']} (value not shown).")
    return 0


def cmd_var_remove(db: dict[str, Any], ws: dict[str, Any], key: str) -> int:
    paths = workspace_paths(ws)
    env = read_env(paths["env"])
    if key not in env:
        raise SystemExit(f"{key} is not set on {ws['name']}")
    del env[key]
    write_env(paths["env"], env)
    write_workspace_helpers(ws)
    ws["updated_at"] = utc_now()
    save_db(db)
    print(f"[OK] Removed {key} from {ws['name']}")
    return 0


def cmd_delete(db: dict[str, Any], target: str, yes: bool) -> int:
    ws = find_workspace(db, target)
    paths = workspace_paths(ws)
    folder = paths["folder"]
    print(f"Will delete workspace {ws['id']} ({ws['name']})")
    print(f"  Folder : {folder}")
    if not yes:
        answer = input(f"Type '{ws['name']}' to confirm: ").strip()
        if answer != ws["name"]:
            print("Aborted.")
            return 1
    remove_workspace_if_exists(db, ws["name"], force_folder=False)
    save_db(db)
    generate_hooks(db)
    print(f"[OK] Deleted workspace {ws['name']}")
    return 0


def cmd_add(args: argparse.Namespace, db: dict[str, Any]) -> int:
    interactive = not args.name
    name = validate_name(args.name or prompt("Workspace name"))
    for existing in db["workspaces"]:
        if existing["name"].lower() == name.lower():
            raise SystemExit(f"Workspace '{name}' already exists (id {existing['id']}).")
    description = args.description
    if interactive and not description:
        description = prompt("Description", required=False)
    description = description or ""

    env_src = args.env
    if interactive and not env_src:
        env_src = prompt(".env file (optional, path or empty)", required=False)
    req_src = args.requirements
    if interactive and not req_src:
        req_src = prompt("requirements.txt (optional, path or empty)", required=False)

    workdir = args.workdir
    if interactive and not workdir:
        workdir = prompt("Workdir (cd on activate)", default=str(VENVS_ROOT / name), required=False)
    pythonpath = list(args.pythonpath or [])
    shortcut = validate_name(args.shortcut or name)
    do_cd = not args.no_cd
    python_exe = args.python or default_python_executable()

    folder_override = args.folder
    if folder_override:
        folder_override = str(_abspath(folder_override))

    env: dict[str, str] = {}
    if args.env_from_cmd:
        cmd_path = Path(args.env_from_cmd)
        env.update(parse_cmd_set_lines(cmd_path.read_text(encoding="utf-8", errors="replace")))
        print(f"[INFO] Imported {len(env)} variables from {cmd_path} (values not shown).")
    if env_src:
        src = Path(env_src).expanduser().resolve()
        if not src.exists():
            raise SystemExit(f".env file not found: {src}")
        loaded = read_env(src)
        env.update(loaded)
        print(f"[INFO] Loaded {len(loaded)} variables from {src.name} (values not shown).")

    ws = make_workspace_record(
        db,
        name=name,
        description=description,
        workdir=workdir,
        pythonpath=pythonpath,
        shortcut=shortcut,
        do_cd=do_cd,
        source={"kind": "venv.py add"},
        folder=folder_override,
    )
    paths = workspace_paths(ws)
    paths["folder"].mkdir(parents=True, exist_ok=True)
    if env:
        write_env(paths["env"], env)
    elif not paths["env"].exists():
        paths["env"].write_text("", encoding="utf-8")

    if req_src:
        src = Path(req_src).expanduser().resolve()
        dest = paths["requirements"].resolve()
        if not src.exists():
            raise SystemExit(f"requirements.txt not found: {src}")
        if src != dest:
            shutil.copy2(src, paths["requirements"])
    elif not paths["requirements"].exists():
        paths["requirements"].write_text("", encoding="utf-8")

    create_virtualenv(python_exe, paths["venv"], prompt=name)
    packages = list(args.packages or [])
    if not args.skip_install:
        install_packages(paths["python"], paths["requirements"], packages)
        if paths["python"].exists():
            freeze_requirements(paths["python"], paths["requirements"])
    ws["venv"]["python"] = python_version_of(paths["python"])
    write_workspace_helpers(ws)
    db["workspaces"].append(ws)
    save_db(db)
    generate_hooks(db)
    print(f"[OK] Workspace {ws['id']} '{ws['name']}' ready at {ws['folder']}")
    print(f"     Activate in a new terminal with: {shortcut}")
    return 0


def cmd_doctor(db: dict[str, Any]) -> int:
    errors = 0
    if not db["workspaces"]:
        print("No workspaces registered.")
        return 0
    for ws in db["workspaces"]:
        paths = workspace_paths(ws)
        issues = []
        if not paths["venv"].exists():
            issues.append("missing .venv")
        if not paths["python"].exists():
            issues.append("missing python.exe")
        if not paths["activate_ps1"].exists():
            issues.append("missing activate.ps1")
        if not paths["activate_cmd"].exists():
            issues.append("missing activate.cmd")
        status = "OK" if not issues else "FAIL"
        if issues:
            errors += 1
        print(f"{ws['id']}  {ws['name']:<12} {status}  {', '.join(issues) if issues else 'venv + env + shortcuts'}")
    return 1 if errors else 0


def cmd_freeze(ws: dict[str, Any]) -> int:
    paths = workspace_paths(ws)
    if not paths["python"].exists():
        raise SystemExit(f"No python in {ws['name']}")
    freeze_requirements(paths["python"], paths["requirements"])
    print(f"[OK] Wrote {paths['requirements']}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def peel_root(argv: list[str]) -> tuple[str | None, list[str]]:
    root: str | None = None
    rest: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--root" and i + 1 < len(argv):
            root = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--root="):
            root = arg.split("=", 1)[1]
            i += 1
            continue
        rest.append(arg)
        i += 1
    return root, rest


def rewrite_argv(argv: list[str]) -> list[str]:
    """Accept both `venv.py --list` and `venv.py list` forms."""
    if not argv:
        return argv
    subs = {
        "list",
        "add",
        "delete",
        "var",
        "info",
        "path",
        "doctor",
        "install-shell",
        "sync",
        "freeze",
        "alias",
        "export",
        "import",
    }
    if argv[0] in subs or argv[0] in {"-h", "--help"}:
        return argv

    if "--install-shell" in argv:
        rest = [a for a in argv if a != "--install-shell"]
        return ["install-shell", *rest]

    if "--var" in argv:
        i = argv.index("--var")
        if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
            raise SystemExit("venv.py --var requires a workspace id or name")
        target = argv[i + 1]
        rest = argv[:i] + argv[i + 2 :]
        action = "list"
        extra: list[str] = []
        if "--remove" in rest:
            j = rest.index("--remove")
            extra = rest[j + 1 :]
            rest = rest[:j]
            action = "remove"
        elif "--add" in rest:
            j = rest.index("--add")
            extra = rest[j + 1 :]
            rest = rest[:j]
            action = "add"
        elif "--list" in rest:
            rest = [a for a in rest if a != "--list"]
            action = "list"
        return ["var", target, action, *extra, *rest]

    if "--delete" in argv:
        i = argv.index("--delete")
        if i + 1 >= len(argv):
            raise SystemExit("venv.py --delete requires a workspace id or name")
        target = argv[i + 1]
        rest = argv[:i] + argv[i + 2 :]
        return ["delete", target, *rest]

    if "--add" in argv:
        rest = [a for a in argv if a != "--add"]
        return ["add", *rest]

    if "--export" in argv:
        i = argv.index("--export")
        if i + 1 >= len(argv):
            raise SystemExit("venv --export requires a workspace id or name")
        target = argv[i + 1]
        rest = argv[:i] + argv[i + 2 :]
        file_path = None
        if "--file" in rest:
            j = rest.index("--file")
            if j + 1 >= len(rest):
                raise SystemExit("venv --export --file requires a zip path")
            file_path = rest[j + 1]
            rest = rest[:j] + rest[j + 2 :]
        return ["export", target, *(["--file", file_path] if file_path else []), *rest]

    if "--import" in argv:
        i = argv.index("--import")
        if i + 1 >= len(argv):
            raise SystemExit("venv --import requires a zip file path")
        zip_path = argv[i + 1]
        rest = argv[:i] + argv[i + 2 :]
        folder = None
        recreate = "--recreate-venv" in rest
        override = "--override" in rest
        rest = [a for a in rest if a not in ("--recreate-venv", "--override")]
        if "--folder" in rest:
            j = rest.index("--folder")
            if j + 1 >= len(rest):
                raise SystemExit("venv --import --folder requires a directory path")
            folder = rest[j + 1]
            rest = rest[:j] + rest[j + 2 :]
        out = ["import", zip_path]
        if folder:
            out.extend(["--folder", folder])
        if recreate:
            out.append("--recreate-venv")
        if override:
            out.append("--override")
        return [*out, *rest]

    if "-A" in argv or "--alias" in argv:
        key = "-A" if "-A" in argv else "--alias"
        i = argv.index(key)
        rest = argv[:i] + argv[i + 1 :]
        if not rest or rest[0].startswith("-"):
            return ["alias", "list", *rest]
        action = rest[0].lower()
        if action not in {"list", "add", "delete"}:
            raise SystemExit("venv -A requires list, add, or delete")
        rest = rest[1:]
        if action == "list":
            return ["alias", "list", *rest]
        if action == "add":
            name = None
            command = None
            description = ""
            if "--name" in rest:
                j = rest.index("--name")
                if j + 1 >= len(rest):
                    raise SystemExit("venv -A add --name requires a value")
                name = rest[j + 1]
                rest = rest[:j] + rest[j + 2 :]
            if "--command" in rest:
                j = rest.index("--command")
                if j + 1 >= len(rest):
                    raise SystemExit("venv -A add --command requires a value")
                command = rest[j + 1]
                rest = rest[:j] + rest[j + 2 :]
            if "--description" in rest:
                j = rest.index("--description")
                if j + 1 >= len(rest):
                    raise SystemExit("venv -A add --description requires a value")
                description = rest[j + 1]
                rest = rest[:j] + rest[j + 2 :]
            out = ["alias", "add"]
            if name:
                out.extend(["--alias-name", name])
            if command:
                out.extend(["--alias-command", command])
            if description:
                out.extend(["--alias-description", description])
            return [*out, *rest]
        if action == "delete":
            target = None
            if "--name" in rest:
                j = rest.index("--name")
                if j + 1 >= len(rest):
                    raise SystemExit("venv -A delete --name requires a value")
                target = rest[j + 1]
                rest = rest[:j] + rest[j + 2 :]
            elif rest and not rest[0].startswith("-"):
                target = rest[0]
                rest = rest[1:]
            if not target:
                raise SystemExit("Usage: venv -A delete --name ALIAS  or  venv -A delete INDEX")
            return ["alias", "delete", target, *rest]

    if "--list" in argv:
        rest = [a for a in argv if a != "--list"]
        return ["list", *rest]

    if "--sync" in argv:
        return ["sync", *[a for a in argv if a != "--sync"]]
    if "--doctor" in argv:
        return ["doctor", *[a for a in argv if a != "--doctor"]]
    if "--info" in argv:
        i = argv.index("--info")
        rest = argv[:i] + argv[i + 1 :]
        return ["info", *rest]
    return argv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="venv.py",
        description="Manage Python workspaces under a shared workspaces folder (any drive letter or UNC path).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  venv.py --list
  venv.py --add
  venv.py --add --name palo --description "Prisma SASE + Claude Code"
  venv.py --add --name demo --folder D:\\workspaces\\custom\\demo
  venv.py --delete palo
  venv.py --var palo --list
  venv.py --var palo --add ANTHROPIC_MODEL claude-opus-4-8
  venv.py --var palo --remove OLD_KEY
  venv.py -A list
  venv.py -A add --name kb --command "cd /d D:\\Projects && code ."
  venv.py -A delete --name kb
  venv.py --export palo --file D:\\backup\\palo.zip
  venv.py --export all --file D:\\backup\\all-workspaces.zip
  venv.py --import D:\\backup\\palo.zip --folder D:\\workspaces\\virtual_envs\\palo --override
  venv.py info palo
  venv.py doctor
""",
    )
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation (delete)")
    parser.add_argument("--json", action="store_true", help="JSON output for --list")
    parser.add_argument(
        "--root",
        help="Workspaces root folder (default: WORKSPACES_ROOT, %%USERPROFILE%%\\.workspaces\\install.json, or this script's parent)",
    )
    sub = parser.add_subparsers(dest="command")

    list_p = sub.add_parser("list", help="List workspaces (ID, name, folder, description)")
    list_p.add_argument("--json", action="store_true", help="JSON output")
    add_p = sub.add_parser("add", help="Create a workspace (interactive if flags omitted)")
    add_p.add_argument("--name")
    add_p.add_argument("--description", default="")
    add_p.add_argument("--folder", help="Override workspace directory (default: <root>/virtual_envs/<name>)")
    add_p.add_argument("--env", help="Optional existing .env to copy")
    add_p.add_argument("--env-from-cmd", help="Import set \"KEY=value\" lines from a CMD launcher")
    add_p.add_argument("--requirements", help="Optional requirements.txt to install")
    add_p.add_argument("--workdir", help="Directory to cd into on activate")
    add_p.add_argument("--python", help="Python executable used to create the venv")
    add_p.add_argument("--packages", nargs="*", default=[], help="Extra pip packages")
    add_p.add_argument("--pythonpath", nargs="*", default=[], help="Paths prepended to PYTHONPATH")
    add_p.add_argument("--shortcut", help="Shell command name (default: workspace name)")
    add_p.add_argument("--no-cd", action="store_true", help="Do not cd on activate")
    add_p.add_argument("--skip-install", action="store_true", help="Create venv but skip pip install")

    del_p = sub.add_parser("delete", help="Delete a workspace by id or name")
    del_p.add_argument("delete_target")
    del_p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")

    var_p = sub.add_parser("var", help="List/add/remove environment variables")
    var_p.add_argument("var_target")
    var_p.add_argument(
        "var_action", nargs="?", default="list", choices=["list", "add", "remove"]
    )
    var_p.add_argument("key", nargs="?")
    var_p.add_argument("value", nargs="?")

    info_p = sub.add_parser("info", help="Show workspace details (secrets masked)")
    info_p.add_argument("info_target")

    path_p = sub.add_parser("path", help="Print workspace folder")
    path_p.add_argument("path_target")

    freeze_p = sub.add_parser("freeze", help="Rewrite requirements.txt from pip freeze")
    freeze_p.add_argument("freeze_target")

    sub.add_parser("doctor", help="Check venvs, activate scripts, and env files")
    sub.add_parser("install-shell", help="Install palo/lab1/... shortcuts for CMD and PowerShell")
    sub.add_parser("sync", help="Regenerate activate scripts and shell hooks from the database")

    alias_p = sub.add_parser("alias", help="Manage CMD/PowerShell aliases (-A)")
    alias_p.add_argument(
        "alias_action", nargs="?", default="list", choices=["list", "add", "delete"]
    )
    alias_p.add_argument("alias_target", nargs="?", help="Delete target: alias name or list index")
    alias_p.add_argument("--alias-name", dest="alias_name", help="Alias name (add)")
    alias_p.add_argument("--alias-command", dest="alias_command", help="Command body for alias .cmd (add)")
    alias_p.add_argument("--alias-description", dest="alias_description", default="", help="Optional note (add)")

    export_p = sub.add_parser("export", help="Export a workspace to a zip file")
    export_p.add_argument("export_target", help='Workspace id, name, or "all"')
    export_p.add_argument("--file", required=True, help="Output .zip path")

    import_p = sub.add_parser("import", help="Import a workspace from an export zip")
    import_p.add_argument("zip_path", help="Path to export .zip")
    import_p.add_argument("--folder", help="Destination folder for the imported workspace")
    import_p.add_argument(
        "--recreate-venv",
        action="store_true",
        help="Always recreate .venv from requirements.txt on this PC",
    )
    import_p.add_argument(
        "--override",
        action="store_true",
        help="Replace an existing workspace with the same name (deletes old folder and registry entry)",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    root_arg, rest = peel_root(raw)
    rewritten = rewrite_argv(rest)
    if root_arg:
        rewritten = ["--root", root_arg, *rewritten]
    parser = build_parser()
    args = parser.parse_args(rewritten)
    if not args.command:
        parser.print_help()
        return 0

    configure_paths(getattr(args, "root", None))
    VENVS_ROOT.mkdir(parents=True, exist_ok=True)
    db = load_db()

    if args.command == "list":
        as_json = bool(getattr(args, "json", False))
        return cmd_list(db, as_json=as_json)
    if args.command == "add":
        return cmd_add(args, db)
    if args.command == "delete":
        return cmd_delete(db, args.delete_target, yes=bool(getattr(args, "yes", False)))
    if args.command == "var":
        ws = find_workspace(db, args.var_target)
        if args.var_action == "list":
            return cmd_var_list(ws)
        if args.var_action == "add":
            if not args.key or args.value is None:
                raise SystemExit("Usage: venv.py --var NAME --add VARIABLE_NAME VALUE")
            return cmd_var_add(db, ws, args.key, args.value)
        if args.var_action == "remove":
            if not args.key:
                raise SystemExit("Usage: venv.py --var NAME --remove VARIABLE_NAME")
            return cmd_var_remove(db, ws, args.key)
    if args.command == "info":
        return cmd_info(find_workspace(db, args.info_target))
    if args.command == "path":
        print(find_workspace(db, args.path_target)["folder"])
        return 0
    if args.command == "freeze":
        return cmd_freeze(find_workspace(db, args.freeze_target))
    if args.command == "doctor":
        return cmd_doctor(db)
    if args.command == "install-shell":
        install_shell(db)
        return 0
    if args.command == "sync":
        for ws in db["workspaces"]:
            write_workspace_helpers(ws)
        generate_hooks(db)
        print("[OK] Regenerated activate scripts and shell hooks")
        return 0
    if args.command == "alias":
        if args.alias_action == "list":
            return cmd_alias_list(db)
        if args.alias_action == "add":
            if not args.alias_name or not args.alias_command:
                raise SystemExit("Usage: venv -A add --name ALIAS --command \"...\"")
            return cmd_alias_add(
                db,
                args.alias_name,
                args.alias_command,
                args.alias_description or "",
            )
        if args.alias_action == "delete":
            if not args.alias_target:
                raise SystemExit("Usage: venv -A delete --name ALIAS  or  venv -A delete INDEX")
            return cmd_alias_delete(db, args.alias_target)
    if args.command == "export":
        if not args.file:
            raise SystemExit("Usage: venv --export NAME --file PATH.zip")
        return cmd_export(db, args.export_target, args.file)
    if args.command == "import":
        return cmd_import(
            db,
            args.zip_path,
            getattr(args, "folder", None),
            bool(getattr(args, "recreate_venv", False)),
            bool(getattr(args, "override", False)),
        )
    parser.print_help()
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nAborted.")
        raise SystemExit(130)
    except RuntimeError as exc:
        print(f"[ERROR] {exc}")
        raise SystemExit(1)
