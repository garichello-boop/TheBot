#!/usr/bin/env python
"""
cli.py — TheBot management CLI.

Управление ботом без прямого SQL: просмотр конфига и состояния,
смена статуса, откат параметров, FSM-хронология, просмотр событий,
резолв STOP_CRANE.

Использование:
  python cli.py config show       --user igor --bot-id btc_paper_01
  python cli.py config history    --user igor --bot-id btc_paper_01 [--limit 20]
  python cli.py config set-status --user igor --bot-id btc_paper_01 --status STOPPED
  python cli.py config rollback   --user igor --bot-id btc_paper_01 --to-version 3

  python cli.py state show        --user igor --bot-id btc_paper_01
  python cli.py state history     --user igor --bot-id btc_paper_01 [--limit 20]
  python cli.py state resolve-stop-crane --user igor --bot-id btc_paper_01 [--yes]

  python cli.py events tail       --bot-id btc_paper_01 [--limit 50]
                                  [--type SL_TRIGGERED] [--cycle-id <id>]
                                  [--file logs/events.ndjson]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Load .env before anything else — settings read from environment.
# Try utf-8-sig first (handles BOM too), fall back to cp1251 for Windows
# machines where .env was saved in Cyrillic encoding.
from dotenv import load_dotenv
try:
    load_dotenv(encoding='utf-8-sig')
except UnicodeDecodeError:
    try:
        load_dotenv(encoding='cp1251')
    except Exception:
        pass  # Fall back to system environment variables

# CLI output must be clean. Suppress all logger noise.
logging.disable(logging.CRITICAL)


# ─────────────────────────────────────────────────────── DB bootstrap ──

def _init_db() -> None:
    """
    Initialize the global connection pool from .env / environment variables.

    Reads DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD directly —
    bypasses AppSettings to avoid Pydantic env-parsing quirks in CLI context
    (AppSettings is designed for the bot process, not short-lived CLI invocations).
    load_dotenv() at the top of the file ensures .env is already loaded.
    """
    import os
    from urllib.parse import quote as _url_quote
    from db.connection import init_pool  # noqa: PLC0415

    host     = os.getenv("DB_HOST",     "localhost")
    port     = os.getenv("DB_PORT",     "5432")
    name     = os.getenv("DB_NAME",     "thebot")
    user     = os.getenv("DB_USER",     "postgres")
    password = os.getenv("DB_PASSWORD", "")

    # URL-encode password so special chars ($, @, :, /) don't break the DSN.
    dsn = f"postgresql://{user}:{_url_quote(password, safe='')}@{host}:{port}/{name}"

    try:
        init_pool(dsn, min_conn=1, max_conn=2)
    except Exception as exc:
        print(f"  Error: cannot connect to database — {exc}", file=sys.stderr)
        sys.exit(1)


# ───────────────────────────────────────────────────────── Formatting ──

def _hr(width: int = 60) -> str:
    return "  " + "─" * width


def _fmt_dt(ts_ms: int | None) -> str:
    """Convert millisecond timestamp to human-readable UTC string."""
    if ts_ms is None:
        return "—"
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def _fmt_tz(dt) -> str:
    """Format a timezone-aware datetime to UTC string."""
    if dt is None:
        return "—"
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _row(label: str, value, w: int = 18) -> str:
    return f"  {label:<{w}}  {value}"


# ──────────────────────────────────────────────────── config commands ──

def cmd_config_show(args) -> int:
    _init_db()
    from bot_config.repository import ConfigRepository, BotConfigNotFoundError  # noqa

    repo = ConfigRepository(db_pool=None)
    try:
        config, result = repo.reload(args.user, args.bot_id)
    except BotConfigNotFoundError as exc:
        print(f"  Error: {exc}", file=sys.stderr)
        return 1

    validity = "valid" if result.is_valid else "INVALID"
    print()
    print(f"  Config  {args.user} / {args.bot_id}  [{validity}]")
    print(_hr(52))
    print(_row("Ticker",         config.ticker))
    print(_row("Exchange",       config.exchange))
    print(_row("Strategy",       config.strategy_name))
    print(_row("Status",         config.status.value))
    print(_row("Config version", config.config_version))
    print(_row("Balance",        f"{config.virtual_balance} USDT"))
    print(_row("Updated at",     _fmt_tz(config.updated_at)))

    if not result.is_valid:
        print()
        print("  Validation errors:")
        for err in result.errors:
            print(f"    • {err}")

    if config.strategy_params:
        print()
        print("  Strategy params:")
        for k, v in config.strategy_params.items():
            print(f"    {k:<22}  {v}")

    print()
    return 0


def cmd_config_history(args) -> int:
    _init_db()
    from bot_config.repository import ConfigRepository  # noqa

    repo = ConfigRepository(db_pool=None)
    history = repo.get_history(args.user, args.bot_id, limit=args.limit)

    print()
    print(f"  Config history  {args.user} / {args.bot_id}  (last {args.limit})")
    print(_hr(72))

    if not history:
        print("  No history found.")
        print()
        return 0

    print(f"  {'ver':>4}  {'changed_by':<34}  changed_at")
    print(f"  {'─'*4}  {'─'*34}  {'─'*28}")
    for h in history:
        print(f"  {h.config_version:>4}  {h.changed_by:<34}  {_fmt_tz(h.changed_at)}")

    print()
    return 0


def cmd_config_set_status(args) -> int:
    _init_db()
    from bot_config.models import BotStatus                                  # noqa
    from bot_config.repository import ConfigRepository, BotConfigNotFoundError  # noqa

    try:
        status = BotStatus(args.status)
    except ValueError:
        valid = ", ".join(s.value for s in BotStatus)
        print(f"  Error: unknown status {args.status!r}. Valid: {valid}", file=sys.stderr)
        return 1

    repo = ConfigRepository(db_pool=None)
    try:
        repo.set_status(args.user, args.bot_id, status)
    except BotConfigNotFoundError as exc:
        print(f"  Error: {exc}", file=sys.stderr)
        return 1

    print(f"\n  ✓  {args.user}/{args.bot_id}  status → {status.value}\n")
    return 0


def cmd_config_rollback(args) -> int:
    _init_db()
    from bot_config.repository import (          # noqa
        ConfigRepository,
        BotConfigNotFoundError,
        ConfigHistoryNotFoundError,
    )

    repo = ConfigRepository(db_pool=None)
    try:
        config = repo.rollback(
            args.user,
            args.bot_id,
            to_version=args.to_version,
            changed_by=args.changed_by,
        )
    except ConfigHistoryNotFoundError as exc:
        print(f"  Error: {exc}", file=sys.stderr)
        return 1
    except BotConfigNotFoundError as exc:
        print(f"  Error: {exc}", file=sys.stderr)
        return 1

    print(
        f"\n  ✓  {args.user}/{args.bot_id}  "
        f"rolled back to v{args.to_version}  →  "
        f"new config_version = {config.config_version}"
    )
    if config.strategy_params:
        print()
        for k, v in config.strategy_params.items():
            print(f"     {k:<24}  {v}")
    print()
    return 0


# ───────────────────────────────────────────────────── state commands ──

def cmd_state_show(args) -> int:
    _init_db()
    from bot_state.state_repo import StateRepository  # noqa

    repo = StateRepository(db_pool=None)
    state = repo.load(args.user, args.bot_id)

    if state is None:
        print(f"  Error: no bot_state row for {args.user}/{args.bot_id}.", file=sys.stderr)
        return 1

    cr = state.closing_reason.value if state.closing_reason else "—"
    upd = _fmt_tz(state.updated_at)

    print()
    print(f"  State  {args.user} / {args.bot_id}")
    print(_hr(52))
    print(_row("Cycle status",   state.cycle_status.value))
    print(_row("Version",        state.version))
    print(_row("Cycle ID",       state.cycle_id or "—"))
    print(_row("DCA count",      state.dca_count))
    print(_row("Position qty",   f"{state.position_qty} BTC"))
    print(_row("Avg price",      f"{state.position_avg_price} USDT"
                                  if state.position_avg_price else "—"))
    print(_row("Balance free",   f"{state.virtual_balance_free} USDT"))
    print(_row("Balance locked", f"{state.virtual_balance_locked} USDT"))
    print(_row("Quote spent",    f"{state.quote_spent} USDT"))
    print(_row("Quote received", f"{state.quote_received} USDT"))
    print(_row("Closing reason", cr))
    print(_row("Updated at",     upd))
    print()
    return 0


def cmd_state_history(args) -> int:
    _init_db()
    from bot_state.state_repo import StateRepository  # noqa

    repo = StateRepository(db_pool=None)
    history = repo.get_history(args.user, args.bot_id, limit=args.limit)

    print()
    print(f"  State history  {args.user} / {args.bot_id}  (last {args.limit})")
    print(_hr(78))

    if not history:
        print("  No history found.")
        print()
        return 0

    print(f"  {'ver':>5}  {'transition':<28}  {'reason':<14}  recorded_at")
    print(f"  {'─'*5}  {'─'*28}  {'─'*14}  {'─'*28}")
    for h in history:
        reason = h.closing_reason.value if h.closing_reason else "—"
        print(
            f"  {h.version:>5}  "
            f"{h.transition_label:<28}  "
            f"{reason:<14}  "
            f"{_fmt_tz(h.recorded_at)}"
        )
    print()
    return 0


def cmd_state_resolve_stop_crane(args) -> int:
    """
    Reset a bot stuck in STOP_CRANE to IDLE.

    STOP_CRANE fires when SL is triggered but market slippage exceeds
    BROKER_SL_MAX_MARKET_SLIPPAGE_PCT. The bot halts to protect against
    a bad fill. The operator must close the open position on the exchange
    manually before running this command — the CLI only resets bot_state.
    """
    _init_db()
    from bot_state.models import CycleStatus           # noqa
    from bot_state.state_repo import StateRepository   # noqa
    from db.connection import transaction              # noqa

    repo = StateRepository(db_pool=None)
    state = repo.load(args.user, args.bot_id)

    if state is None:
        print(f"  Error: no bot_state row for {args.user}/{args.bot_id}.", file=sys.stderr)
        return 1

    if state.cycle_status != CycleStatus.STOP_CRANE:
        print(
            f"  Error: bot is in {state.cycle_status.value}, not STOP_CRANE.\n"
            f"         No changes made.",
            file=sys.stderr,
        )
        return 1

    print()
    print(f"  Bot:          {args.user}/{args.bot_id}")
    print(f"  State:        {state.cycle_status.value}")
    print(f"  Cycle ID:     {state.cycle_id or '—'}")
    print(f"  Position qty: {state.position_qty} BTC")
    print()
    print("  ⚠  Ensure the open position is CLOSED on the exchange before")
    print("     proceeding. This command only resets bot_state to IDLE.")
    print("     Balance fields are not modified — reconcile manually if needed.")
    print()

    if not args.yes:
        try:
            answer = input("  Type 'yes' to confirm: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\n  Aborted.")
            return 0
        if answer != "yes":
            print("  Aborted.")
            return 0

    with transaction() as cur:
        cur.execute(
            """
            UPDATE bot_state
               SET cycle_status          = 'IDLE',
                   cycle_id              = NULL,
                   active_entry_order_id = NULL,
                   active_tp_order_id    = NULL,
                   active_dca_order_ids  = '{}',
                   closing_reason        = NULL,
                   version               = version + 1,
                   updated_at            = NOW()
             WHERE user_id = %s AND bot_id = %s
            """,
            (args.user, args.bot_id),
        )
        if cur.rowcount == 0:
            print("  Error: update affected 0 rows.", file=sys.stderr)
            return 1

    print(f"\n  ✓  {args.user}/{args.bot_id}  reset to IDLE\n")
    return 0


# ───────────────────────────────────────────────────── events commands ──

def cmd_events_tail(args) -> int:
    """
    Show the last N events from the NDJSON log file with optional filtering.
    Reads the file sequentially and takes the last --limit matching lines.
    """
    path = Path(args.file)
    if not path.exists():
        print(f"  Error: file not found: {path}", file=sys.stderr)
        print(f"  Default path is logs/events.ndjson. Pass --file to override.", file=sys.stderr)
        return 1

    events: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            if args.bot_id and ev.get("bot_id") != args.bot_id:
                continue
            if args.type and ev.get("event_type") != args.type:
                continue
            if args.cycle_id and ev.get("cycle_id") != args.cycle_id:
                continue

            events.append(ev)

    events = events[-args.limit:]

    # Build filter description for the header
    filters = []
    if args.bot_id:
        filters.append(f"bot={args.bot_id}")
    if args.type:
        filters.append(f"type={args.type}")
    if args.cycle_id:
        filters.append(f"cycle={args.cycle_id}")
    filter_str = "  [" + ", ".join(filters) + "]" if filters else ""

    print()
    print(f"  Events  {path}{filter_str}  (last {args.limit})")
    print(_hr(80))

    if not events:
        print("  No events found.")
        print()
        return 0

    print(f"  {'event_type':<30}  {'level':<8}  {'cycle_id':<18}  timestamp")
    print(f"  {'─'*30}  {'─'*8}  {'─'*18}  {'─'*24}")

    for ev in events:
        etype    = ev.get("event_type", "—")
        level    = ev.get("level", "—")
        cycle_id = ev.get("cycle_id") or "—"
        ts_ms    = ev.get("ts_ms")
        ts_str   = _fmt_dt(ts_ms)
        print(f"  {etype:<30}  {level:<8}  {cycle_id:<18}  {ts_str}")

    print()
    return 0


# ─────────────────────────────────────────────────────── CLI structure ──

def _add_bot_args(p: argparse.ArgumentParser) -> None:
    """Add --user and --bot-id to a subcommand parser."""
    p.add_argument("--user",   required=True, help="user_id (e.g. igor)")
    p.add_argument("--bot-id", required=True, dest="bot_id",
                   help="bot_id (e.g. btc_paper_01)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="TheBot management CLI — bot control without raw SQL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python cli.py config show       --user igor --bot-id btc_paper_01\n"
            "  python cli.py config history    --user igor --bot-id btc_paper_01\n"
            "  python cli.py config set-status --user igor --bot-id btc_paper_01 --status STOPPED\n"
            "  python cli.py config rollback   --user igor --bot-id btc_paper_01 --to-version 3\n"
            "\n"
            "  python cli.py state show        --user igor --bot-id btc_paper_01\n"
            "  python cli.py state history     --user igor --bot-id btc_paper_01\n"
            "  python cli.py state resolve-stop-crane --user igor --bot-id btc_paper_01\n"
            "\n"
            "  python cli.py events tail --bot-id btc_paper_01 --limit 50\n"
            "  python cli.py events tail --bot-id btc_paper_01 --type SL_TRIGGERED\n"
            "  python cli.py events tail --bot-id btc_paper_01 --cycle-id <id>\n"
        ),
    )

    groups = parser.add_subparsers(dest="group", metavar="<group>")
    groups.required = True

    # ── config ──────────────────────────────────────────────────────────

    config_p = groups.add_parser("config", help="Bot config commands")
    config_cmds = config_p.add_subparsers(dest="command", metavar="<command>")
    config_cmds.required = True

    p = config_cmds.add_parser("show", help="Show current config and strategy params")
    _add_bot_args(p)
    p.set_defaults(func=cmd_config_show)

    p = config_cmds.add_parser("history", help="Show config change history")
    _add_bot_args(p)
    p.add_argument("--limit", type=int, default=20,
                   help="Max rows to show (default: 20)")
    p.set_defaults(func=cmd_config_history)

    p = config_cmds.add_parser("set-status", help="Change bot status")
    _add_bot_args(p)
    p.add_argument(
        "--status", required=True,
        choices=["ACTIVE", "CLOSE_ONLY", "STOPPED", "FORCE_CLOSE"],
        help="New status value",
    )
    p.set_defaults(func=cmd_config_set_status)

    p = config_cmds.add_parser(
        "rollback",
        help="Restore strategy_params from a history version",
    )
    _add_bot_args(p)
    p.add_argument("--to-version", type=int, required=True, dest="to_version",
                   help="config_version to restore from history")
    p.add_argument("--changed-by", default="operator", dest="changed_by",
                   help="Attribution label written to audit trail (default: operator)")
    p.set_defaults(func=cmd_config_rollback)

    # ── state ────────────────────────────────────────────────────────────

    state_p = groups.add_parser("state", help="Bot state commands")
    state_cmds = state_p.add_subparsers(dest="command", metavar="<command>")
    state_cmds.required = True

    p = state_cmds.add_parser("show", help="Show current bot state")
    _add_bot_args(p)
    p.set_defaults(func=cmd_state_show)

    p = state_cmds.add_parser("history", help="Show FSM transition history")
    _add_bot_args(p)
    p.add_argument("--limit", type=int, default=20,
                   help="Max rows to show (default: 20)")
    p.set_defaults(func=cmd_state_history)

    p = state_cmds.add_parser(
        "resolve-stop-crane",
        help="Reset bot stuck in STOP_CRANE to IDLE",
        description=(
            "Resets cycle_status to IDLE for a bot halted in STOP_CRANE.\n"
            "STOP_CRANE fires when SL cannot be filled within the allowed slippage.\n\n"
            "⚠  Close the open position on the exchange BEFORE running this command.\n"
            "   Balance fields in bot_state are NOT modified — reconcile manually."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_bot_args(p)
    p.add_argument("--yes", action="store_true",
                   help="Skip confirmation prompt (for scripted use)")
    p.set_defaults(func=cmd_state_resolve_stop_crane)

    # ── events ───────────────────────────────────────────────────────────

    events_p = groups.add_parser("events", help="Events log commands")
    events_cmds = events_p.add_subparsers(dest="command", metavar="<command>")
    events_cmds.required = True

    p = events_cmds.add_parser(
        "tail",
        help="Show last N events from NDJSON log with optional filtering",
    )
    p.add_argument("--bot-id", dest="bot_id", default=None,
                   help="Filter by bot_id")
    p.add_argument("--type", default=None,
                   help="Filter by event_type (e.g. SL_TRIGGERED, TRADE_APPLIED)")
    p.add_argument("--cycle-id", dest="cycle_id", default=None,
                   help="Filter by cycle_id")
    p.add_argument("--limit", type=int, default=50,
                   help="Max rows to show (default: 50)")
    p.add_argument("--file", default="logs/events.ndjson",
                   help="Path to NDJSON log file (default: logs/events.ndjson)")
    p.set_defaults(func=cmd_events_tail)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
