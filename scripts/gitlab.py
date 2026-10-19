#!/usr/bin/env python3
"""GitLab wrapper for Palo (code.pan.run): custom helpers + full glab passthrough.

Local JSON: E:/PC3_Shared/Palo/gitlab/gitlab.config.json (--gitlab-config).

Custom commands:
  gitlab --login
  gitlab --config                    Open glab CLI config in Notepad
  gitlab --clone ns/project [--dest DIR]
  gitlab --branch create|delete|change NAME

All other arguments are passed to glab (same as running glab directly), e.g.:
  gitlab auth status --hostname code.pan.run
  gitlab repo clone gcs-automation/netsec/netsec-assistant
  gitlab mr list
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = Path(r"E:/PC3_Shared/Palo/gitlab/gitlab.config.json")


def eprint(msg: str) -> None:
    print(msg, file=sys.stderr)


def find_glab() -> Path:
    env = (os.environ.get("GLAB_EXE") or "").strip()
    if env:
        path = Path(env)
        if path.is_file():
            return path
    found = shutil.which("glab")
    if found:
        return Path(found)
    for candidate in (
        Path.home() / "AppData/Local/Programs/glab/glab.exe",
        Path(r"C:\Program Files\GitLab CLI\glab.exe"),
    ):
        if candidate.is_file():
            return candidate
    raise SystemExit(
        "glab executable not found. Install GLab.GLab via winget or set GLAB_EXE to glab.exe."
    )


def glab_config_file() -> Path:
    proc = subprocess.run(
        [str(find_glab()), "config", "path"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        return Path(proc.stdout.strip())
    return Path.home() / "AppData/Local/glab-cli/config.yml"


def cmd_open_glab_config_in_notepad() -> int:
    path = glab_config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        path.write_text("host: code.pan.run\ngit_protocol: ssh\n", encoding="utf-8")
    subprocess.Popen(["notepad.exe", str(path)], close_fds=True)
    print(f"[OK] Opened glab config in Notepad: {path}")
    return 0


def parse_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        result[key.strip()] = value
    return result


def load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit(
            f"GitLab config not found: {path}\n"
            "Copy gitlab.config.json.example to gitlab.config.json and edit paths."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"Invalid config (expected JSON object): {path}")
    return data


def save_config(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")


def resolve_token(config: dict[str, Any]) -> str:
    direct = str(config.get("api_token") or "").strip()
    if direct:
        return direct
    env_name = str(config.get("api_token_env") or "GITLAB_API_KEY").strip()
    if env_name and os.environ.get(env_name):
        return str(os.environ[env_name]).strip()
    env_file = str(config.get("env_file") or "").strip()
    if env_file:
        env = parse_env_file(Path(env_file))
        if env_name in env:
            return env[env_name]
    return ""


def git_ssh_command(config: dict[str, Any]) -> str:
    key = Path(str(config["ssh_private_key"])).resolve()
    if not key.is_file():
        raise SystemExit(f"SSH private key not found: {key}")
    key_s = str(key).replace("\\", "/")
    return (
        f'ssh -i "{key_s}" -o IdentitiesOnly=yes -o BatchMode=yes '
        f"-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile={_known_hosts_path()}"
    )


def _known_hosts_path() -> str:
    p = Path.home() / ".ssh" / "known_hosts"
    p.parent.mkdir(parents=True, exist_ok=True)
    return str(p).replace("\\", "/")


def git_env(config: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_SSH_COMMAND"] = git_ssh_command(config)
    env["GIT_TERMINAL_PROMPT"] = "0"
    host = str(config.get("gitlab_host") or "code.pan.run")
    env["GITLAB_HOST"] = host
    env["GL_HOST"] = host
    env["GITLAB_URI"] = host
    token = resolve_token(config)
    if token:
        env["GITLAB_TOKEN"] = token
        env["GITLAB_ACCESS_TOKEN"] = token
    return env


def sync_glab_cli_config(config: dict[str, Any]) -> None:
    """Align glab global/host config with gitlab.config.json (avoids gitlab.com default)."""
    glab = str(find_glab())
    host = str(config.get("gitlab_host") or "code.pan.run")
    git_protocol = str(config.get("git_protocol") or "ssh")
    for args in (
        ["config", "set", "--global", "host", host],
        ["config", "set", "--global", "git_protocol", git_protocol],
        ["config", "set", "--host", host, "api_host", host],
        ["config", "set", "--host", host, "api_protocol", "https"],
        ["config", "set", "--host", host, "git_protocol", git_protocol],
    ):
        subprocess.run([glab, *args], check=False, capture_output=True, text=True)
    token = resolve_token(config)
    if token:
        subprocess.run(
            [
                glab,
                "auth",
                "login",
                "--hostname",
                host,
                "--git-protocol",
                git_protocol,
                "--token",
                token,
            ],
            check=False,
            capture_output=True,
            text=True,
        )


def normalize_glab_argv(config: dict[str, Any], glab_argv: list[str]) -> list[str]:
    """Use project path + GITLAB_HOST instead of HTTPS URL (glab otherwise hits gitlab.com API)."""
    if len(glab_argv) >= 3 and glab_argv[0] == "repo" and glab_argv[1] == "clone":
        repo = glab_argv[2]
        host = str(config.get("gitlab_host") or "code.pan.run")
        if repo.startswith("http://") or repo.startswith("https://"):
            m = re.match(rf"https?://{re.escape(host)}/(.+?)(?:\.git)?/?$", repo, re.I)
            if m:
                glab_argv = [*glab_argv[:2], m.group(1).strip("/"), *glab_argv[3:]]
    return glab_argv


def run_glab(config: dict[str, Any], glab_argv: list[str]) -> int:
    glab = find_glab()
    glab_argv = normalize_glab_argv(config, glab_argv)
    proc = subprocess.run([str(glab), *glab_argv], env=git_env(config))
    return int(proc.returncode)


def api_request(
    config: dict[str, Any], method: str, api_path: str, token: str, body: dict | None = None
) -> Any:
    base = str(config.get("api_base_url") or f"https://{config['gitlab_host']}/api/v4").rstrip("/")
    url = f"{base}/{api_path.lstrip('/')}"
    data = None
    headers = {"PRIVATE-TOKEN": token, "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            if not raw:
                return None
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise SystemExit(f"GitLab API {method} {api_path} failed: HTTP {exc.code} {detail}") from exc


def ensure_ssh_config_snippet(config: dict[str, Any]) -> None:
    key = Path(str(config["ssh_private_key"])).resolve()
    host = str(config.get("ssh_host") or config.get("gitlab_host"))
    ssh_user = str(config.get("ssh_user") or "git")
    ssh_dir = Path.home() / ".ssh"
    config_d = ssh_dir / "config.d"
    config_d.mkdir(parents=True, exist_ok=True)
    snippet_path = config_d / f"{host}.gitlab"
    key_posix = str(key).replace("\\", "/")
    body = (
        f"# Managed by gitlab.py — Git only for {host}\n"
        f"Host {host}\n"
        f"  HostName {host}\n"
        f"  User {ssh_user}\n"
        f"  IdentityFile {key_posix}\n"
        f"  IdentitiesOnly yes\n"
    )
    snippet_path.write_text(body, encoding="utf-8", newline="\n")
    main = ssh_dir / "config"
    include_marker = "# gitlab.py include config.d"
    include_line = "Include config.d/*.gitlab"
    existing = main.read_text(encoding="utf-8") if main.is_file() else ""
    if include_marker not in existing:
        block = f"{include_marker}\n{include_line}\n"
        main.write_text(block + existing.lstrip(), encoding="utf-8", newline="\n")


def cmd_login(config_path: Path, config: dict[str, Any]) -> int:
    token = resolve_token(config)
    if not token:
        raise SystemExit(
            "No API token. Set api_token in gitlab.config.json or api_token_env in palo .env."
        )
    ensure_ssh_config_snippet(config)
    sync_glab_cli_config(config)
    user = api_request(config, "GET", "user", token)
    username = str(user.get("username") or user.get("name") or "?")
    print(f"[OK] API: logged in as {username} on {config.get('gitlab_host')}")

    ssh_cmd = [
        "ssh",
        "-i",
        str(Path(str(config["ssh_private_key"])).resolve()).replace("\\", "/"),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-T",
        f"{config.get('ssh_user', 'git')}@{config.get('ssh_host')}",
    ]
    proc = subprocess.run(ssh_cmd, capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode not in (0, 1) and "Welcome to GitLab" not in out:
        raise SystemExit(f"SSH auth failed (exit {proc.returncode}): {out.strip()}")
    if "Welcome to GitLab" in out:
        print(f"[OK] SSH: {out.strip()}")
    else:
        print(f"[OK] SSH: {out.strip() or 'connected'}")

    config["last_login_at"] = _utc_now()
    config["last_login_user"] = username
    save_config(config_path, config)
    print(f"[OK] Config updated: {config_path}")
    return 0


def normalize_repo_spec(spec: str, host: str) -> str:
    spec = spec.strip()
    if not spec:
        raise SystemExit("Repository path or URL is required.")
    if spec.startswith("git@"):
        return spec if spec.endswith(".git") else spec + ".git"
    if spec.startswith("ssh://"):
        return spec
    if spec.startswith("http://") or spec.startswith("https://"):
        m = re.match(r"https?://[^/]+/(.+?)(?:\.git)?/?$", spec)
        if not m:
            raise SystemExit(f"Could not parse HTTPS repo URL: {spec}")
        path = m.group(1).strip("/") + ".git"
        return f"git@{host}:{path}"
    path = spec.strip("/")
    if not path.endswith(".git"):
        path += ".git"
    return f"git@{host}:{path}"


def cmd_clone(config: dict[str, Any], repo: str, dest: str | None) -> int:
    host = str(config.get("gitlab_host") or "code.pan.run")
    url = normalize_repo_spec(repo, host)
    target = Path(dest).resolve() if dest else Path.cwd() / Path(repo.split("/")[-1].replace(".git", ""))
    if target.exists() and any(target.iterdir()):
        raise SystemExit(f"Destination not empty: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    branch = str(config.get("default_branch") or "master")
    cmd = ["git", "clone", "--branch", branch, url, str(target)]
    print(f"[RUN] git clone -> {target}")
    proc = subprocess.run(cmd, env=git_env(config))
    if proc.returncode != 0:
        raise SystemExit(f"git clone failed with exit code {proc.returncode}")
    print(f"[OK] Cloned to {target}")
    return 0


def repo_root(config: dict[str, Any]) -> Path:
    explicit = str(config.get("active_repo_path") or "").strip()
    root = Path(explicit).resolve() if explicit else Path.cwd().resolve()
    if not (root / ".git").is_dir():
        raise SystemExit(
            f"Not a git repository: {root}\n"
            "Run --clone first or set active_repo_path in gitlab.config.json."
        )
    return root


def cmd_branch_create(config: dict[str, Any], name: str) -> int:
    root = repo_root(config)
    env = git_env(config)
    base = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(["git", "checkout", "-b", name], cwd=root, env=env, check=True)
    print(f"[OK] Created local branch {name} from {base}")
    push = subprocess.run(["git", "push", "-u", "origin", name], cwd=root, env=env)
    if push.returncode != 0:
        raise SystemExit(f"git push failed for branch {name}")
    print(f"[OK] Pushed {name} to origin")
    return 0


def cmd_branch_delete(config: dict[str, Any], name: str) -> int:
    root = repo_root(config)
    env = git_env(config)
    cur = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if cur == name:
        raise SystemExit(f"Refusing to delete the current branch ({name}). Checkout another branch first.")
    subprocess.run(["git", "push", "origin", "--delete", name], cwd=root, env=env, check=False)
    subprocess.run(["git", "branch", "-D", name], cwd=root, env=env, check=True)
    print(f"[OK] Deleted branch {name} (local and remote if push succeeded)")
    return 0


def cmd_branch_change(config: dict[str, Any], name: str) -> int:
    root = repo_root(config)
    env = git_env(config)
    subprocess.run(["git", "fetch", "origin"], cwd=root, env=env, check=False)
    proc = subprocess.run(["git", "checkout", name], cwd=root, env=env)
    if proc.returncode != 0:
        proc = subprocess.run(["git", "checkout", "-b", name, f"origin/{name}"], cwd=root, env=env)
    if proc.returncode != 0:
        raise SystemExit(f"Could not checkout branch {name}")
    print(f"[OK] Checked out {name} in {root}")
    return 0


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gitlab",
        description="Palo GitLab helper (custom commands) + glab passthrough.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Passthrough: any glab command works unchanged, e.g.\n"
            "  gitlab auth login --hostname code.pan.run --git-protocol ssh -t TOKEN\n"
            "  gitlab repo clone gcs-automation/netsec/netsec-assistant\n"
            "  gitlab mr list -R gcs-automation/netsec/netsec-assistant\n"
            "  gitlab config edit\n"
            "Run `gitlab --help` for wrapper help; `gitlab help` for glab commands."
        ),
    )
    p.add_argument(
        "--config",
        action="store_true",
        help="Open glab CLI config file (config.yml) in Notepad.",
    )
    p.add_argument(
        "--gitlab-config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Path to gitlab.config.json (default: {DEFAULT_CONFIG})",
    )
    p.add_argument("--login", action="store_true", help="Validate API token and SSH; sync glab config.")
    p.add_argument(
        "--setup-glab",
        action="store_true",
        help="Write glab host/git_protocol/auth from gitlab.config.json (fixes gitlab.com 401).",
    )
    p.add_argument("--clone", metavar="REPO", help="Clone repo via SSH (custom git clone).")
    p.add_argument(
        "--dest",
        dest="clone_dest",
        metavar="DIR",
        help="Destination directory for --clone (default: ./<repo-name>).",
    )
    p.add_argument("--branch", nargs=2, metavar=("ACTION", "NAME"), help="create|delete|change")
    return p


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args, glab_argv = parser.parse_known_args(raw)

    if args.config:
        return cmd_open_glab_config_in_notepad()

    has_custom = args.login or args.setup_glab or args.clone or args.branch is not None

    if glab_argv and not has_custom:
        config = load_config(args.gitlab_config.resolve())
        return run_glab(config, glab_argv)

    config_path = args.gitlab_config.resolve()
    config = load_config(config_path)

    if args.setup_glab:
        sync_glab_cli_config(config)
        print(f"[OK] glab configured for host {config.get('gitlab_host')} (git_protocol={config.get('git_protocol', 'ssh')})")
        print("     Verify: gitlab auth status --hostname code.pan.run")
        return 0

    if args.login:
        return cmd_login(config_path, config)
    if args.clone:
        return cmd_clone(config, args.clone, args.clone_dest)
    if args.branch:
        action, name = args.branch[0].lower(), args.branch[1]
        if action == "create":
            return cmd_branch_create(config, name)
        if action == "delete":
            return cmd_branch_delete(config, name)
        if action in {"change", "checkout", "switch"}:
            return cmd_branch_change(config, name)
        raise SystemExit("Branch action must be create, delete, or change.")

    if glab_argv:
        return run_glab(config, glab_argv)

    parser.print_help()
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        eprint(f"[ERROR] git command failed: {exc}")
        raise SystemExit(exc.returncode) from exc
