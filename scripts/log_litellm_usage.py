#!/usr/bin/env python3
"""LiteLLM usage logger — tokens from Claude transcripts, USD from proxy key spend delta."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

WS_ROOT = Path(os.environ.get("WORKSPACES_ROOT") or r"E:\PC3_Shared\workspaces")
LOG_DIR = WS_ROOT / "logs"
LOG_FILE = Path(os.environ.get("LITELLM_USAGE_LOG") or (LOG_DIR / "litellm-usage.jsonl"))
STATE_FILE = LOG_DIR / "litellm-usage-state.json"
SCRIPTS = WS_ROOT / "scripts"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"last_key_spend_usd": None}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"last_key_spend_usd": None}


def save_state(state: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def append_log(entry: dict[str, Any]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def parse_entry_timestamp(row: dict[str, Any]) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(row.get("timestamp", "")).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def iter_log_entries() -> list[dict[str, Any]]:
    if not LOG_FILE.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def project_label(project_dir: str | None) -> str:
    if not project_dir:
        return "(no project)"
    p = Path(project_dir)
    name = p.name or str(p)
    try:
        resolved = str(p.resolve())
    except OSError:
        resolved = str(p)
    if resolved.lower() != str(p).lower():
        return f"{name}  ({resolved})"
    return resolved


def _entry_tokens(row: dict[str, Any]) -> int:
    return int((row.get("tokens") or {}).get("total", 0) or 0)


def _entry_run_usd(row: dict[str, Any]) -> float:
    return float((row.get("cost_usd") or {}).get("run", 0) or 0)


def aggregate_by_project(rows: list[dict[str, Any]]) -> list[tuple[str, str, int, float]]:
    """Return (sort_key, display_label, tokens, usd) sorted by usd desc then tokens."""
    by_key: dict[str, list[int | float | str]] = {}
    for row in rows:
        raw = row.get("project_dir")
        key = str(raw).lower().replace("/", "\\") if raw else ""
        label = project_label(str(raw) if raw else None)
        bucket = by_key.setdefault(key, [label, 0, 0.0])
        bucket[1] = int(bucket[1]) + _entry_tokens(row)
        bucket[2] = float(bucket[2]) + _entry_run_usd(row)
    out: list[tuple[str, str, int, float]] = []
    for key, (label, tokens, usd) in by_key.items():
        out.append((key, str(label), int(tokens), float(usd)))
    out.sort(key=lambda x: (-x[3], -x[2], x[1].lower()))
    return out


def period_totals(rows: list[dict[str, Any]], since: datetime) -> tuple[int, float]:
    total_tokens = 0
    total_usd = 0.0
    for row in rows:
        ts = parse_entry_timestamp(row)
        if ts is None or ts < since:
            continue
        total_tokens += _entry_tokens(row)
        total_usd += _entry_run_usd(row)
    return total_tokens, total_usd


def start_of_today_local() -> datetime:
    now = datetime.now().astimezone()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def weekly_totals(days: int = 7) -> tuple[int, float]:
    cutoff = _utcnow() - timedelta(days=days)
    return period_totals(iter_log_entries(), cutoff)


def build_usage_report() -> dict[str, Any]:
    rows = iter_log_entries()
    now_local = datetime.now().astimezone()
    today_start = start_of_today_local()
    week_start = _utcnow() - timedelta(days=7)
    month_start = _utcnow() - timedelta(days=30)
    projects = [
        {"project": label, "tokens": tokens, "cost_usd": round(usd, 6)}
        for _key, label, tokens, usd in aggregate_by_project(rows)
    ]
    t_today, u_today = period_totals(rows, today_start)
    t_7, u_7 = period_totals(rows, week_start)
    t_30, u_30 = period_totals(rows, month_start)
    return {
        "log_file": str(LOG_FILE),
        "generated_at": _iso(_utcnow()),
        "timezone": str(now_local.tzinfo),
        "projects": projects,
        "totals": {
            "today": {"tokens": t_today, "cost_usd": round(u_today, 6)},
            "last_7_days": {"tokens": t_7, "cost_usd": round(u_7, 6)},
            "last_30_days": {"tokens": t_30, "cost_usd": round(u_30, 6)},
        },
    }


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join(f"{{:{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for row in rows:
        print(fmt.format(*row))


def print_machine_usage_report(*, json_output: bool = False) -> int:
    report = build_usage_report()
    if json_output:
        print(json.dumps(report, indent=2))
        return 0
    if not report["projects"]:
        print(f"No LiteLLM usage logged yet on this machine.\nLog: {LOG_FILE}")
        return 0
    table_rows = [
        [
            p["project"],
            fmt_tokens(int(p["tokens"])),
            fmt_usd(float(p["cost_usd"])),
        ]
        for p in report["projects"]
    ]
    print("LiteLLM usage by project (all time, this machine - palo proxy key)\n")
    _print_table(["Project", "Tokens", "Cost"], table_rows)
    tot = report["totals"]
    print()
    print("Totals (logged runs on this machine for your LiteLLM API key)")
    print(
        f"  Today:      {fmt_tokens(tot['today']['tokens'])} tokens  "
        f"{fmt_usd(float(tot['today']['cost_usd']))}"
    )
    print(
        f"  Last 7 days:  {fmt_tokens(tot['last_7_days']['tokens'])} tokens  "
        f"{fmt_usd(float(tot['last_7_days']['cost_usd']))}"
    )
    print(
        f"  Last 30 days: {fmt_tokens(tot['last_30_days']['tokens'])} tokens  "
        f"{fmt_usd(float(tot['last_30_days']['cost_usd']))}"
    )
    print(f"\nLog: {LOG_FILE}")
    return 0


def get_key_spend_usd() -> float | None:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import litellm as L  # type: ignore
    except ImportError:
        return None
    import shutil
    import subprocess

    base, key = L.load_credentials()
    model = L.load_palo_env().get("ANTHROPIC_MODEL") or L.DEFAULT_MODEL
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        return None
    body = json.dumps({"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]})
    cmd = [
        curl,
        "-sS",
        "--ssl-no-revoke",
        "-D",
        "-",
        "-o",
        "NUL",
        "--max-time",
        "45",
        f"{base.rstrip('/')}/v1/chat/completions",
        "-H",
        f"Authorization: Bearer {key}",
        "-H",
        "Content-Type: application/json",
        "-d",
        body,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    raw = proc.stdout or ""
    for line in raw.splitlines():
        low = line.lower()
        if low.startswith("x-litellm-key-spend:"):
            try:
                return float(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def _usage_from_obj(usage: dict[str, Any]) -> dict[str, int]:
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    cr = int(usage.get("cache_read_input_tokens") or 0)
    cc = int(usage.get("cache_creation_input_tokens") or 0)
    total = inp + out + cr + cc
    return {"input": inp, "output": out, "cache_read": cr, "cache_creation": cc, "total": total}


def sum_tokens_transcript(path: Path, since: datetime | None = None) -> tuple[dict[str, int], str | None, str | None]:
    if not path.is_file():
        zeros = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "total": 0}
        return zeros, None, None
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "total": 0}
    seen_ids: set[str] = set()
    session_id: str | None = None
    model: str | None = None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "assistant":
            continue
        ts_raw = obj.get("timestamp")
        if since and ts_raw:
            try:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < since:
                    continue
            except ValueError:
                pass
        msg = obj.get("message") or {}
        mid = str(msg.get("id") or obj.get("uuid") or "")
        if mid and mid in seen_ids:
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        if mid:
            seen_ids.add(mid)
        part = _usage_from_obj(usage)
        for k in totals:
            totals[k] += part[k]
        session_id = session_id or obj.get("session_id") or obj.get("sessionId")
        model = model or msg.get("model")
    return totals, session_id, model


def find_transcript(project_dir: Path, since: datetime) -> Path | None:
    root = WS_ROOT / "virtual_envs" / "palo" / ".claude-code" / "projects"
    if not root.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for p in root.rglob("*.jsonl"):
        try:
            if p.stat().st_mtime >= since.timestamp() - 5:
                candidates.append((p.stat().st_mtime, p))
        except OSError:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    resolved = str(project_dir.resolve()).lower()
    for _, p in candidates:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")[:8000]
            if resolved in text.lower().replace("/", "\\"):
                return p
        except OSError:
            continue
    return candidates[0][1]


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        core = f"{n / 1_000_000:.2f}".rstrip("0").rstrip(".")
        return f"{core}M"
    if n >= 1_000:
        core = f"{n / 1_000:.1f}".rstrip("0").rstrip(".")
        return f"{core}K"
    return str(n)


def fmt_usd(x: float) -> str:
    return f"${x:.4f}" if x < 1 else f"${x:.2f}"


def record_run(
    *,
    source: str,
    project_dir: Path | None,
    since: datetime | None,
    transcript: Path | None,
    spend_before: float | None,
) -> dict[str, Any]:
    state = load_state()
    spend_after = get_key_spend_usd()
    run_usd = 0.0
    if spend_after is not None:
        prev = spend_before if spend_before is not None else state.get("last_key_spend_usd")
        if prev is not None:
            run_usd = max(0.0, spend_after - float(prev))
        state["last_key_spend_usd"] = spend_after
        save_state(state)

    if transcript is None and project_dir and since:
        transcript = find_transcript(project_dir, since)
    tokens, session_id, model = (
        sum_tokens_transcript(transcript, since) if transcript else ({"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "total": 0}, None, None)
    )

    week_tokens, week_usd = weekly_totals(7)
    week_tokens += tokens["total"]
    week_usd += run_usd

    entry = {
        "timestamp": _iso(_utcnow()),
        "source": source,
        "project_dir": str(project_dir) if project_dir else None,
        "session_id": session_id,
        "model": model,
        "tokens": tokens,
        "cost_usd": {"run": round(run_usd, 6), "cumulative_key": spend_after},
        "weekly": {"tokens": week_tokens, "usd": round(week_usd, 4), "days": 7},
    }
    append_log(entry)
    return entry


def print_summary(entry: dict[str, Any]) -> None:
    t = entry["tokens"]["total"]
    run = entry["cost_usd"]["run"]
    w = entry["weekly"]
    print(
        f"LiteLLM usage this run: {fmt_tokens(t)} tokens ({fmt_usd(run)}) | "
        f"This week: {fmt_tokens(int(w['tokens']))} tokens ({fmt_usd(float(w['usd']))} USD)"
    )


def cmd_get_spend(_: argparse.Namespace) -> int:
    spend = get_key_spend_usd()
    if spend is None:
        print("null")
        return 1
    print(f"{spend:.6f}")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    since = None
    if args.since:
        since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
    spend_before = float(args.spend_before) if args.spend_before else None
    project = Path(args.project_dir).resolve() if args.project_dir else None
    transcript = Path(args.transcript).resolve() if args.transcript else None
    entry = record_run(
        source=args.source,
        project_dir=project,
        since=since,
        transcript=transcript,
        spend_before=spend_before,
    )
    print_summary(entry)
    if args.json:
        print(json.dumps(entry, indent=2))
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    raw = sys.stdin.read()
    hook: dict[str, Any] = {}
    if raw.strip():
        try:
            hook = json.loads(raw)
        except json.JSONDecodeError:
            hook = {}
    transcript = hook.get("transcript_path") or args.transcript
    if not transcript:
        return 0
    path = Path(str(transcript))
    state = load_state()
    since: datetime | None = None
    if state.get("last_hook_at"):
        try:
            since = datetime.fromisoformat(str(state["last_hook_at"]).replace("Z", "+00:00"))
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
        except ValueError:
            since = None
    if since is None:
        since = _utcnow() - timedelta(minutes=5)
    entry = record_run(
        source="litellm_claude_stop_hook",
        project_dir=Path(hook.get("cwd") or os.getcwd()),
        since=since,
        transcript=path,
        spend_before=None,
    )
    print_summary(entry)
    return 0


def cmd_weekly(_: argparse.Namespace) -> int:
    t, u = weekly_totals(7)
    print(f"This week (7d): {fmt_tokens(t)} tokens, {fmt_usd(u)} USD — log: {LOG_FILE}")
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    return print_machine_usage_report(json_output=bool(args.json))


def main() -> int:
    ap = argparse.ArgumentParser(description="Log LiteLLM token and USD usage")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("get-spend")
    p.set_defaults(fn=cmd_get_spend)
    p = sub.add_parser("record")
    p.add_argument("--source", default="amir_use_litellm_subagent")
    p.add_argument("--project-dir")
    p.add_argument("--since")
    p.add_argument("--transcript")
    p.add_argument("--spend-before")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_record)
    p = sub.add_parser("hook")
    p.add_argument("--transcript")
    p.set_defaults(fn=cmd_hook)
    p = sub.add_parser("weekly")
    p.set_defaults(fn=cmd_weekly)
    p = sub.add_parser("report", help="Full project table + today/7d/30d totals")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_usage)
    args = ap.parse_args()
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
