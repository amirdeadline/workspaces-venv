#!/usr/bin/env python3
"""LiteLLM CLI — Palo Scale Team proxy + Claude Code launcher.

Usage (from any directory, after install):
    litellm                    Open Claude Code here (LiteLLM models via palo .env)
    litellm D:\\path\\to\\proj  Open Claude Code in that folder
    litellm --models
    litellm --api-token <key>
    litellm --set-model claude-opus-4-8
    litellm --mcp              List local palo MCP servers (Jira / Confluence / Asana)
    litellm --mcp jira         Probe palo-jira (personal token via palo .env)
    litellm --mcp confluence   Probe palo-confluence
    litellm --mcp asana        Probe palo-asana
    litellm --notification C:\\Windows\\Media\\litellm.wav
                               Save hook notification WAV for litellm Claude sessions
    litellm --usage            Per-project token/cost table + today/7d/30d totals (local log)
    litellm --model opus-5 D:\\proj -- -p "..." --print
                               One-shot model for this launch only (does not change palo .env)

Personal subscription Claude in the same repo: use `claude-personal` (palo shell)
or run `claude` without the LiteLLM env (see palo activate helpers).

Credentials come from the palo workspace .env. Never prints the key.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://scaleteam-litellm-eu.paloaltonetworks.com"
DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_CONTEXT_TOKENS = 1_000_000
DEFAULT_NOTIFICATION_WAV = r"C:\Windows\Media\litellm.wav"
NOTIFICATION_ENV = "LITELLM_NOTIFICATION_WAV"
PALO_WORKSPACE = "palo"
SCRIPTS_DIR = Path(__file__).resolve().parent
NOTIFICATION_HOOK_SCRIPT = "play-litellm-notification.ps1"
NOTIFICATION_HOOK_TEMPLATE = SCRIPTS_DIR / "claude_play_notification.ps1"
WORKSPACE_MCP_PY = Path.home() / ".amir" / "workspace_mcp" / "workspace_mcp.py"
MCP_PROBE_SERVERS = frozenset({"jira", "confluence", "asana"})
MODEL_CTX_RE = re.compile(r"^(?P<id>.+?)(?:\[(?P<ctx>[^\]]+)\])?$", re.IGNORECASE)

# Friendly aliases for --model / subagent -Model (see `litellm --models` for proxy ids).
MODEL_ALIASES: dict[str, str] = {
    "default": "claude-opus-4-8[1m]",
    "opus-4.8": "claude-opus-4-8[1m]",
    "opus4.8": "claude-opus-4-8[1m]",
    "4.8": "claude-opus-4-8[1m]",
    "opus-48": "claude-opus-4-8[1m]",
    "opus-5": "claude-opus-5[1m]",
    "opus5": "claude-opus-5[1m]",
    "5": "claude-opus-5[1m]",
}


def palo_env_path() -> Path:
    root = (os.environ.get("WORKSPACES_ROOT") or "").strip()
    if root:
        return Path(root) / "virtual_envs" / "palo" / ".env"
    return SCRIPTS_DIR.parent / "virtual_envs" / "palo" / ".env"
KEY_RE = re.compile(r"sk-[A-Za-z0-9_-]+")


def redact(text: str) -> str:
    return KEY_RE.sub("sk-***", text)


def parse_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
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


def palo_workspace_root() -> Path:
    return palo_env_path().parent


def claude_config_dir() -> Path:
    return palo_workspace_root() / ".claude-code"


def notification_wav_path() -> Path:
    env = load_palo_env()
    raw = (env.get(NOTIFICATION_ENV) or "").strip()
    if not raw:
        raw = DEFAULT_NOTIFICATION_WAV
    return Path(raw)


def _powershell_hook(script_path: Path) -> list[dict[str, Any]]:
    return [
        {
            "type": "command",
            "command": "powershell",
            "args": [
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
            ],
        }
    ]


def ensure_litellm_claude_notifications() -> Path:
    """Install WAV hook script + Claude Code hooks under palo .claude-code (litellm sessions)."""
    cfg = claude_config_dir()
    hooks_dir = cfg / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    if not NOTIFICATION_HOOK_TEMPLATE.is_file():
        raise SystemExit(f"Missing hook template: {NOTIFICATION_HOOK_TEMPLATE}")
    dst = hooks_dir / NOTIFICATION_HOOK_SCRIPT
    shutil.copy2(NOTIFICATION_HOOK_TEMPLATE, dst)

    settings_path = cfg / "settings.json"
    data: dict[str, Any] = {}
    if settings_path.exists():
        data = json.loads(settings_path.read_text(encoding="utf-8-sig"))

    hook_cmds = _powershell_hook(dst)
    hooks: dict[str, Any] = data.setdefault("hooks", {})
    hooks["Stop"] = [{"hooks": hook_cmds}]
    hooks["StopFailure"] = [{"hooks": hook_cmds}]
    hooks["PostToolUseFailure"] = [{"hooks": hook_cmds}]
    hooks["PreToolUse"] = [{"matcher": "AskUserQuestion", "hooks": hook_cmds}]
    hooks["Notification"] = [
        {
            "matcher": "permission_prompt|idle_prompt|agent_needs_input|agent_completed|elicitation_dialog",
            "hooks": hook_cmds,
        }
    ]

    settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return dst


def cmd_notification(wav_path: str) -> int:
    path = Path(wav_path).expanduser()
    if not path.is_file():
        raise SystemExit(f"WAV file not found: {path}")
    resolved = str(path.resolve())
    update_palo_env_var(NOTIFICATION_ENV, resolved)
    os.environ[NOTIFICATION_ENV] = resolved
    ensure_litellm_claude_notifications()
    print(f"[OK] {NOTIFICATION_ENV} -> {resolved}")
    print("Plays on Stop, StopFailure, tool failure, questions, and permission prompts in litellm Claude.")
    return 0


def load_palo_env() -> dict[str, str]:
    """Palo .env merged with current process (file wins for routing keys)."""
    file_env = parse_env_file(palo_env_path())
    merged = dict(os.environ)
    for key, value in file_env.items():
        merged[key] = value
    return merged


def load_credentials() -> tuple[str, str]:
    env = load_palo_env()
    base = (env.get("ANTHROPIC_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    key = env.get("ANTHROPIC_API_KEY") or ""
    if not key:
        raise SystemExit(
            "ANTHROPIC_API_KEY is not set. Run `palo` first, or:\n"
            "  venv --var palo --add ANTHROPIC_API_KEY <key>"
        )
    return base, key


def workspaces_root() -> Path:
    root = (os.environ.get("WORKSPACES_ROOT") or "").strip()
    if root:
        return Path(root)
    return SCRIPTS_DIR.parent


def update_palo_env_var(key: str, value: str) -> None:
    """Persist a variable on the palo workspace via venv.py (regenerates env.cmd)."""
    venv_py = SCRIPTS_DIR / "venv.py"
    if not venv_py.exists():
        raise SystemExit(f"venv.py not found next to litellm.py: {venv_py}")
    env = os.environ.copy()
    env.setdefault("WORKSPACES_ROOT", str(workspaces_root()))
    proc = subprocess.run(
        [sys.executable, str(venv_py), "--var", PALO_WORKSPACE, "--add", key, value],
        capture_output=True,
        text=True,
        env=env,
    )
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "venv --var failed").strip()
        raise SystemExit(redact(msg))
    line = (proc.stdout or "").strip()
    if line:
        print(line)


def fmt_context_tokens(value: object) -> str:
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(value or "")
    if n >= 1_000_000:
        millions = n / 1_000_000
        if abs(millions - round(millions)) < 0.02:
            return f"{int(round(millions))}M"
        return f"{millions:.2f}M".rstrip("0").rstrip(".") + "M"
    if n >= 1_000:
        thousands = n / 1_000
        if abs(thousands - round(thousands)) < 0.05:
            return f"{int(round(thousands))}K"
        return f"{thousands:.1f}K".rstrip("0").rstrip(".") + "K"
    return str(n)


def parse_context_spec(spec: str) -> int:
    raw = (spec or "1m").strip().lower().replace("_", "")
    if not raw:
        raw = "1m"
    if raw in {"1m", "1000k"}:
        return 1_000_000
    if raw.endswith("m"):
        return int(float(raw[:-1]) * 1_000_000)
    if raw.endswith("k"):
        return int(float(raw[:-1]) * 1_000)
    return int(raw)


def resolve_model_spec(raw: str | None) -> str | None:
    """Map alias or full id to MODEL[CTX] spec. None/empty -> no override."""
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    key = text.lower().replace("_", "-")
    if key in MODEL_ALIASES:
        return MODEL_ALIASES[key]
    return text


def apply_model_spec_to_env(
    env: dict[str, str], spec: str, base_url: str, api_key: str, *, validate: bool = True
) -> tuple[str, int]:
    """Apply one-shot model + context to launch env (does not write palo .env)."""
    model_id, context_tokens = parse_model_spec(spec)
    if validate:
        payload = fetch_models(base_url, api_key)
        available = model_ids(payload)
        limits = model_limits(payload)
        if model_id not in available:
            sample = ", ".join(available[:8])
            more = f" (+{len(available) - 8} more)" if len(available) > 8 else ""
            raise SystemExit(
                f"Model '{model_id}' is not available on the proxy.\n"
                f"Run `litellm --models`. Aliases: {', '.join(sorted(MODEL_ALIASES))}"
                + (f"\nAvailable includes: {sample}{more}" if available else "")
            )
        max_in = limits.get(model_id, (None, None))[0]
        if max_in is not None and context_tokens > max_in:
            print(
                f"[WARN] Context {fmt_context_tokens(context_tokens)} capped to proxy max "
                f"{fmt_context_tokens(max_in)} for {model_id}."
            )
            context_tokens = max_in
    env["ANTHROPIC_MODEL"] = model_id
    apply_context_to_env(env, context_tokens)
    return model_id, context_tokens


def parse_model_spec(raw: str) -> tuple[str, int]:
    text = (raw or "").strip()
    if not text:
        raise SystemExit("--set-model requires a model id (e.g. claude-opus-4-8 or claude-opus-4-8[1m])")
    match = MODEL_CTX_RE.match(text)
    if not match:
        raise SystemExit(f"Invalid model spec: {raw}")
    model_id = match.group("id").strip()
    ctx_raw = match.group("ctx")
    context = parse_context_spec(ctx_raw if ctx_raw else "1m")
    return model_id, context


def model_limits(payload: dict) -> dict[str, tuple[int | None, int | None]]:
    rows = payload.get("data")
    out: dict[str, tuple[int | None, int | None]] = {}
    if not isinstance(rows, list):
        return out
    for item in rows:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        mid = str(item["id"])
        try:
            inp = int(item["max_input_tokens"]) if item.get("max_input_tokens") is not None else None
        except (TypeError, ValueError):
            inp = None
        try:
            out_t = int(item["max_output_tokens"]) if item.get("max_output_tokens") is not None else None
        except (TypeError, ValueError):
            out_t = None
        out[mid] = (inp, out_t)
    return out


def apply_context_to_env(env: dict[str, str], context_tokens: int) -> None:
    env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(context_tokens)
    env["CLAUDE_CODE_DISABLE_1M_CONTEXT"] = "0" if context_tokens >= 1_000_000 else "1"


def persist_context_tokens(context_tokens: int) -> None:
    update_palo_env_var("CLAUDE_CODE_MAX_CONTEXT_TOKENS", str(context_tokens))
    disable = "0" if context_tokens >= 1_000_000 else "1"
    update_palo_env_var("CLAUDE_CODE_DISABLE_1M_CONTEXT", disable)
    os.environ["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(context_tokens)
    os.environ["CLAUDE_CODE_DISABLE_1M_CONTEXT"] = disable


def model_ids(payload: dict) -> list[str]:
    rows = payload.get("data")
    if not isinstance(rows, list):
        return []
    ids: list[str] = []
    for item in rows:
        if isinstance(item, dict) and item.get("id"):
            ids.append(str(item["id"]))
    return ids


def cmd_api_token(token: str) -> int:
    token = (token or "").strip()
    if not token:
        raise SystemExit("--api-token requires a non-empty value")
    if not KEY_RE.fullmatch(token):
        raise SystemExit(
            "API token does not look like a LiteLLM key (expected sk-...). "
            "Value was not saved."
        )
    update_palo_env_var("ANTHROPIC_API_KEY", token)
    update_palo_env_var("ANTHROPIC_AUTH_TOKEN", token)
    os.environ["ANTHROPIC_API_KEY"] = token
    os.environ["ANTHROPIC_AUTH_TOKEN"] = token
    print("Re-run `palo` in open shells so ANTHROPIC_API_KEY updates in your session.")
    return 0


def cmd_set_model(model: str, base_url: str, api_key: str) -> int:
    model_id, context_tokens = parse_model_spec(model)
    payload = fetch_models(base_url, api_key)
    available = model_ids(payload)
    limits = model_limits(payload)
    if model_id not in available:
        sample = ", ".join(available[:8])
        more = f" (+{len(available) - 8} more)" if len(available) > 8 else ""
        raise SystemExit(
            f"Model '{model_id}' is not available on the proxy.\n"
            "Context is set with [1m] after the id (e.g. claude-opus-4-8[1m]), not as part of the proxy model name.\n"
            f"Run `litellm --models` for the full list."
            + (f"\nAvailable includes: {sample}{more}" if available else "")
        )
    max_in = limits.get(model_id, (None, None))[0]
    if max_in is not None and context_tokens > max_in:
        print(
            f"[WARN] Requested context {fmt_context_tokens(context_tokens)} exceeds proxy max "
            f"{fmt_context_tokens(max_in)} for {model_id}; capping."
        )
        context_tokens = max_in
    update_palo_env_var("ANTHROPIC_MODEL", model_id)
    persist_context_tokens(context_tokens)
    os.environ["ANTHROPIC_MODEL"] = model_id
    print(
        f"[OK] Model {model_id} with {fmt_context_tokens(context_tokens)} context "
        f"({context_tokens} tokens) saved on workspace palo."
    )
    print("Re-run `palo` or use `litellm` to open Claude with these settings.")
    return 0


def fetch_models(base_url: str, api_key: str) -> dict:
    url = f"{base_url}/v1/models"
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise SystemExit("curl.exe not found. Install curl or use Windows 10+.")
    cmd = [
        curl,
        "-sS",
        "--ssl-no-revoke",
        "-w",
        "\nHTTP_CODE:%{http_code}",
        "--max-time",
        "30",
        url,
        "-H",
        f"Authorization: Bearer {api_key}",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    raw = proc.stdout or ""
    err = proc.stderr or ""
    http_code = ""
    body = raw
    if "HTTP_CODE:" in raw:
        body, _, tail = raw.rpartition("HTTP_CODE:")
        http_code = tail.strip().splitlines()[0].strip()
        body = body.strip()
    if proc.returncode != 0 and not body:
        raise SystemExit(f"curl failed ({proc.returncode}): {redact(err) or 'no output'}")
    try:
        data = json.loads(body) if body else {}
    except json.JSONDecodeError:
        raise SystemExit(f"Non-JSON response (HTTP {http_code or '?'}):\n{redact(body)[:500]}")
    if http_code and http_code != "200":
        message = ""
        if isinstance(data, dict):
            err_obj = data.get("error")
            if isinstance(err_obj, dict):
                message = str(err_obj.get("message") or err_obj)
            elif data.get("message"):
                message = str(data["message"])
        raise SystemExit(
            f"LiteLLM HTTP {http_code}: {redact(message) or redact(body)[:400]}"
        )
    return data


def fmt_created(value: object) -> str:
    try:
        ts = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(value or "")
    if ts <= 0:
        return str(ts)
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def print_models_table(payload: dict) -> int:
    rows = payload.get("data")
    if not isinstance(rows, list) or not rows:
        print("No models returned.")
        return 0
    headers = (
        "ID",
        "MODE",
        "MAX_CONTEXT",
        "MAX_OUTPUT",
        "CREATED",
        "OBJECT",
    )
    table: list[tuple[str, ...]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        table.append(
            (
                str(item.get("id") or ""),
                str(item.get("mode") or ""),
                fmt_context_tokens(item.get("max_input_tokens")),
                fmt_context_tokens(item.get("max_output_tokens")),
                fmt_created(item.get("created")),
                str(item.get("object") or ""),
            )
        )
    widths = [len(h) for h in headers]
    for row in table:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print("  ".join("-" * w for w in widths))
    for row in table:
        print(fmt.format(*row))
    print(f"\n{len(table)} model(s)")
    return 0


def claude_cli() -> str:
    for name in ("claude", "claude.cmd", "claude.exe"):
        found = shutil.which(name)
        if found:
            return found
    local = Path.home() / ".local" / "bin" / "claude.exe"
    if local.is_file():
        return str(local)
    raise SystemExit(
        "claude CLI not found on PATH. Install Claude Code, then retry."
    )


def build_litellm_claude_env() -> dict[str, str]:
    env = load_palo_env()
    base = (env.get("ANTHROPIC_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    key = env.get("ANTHROPIC_API_KEY") or ""
    if not key:
        raise SystemExit(
            "ANTHROPIC_API_KEY is not set on workspace palo. Run:\n"
            "  litellm --api-token <key>\n"
            "  or: venv --var palo --add ANTHROPIC_API_KEY <key>"
        )
    env["ANTHROPIC_BASE_URL"] = base
    env["ANTHROPIC_API_KEY"] = key
    if not env.get("ANTHROPIC_AUTH_TOKEN"):
        env["ANTHROPIC_AUTH_TOKEN"] = key
    cfg = palo_workspace_root() / ".claude-code"
    cfg.mkdir(parents=True, exist_ok=True)
    env["CLAUDE_CONFIG_DIR"] = str(cfg)
    if not env.get("ANTHROPIC_MODEL"):
        env["ANTHROPIC_MODEL"] = DEFAULT_MODEL
    try:
        ctx = int(env.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS") or DEFAULT_CONTEXT_TOKENS)
    except ValueError:
        ctx = DEFAULT_CONTEXT_TOKENS
    apply_context_to_env(env, ctx)
    ensure_litellm_claude_notifications()
    env[NOTIFICATION_ENV] = str(notification_wav_path())
    return env


def cmd_mcp(server: str | None) -> int:
    """Delegate to ~/.amir/workspace_mcp/workspace_mcp.py (palo personal-token MCP)."""
    if not WORKSPACE_MCP_PY.is_file():
        raise SystemExit(
            f"workspace MCP tool not found: {WORKSPACE_MCP_PY}\n"
            "Deploy Amir workspace MCP to ~/.amir/workspace_mcp first."
        )
    name = (server or "").strip().lower()
    if not name:
        argv = [sys.executable, str(WORKSPACE_MCP_PY), "list"]
    elif name in MCP_PROBE_SERVERS:
        argv = [sys.executable, str(WORKSPACE_MCP_PY), "status", name, "--probe"]
    else:
        raise SystemExit(
            f"Unknown MCP server '{server}'. Use: {', '.join(sorted(MCP_PROBE_SERVERS))}"
        )
    proc = subprocess.run(argv)
    return int(proc.returncode or 0)


def cmd_launch_claude(
    project_dir: str | None, claude_argv: list[str], *, model_override: str | None = None
) -> int:
    target = Path(project_dir or os.getcwd()).resolve()
    if not target.is_dir():
        raise SystemExit(f"Not a directory: {target}")
    env = build_litellm_claude_env()
    if model_override:
        spec = resolve_model_spec(model_override)
        if spec:
            base, key = load_credentials()
            apply_model_spec_to_env(env, spec, base, key)
            print(f"  (one-shot --model {model_override!r} -> {spec}; palo .env unchanged)")
    model = env.get("ANTHROPIC_MODEL") or DEFAULT_MODEL
    ctx = env.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS") or str(DEFAULT_CONTEXT_TOKENS)
    print(f"Claude Code @ {target}")
    print(f"  API base : {env.get('ANTHROPIC_BASE_URL')}")
    print(f"  Model    : {model}")
    print(f"  Context  : {fmt_context_tokens(ctx)} ({ctx} tokens)")
    print(f"  Config   : {env.get('CLAUDE_CONFIG_DIR')}")
    print(f"  Notify   : {env.get(NOTIFICATION_ENV)}")
    exe = claude_cli()
    proc = subprocess.run([exe, *claude_argv], cwd=str(target), env=env)
    return proc.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="litellm",
        description="Palo Alto Scale Team LiteLLM proxy and Claude Code launcher.",
    )
    parser.add_argument(
        "--models",
        action="store_true",
        help="GET /v1/models and print a table",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print raw JSON instead of a table",
    )
    parser.add_argument(
        "--api-token",
        metavar="TOKEN",
        help="Save a new ANTHROPIC_API_KEY (and ANTHROPIC_AUTH_TOKEN) on workspace palo",
    )
    parser.add_argument(
        "--set-model",
        metavar="MODEL[CTX]",
        help=(
            "Set ANTHROPIC_MODEL on palo; optional context suffix defaults to [1m] "
            "(e.g. claude-opus-4-8, claude-sonnet-4-6[200k])"
        ),
    )
    parser.add_argument(
        "--model",
        metavar="MODEL[CTX]|ALIAS",
        dest="model_override",
        help=(
            "Use this model for one Claude launch only (does not change palo .env). "
            "Aliases: opus-4.8 (default 1M), opus-5, or full id from litellm --models "
            "(e.g. claude-opus-4-8[1m])"
        ),
    )
    parser.add_argument(
        "--mcp",
        nargs="?",
        const="",
        metavar="SERVER",
        help=(
            "Local palo MCP (personal tokens): list servers, or probe "
            "jira | confluence | asana"
        ),
    )
    parser.add_argument(
        "--notification",
        metavar="WAV",
        help=(
            "Save LITELLM_NOTIFICATION_WAV on workspace palo and refresh Claude hooks "
            f"(default when unset: {DEFAULT_NOTIFICATION_WAV})"
        ),
    )
    parser.add_argument(
        "--usage",
        action="store_true",
        help=(
            "Show LiteLLM usage on this machine: per-project tokens and USD (all time), "
            "plus today / last 7 / last 30 days totals from the local usage log"
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        help="Optional project path, or 'models' (same as --models)",
    )
    parser.add_argument(
        "claude_argv",
        nargs=argparse.REMAINDER,
        help="Extra arguments passed to the claude CLI",
    )
    return parser


def cmd_usage(json_output: bool) -> int:
    sys.path.insert(0, str(SCRIPTS_DIR))
    from log_litellm_usage import print_machine_usage_report

    return print_machine_usage_report(json_output=json_output)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    wants_models = args.models or args.command == "models"
    launch_path: str | None = None
    claude_argv = list(args.claude_argv or [])
    if claude_argv and claude_argv[0] == "--":
        claude_argv = claude_argv[1:]
    if args.command and args.command != "models":
        launch_path = args.command

    if args.usage:
        return cmd_usage(args.json)

    if args.mcp is not None:
        return cmd_mcp(args.mcp or None)

    if args.notification:
        return cmd_notification(args.notification)

    if args.api_token:
        rc = cmd_api_token(args.api_token)
        if not args.set_model and not wants_models:
            return rc

    base, key = load_credentials()
    if args.set_model:
        return cmd_set_model(args.set_model, base, key)

    if wants_models:
        payload = fetch_models(base, key)
        if args.json:
            print(json.dumps(payload, indent=2))
            return 0
        return print_models_table(payload)

    return cmd_launch_claude(launch_path, claude_argv, model_override=args.model_override)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nAborted.")
        raise SystemExit(130)
