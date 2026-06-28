#!/usr/bin/env python3
""" #lLHCoqNnaQu-OEO6IflvAGMZaxzG-nJ5CgAwFcHJAPM
MailGen_host.py — long-running FakeMail bot host + local HTTP API
===================================================================
This is the "always-on" sibling to mailgen_portable.py / tui.py.

The idea: instead of every caller spawning its own Telethon login, ONE
instance of this script stays connected to Telegram 24/7 (one session,
authenticated once), and your other tools (e.g. main.py) talk to it over
a small local HTTP API instead of touching Telethon directly.

Run it once interactively to log in (same OTP flow as the CLI tool):

    python MailGen_host.py -p +911234567890
    python MailGen_host.py -api 9844616 abcdef123456
    python MailGen_host.py serve

The first `serve` will ask for the Telegram OTP exactly like the CLI
tool does. After that, the session file under the storage dir is reused
on every restart and no further input is needed (unless Telegram forces
a re-login).

On first `serve`, a random API key is generated and printed once — copy
it into main.py. Every request must send it back as:

    X-API-Key: <key>

(except GET /health, which is unauthenticated so you can health-check it
from outside).

Endpoints
---------
    GET  /health
    GET  /queue
    POST /generate          {"username": "...", "domain": "1"|"2"}
    GET  /mailboxes
    GET  /inbox             ?days=7
    GET  /mailbox           ?address=foo@hi2.in&days=30
    GET  /poll              ?since=<ISO8601>&address=optional

/poll is the cheap one — it reads from an in-memory buffer that's fed by
a permanent NewMessage handler (the same fragment-grouping logic as the
CLI's monitor()), so it doesn't re-hit Telegram on every call. The others
(/inbox, /mailbox, /mailboxes, /generate) do talk to Telegram, same as
the CLI commands they're based on.

All Telegram-touching routes share one bounded job queue (default cap:
see `serve --queue-maxsize`) so only one conversation with @fakemailbot
is ever in flight. If the queue is already full, those routes return 503
instead of letting requests pile up in memory. GET /queue reports queue
depth, how long the in-flight job has been running, and whether the
worker is still alive; GET /health folds the worker's liveness in too
and reports "degraded" if it has died.

Storage (separate from mailgen_portable.py's ~/.config/mailgen so the two
session files never collide if you ever run both):

    ~/.config/mailgen_host/config.json   credentials + proxy + api_key
    ~/.config/mailgen_host/session/      Telethon session file
    ~/.config/mailgen_host/mails/        every reconstructed mail, one file each
    ~/.config/mailgen_host/logs/         host.log

Override the base dir with MAILGEN_HOST_HOME, same pattern as the CLI
tool's MAILGEN_HOME.

A couple of things worth keeping in mind since this runs unattended:
  - Bind to 127.0.0.1 (the default) unless you've put a firewall/VPN/
    reverse-proxy in front of it — the API key is the only thing standing
    between this and anyone who can reach the port.
  - This is Flask's dev server (fine for personal use); swap in waitress
    or gunicorn behind a reverse proxy if you want a sturdier 24/7 setup.
  - fakemailbot is a shared, unofficial Telegram bot — go easy on request
    volume so your account doesn't get rate-limited or banned, and keep in
    mind that whatever you sign these throwaway addresses up for is still
    subject to that other service's own terms.
"""
from werkzeug.exceptions import HTTPException
import os
import sys
import platform

# ---------------------------------------------------------------------------
# Console setup (identical reasoning to mailgen_portable.py)
# ---------------------------------------------------------------------------
def _prepare_console():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if platform.system() == "Windows":
        try:
            os.system("chcp 65001 >NUL 2>&1")
        except Exception:
            pass


_prepare_console()

# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------
_MISSING = []
try:
    import rich  # noqa: F401
except ImportError:
    _MISSING.append("rich")
try:
    import telethon  # noqa: F401
except ImportError:
    _MISSING.append("telethon")
try:
    import flask  # noqa: F401
except ImportError:
    _MISSING.append("flask")

if _MISSING:
    print("MailGen_host needs a couple of packages that aren't installed yet:\n")
    print(f"    pip install {' '.join(_MISSING)}\n")
    print("If pip refuses due to permissions, try: pip install --user " + " ".join(_MISSING))
    print("(If you plan to use a SOCKS5 proxy, also run: pip install pysocks)")
    sys.exit(1)

import argparse
import asyncio
import atexit
import getpass
import json
import logging
import re
import secrets
import threading
import time
from collections import deque
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from flask import Flask, request, jsonify

__version__ = "1.0.0-host"

console = Console()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def _home_dir() -> Path:
    try:
        return Path.home()
    except RuntimeError:
        return Path.cwd()


BASE_DIR = (
    Path(os.environ["MAILGEN_HOST_HOME"])
    if os.environ.get("MAILGEN_HOST_HOME")
    else (_home_dir() / ".config" / "mailgen_host")
)
CONFIG_PATH = BASE_DIR / "config.json"
SESSION_DIR = BASE_DIR / "session"
SESSION_NAME = str(SESSION_DIR / "fakemail_host")
MAILS_DIR = BASE_DIR / "mails"
LOGS_DIR = BASE_DIR / "logs"
LOG_FILE = LOGS_DIR / "host.log"

BOT_USERNAME = "fakemailbot"
SEP_RUN = "➖" * 5
LIST_HEADER_HINT = "here are the list of fake mail ids you have"
NEW_MAIL_HINT = "your new fake mail id is"
DOMAIN_MAP = {"1": "@hi2.in", "2": "@telegmail.com", "hi2.in": "@hi2.in", "telegmail.com": "@telegmail.com"}
QUEUE_MAXSIZE = 100  # default cap on pending jobs before /generate etc. return 503 "server busy"

BANNER = r"""
███╗   ███╗ █████╗ ██╗██╗      ██████╗ ███████╗███╗   ██╗    ██╗  ██╗ ██████╗ ███████╗████████╗
████╗ ████║██╔══██╗██║██║     ██╔════╝ ██╔════╝████╗  ██║    ██║  ██║██╔═══██╗██╔════╝╚══██╔══╝
██╔████╔██║███████║██║██║     ██║  ███╗█████╗  ██╔██╗ ██║    ███████║██║   ██║███████╗   ██║
██║╚██╔╝██║██╔══██║██║██║     ██║   ██║██╔══╝  ██║╚██╗██║    ██╔══██║██║   ██║╚════██║   ██║
██║ ╚═╝ ██║██║  ██║██║███████╗╚██████╔╝███████╗██║ ╚████║    ██║  ██║╚██████╔╝███████║   ██║
╚═╝     ╚═╝╚═╝  ╚═╝╚═╝╚══════╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝    ╚═╝  ╚═╝ ╚═════╝ ╚══════╝   ╚═╝
"""
BANNER_FALLBACK = "=== MailGen HOST ==="

log = logging.getLogger("mailgen_host")


def _print_banner():
    try:
        console.print(BANNER, style="bold green")
    except Exception:
        console.print(BANNER_FALLBACK, style="bold green")


# ---------------------------------------------------------------------------
# directories / logging
# ---------------------------------------------------------------------------
def ensure_dirs():
    for d in (BASE_DIR, SESSION_DIR, MAILS_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def setup_logging():
    ensure_dirs()
    logging.basicConfig(
        filename=str(LOG_FILE),
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def _print_connection_help():
    console.print("[yellow]Possible causes:[/yellow]")
    console.print("  • Proxy is offline or misconfigured")
    console.print("  • Telegram is blocked on this network")
    console.print("  • Invalid API credentials")
    console.print("  • No internet connection")


def _print_proxy_dependency_help(e: ImportError):
    console.print(f"[bold red][✗] Missing dependency for proxy support:[/bold red] {e}")
    console.print("Run: [bold]pip install pysocks[/bold]")


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "phone": None,
    "api_id": None,
    "api_hash": None,
    "proxy": None,
    "api_key": None,
}


def load_config() -> dict:
    ensure_dirs()
    if not CONFIG_PATH.exists():
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        console.print(f"[bold red][!] Failed to read config ({e}); using defaults.[/bold red]")
        return dict(DEFAULT_CONFIG)
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(data)
    return cfg


def save_config(cfg: dict):
    ensure_dirs()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def _require_config(cfg: dict) -> bool:
    missing = [k for k in ("phone", "api_id", "api_hash") if not cfg.get(k)]
    if missing:
        console.print(f"[bold red][!] Missing configuration:[/bold red] {', '.join(missing)}")
        console.print(
            "Use -p / -api (see --help) to configure MailGen_host before running `serve`."
        )
        return False
    return True


def _build_proxy(cfg: dict):
    proxy = cfg.get("proxy")
    if not proxy:
        return None
    return ("socks5", proxy["host"], int(proxy["port"]), True, proxy.get("user"), proxy.get("pass"))


def _mask_phone(phone):
    if not phone:
        return "(not set)"
    return f"{phone[:3]}{'*' * 7}"


def _mask_secret(secret, keep=4):
    if not secret:
        return "(not set)"
    secret = str(secret)
    if len(secret) <= keep:
        return "*" * max(len(secret), 4)
    return secret[:keep] + "*" * max(len(secret) - keep, 4)


def show_config():
    cfg = load_config()
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("Phone", _mask_phone(cfg.get("phone")))
    table.add_row("API ID", str(cfg.get("api_id")) if cfg.get("api_id") else "(not set)")
    table.add_row("API Hash", _mask_secret(cfg.get("api_hash")))
    table.add_row("API Key (for your main.py)", _mask_secret(cfg.get("api_key"), keep=6))
    proxy = cfg.get("proxy")
    table.add_row("Proxy", f"{proxy.get('host')}:{proxy.get('port')}" if proxy else "(not set)")
    table.add_row("Config file", str(CONFIG_PATH))
    table.add_row("Session dir", str(SESSION_DIR))
    console.print(Panel(table, title="MailGen_host config", border_style="cyan"))


def cmd_set_phone(phone: str):
    cfg = load_config()
    cfg["phone"] = phone
    save_config(cfg)
    console.print("[bold green][✓] Phone saved.[/bold green]")


def cmd_set_api(api_id: str, api_hash: str):
    cfg = load_config()
    try:
        cfg["api_id"] = int(api_id)
    except ValueError:
        console.print("[bold red][!] API ID must be a number.[/bold red]")
        return
    cfg["api_hash"] = api_hash
    save_config(cfg)
    console.print("[bold green][✓] API credentials saved.[/bold green]")


def cmd_set_proxy(host: str, port: str, user: str, password: str):
    cfg = load_config()
    try:
        port_int = int(port)
    except ValueError:
        console.print("[bold red][!] Proxy port must be a number.[/bold red]")
        return
    cfg["proxy"] = {"host": host, "port": port_int, "user": user, "pass": password}
    save_config(cfg)
    console.print("[bold green][✓] Proxy saved.[/bold green]")


# ---------------------------------------------------------------------------
# shared mail-parsing logic (same approach as mailgen_portable.py)
# ---------------------------------------------------------------------------
def is_mail_header(text: str) -> bool:
    return bool(text) and text.strip().startswith(SEP_RUN)


def group_into_mails(messages):
    mails = []
    current = []
    for msg in messages:
        text = msg.message
        if text is None:
            continue
        if text.startswith("/id"):
            continue
        if LIST_HEADER_HINT in text.lower():
            continue
        if NEW_MAIL_HINT in text.lower():
            continue
        if is_mail_header(text):
            if current:
                mails.append("\n".join(current))
            current = [text]
        elif current:
            current.append(text)
    if current:
        mails.append("\n".join(current))
    return mails


def _extract_to_address(mail_text: str):
    match = re.search(r"To:\s*<([^>]+)>", mail_text, re.IGNORECASE)
    return match.group(1).lower() if match else None


def _save_mail(mail_text: str) -> Path:
    ensure_dirs()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    subject_match = re.search(r"Subject:\s*(.+)", mail_text)
    subject = subject_match.group(1)[:40] if subject_match else "mail"
    subject = re.sub(r"[^a-zA-Z0-9_-]+", "_", subject).strip("_") or "mail"
    path = MAILS_DIR / f"{ts}_{subject}.txt"
    path.write_text(mail_text, encoding="utf-8")
    return path


async def _wait_for_bot_reply(client: TelegramClient, timeout: float = 20):
    future = asyncio.get_running_loop().create_future()

    async def handler(event):
        if not future.done():
            future.set_result(event.message)

    client.add_event_handler(handler, events.NewMessage(from_users=BOT_USERNAME))
    try:
        return await asyncio.wait_for(future, timeout=timeout)
    finally:
        client.remove_event_handler(handler, events.NewMessage(from_users=BOT_USERNAME))


async def _safe_wait_for_bot_reply(client: TelegramClient, timeout: float = 20):
    try:
        return await _wait_for_bot_reply(client, timeout=timeout)
    except asyncio.TimeoutError:
        return None


# ---------------------------------------------------------------------------
# login() — same OTP flow as the CLI tool, reused for the one-time bootstrap
# ---------------------------------------------------------------------------
async def login():
    ensure_dirs()
    cfg = load_config()
    if not _require_config(cfg):
        return None

    console.print("[green][✓][/green] Configuration loaded")
    if cfg.get("proxy"):
        console.print("[green][✓][/green] Proxy configured")

    client = TelegramClient(SESSION_NAME, cfg["api_id"], cfg["api_hash"], proxy=_build_proxy(cfg))

    try:
        with console.status("[bold cyan]Connecting to Telegram...[/bold cyan]"):
            await client.connect()
    except ImportError as e:
        _print_proxy_dependency_help(e)
        return None
    except (ConnectionError, OSError) as e:
        console.print(f"[bold red][✗] Connection failed:[/bold red] {e}")
        _print_connection_help()
        return None

    console.print("[green][✓][/green] Connected to Telegram")

    if await client.is_user_authorized():
        console.print("[green][✓][/green] Existing session is valid — reusing it")
        return client

    console.print("[yellow][·][/yellow] No valid session found — logging in fresh...")

    try:
        with console.status("[cyan]Requesting OTP...[/cyan]"):
            sent = await client.send_code_request(cfg["phone"])
    except (ConnectionError, OSError) as e:
        console.print(f"[bold red][✗] Failed to request OTP:[/bold red] {e}")
        _print_connection_help()
        await client.disconnect()
        return None

    console.print(f"[green][✓][/green] OTP requested — check Telegram on {_mask_phone(cfg['phone'])}")
    code = input(f"Enter the OTP sent to {cfg['phone']}: ").strip()

    try:
        await client.sign_in(phone=cfg["phone"], code=code, phone_code_hash=sent.phone_code_hash)
    except SessionPasswordNeededError:
        password = getpass.getpass("Two-step verification is on. Enter your password: ")
        await client.sign_in(password=password)

    console.print("[bold green][✓][/bold green] Login successful — session saved for next run.")
    log.info("Logged in successfully.")
    return client


# ---------------------------------------------------------------------------
# Background asyncio loop that owns the Telethon client for the lifetime
# of the process. Flask runs in the main thread; every Telegram-touching
# request hands its coroutine to this loop and blocks on the result.
#
# @fakemailbot is a single chat — it can't safely have two conversations
# in flight at once (a /generate from user A racing a /generate from user
# B would scramble whose reply is whose). So instead of letting every
# Flask request thread talk to tg.client directly and concurrently, every
# Telegram-touching request becomes a TelegramJob pushed onto one
# asyncio.Queue. A single worker coroutine drains that queue one job at a
# time — no locks needed, since there's only ever one reader.
# ---------------------------------------------------------------------------
class QueueFullError(Exception):
    """Raised when the Telegram job queue is already at capacity
    (QUEUE_MAXSIZE / --queue-maxsize). Mapped to HTTP 503 below."""


class TelegramJob:
    """One queued unit of work for the Telegram worker.

    `future` is a concurrent.futures.Future (not asyncio.Future) because
    it's created on a Flask request thread and resolved on the asyncio
    loop thread — concurrent.futures.Future is explicitly designed to be
    shared safely across threads like that; asyncio.Future is not.
    """

    __slots__ = ("id", "kind", "payload", "future", "submitted_at")

    def __init__(self, kind: str, payload: dict):
        self.id = secrets.token_hex(6)
        self.kind = kind
        self.payload = payload
        self.future = Future()
        self.submitted_at = time.monotonic()


class TelegramLoop:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True, name="telethon-loop")
        self.client = None
        self.queue = None          # asyncio.Queue, created on self.loop once it's running
        self.current_job = None    # kind of the job the worker is processing right now, or None
        self.current_job_started_at = None  # time.monotonic() when current_job was picked up, or None
        self.worker_future = None  # concurrent.futures.Future for the running worker task

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self):
        self.thread.start()

    def run_coro(self, coro, timeout=30):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)

    def submit(self, kind: str, payload: dict, timeout: float = 30):
        """Enqueue a job for the single Telegram worker and block (this
        thread only) until it's processed. Returns (result, queued_ahead)
        where queued_ahead is how many jobs were already waiting when
        this one was submitted.

        Raises QueueFullError (without ever touching Telegram) if the
        queue is already at capacity — see QUEUE_MAXSIZE."""
        if self.queue is None:
            raise RuntimeError("request queue is not running yet")
        job = TelegramJob(kind, payload)
        queued_ahead = self.queue.qsize()

        def _enqueue():
            # Runs on the loop's own thread (scheduled below) since
            # touching asyncio.Queue internals from another thread isn't
            # safe. put_nowait() instead of put() so a full queue fails
            # fast instead of blocking — that's what keeps a burst of
            # requests from piling up in memory forever.
            try:
                self.queue.put_nowait(job)
            except asyncio.QueueFull:
                if not job.future.done():
                    job.future.set_exception(QueueFullError(
                        f"server busy — {self.queue.maxsize} job(s) already queued; try again shortly"
                    ))

        self.loop.call_soon_threadsafe(_enqueue)
        try:
            result = job.future.result(timeout=timeout)
        except FutureTimeoutError:
            raise TimeoutError(
                f"'{kind}' timed out after {timeout}s (there were {queued_ahead} job(s) ahead of it)"
            )
        return result, queued_ahead

    def stop(self):
        if self.client is not None:
            try:
                self.run_coro(self.client.disconnect(), timeout=10)
            except Exception:
                pass
        self.loop.call_soon_threadsafe(self.loop.stop)


tg = TelegramLoop()

# Rolling buffer of mail captured live by the background handler, so /poll
# never has to touch Telegram. Each entry: {received_at, to, text}.
RECENT_MAILS = deque(maxlen=2000)
_buffer_lock = threading.Lock()
_live_buffer = []


async def _attach_live_monitor(client: TelegramClient, flush_delay: float = 3.0):
    """
    Same fragment-grouping idea as monitor()/mailbox_inbox() in the CLI
    tool, but permanent: registered once at startup and left running for
    as long as the process is alive, feeding RECENT_MAILS instead of the
    console.
    """
    flush_task_holder = {"task": None}

    async def _flush():
        global _live_buffer
        with _buffer_lock:
            if not _live_buffer:
                return
            mail_text = "\n".join(_live_buffer)
            _live_buffer = []
        to_addr = _extract_to_address(mail_text)
        entry = {
            "received_at": datetime.now(timezone.utc).isoformat(),
            "to": to_addr,
            "text": mail_text,
        }
        RECENT_MAILS.append(entry)
        try:
            _save_mail(mail_text)
        except OSError:
            log.exception("Failed to persist captured mail to disk")
        log.info(f"Captured live mail for {to_addr or 'unknown'}")

    async def _schedule_flush():
        if flush_task_holder["task"] and not flush_task_holder["task"].done():
            flush_task_holder["task"].cancel()

        async def _delayed():
            await asyncio.sleep(flush_delay)
            await _flush()

        flush_task_holder["task"] = asyncio.create_task(_delayed())

    async def handler(event):
        global _live_buffer
        text = event.message.message
        if text is None:
            return
        if is_mail_header(text):
            await _flush()
            with _buffer_lock:
                _live_buffer = [text]
        else:
            with _buffer_lock:
                if _live_buffer:
                    _live_buffer.append(text)
        await _schedule_flush()

    client.add_event_handler(handler, events.NewMessage(from_users=BOT_USERNAME))


# ---------------------------------------------------------------------------
# Telegram-touching operations the API routes call via tg.run_coro(...)
# ---------------------------------------------------------------------------
async def api_generate_email(username: str, domain_choice: str):
    domain = DOMAIN_MAP.get(str(domain_choice))
    if not domain:
        raise ValueError("domain must be '1' (@hi2.in), '2' (@telegmail.com), 'hi2.in', or 'telegmail.com'")
    if not username or not re.fullmatch(r"[a-zA-Z0-9._-]+", username):
        raise ValueError("username must be non-empty and contain only letters, numbers, '.', '_' or '-'")

    email = f"{username}{domain}"
    await tg.client.send_message(BOT_USERNAME, email)
    response = await _safe_wait_for_bot_reply(tg.client)
    if response is None:
        raise TimeoutError("no reply from @fakemailbot — it may be slow or down right now")
    log.info(f"[api] generated {email}")
    return {"email": email, "bot_reply": response.text}


async def api_list_mailboxes():
    await tg.client.send_message(BOT_USERNAME, "/id")
    response = await _safe_wait_for_bot_reply(tg.client)
    if response is None:
        raise TimeoutError("no reply from @fakemailbot — it may be slow or down right now")
    text = response.text or ""
    if LIST_HEADER_HINT not in text.lower():
        raise ValueError("unexpected response from @fakemailbot")
    emails = []
    for line in text.splitlines():
        m = re.search(r"([a-zA-Z0-9._%+-]+@(?:hi2\.in|telegmail\.com))", line)
        if m:
            emails.append(m.group(1))
    return emails


async def api_inbox(days: int = 7, address: str = None):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    raw = []
    async for msg in tg.client.iter_messages(BOT_USERNAME, limit=5000):
        if msg.date < cutoff:
            break
        raw.append(msg)
    raw.reverse()
    mails = group_into_mails(raw)
    results = [{"to": _extract_to_address(m), "text": m} for m in mails]
    if address:
        results = [m for m in results if m["to"] == address.lower()]
    return results


# ---------------------------------------------------------------------------
# Request-queue worker — the ONLY coroutine that ever talks to
# @fakemailbot. Everything else (Flask routes) goes through tg.submit(),
# which pushes a TelegramJob here and waits for it to come back.
# ---------------------------------------------------------------------------
async def _make_queue(maxsize: int = QUEUE_MAXSIZE) -> "asyncio.Queue":
    # Bounded so a burst of requests can't pile up in memory without
    # limit — once full, submit() rejects new jobs immediately with
    # QueueFullError (-> HTTP 503) instead of queueing forever.
    return asyncio.Queue(maxsize=maxsize)


async def _dispatch_job(job: TelegramJob):
    if job.kind == "generate":
        return await api_generate_email(job.payload.get("username", ""), job.payload.get("domain", "1"))
    if job.kind == "mailboxes":
        return await api_list_mailboxes()
    if job.kind == "inbox":
        return await api_inbox(days=job.payload.get("days", 7))
    if job.kind == "mailbox":
        return await api_inbox(days=job.payload.get("days", 30), address=job.payload.get("address"))
    raise ValueError(f"unknown job kind: {job.kind!r}")


async def telegram_worker(queue: "asyncio.Queue"):
    """Single consumer for the whole queue. Jobs are handled strictly one
    at a time, in submission order — that's what keeps two concurrent
    /generate calls from racing each other in the same bot chat."""
    log.info("Telegram request-queue worker started.")
    while True:
        job = await queue.get()
        tg.current_job = job.kind
        tg.current_job_started_at = time.monotonic()
        log.info(f"[queue] job {job.id} ('{job.kind}') started")
        try:
            result = await _dispatch_job(job)
            # Guard against double-resolving: a job can already have had
            # its future rejected upstream (e.g. QueueFullError) before
            # the worker ever sees it.
            if not job.future.done():
                job.future.set_result(result)
        except Exception as e:  # noqa: BLE001 — deliberately broad: surface it to the waiting request
            if not job.future.done():
                job.future.set_exception(e)
            log.exception(f"[queue] job {job.id} ('{job.kind}') failed")
        finally:
            elapsed = time.monotonic() - job.submitted_at
            log.info(f"[queue] job {job.id} ('{job.kind}') done in {elapsed:.2f}s")
            tg.current_job = None
            tg.current_job_started_at = None
            queue.task_done()


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)


def require_api_key(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        cfg = load_config()
        key = cfg.get("api_key")
        if not key:
            return jsonify({"error": "server has no api_key configured — restart `serve` to generate one"}), 500
        supplied = request.headers.get("X-API-Key")
        if not supplied or supplied != key:
            return jsonify({"error": "unauthorized — missing or invalid X-API-Key header"}), 401
        return fn(*a, **kw)

    return wrapper


@app.errorhandler(Exception)
def _handle_any_error(e):

    if isinstance(e, HTTPException):
        return jsonify({
            "error": e.name,
            "detail": e.description
        }), e.code

    if isinstance(e, TimeoutError):
        return jsonify({"error": str(e)}), 504

    if isinstance(e, QueueFullError):
        return jsonify({"error": str(e)}), 503

    if isinstance(e, ValueError):
        return jsonify({"error": str(e)}), 400

    log.exception("Unhandled error in API route")

    return jsonify({
        "error": "internal error",
        "detail": str(e)
    }), 500

@app.get("/health")
def health():
    connected = tg.client is not None and tg.client.is_connected()
    worker_alive = tg.worker_future is not None and not tg.worker_future.done()
    status = "ok" if (connected and worker_alive) else "degraded"
    payload = {"status": status, "telegram_connected": connected, "worker_alive": worker_alive}
    if tg.worker_future is not None and tg.worker_future.done():
        worker_exc = tg.worker_future.exception()
        if worker_exc is not None:
            payload["worker_error"] = str(worker_exc)
    return jsonify(payload)


@app.get("/queue")
@require_api_key
def route_queue():
    pending = tg.queue.qsize() if tg.queue is not None else 0
    worker_alive = tg.worker_future is not None and not tg.worker_future.done()
    current_wait_seconds = (
        round(time.monotonic() - tg.current_job_started_at, 2)
        if tg.current_job_started_at is not None
        else None
    )
    return jsonify({
        "pending": pending,
        "processing": tg.current_job,
        "current_wait_seconds": current_wait_seconds,
        "worker_alive": worker_alive,
        "queue_maxsize": tg.queue.maxsize if tg.queue is not None else None,
    })


@app.post("/generate")
@require_api_key
def route_generate():
    body = request.get_json(silent=True) or {}
    payload = {"username": body.get("username", ""), "domain": body.get("domain", "1")}
    result, queued_ahead = tg.submit("generate", payload, timeout=45)
    result["queued_ahead"] = queued_ahead
    return jsonify(result)


@app.get("/mailboxes")
@require_api_key
def route_mailboxes():
    emails, queued_ahead = tg.submit("mailboxes", {}, timeout=45)
    return jsonify({"mailboxes": emails, "queued_ahead": queued_ahead})


@app.get("/inbox")
@require_api_key
def route_inbox():
    days = request.args.get("days", default=7, type=int)
    mails, queued_ahead = tg.submit("inbox", {"days": days}, timeout=90)
    return jsonify({"days": days, "count": len(mails), "mails": mails, "queued_ahead": queued_ahead})


@app.get("/mailbox")
@require_api_key
def route_mailbox():
    address = request.args.get("address")
    if not address:
        return jsonify({"error": "?address=... is required"}), 400
    days = request.args.get("days", default=30, type=int)
    mails, queued_ahead = tg.submit("mailbox", {"days": days, "address": address}, timeout=90)
    return jsonify({"address": address, "days": days, "count": len(mails), "mails": mails, "queued_ahead": queued_ahead})


@app.get("/poll")
@require_api_key
def route_poll():
    since_raw = request.args.get("since")
    address = request.args.get("address")
    since = None
    if since_raw:
        try:
            since = datetime.fromisoformat(since_raw)
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
        except ValueError:
            return jsonify({"error": "since must be an ISO8601 timestamp"}), 400

    snapshot = list(RECENT_MAILS)
    if since:
        snapshot = [m for m in snapshot if datetime.fromisoformat(m["received_at"]) > since]
    if address:
        snapshot = [m for m in snapshot if m["to"] == address.lower()]
    return jsonify({"count": len(snapshot), "mails": snapshot})


# ---------------------------------------------------------------------------
# bootstrap + entry point
# ---------------------------------------------------------------------------
def bootstrap(queue_maxsize: int = QUEUE_MAXSIZE):
    """Starts the background event loop, logs in once (OTP prompt only if
    no valid session exists yet), attaches the permanent live monitor, and
    starts the single request-queue worker that serializes every other
    call into @fakemailbot."""
    ensure_dirs()
    tg.start()

    client = tg.run_coro(login(), timeout=300)
    if client is None:
        console.print("[bold red]Login failed — fix configuration and re-run `serve`.[/bold red]")
        sys.exit(1)
    tg.client = client

    tg.run_coro(_attach_live_monitor(client), timeout=10)

    tg.queue = tg.run_coro(_make_queue(maxsize=queue_maxsize), timeout=5)
    tg.worker_future = asyncio.run_coroutine_threadsafe(telegram_worker(tg.queue), tg.loop)

    console.print(
        f"[bold green][✓][/bold green] Live monitor attached and request-queue worker running "
        f"(queue capacity: {queue_maxsize})."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="MailGen_host.py",
        description="Persistent FakeMail bot host with a local HTTP API for your own tools to call.",
    )
    parser.add_argument("--version", action="version", version=f"MailGen_host {__version__}")
    parser.add_argument("-p", "--phone", metavar="PHONE", help="Save phone number and exit.")
    parser.add_argument("-api", nargs=2, metavar=("API_ID", "API_HASH"), help="Save API credentials and exit.")
    parser.add_argument(
        "-proxy", nargs=4, metavar=("HOST", "PORT", "USER", "PASS"), help="Save SOCKS5 proxy settings and exit."
    )
    parser.add_argument("--show-config", action="store_true", help="Print current configuration (secrets masked).")
    parser.add_argument(
        "--rotate-api-key", action="store_true", help="Generate a new API key (invalidates the old one) and exit."
    )

    sub = parser.add_subparsers(dest="command")
    serve_p = sub.add_parser("serve", help="Start the host service (default).")
    serve_p.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1, localhost-only).")
    serve_p.add_argument("--port", type=int, default=8765)
    serve_p.add_argument(
        "--queue-maxsize",
        type=int,
        default=QUEUE_MAXSIZE,
        help=f"Max jobs allowed to queue before /generate etc. return 503 'server busy' (default: {QUEUE_MAXSIZE}).",
    )

    return parser


def main():
    setup_logging()
    parser = build_parser()
    args = parser.parse_args()

    if args.phone:
        cmd_set_phone(args.phone)
    if args.api:
        cmd_set_api(*args.api)
    if args.proxy:
        cmd_set_proxy(*args.proxy)

    if args.rotate_api_key:
        cfg = load_config()
        cfg["api_key"] = secrets.token_urlsafe(32)
        save_config(cfg)
        console.print(Panel.fit(f"New API key:\n[bold]{cfg['api_key']}[/bold]", border_style="yellow"))
        return

    if args.show_config:
        show_config()

    command = args.command or "serve"
    if command != "serve":
        return

    cfg = load_config()
    if not _require_config(cfg):
        sys.exit(1)

    if not cfg.get("api_key"):
        cfg["api_key"] = secrets.token_urlsafe(32)
        save_config(cfg)
        console.print(
            Panel.fit(
                f"Generated API key (copy this into main.py — it won't be printed in full again):\n\n"
                f"[bold]{cfg['api_key']}[/bold]",
                title="[!] First-run API key",
                border_style="yellow",
            )
        )
    else:
        console.print(f"[dim]Using existing API key ({cfg['api_key'][:6]}...) — run with --show-config to see it masked, or --rotate-api-key to replace it.[/dim]")

    _print_banner()

    try:
        bootstrap(queue_maxsize=args.queue_maxsize)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted during login — exiting.[/yellow]")
        sys.exit(0)

    atexit.register(tg.stop)
    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 8765)
    console.print(
        Panel.fit(
            f"Listening on http://{host}:{port}\n"
            f"All routes except /health require header:  X-API-Key: <your key>\n"
            f"Queue capacity: {args.queue_maxsize} job(s) (503 'server busy' beyond that)\n"
            f"Mail dir: {MAILS_DIR}\nLog file: {LOG_FILE}",
            title="MailGen_host running",
            border_style="cyan",
        )
    )

    try:
        app.run(host=host, port=port, threaded=True)
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutting down...[/yellow]")
    finally:
        tg.stop()


if __name__ == "__main__":
    main()