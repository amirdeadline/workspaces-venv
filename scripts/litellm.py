#!/usr/bin/env python3
"""LiteLLM CLI — query the Scale Team LiteLLM proxy.

Usage (from any directory, after install):
    litellm --models

Uses ANTHROPIC_API_KEY from the environment (e.g. after `palo`), or from
the palo workspace .env. Never prints the key.
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

DEFAULT_BASE_URL = "https://scaleteam-litellm-eu.paloaltonetworks.com"
SCRIPTS_DIR = Path(__file__).resolve().parent


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


def load_credentials() -> tuple[str, str]:
    env = parse_env_file(palo_env_path())
    base = (
        os.environ.get("ANTHROPIC_BASE_URL")
        or env.get("ANTHROPIC_BASE_URL")
        or DEFAULT_BASE_URL
    ).rstrip("/")
    key = os.environ.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_API_KEY") or ""
    if not key:
        raise SystemExit(
            "ANTHROPIC_API_KEY is not set. Run `palo` first, or:\n"
            "  venv --var palo --add ANTHROPIC_API_KEY <key>"
        )
    return base, key


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
        "OWNED_BY",
        "MAX_INPUT",
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
                str(item.get("owned_by") or ""),
                str(item.get("max_input_tokens") or ""),
                str(item.get("max_output_tokens") or ""),
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="litellm",
        description="Query the Palo Alto Scale Team LiteLLM proxy.",
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
        "command",
        nargs="?",
        choices=["models"],
        help="Same as --models",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.models and args.command != "models":
        build_parser().print_help()
        return 0
    base, key = load_credentials()
    payload = fetch_models(base, key)
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    return print_models_table(payload)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nAborted.")
        raise SystemExit(130)
