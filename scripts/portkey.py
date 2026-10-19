#!/usr/bin/env python3
"""Portkey KeyForge — isolated Claude Code sessions (separate from LiteLLM ``litellm``).

Does not call GET /v1/models (many keys get 403 on list); launches Claude Code with
KeyForge env (``ANTHROPIC_AUTH_TOKEN``, ``ANTHROPIC_BASE_URL``, ``ANTHROPIC_CUSTOM_HEADERS``).

Workspace ``palo`` variables (via ``venv --var palo --add``):

    PORTKEY_API_TOKEN          required
    PORTKEY_BASE_URL           optional (default prod LLM gateway)
    PORTKEY_MODEL              optional (default haiku 5.5)
    PORTKEY_HTTPS_PROXY        optional (http://10.0.0.117:3128)

Usage:
    portkey                    Start isolated chat here
    portkey chat               Same
    portkey test               Quick Claude Code probe (expects: portkey-ok)
    portkey hermes             Sync KeyForge into Hermes (default + all profiles)
    portkey hermes-test        Sync + Hermes one-shot probe
    portkey chat D:\\proj        Chat in that folder
    portkey --model opus-5       One-shot model for this session
    portkey --api-token <key>
    portkey --set-model @llmgateway-eu-vertex-ai/anthropic.claude-opus-5-5
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from litellm import (
    apply_context_to_env,
    claude_cli,
    load_palo_env,
    palo_workspace_root,
    print_models_table,
    redact,
    update_palo_env_var,
)

DEFAULT_BASE_URL = "https://scaleteam-llmgateway-prod.paloaltonetworks.com"
DEFAULT_MODEL = "@llmgateway-eu-vertex-ai/anthropic.claude-haiku-5-5"
DEFAULT_CONTEXT_TOKENS = 1_000_000
CONFIG_SUBDIR = ".claude-code-portkey"
HERMES_KEY_ENV = "HERMES_CUSTOM_PORTKEY_API_KEY"
HERMES_PROVIDER_ID = "portkey"
HERMES_LEGACY_PROVIDER_IDS = ("paloportkey",)
PORTKEY_PROBE_PROMPT = "Reply with exactly: portkey-ok"
PORTKEY_PROBE_EXPECT = "portkey-ok"
# Declared models so the desktop picker lists Portkey without /v1/models (often 403).
HERMES_DECLARED_MODELS: tuple[str, ...] = (
    DEFAULT_MODEL,
    "@llmgateway-eu-vertex-ai/anthropic.claude-sonnet-5-5",
    "@llmgateway-eu-vertex-ai/anthropic.claude-opus-5-5",
)

# KeyForge catalog (no live /v1/models probe required).
MODEL_ALIASES: dict[str, str] = {
    "default": DEFAULT_MODEL,
    "haiku": DEFAULT_MODEL,
    "haiku-5-5": DEFAULT_MODEL,
    "haiku5": DEFAULT_MODEL,
    "sonnet-5-5": "@llmgateway-eu-vertex-ai/anthropic.claude-sonnet-5-5",
    "sonnet": "@llmgateway-eu-vertex-ai/anthropic.claude-sonnet-5-5",
    "opus-5-5": "@llmgateway-eu-vertex-ai/anthropic.claude-opus-5-5",
    "opus-5": "@llmgateway-eu-vertex-ai/anthropic.claude-opus-5-5",
    "opus5": "@llmgateway-eu-vertex-ai/anthropic.claude-opus-5-5",
    "opus": "@llmgateway-eu-vertex-ai/anthropic.claude-opus-5-5",
}


def load_portkey_credentials() -> tuple[str, str, str]:
    env = load_palo_env()
    base = (env.get("PORTKEY_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    token = (env.get("PORTKEY_API_TOKEN") or "").strip()
    model = (env.get("PORTKEY_MODEL") or DEFAULT_MODEL).strip()
    if not token:
        raise SystemExit(
            "PORTKEY_API_TOKEN is not set on workspace palo. Run:\n"
            "  venv --var palo --add PORTKEY_API_TOKEN <key>\n"
            "  or: portkey --api-token <key>"
        )
    return base, token, model


def portkey_auth_headers(token: str) -> list[str]:
    return [
        "-H",
        f"Authorization: Bearer {token}",
        "-H",
        f"x-portkey-api-key: {token}",
    ]


def fetch_models(base_url: str, token: str) -> dict:
    """Optional; may return 403 for KeyForge keys without list permission."""
    url = f"{base_url.rstrip('/')}/v1/models"
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise SystemExit("curl.exe not found.")
    proxy = (load_palo_env().get("PORTKEY_HTTPS_PROXY") or "").strip()
    cmd: list[str] = [curl, "-sS", "--ssl-no-revoke"]
    if proxy:
        cmd.extend(["-x", proxy])
    cmd.extend(
        [
            "-w",
            "\nHTTP_CODE:%{http_code}",
            "--max-time",
            "45",
            url,
            *portkey_auth_headers(token),
        ]
    )
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
            f"Portkey gateway HTTP {http_code}: {redact(message) or redact(body)[:400]}"
        )
    return data


def ensure_portkey_settings(env: dict[str, str]) -> None:
    """KeyForge-style settings.json in the isolated config dir (not ~/.claude)."""
    cfg = Path(env["CLAUDE_CONFIG_DIR"])
    cfg.mkdir(parents=True, exist_ok=True)
    path = cfg / "settings.json"
    model = env["ANTHROPIC_MODEL"]
    token = env["ANTHROPIC_AUTH_TOKEN"]
    base = env["ANTHROPIC_BASE_URL"]
    headers = env["ANTHROPIC_CUSTOM_HEADERS"]
    data: dict = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            data = {}
    data["env"] = {
        "ANTHROPIC_AUTH_TOKEN": token,
        "ANTHROPIC_BASE_URL": base,
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_CUSTOM_HEADERS": headers,
    }
    data["model"] = model
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def build_portkey_claude_env() -> dict[str, str]:
    env = load_palo_env()
    base, token, model = load_portkey_credentials()
    env["ANTHROPIC_BASE_URL"] = base
    env["ANTHROPIC_AUTH_TOKEN"] = token
    env["ANTHROPIC_API_KEY"] = token
    env["ANTHROPIC_MODEL"] = model
    env["ANTHROPIC_CUSTOM_HEADERS"] = f"x-portkey-api-key: {token}"
    cfg = palo_workspace_root() / CONFIG_SUBDIR
    cfg.mkdir(parents=True, exist_ok=True)
    env["CLAUDE_CONFIG_DIR"] = str(cfg)
    try:
        ctx = int(env.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS") or DEFAULT_CONTEXT_TOKENS)
    except ValueError:
        ctx = DEFAULT_CONTEXT_TOKENS
    apply_context_to_env(env, ctx)
    proxy = (env.get("PORTKEY_HTTPS_PROXY") or "").strip()
    if proxy:
        env["HTTPS_PROXY"] = proxy
        env["HTTP_PROXY"] = proxy
    ensure_portkey_settings(env)
    return env


def resolve_model_alias(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if text.startswith("@llmgateway-"):
        return text
    key = text.lower().replace("_", "-")
    return MODEL_ALIASES.get(key, text)


def cmd_api_token(token: str) -> int:
    token = (token or "").strip()
    if not token or len(token) < 8:
        raise SystemExit("--api-token requires a non-empty Portkey API token.")
    update_palo_env_var("PORTKEY_API_TOKEN", token)
    os.environ["PORTKEY_API_TOKEN"] = token
    print("Re-run `palo` in open shells so PORTKEY_API_TOKEN updates in your session.")
    return 0


def cmd_set_base_url(url: str) -> int:
    url = (url or "").strip().rstrip("/")
    if not url.startswith("https://"):
        raise SystemExit("--set-base-url must be an https:// URL")
    update_palo_env_var("PORTKEY_BASE_URL", url)
    os.environ["PORTKEY_BASE_URL"] = url
    print(f"[OK] PORTKEY_BASE_URL -> {url}")
    return 0


def cmd_set_model(model_id: str) -> int:
    model_id = resolve_model_alias(model_id) or (model_id or "").strip()
    if not model_id:
        raise SystemExit("--set-model requires a model id (KeyForge @llmgateway-eu-vertex-ai/... )")
    update_palo_env_var("PORTKEY_MODEL", model_id)
    os.environ["PORTKEY_MODEL"] = model_id
    print(f"[OK] PORTKEY_MODEL -> {model_id}")
    return 0


def hermes_home() -> Path:
    raw = (os.environ.get("HERMES_HOME") or "").strip()
    if raw:
        return Path(raw)
    return Path.home() / "AppData" / "Local" / "hermes"


def hermes_cli() -> Path:
    exe = hermes_home() / "bin" / "hermes.cmd"
    if not exe.is_file():
        raise SystemExit(f"Hermes not found (expected {exe}). Install Hermes Agent first.")
    return exe


def iter_hermes_profile_names() -> list[str]:
    """Default profile + every named profile with a config.yaml (desktop uses these homes)."""
    names = ["default"]
    profiles_root = hermes_home() / "profiles"
    if profiles_root.is_dir():
        for child in sorted(profiles_root.iterdir()):
            if child.is_dir() and (child / "config.yaml").is_file():
                names.append(child.name)
    return names


def hermes_profile_env_path(profile: str) -> Path:
    if profile == "default":
        return hermes_home() / ".env"
    return hermes_home() / "profiles" / profile / ".env"


def hermes_profile_config_path(profile: str) -> Path:
    if profile == "default":
        return hermes_home() / "config.yaml"
    return hermes_home() / "profiles" / profile / "config.yaml"


def update_hermes_env_var(key: str, value: str, profile: str = "default") -> None:
    """Set or replace one key in a profile's Hermes .env (never prints the value)."""
    path = hermes_profile_env_path(profile)
    lines: list[str] = []
    if path.is_file():
        lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    found = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            out.append(line)
            continue
        name, _, _rest = line.partition("=")
        if name.strip() == key:
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        if out and out[-1].strip():
            out.append("")
        out.append("# Portkey KeyForge (synced by `portkey hermes`)")
        out.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def _hermes_cli_prefix(profile: str) -> list[str]:
    cmd = [str(hermes_cli())]
    if profile != "default":
        cmd.extend(["-p", profile])
    return cmd


def _hermes_config_get(key: str, profile: str) -> str:
    proc = subprocess.run(
        [*_hermes_cli_prefix(profile), "config", "get", key],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip().splitlines()[0].strip()


def _hermes_config_set(key: str, value: str, profile: str, *, force: bool = False) -> None:
    cmd = [*_hermes_cli_prefix(profile), "config", "set", key, value]
    if force:
        cmd.append("--force")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        msg = redact((proc.stderr or proc.stdout or "hermes config set failed").strip())
        raise SystemExit(f"[profile {profile}] {msg}")


def _hermes_python() -> Path:
    venv_py = hermes_home() / "hermes-agent" / "venv" / "Scripts" / "python.exe"
    if venv_py.is_file():
        return venv_py
    tools = hermes_home() / "tools"
    candidates = sorted(tools.glob("python-*/python.exe"))
    if not candidates:
        raise SystemExit(f"Hermes Python not found (no venv or tools/python-* under {hermes_home()})")
    return candidates[-1]


def _hermes_profile_home(profile: str) -> Path:
    if profile == "default":
        return hermes_home()
    return hermes_home() / "profiles" / profile


def _fix_portkey_models_map(profile: str, model_ids: list[str]) -> None:
    """Rewrite ``providers.portkey.models`` (model ids contain ``.`` — dotted config paths break)."""
    py = _hermes_python()
    agent_dir = hermes_home() / "hermes-agent"
    home = _hermes_profile_home(profile)
    unique = [m for m in dict.fromkeys(model_ids) if m]
    script = r"""
import os
import sys
sys.path.insert(0, sys.argv[1])
os.environ["HERMES_HOME"] = sys.argv[2]
from hermes_cli.config import load_config, save_config

cfg = load_config()
providers = cfg.setdefault("providers", {})
entry = providers.setdefault("portkey", {})
for key in list(entry.keys()):
    text = str(key)
    if text != "models" and ("models[" in text or text.startswith("models")):
        entry.pop(key, None)
entry["models"] = {mid: {} for mid in sys.argv[3:]}
save_config(cfg)
"""
    proc = subprocess.run(
        [str(py), "-c", script, str(agent_dir), str(home), *unique],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        msg = redact((proc.stderr or proc.stdout or "portkey models rewrite failed").strip())
        raise SystemExit(f"[profile {profile}] {msg}")


def _sync_portkey_provider_config(profile: str, base: str, token: str, model: str) -> None:
    pid = HERMES_PROVIDER_ID
    _hermes_config_set(f"providers.{pid}.name", "Portkey KeyForge", profile)
    _hermes_config_set(f"providers.{pid}.base_url", base.rstrip("/"), profile)
    _hermes_config_set(f"providers.{pid}.api_mode", "anthropic_messages", profile, force=True)
    _hermes_config_set(f"providers.{pid}.model", model, profile, force=True)
    _hermes_config_set(f"providers.{pid}.key_env", HERMES_KEY_ENV, profile, force=True)
    _hermes_config_set(f"providers.{pid}.discover_models", "false", profile, force=True)
    _hermes_config_set(
        f"providers.{pid}.extra_headers.x-portkey-api-key",
        token,
        profile,
        force=True,
    )
    _fix_portkey_models_map(profile, [model, *HERMES_DECLARED_MODELS])


def _migrate_legacy_portkey_provider(profile: str) -> None:
    """Point ``model.provider`` at ``portkey`` when a profile still uses ``paloportkey``."""
    current = _hermes_config_get("model.provider", profile).strip().lower()
    if current not in {x.lower() for x in HERMES_LEGACY_PROVIDER_IDS}:
        return
    _hermes_config_set("model.provider", HERMES_PROVIDER_ID, profile, force=True)
    _hermes_config_set("model.api_mode", "anthropic_messages", profile, force=True)
    base = _hermes_config_get(f"providers.{HERMES_PROVIDER_ID}.base_url", profile)
    if base:
        _hermes_config_set("model.base_url", base, profile, force=True)
    _hermes_config_set("model.key_env", HERMES_KEY_ENV, profile, force=True)


def _sync_portkey_to_hermes_profile(
    profile: str,
    base: str,
    token: str,
    model: str,
    proxy: str,
) -> None:
    update_hermes_env_var(HERMES_KEY_ENV, token, profile)
    if proxy:
        update_hermes_env_var("HTTPS_PROXY", proxy, profile)
        update_hermes_env_var("HTTP_PROXY", proxy, profile)
    _sync_portkey_provider_config(profile, base, token, model)
    _migrate_legacy_portkey_provider(profile)


def cmd_hermes(*, run_test: bool = False, model_override: str | None = None) -> int:
    """Register/sync Portkey KeyForge as Hermes provider ``portkey`` (all profiles)."""
    base, token, model = load_portkey_credentials()
    model = resolve_model_alias(model_override) or model
    env = load_palo_env()
    proxy = (env.get("PORTKEY_HTTPS_PROXY") or "").strip()

    profiles = iter_hermes_profile_names()
    for profile in profiles:
        _sync_portkey_to_hermes_profile(profile, base, token, model, proxy)

    print("Hermes Portkey KeyForge synced to every profile (desktop reads profile config, not only default).")
    for profile in profiles:
        print(f"  [{profile}] {hermes_profile_config_path(profile)}")
    print(f"  API key: {HERMES_KEY_ENV} in each profile's .env")
    print(f"  Base   : {base}")
    print(f"  Model  : {model}")
    if proxy:
        print(f"  Proxy  : {proxy}")
    print("  Desktop: Settings > Providers > Custom Endpoints > Portkey KeyForge")
    print("           Model menu > Portkey KeyForge (restart the app if it was already open)")
    print("  CLI     : hermes -p amir-palo chat --provider portkey -m <model>")

    if not run_test:
        return 0
    return cmd_hermes_test(model_override=model)


def cmd_hermes_test(*, model_override: str | None = None) -> int:
    base, _token, model = load_portkey_credentials()
    model = resolve_model_alias(model_override) or model
    env = os.environ.copy()
    palo = load_palo_env()
    proxy = (palo.get("PORTKEY_HTTPS_PROXY") or "").strip()
    if proxy:
        env["HTTPS_PROXY"] = proxy
        env["HTTP_PROXY"] = proxy
    proc = subprocess.run(
        [
            str(hermes_cli()),
            "chat",
            "-q",
            PORTKEY_PROBE_PROMPT,
            "--oneshot",
            "-Q",
            "--provider",
            "portkey",
            "-m",
            model,
            "--no-restore-cwd",
        ],
        env=env,
        capture_output=True,
        text=True,
        cwd=os.getcwd(),
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    out_red = redact(out)
    if proc.returncode != 0:
        raise SystemExit(f"Hermes Portkey test failed (exit {proc.returncode}):\n{out_red[:1200]}")
    if PORTKEY_PROBE_EXPECT not in out:
        raise SystemExit(f"Hermes Portkey test: expected {PORTKEY_PROBE_EXPECT!r} in output:\n{out_red[:800]}")
    print(f"Hermes Portkey OK ({model} @ {base})")
    return 0


def cmd_test(*, model_override: str | None = None) -> int:
    """Claude Code one-shot probe via KeyForge (same as successful manual chat test)."""
    return cmd_launch(
        None,
        ["-p", PORTKEY_PROBE_PROMPT, "--print"],
        model_override=model_override,
    )


def cmd_status() -> int:
    """Show config only (no /v1/models)."""
    base, _token, model = load_portkey_credentials()
    env = load_palo_env()
    proxy = (env.get("PORTKEY_HTTPS_PROXY") or "").strip()
    cfg = palo_workspace_root() / CONFIG_SUBDIR
    print("Portkey KeyForge (isolated Claude Code)")
    print(f"  Base URL  : {base}")
    print(f"  Model     : {model}")
    print(f"  Config dir: {cfg}")
    print(f"  Token     : set (PORTKEY_API_TOKEN)")
    print(f"  Proxy     : {proxy or '(none; use PORTKEY_HTTPS_PROXY if needed)'}")
    print("  Launch    : portkey   or   portkey chat")
    return 0


def cmd_launch(project_dir: str | None, claude_argv: list[str], *, model_override: str | None) -> int:
    target = Path(project_dir or os.getcwd()).resolve()
    if not target.is_dir():
        raise SystemExit(f"Not a directory: {target}")
    env = build_portkey_claude_env()
    if model_override:
        resolved = resolve_model_alias(model_override) or model_override.strip()
        if resolved:
            env["ANTHROPIC_MODEL"] = resolved
            env["ANTHROPIC_CUSTOM_HEADERS"] = f"x-portkey-api-key: {env['ANTHROPIC_AUTH_TOKEN']}"
            ensure_portkey_settings(env)
            print(f"  (one-shot --model {model_override!r} -> {resolved})")
    print(f"Portkey chat (isolated) @ {target}")
    print(f"  API base : {env.get('ANTHROPIC_BASE_URL')}")
    print(f"  Model    : {env.get('ANTHROPIC_MODEL')}")
    print(f"  Config   : {env.get('CLAUDE_CONFIG_DIR')}  (not LiteLLM .claude-code)")
    if env.get("HTTPS_PROXY"):
        print(f"  Proxy    : {env.get('HTTPS_PROXY')}")
    exe = claude_cli()
    proc = subprocess.run([exe, *claude_argv], cwd=str(target), env=env)
    return proc.returncode


def _resolve_launch_path(command: str | None, claude_argv: list[str]) -> tuple[str | None, list[str]]:
    argv = list(claude_argv)
    if argv and argv[0] == "--":
        argv = argv[1:]
    launch_path: str | None = None
    if command in (None, "chat"):
        if argv and not argv[0].startswith("-"):
            cand = argv[0]
            p = Path(cand)
            if p.is_dir() or (len(cand) > 1 and cand[1] == ":"):
                launch_path = str(p.resolve())
                argv = argv[1:]
    elif command in ("test", "hermes", "hermes-test", "models"):
        pass
    elif command is not None:
        launch_path = command
    return launch_path, argv


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="portkey",
        description="Portkey KeyForge: isolated Claude Code chat (default). LiteLLM stays on `litellm`.",
    )
    p.add_argument(
        "--models",
        action="store_true",
        help="Try GET /v1/models (optional; may 403 without list permission)",
    )
    p.add_argument("--json", action="store_true", help="JSON output with --models")
    p.add_argument("--status", action="store_true", help="Show Portkey config (no API call)")
    p.add_argument("--api-token", metavar="TOKEN", help="Save PORTKEY_API_TOKEN on palo")
    p.add_argument("--set-base-url", metavar="URL", help="Save PORTKEY_BASE_URL on palo")
    p.add_argument("--set-model", metavar="MODEL", help="Save PORTKEY_MODEL on palo")
    p.add_argument(
        "--model",
        metavar="MODEL|ALIAS",
        dest="model_override",
        help="One-shot model (aliases: haiku, sonnet, opus-5, or full @llmgateway-... id)",
    )
    p.add_argument(
        "command",
        nargs="?",
        help="Optional: chat, test, hermes, hermes-test, project path, or 'models'",
    )
    p.add_argument("claude_argv", nargs=argparse.REMAINDER, help="Arguments passed to claude CLI")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    wants_models = args.models or args.command == "models"
    launch_path, claude_argv = _resolve_launch_path(args.command, list(args.claude_argv or []))

    if args.status:
        return cmd_status()

    if args.api_token:
        cmd_api_token(args.api_token)

    if args.set_base_url:
        return cmd_set_base_url(args.set_base_url)

    if args.set_model:
        return cmd_set_model(args.set_model)

    if args.command == "test":
        return cmd_test(model_override=args.model_override)

    if args.command == "hermes":
        return cmd_hermes(run_test=False, model_override=args.model_override)

    if args.command == "hermes-test":
        cmd_hermes(run_test=False, model_override=args.model_override)
        return cmd_hermes_test(model_override=args.model_override)

    if wants_models:
        base, token, _ = load_portkey_credentials()
        payload = fetch_models(base, token)
        if args.json:
            print(json.dumps(payload, indent=2))
            return 0
        return print_models_table(payload)

    load_portkey_credentials()
    return cmd_launch(launch_path, claude_argv, model_override=args.model_override)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nAborted.")
        raise SystemExit(130)
