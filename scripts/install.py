#!/usr/bin/env python3
"""Install or uninstall workspace CLIs on this Windows user.

Git clone workflow (Windows 11, any PC):

    git clone https://github.com/amirdeadline/workspaces-venv.git
    cd workspaces-venv
    copy scripts\\venv.config.json.example scripts\\venv.config.json
    rem Edit scripts\\venv.config.json (virtual_envs_dir, default_python, ...)
    copy scripts\\venvs.json.example scripts\\venvs.json
    python install.py

Or from the scripts folder:

    python scripts\\install.py --path C:\\path\\to\\workspaces-venv

The shared folder can be any path this PC uses:

    python Z:\\workspaces\\scripts\\install.py
    python \\\\server\\share\\workspaces\\scripts\\install.py
    python install.py --path Z:\\workspaces

On a NEW Windows PC:
    1. Map or mount the shared workspaces folder (E:, Z:, UNC, ...).
    2. Install Python 3.10+ (tick "Add python.exe to PATH").
    3. In cmd or PowerShell (no admin required):

           python <workspaces>\\scripts\\install.py --path <workspaces>

    4. Close that window. Open a NEW cmd or PowerShell, then:

           venv --list
           litellm --models
           palo

Uninstall (this user only; does not delete the shared folder or .env files):

           python <workspaces>\\scripts\\install.py --uninstall
           python <workspaces>\\scripts\\install.py --uninstall --path Z:\\workspaces

This script:
    - Records the workspaces root in user env WORKSPACES_ROOT (not hardcoded)
    - Adds %USERPROFILE%\\bin to the user PATH
    - Installs `venv` and `litellm` for CMD and PowerShell
    - Hooks PowerShell profiles and CMD AutoRun
    - Writes machine-local hooks in %USERPROFILE%\\.workspaces
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
from pathlib import Path

if os.name == "nt":
    import winreg
else:  # pragma: no cover
    winreg = None

USER_BIN = Path.home() / "bin"
LOCAL_STATE_DIR = Path.home() / ".workspaces"
LOCAL_CONFIG = LOCAL_STATE_DIR / "install.json"
USER_ENV_ROOT = "WORKSPACES_ROOT"
POWERSHELL_PROFILES = [
    Path.home() / "Documents" / "WindowsPowerShell" / "profile.ps1",
    Path.home() / "Documents" / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1",
    Path.home() / "Documents" / "PowerShell" / "profile.ps1",
    Path.home() / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1",
]
MARKER_BEGIN = "# >>> workspaces-venv >>>"
MARKER_END = "# <<< workspaces-venv <<<"
CORE_LAUNCHERS = ("venv.cmd", "ws.cmd", "litellm.cmd")
USER_ENV_VARS_WE_SET = (USER_ENV_ROOT, "WORKSPACES_SCRIPTS")
SESSION_ENV_CLEANUP = (
    USER_ENV_ROOT,
    "WORKSPACES_SCRIPTS",
    "WORKSPACE_ROOT",
    "WORKSPACE_NAME",
    "WORKSPACE_ID",
)


def python_exe() -> Path:
    return Path(sys.executable).resolve()


def _abspath(path: Path | str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _line_buffer_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except Exception:
            pass


def check_python() -> None:
    if sys.version_info < (3, 10):
        raise SystemExit(
            f"Python 3.10+ is required. This is {sys.version.split()[0]}.\n"
            "Install Python from https://www.python.org/downloads/ and "
            "check 'Add python.exe to PATH', then re-run install.py."
        )
    if os.name != "nt":
        raise SystemExit("This installer is for Windows only.")


def load_local_config() -> dict:
    if not LOCAL_CONFIG.is_file():
        return {}
    try:
        data = json.loads(LOCAL_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_local_config(data: dict) -> None:
    LOCAL_STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_CONFIG.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")


def detect_root(explicit: str | None) -> Path:
    if explicit:
        return _abspath(explicit)
    env = (os.environ.get(USER_ENV_ROOT) or "").strip()
    if env:
        return _abspath(env)
    stored = load_local_config().get("root")
    if stored:
        return _abspath(stored)
    return Path(__file__).resolve().parent.parent


def normalize_workspaces_root(path: Path) -> Path:
    path = _abspath(path)
    if (path / "scripts" / "venv.py").is_file():
        return path
    if path.name.lower() == "scripts" and (path / "venv.py").is_file():
        return path.parent
    raise SystemExit(
        f"Not a workspaces folder (missing scripts\\venv.py): {path}\n"
        "Example:  python install.py --path Z:\\workspaces"
    )


def find_script(root: Path, name: str) -> Path:
    path = root / "scripts" / name
    if not path.is_file():
        raise SystemExit(f"Missing {name}: {path}")
    return path


def bootstrap_workspaces_root(root: Path) -> None:
    """Prepare a fresh git clone (registry, config, virtual_envs folder)."""
    scripts = root / "scripts"
    config = scripts / "venv.config.json"
    example = scripts / "venv.config.json.example"
    if not config.is_file():
        raise SystemExit(
            f"Missing {config}\n\n"
            "After git clone, copy and edit the config template:\n"
            f"  copy {example} {config}\n\n"
            "Then re-run:  python install.py"
        )
    venvs_subdir = "virtual_envs"
    try:
        raw = json.loads(config.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            venvs_subdir = str(raw.get("virtual_envs_dir") or venvs_subdir).strip() or venvs_subdir
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[WARN] Could not parse {config.name}: {exc}")

    venvs_dir = root / venvs_subdir.replace("\\", "/").strip("/")
    venvs_dir.mkdir(parents=True, exist_ok=True)
    print(f"[OK] Workspace data folder: {venvs_dir}")

    registry = scripts / "venvs.json"
    reg_example = scripts / "venvs.json.example"
    if not registry.is_file():
        if reg_example.is_file():
            shutil.copy2(reg_example, registry)
            print(f"[OK] Created {registry.name} from {reg_example.name}")
        else:
            registry.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "workspaces": [],
                        "aliases": [],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            print(f"[OK] Created empty {registry.name}")

    hooks = scripts / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)


def broadcast_environment_change() -> None:
    ctypes.windll.user32.SendMessageTimeoutW(  # type: ignore[attr-defined]
        0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None
    )


def read_user_path() -> tuple[list[str], int]:
    if winreg is None:
        return [], 0
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE
    )
    try:
        try:
            current_path, reg_type = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            return [], winreg.REG_EXPAND_SZ
        entries = [entry.strip() for entry in str(current_path).split(";") if entry.strip()]
        return entries, reg_type
    finally:
        winreg.CloseKey(key)


def write_user_path(entries: list[str], reg_type: int) -> None:
    if winreg is None:
        return
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE
    )
    try:
        winreg.SetValueEx(key, "Path", 0, reg_type, ";".join(entries))
    finally:
        winreg.CloseKey(key)
    broadcast_environment_change()


def ensure_user_bin_on_path() -> bool:
    """Return True if this call added USER_BIN to the user PATH."""
    USER_BIN.mkdir(parents=True, exist_ok=True)
    if winreg is None:
        return False
    entries, reg_type = read_user_path()
    normalized = {str(Path(entry)).lower() for entry in entries}
    if str(USER_BIN).lower() in normalized:
        print(f"[OK] User PATH already contains: {USER_BIN}")
        os.environ["PATH"] = str(USER_BIN) + os.pathsep + os.environ.get("PATH", "")
        return False
    entries.append(str(USER_BIN))
    write_user_path(entries, reg_type or winreg.REG_EXPAND_SZ)
    os.environ["PATH"] = str(USER_BIN) + os.pathsep + os.environ.get("PATH", "")
    print(f"[OK] Added to user PATH: {USER_BIN}")
    return True


def remove_user_bin_from_path() -> None:
    if winreg is None:
        return
    entries, reg_type = read_user_path()
    target = str(USER_BIN).lower()
    kept = [entry for entry in entries if str(Path(entry)).lower() != target]
    if kept == entries:
        print(f"[OK] User PATH did not contain: {USER_BIN}")
        return
    write_user_path(kept, reg_type or winreg.REG_EXPAND_SZ)
    print(f"[OK] Removed from user PATH: {USER_BIN}")


def set_user_env(name: str, value: str) -> None:
    if winreg is None:
        return
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE
    )
    try:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    finally:
        winreg.CloseKey(key)
    os.environ[name] = value
    broadcast_environment_change()
    print(f"[OK] User env {name}={value}")


def delete_user_env(name: str) -> None:
    if winreg is None:
        return
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE
    )
    try:
        try:
            winreg.DeleteValue(key, name)
            print(f"[OK] Deleted user env {name}")
        except FileNotFoundError:
            print(f"[OK] User env {name} was not set")
    finally:
        winreg.CloseKey(key)
    os.environ.pop(name, None)
    broadcast_environment_change()


def write_cmd_launcher(name: str, script: Path, workspaces_root: Path | None = None) -> Path:
    launcher = USER_BIN / f"{name}.cmd"
    lines = ["@echo off"]
    if workspaces_root is not None:
        lines.append(f'if not defined {USER_ENV_ROOT} set "{USER_ENV_ROOT}={workspaces_root}"')
    lines.append(f'"{python_exe()}" "{script}" %*')
    launcher.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"[OK] CMD launcher: {name}  ->  {launcher}")
    return launcher


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
    autorun = LOCAL_STATE_DIR / "autorun.cmd"
    call = f'call "{autorun}"'
    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Command Processor")
    try:
        try:
            existing, _ = winreg.QueryValueEx(key, "AutoRun")
        except FileNotFoundError:
            existing = ""
        cleaned = strip_workspaces_autorun(str(existing))
        new_value = f"{cleaned} & {call}" if cleaned else call
        winreg.SetValueEx(key, "AutoRun", 0, winreg.REG_SZ, new_value)
        print("[OK] CMD AutoRun updated")
    finally:
        winreg.CloseKey(key)


def uninstall_cmd_autorun() -> None:
    if winreg is None:
        return
    key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Command Processor")
    try:
        try:
            existing, _ = winreg.QueryValueEx(key, "AutoRun")
        except FileNotFoundError:
            print("[OK] CMD AutoRun was not set")
            return
        cleaned = strip_workspaces_autorun(str(existing))
        if cleaned == str(existing):
            print("[OK] CMD AutoRun had no workspaces entry")
            return
        if cleaned:
            winreg.SetValueEx(key, "AutoRun", 0, winreg.REG_SZ, cleaned)
        else:
            try:
                winreg.DeleteValue(key, "AutoRun")
            except FileNotFoundError:
                pass
        print("[OK] Removed workspaces CMD AutoRun")
    finally:
        winreg.CloseKey(key)


def install_powershell_profiles(root: Path) -> None:
    hook = LOCAL_STATE_DIR / "profile.ps1"
    hook_ps = str(hook).replace("'", "''")
    block = "\n".join(
        [
            MARKER_BEGIN,
            f"if (Test-Path -LiteralPath '{hook_ps}') {{ . '{hook_ps}' }}",
            "if (-not (Get-Command venv -ErrorAction SilentlyContinue)) {",
            "    function global:venv { & (Join-Path $env:USERPROFILE 'bin\\venv.cmd') @args }",
            "}",
            "if (-not (Get-Command litellm -ErrorAction SilentlyContinue)) {",
            "    function global:litellm { & (Join-Path $env:USERPROFILE 'bin\\litellm.cmd') @args }",
            "}",
            MARKER_END,
            "",
        ]
    )
    pattern = re.compile(
        rf"{re.escape(MARKER_BEGIN)}\n.*?\n{re.escape(MARKER_END)}\n?",
        flags=re.DOTALL,
    )
    for profile in POWERSHELL_PROFILES:
        profile.parent.mkdir(parents=True, exist_ok=True)
        existing = profile.read_text(encoding="utf-8") if profile.exists() else ""
        if pattern.search(existing):
            updated = pattern.sub(lambda _m: block, existing)
        else:
            sep = "" if existing.endswith("\n") or not existing else "\n"
            updated = f"{existing}{sep}{block}"
        if not updated.endswith("\n"):
            updated += "\n"
        profile.write_text(updated, encoding="utf-8", newline="\n")
        print(f"[OK] PowerShell profile: {profile}")
    _ = root


def uninstall_powershell_profiles() -> None:
    pattern = re.compile(
        rf"{re.escape(MARKER_BEGIN)}\n.*?\n{re.escape(MARKER_END)}\n?",
        flags=re.DOTALL,
    )
    for profile in POWERSHELL_PROFILES:
        if not profile.is_file():
            continue
        existing = profile.read_text(encoding="utf-8")
        updated = pattern.sub("", existing).strip()
        if updated:
            profile.write_text(updated + "\n", encoding="utf-8", newline="\n")
        else:
            profile.write_text("", encoding="utf-8")
        print(f"[OK] Removed workspaces block from {profile}")


def set_execution_policy() -> None:
    proc = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned -Force",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    text = ((proc.stdout or "") + (proc.stderr or "")).lower()
    if proc.returncode == 0:
        print("[OK] PowerShell ExecutionPolicy CurrentUser = RemoteSigned")
    elif "updated your execution policy successfully" in text:
        print("[OK] ExecutionPolicy CurrentUser = RemoteSigned (a machine/GPO policy may still override it)")
    else:
        print("[WARN] Could not set ExecutionPolicy (Activate.ps1 may need RemoteSigned)")
        if proc.stderr:
            print(f"       {proc.stderr.strip()[:200]}")


def run_venv_install_shell(root: Path, venv_py: Path) -> int:
    print()
    print("=== venv.py install-shell (workspace shortcuts) ===")
    env = os.environ.copy()
    env[USER_ENV_ROOT] = str(root)
    proc = subprocess.run(
        [str(python_exe()), str(venv_py), "--root", str(root), "install-shell"],
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        print(f"[WARN] venv.py install-shell exited {proc.returncode} (venv/litellm launchers were still written)")
    return proc.returncode


def launcher_names(root: Path) -> list[str]:
    names = list(CORE_LAUNCHERS)
    db_path = root / "scripts" / "venvs.json"
    if db_path.is_file():
        try:
            data = json.loads(db_path.read_text(encoding="utf-8"))
            for ws in data.get("workspaces") or []:
                shortcut = ((ws.get("activate") or {}).get("shortcut") or ws.get("name") or "").strip()
                if shortcut:
                    names.append(f"{shortcut}.cmd")
            for alias in data.get("aliases") or []:
                alias_name = str(alias.get("name") or "").strip()
                if alias_name:
                    names.append(f"{alias_name}.cmd")
        except (OSError, json.JSONDecodeError):
            pass
    for extra in ("palo.cmd", "lab1.cmd", "prisma.cmd", "nam.cmd"):
        if extra not in names:
            names.append(extra)
    # preserve order, drop dupes
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def delete_launchers(names: list[str]) -> None:
    for name in names:
        path = USER_BIN / name
        if path.is_file():
            path.unlink()
            print(f"[OK] Deleted launcher: {path}")


def delete_local_state() -> None:
    if not LOCAL_STATE_DIR.exists():
        print(f"[OK] No local state dir: {LOCAL_STATE_DIR}")
        return
    for child in LOCAL_STATE_DIR.glob("*"):
        if child.is_file():
            child.unlink()
            print(f"[OK] Deleted {child}")
    try:
        LOCAL_STATE_DIR.rmdir()
        print(f"[OK] Removed {LOCAL_STATE_DIR}")
    except OSError:
        print(f"[WARN] Left non-empty {LOCAL_STATE_DIR}")


def cmd_check(root: Path) -> int:
    venv_py = root / "scripts" / "venv.py"
    litellm_py = root / "scripts" / "litellm.py"
    errors = 0
    print(f"Root        : {root}")
    print(f"Scripts     : {root / 'scripts'}")
    print(f"Python      : {python_exe()}")
    print(f"User bin    : {USER_BIN}")
    print(f"Local state : {LOCAL_STATE_DIR}")
    print(f"WORKSPACES_ROOT (session) : {os.environ.get(USER_ENV_ROOT, '(not set)')}")
    for name, path in [("venv.py", venv_py), ("litellm.py", litellm_py)]:
        ok = path.is_file()
        print(f"  {name:<12} {'OK' if ok else 'MISSING'}  {path}")
        if not ok:
            errors += 1
    for name in ("venv", "litellm"):
        path = USER_BIN / f"{name}.cmd"
        ok = path.is_file()
        print(f"  {name + '.cmd':<12} {'OK' if ok else 'MISSING'}  {path}")
        if not ok:
            errors += 1
    user_path = os.environ.get("PATH", "")
    in_session = str(USER_BIN).lower() in user_path.lower()
    print(f"  PATH (this session) contains user bin: {in_session}")
    return 1 if errors else 0


def cmd_install(root: Path) -> int:
    check_python()
    bootstrap_workspaces_root(root)
    venv_py = find_script(root, "venv.py")
    litellm_py = find_script(root, "litellm.py")

    print(f"Root        : {root}")
    print(f"venv.py     : {venv_py}")
    print(f"litellm.py  : {litellm_py}")
    print(f"Python      : {python_exe()}")
    print()

    print("=== User environment ===")
    set_user_env(USER_ENV_ROOT, str(root))

    print()
    print("=== PATH ===")
    added_path = ensure_user_bin_on_path()
    previous = load_local_config()
    added_path = bool(previous.get("added_user_bin_to_path")) or added_path

    print()
    print("=== CMD launchers (%USERPROFILE%\\bin) ===")
    write_cmd_launcher("venv", venv_py, root)
    write_cmd_launcher("ws", venv_py, root)
    write_cmd_launcher("litellm", litellm_py, root)

    print()
    print("=== PowerShell ExecutionPolicy ===")
    set_execution_policy()

    shell_rc = run_venv_install_shell(root, venv_py)

    print()
    print("=== PowerShell profiles (venv + litellm) ===")
    install_powershell_profiles(root)

    print()
    print("=== CMD AutoRun ===")
    install_cmd_autorun()

    write_cmd_launcher("venv", venv_py, root)
    write_cmd_launcher("ws", venv_py, root)
    write_cmd_launcher("litellm", litellm_py, root)

    save_local_config(
        {
            "root": str(root),
            "scripts": str(root / "scripts"),
            "added_user_bin_to_path": added_path,
            "env_vars": {USER_ENV_ROOT: str(root)},
            "launchers": launcher_names(root),
        }
    )

    print()
    print("=== Install complete ===")
    print(f"  WORKSPACES_ROOT = {root}")
    print(f"  venv     ->  {USER_BIN / 'venv.cmd'}")
    print(f"  litellm  ->  {USER_BIN / 'litellm.cmd'}")
    print()
    print("Close this window. Open a NEW cmd or PowerShell, then:")
    print("  venv --list")
    print("  litellm --models")
    print("  palo")
    print()
    print("Note: Python .venv folders copied from another PC will not run.")
    print("      Recreate them on this machine with:  venv --add")
    print("Uninstall:  python {} --uninstall --path {}".format(root / "scripts" / "install.py", root))
    return 0 if shell_rc == 0 else shell_rc


def cmd_uninstall(root: Path | None) -> int:
    check_python()
    cfg = load_local_config()
    names = list(cfg.get("launchers") or [])
    if root is not None:
        for name in launcher_names(root):
            if name not in names:
                names.append(name)
    if not names:
        names = launcher_names(root) if root is not None else list(CORE_LAUNCHERS)

    print("=== Uninstall (this Windows user) ===")
    if root is not None:
        print(f"Root        : {root}")
    print(f"User bin    : {USER_BIN}")
    print()

    print("=== Launchers ===")
    delete_launchers(names)

    print()
    print("=== PowerShell profiles ===")
    uninstall_powershell_profiles()

    print()
    print("=== CMD AutoRun ===")
    uninstall_cmd_autorun()

    print()
    print("=== User environment variables ===")
    extra_env = cfg.get("env_vars") if isinstance(cfg.get("env_vars"), dict) else {}
    env_names: list[str] = []
    for name in (*USER_ENV_VARS_WE_SET, *extra_env.keys()):
        key = str(name)
        if key not in env_names:
            env_names.append(key)
    for name in env_names:
        delete_user_env(name)
    for name in SESSION_ENV_CLEANUP:
        os.environ.pop(name, None)

    print()
    print("=== PATH ===")
    remove_user_bin_from_path()

    print()
    print("=== Local state ===")
    delete_local_state()

    print()
    print("=== Uninstall complete ===")
    print("Shared workspaces folder and .env files were not deleted.")
    print("Open a NEW cmd or PowerShell for PATH/env changes to take effect.")
    return 0


def main(argv: list[str] | None = None) -> int:
    _line_buffer_stdio()
    parser = argparse.ArgumentParser(
        description="Install or uninstall venv + litellm for this Windows user from a shared workspaces folder.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  git clone https://github.com/amirdeadline/workspaces-venv.git
  cd workspaces-venv
  copy scripts\\venv.config.json.example scripts\\venv.config.json
  python install.py
  python install.py --path Z:\\workspaces
  python install.py --check --path Z:\\workspaces
  python install.py --uninstall --path Z:\\workspaces
""",
    )
    parser.add_argument(
        "--path",
        help="Workspaces root on THIS PC (Z:\\workspaces, E:\\PC3_Shared\\workspaces, UNC, ...). "
        "If omitted, uses WORKSPACES_ROOT, then the last install, then this script's parent folder.",
    )
    parser.add_argument("--check", action="store_true", help="Verify install without changing anything")
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove launchers, PATH entry, WORKSPACES_ROOT, PowerShell/CMD hooks, and local state",
    )
    args = parser.parse_args(argv)

    if args.uninstall:
        root: Path | None
        if args.path:
            root = normalize_workspaces_root(detect_root(args.path))
        else:
            stored = load_local_config().get("root")
            env = (os.environ.get(USER_ENV_ROOT) or "").strip()
            candidate = args.path or stored or env
            root = normalize_workspaces_root(detect_root(candidate)) if candidate else None
            if root is None:
                try:
                    root = normalize_workspaces_root(Path(__file__).resolve().parent.parent)
                except SystemExit:
                    root = None
        return cmd_uninstall(root)

    root = normalize_workspaces_root(detect_root(args.path))
    if args.check:
        return cmd_check(root)
    return cmd_install(root)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nAborted.")
        raise SystemExit(130)
