#!/usr/bin/env python3
"""
mailgen — FakeMail Telegram Bot Automation (Kali/Linux CLI edition)
=====================================================================
Wraps interactions with @fakemailbot behind a proper CLI:

    mailgen setup                  interactive first-time configuration
    mailgen login                  (re)authenticate with Telegram
    mailgen generate                request a new fake address
    mailgen inbox   [--days N]      reconstruct + save the last N days of mail
    mailgen monitor [--minutes N]   live-watch the chat for new mail
    mailgen mailbox                 pick one mailbox, see history, live-monitor it
    mailgen export                  copy saved mail into exports/<mailbox>/
    mailgen doctor                  run diagnostics (config, session, connectivity)
    mailgen                         open the interactive menu (default)

Quick config (no subcommand needed):

    mailgen -p +911234567890
    mailgen -api 9844616 abcdef123456
    mailgen -proxy 31.59.20.176 6754 user pass
    mailgen --show-config

Everything lives under ~/.config/mailgen/:

    config.json   credentials + proxy (chmod 600)
    session/      Telethon session file
    mails/        every reconstructed mail, one file each
    logs/         mailgen.log
    exports/      output of `mailgen export`, grouped by mailbox
"""

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError

from . import __version__

console = Console()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path.home() / ".config" / "mailgen"
CONFIG_PATH = BASE_DIR / "config.json"
SESSION_DIR = BASE_DIR / "session"
SESSION_NAME = str(SESSION_DIR / "fakemail")  # telethon appends .session
MAILS_DIR = BASE_DIR / "mails"
LOGS_DIR = BASE_DIR / "logs"
EXPORTS_DIR = BASE_DIR / "exports"
LIVE_LOG = MAILS_DIR / "live_inbox.txt"
LOG_FILE = LOGS_DIR / "mailgen.log"

BOT_USERNAME = "fakemailbot"
SEP_RUN = "➖" * 5  # 5+ repeated dashes marks the start of a new mail
LIST_HEADER_HINT = "here are the list of fake mail ids you have"
NEW_MAIL_HINT = "your new fake mail id is"

BANNER = r"""
███╗   ███╗ █████╗ ██╗██╗      ██████╗ ███████╗███╗   ██╗
████╗ ████║██╔══██╗██║██║     ██╔════╝ ██╔════╝████╗  ██║
██╔████╔██║███████║██║██║     ██║  ███╗█████╗  ██╔██╗ ██║
██║╚██╔╝██║██╔══██║██║██║     ██║   ██║██╔══╝  ██║╚██╗██║
██║ ╚═╝ ██║██║  ██║██║███████╗╚██████╔╝███████╗██║ ╚████║
╚═╝     ╚═╝╚═╝  ╚═╝╚═╝╚══════╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝
"""

log = logging.getLogger("mailgen")


# ---------------------------------------------------------------------------
# directories / logging
# ---------------------------------------------------------------------------
def ensure_dirs():
    for d in (BASE_DIR, SESSION_DIR, MAILS_DIR, LOGS_DIR, EXPORTS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def setup_logging():
    ensure_dirs()
    logging.basicConfig(
        filename=str(LOG_FILE),
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


# ---------------------------------------------------------------------------
# friendly error helper
# ---------------------------------------------------------------------------
def _print_connection_help():
    console.print("[yellow]Possible causes:[/yellow]")
    console.print("  • Proxy is offline or misconfigured")
    console.print("  • Telegram is blocked on this network")
    console.print("  • Invalid API credentials")
    console.print("  • No internet connection")
    console.print("\nRun [bold]mailgen doctor[/bold] for a full diagnostic check.")


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "phone": None,
    "api_id": None,
    "api_hash": None,
    "proxy": None,  # {"host": ..., "port": ..., "user": ..., "pass": ...}
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
        os.chmod(CONFIG_PATH, 0o600)  # contains secrets — owner read/write only
    except OSError:
        pass


def _require_config(cfg: dict) -> bool:
    missing = []
    if not cfg.get("phone"):
        missing.append("phone")
    if not cfg.get("api_id"):
        missing.append("api_id")
    if not cfg.get("api_hash"):
        missing.append("api_hash")
    if missing:
        console.print(f"[bold red][!] Missing configuration:[/bold red] {', '.join(missing)}")
        console.print("Run [bold]mailgen setup[/bold] or use -p / -api to configure mailgen.")
        return False
    return True


def _build_proxy(cfg: dict):
    proxy = cfg.get("proxy")
    if not proxy:
        return None
    return (
        "socks5",
        proxy["host"],
        int(proxy["port"]),
        True,
        proxy.get("user"),
        proxy.get("pass"),
    )


# ---------------------------------------------------------------------------
# config masking / display
# ---------------------------------------------------------------------------
def _mask_phone(phone):
    if not phone:
        return "[dim](not set)[/dim]"
    prefix = phone[:3]
    return f"{prefix}{'*' * 7}"


def _mask_secret(secret, keep=4):
    if not secret:
        return "[dim](not set)[/dim]"
    secret = str(secret)
    if len(secret) <= keep:
        return "*" * max(len(secret), 4)
    return secret[:keep] + "*" * max(len(secret) - keep, 4)


def show_config():
    cfg = load_config()
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("Phone", _mask_phone(cfg.get("phone")))
    table.add_row("API ID", str(cfg.get("api_id")) if cfg.get("api_id") else "[dim](not set)[/dim]")
    table.add_row("API Hash", _mask_secret(cfg.get("api_hash")))

    proxy = cfg.get("proxy")
    if proxy:
        table.add_row("Proxy", f"{proxy.get('host')}:{proxy.get('port')}")
        table.add_row("Proxy Auth", f"{_mask_secret(proxy.get('user'), keep=2)} / {_mask_secret(proxy.get('pass'), keep=0)}")
    else:
        table.add_row("Proxy", "[dim](not set)[/dim]")

    table.add_row("Config file", str(CONFIG_PATH))
    table.add_row("Session dir", str(SESSION_DIR))
    console.print(Panel(table, title="mailgen config", border_style="cyan"))


def cmd_set_phone(phone: str):
    cfg = load_config()
    cfg["phone"] = phone
    save_config(cfg)
    console.print("[bold green][✓] Phone saved.[/bold green]")
    log.info("Phone number updated.")


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
    log.info("API credentials updated.")


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
    log.info("Proxy settings updated.")


def cmd_setup():
    console.print(BANNER, style="bold green")
    console.print(Panel.fit("Initial setup wizard", border_style="cyan"))
    cfg = load_config()

    phone_in = input(f"Phone (with country code) [{cfg.get('phone') or ''}]: ").strip()
    phone = phone_in or cfg.get("phone")

    api_id_in = input(f"API ID [{cfg.get('api_id') or ''}]: ").strip()
    api_hash_in = input(f"API Hash [{'set' if cfg.get('api_hash') else ''}]: ").strip()

    cfg["phone"] = phone
    if api_id_in:
        try:
            cfg["api_id"] = int(api_id_in)
        except ValueError:
            console.print("[bold red][!] API ID must be numeric — keeping previous value.[/bold red]")
    if api_hash_in:
        cfg["api_hash"] = api_hash_in

    if input("Configure a proxy? (y/N): ").strip().lower() == "y":
        host = input("Proxy host: ").strip()
        port = input("Proxy port: ").strip()
        user = input("Proxy username: ").strip()
        password = input("Proxy password: ").strip()
        try:
            cfg["proxy"] = {"host": host, "port": int(port), "user": user, "pass": password}
        except ValueError:
            console.print("[bold red][!] Proxy port must be numeric — proxy not saved.[/bold red]")

    save_config(cfg)
    console.print(f"[bold green][✓] Configuration saved to[/bold green] {CONFIG_PATH}")
    log.info("Setup wizard completed.")

    if input("Log in to Telegram now? (Y/n): ").strip().lower() != "n":
        asyncio.run(_login_and_disconnect())


async def _login_and_disconnect():
    client = await login()
    if client:
        await client.disconnect()


# ---------------------------------------------------------------------------
# small helper: wait for exactly one reply from the bot
# ---------------------------------------------------------------------------
async def _wait_for_bot_reply(client: TelegramClient, timeout: float = 20):
    """
    Waits for the next message from BOT_USERNAME and returns it.
    Implemented with a temporary event handler + future rather than relying
    on an undocumented client.wait_for(), so it works across Telethon versions.
    """
    future = asyncio.get_event_loop().create_future()

    async def handler(event):
        if not future.done():
            future.set_result(event.message)

    client.add_event_handler(handler, events.NewMessage(from_users=BOT_USERNAME))
    try:
        return await asyncio.wait_for(future, timeout=timeout)
    finally:
        client.remove_event_handler(handler, events.NewMessage(from_users=BOT_USERNAME))


async def _safe_wait_for_bot_reply(client: TelegramClient, timeout: float = 20):
    """
    Same as _wait_for_bot_reply, but swallows the timeout and returns None
    instead of letting asyncio.TimeoutError crash the caller.
    """
    try:
        return await _wait_for_bot_reply(client, timeout=timeout)
    except asyncio.TimeoutError:
        return None


# ---------------------------------------------------------------------------
# login()
# ---------------------------------------------------------------------------
async def login():
    """
    Run before any Telegram-dependent flow. Reuses the saved session if
    it's still valid. If it's missing or expired, walks through Telegram's
    OTP login and saves a fresh session for next time. Returns None (and
    prints what's missing, or a friendly connection error) if something
    stops it from completing.
    """
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
        password = input("Two-step verification is on. Enter your password: ").strip()
        await client.sign_in(password=password)

    console.print("[bold green][✓][/bold green] Login successful — session saved for next run.")
    log.info("Logged in successfully.")
    return client


# ---------------------------------------------------------------------------
# generate_email()
# ---------------------------------------------------------------------------
async def generate_email(client: TelegramClient):
    console.print("\n[bold]=== Email Generator ===[/bold]")
    username = input("Enter email name: ").strip()

    print("\nChoose domain:")
    print("1. @hi2.in")
    print("2. @telegmail.com")
    choice = input("\nSelect (1/2): ").strip()

    domain = {"1": "@hi2.in", "2": "@telegmail.com"}.get(choice)
    if not domain:
        console.print("[red]Invalid choice.[/red]")
        return None

    email = f"{username}{domain}"
    console.print(f"\nGenerated Email: [bold cyan]{email}[/bold cyan]")

    console.print("[cyan]Sending request to FakeMail Bot...[/cyan]")
    await client.send_message(BOT_USERNAME, email)

    with console.status("[cyan]Waiting for bot reply...[/cyan]"):
        response = await _safe_wait_for_bot_reply(client)

    if response is None:
        console.print("[red][generate] No reply from the bot — it may be slow or down. Try again.[/red]")
        return None

    print(response.text)
    print(email)
    log.info(f"Generated email {email}")
    return email


# ---------------------------------------------------------------------------
# list_emails()
# ---------------------------------------------------------------------------
async def list_emails(client: TelegramClient, retries: int = 2):
    """
    Sends /id and prints the bot's reply, validated by checking for the
    expected header line. Retries once if the reply doesn't look right
    (including if the bot simply doesn't reply in time).
    """
    for attempt in range(1, retries + 1):
        await client.send_message(BOT_USERNAME, "/id")
        with console.status("[cyan]Waiting for bot reply...[/cyan]"):
            response = await _safe_wait_for_bot_reply(client)

        if response is None:
            console.print(f"[yellow][list] Attempt {attempt}: no reply from bot within timeout, retrying...[/yellow]")
            continue

        text = response.text or ""

        if LIST_HEADER_HINT in text.lower():
            console.print("\n[bold]=== Your Fake Emails ===[/bold]")
            print(text)
            return text

        console.print(f"[yellow][list] Attempt {attempt}: response didn't match the expected header, retrying...[/yellow]")

    console.print("[red][list] Error: could not get a valid email list from the bot.[/red]")
    return None


# ---------------------------------------------------------------------------
# shared mail-parsing logic
# ---------------------------------------------------------------------------
def is_mail_header(text: str) -> bool:
    """True if a message's text starts a new mail (the ➖ separator line)."""
    return bool(text) and text.strip().startswith(SEP_RUN)


def group_into_mails(messages):
    """
    Takes a chronologically ordered (oldest -> newest) list of Telethon
    Message objects from the bot chat and reconstructs full mail bodies.
    A message starting with the ➖ separator opens a new mail; any
    following message that is NOT itself a new header is a continuation
    fragment of that same mail (the bot splits long mails across several
    Telegram messages). Pure-media messages (e.g. the .html attachment
    some mails include) have no text and are skipped without breaking
    the current mail's continuation chain. Bot chrome (command echoes,
    the /id mailbox list, "new mail id" confirmations) is filtered out
    before grouping so it never gets mistaken for mail content.
    """
    mails = []
    current = []

    for msg in messages:
        text = msg.message  # raw text; None for media-only messages

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
        # else: stray text before any header seen yet — not a mail, ignore

    if current:
        mails.append("\n".join(current))

    return mails


def _save_mail(mail_text: str) -> Path:
    ensure_dirs()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

    subject_match = re.search(r"Subject:\s*(.+)", mail_text)
    subject = subject_match.group(1)[:40] if subject_match else "mail"
    subject = re.sub(r"[^a-zA-Z0-9_-]+", "_", subject).strip("_") or "mail"

    path = MAILS_DIR / f"{ts}_{subject}.txt"
    path.write_text(mail_text, encoding="utf-8")
    return path


def _display_and_save(mail_text: str, header: str = "Mail") -> Path:
    print(f"--- {header} ---")
    print(mail_text)
    print()
    path = _save_mail(mail_text)
    with open(LIVE_LOG, "a", encoding="utf-8") as f:
        f.write(mail_text + "\n\n" + ("=" * 40) + "\n\n")
    return path


def _extract_to_address(mail_text: str):
    """Pulls the To: address out of a reconstructed mail, if present."""
    match = re.search(r"To:\s*<([^>]+)>", mail_text, re.IGNORECASE)
    return match.group(1).lower() if match else None


# ---------------------------------------------------------------------------
# inbox()
# ---------------------------------------------------------------------------
async def inbox(client: TelegramClient, days: int = 7):
    """
    Pulls the last `days` days of mail from the bot chat, reconstructs
    each full mail, prints it, and saves each one to its own file under
    mails/ for later browsing.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    raw = []
    with console.status("[cyan]Scanning message history...[/cyan]"):
        # iter_messages() without reverse= returns newest-first, so we can
        # stop as soon as we pass the cutoff instead of walking the whole history.
        async for msg in client.iter_messages(BOT_USERNAME, limit=5000):
            if msg.date < cutoff:
                break
            raw.append(msg)
    raw.reverse()  # chronological order, oldest -> newest

    mails = group_into_mails(raw)

    if not mails:
        console.print(f"[yellow][inbox] No mail messages found in the last {days} day(s).[/yellow]")
        return []

    ensure_dirs()
    console.print(f"[green][✓][/green] Found {len(mails)} mail(s) in the last {days} day(s)")
    console.print(f"\n[bold]=== Inbox: {len(mails)} mail(s) from the last {days} day(s) ===[/bold]\n")

    paths = []
    for i, mail in enumerate(mails, 1):
        paths.append(_display_and_save(mail, header=f"Mail {i}"))

    console.print(f"[green][inbox] Saved {len(mails)} mail(s) to '{MAILS_DIR}'[/green]")
    log.info(f"Inbox: saved {len(mails)} mail(s).")
    return paths


# ---------------------------------------------------------------------------
# monitor()
# ---------------------------------------------------------------------------
async def monitor(client: TelegramClient, lookback_minutes: int = 10, flush_delay: float = 3.0):
    """
    Shows mail from the last `lookback_minutes`, then stays connected and
    live-prints/saves new mail as it arrives.

    There's no explicit "end of mail" marker from the bot — only the next
    ➖ header tells you the previous mail is done. For messages that have
    already arrived (inbox/initial load) that's enough. For live traffic,
    `flush_delay` is used instead: once `flush_delay` seconds pass without
    a new fragment, whatever is buffered is treated as a complete mail.

    The handler is registered/removed explicitly (rather than via the
    @client.on decorator) so re-entering this function never stacks a
    second copy of the handler on top of the first.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)

    raw = []
    with console.status("[cyan]Scanning message history...[/cyan]"):
        async for msg in client.iter_messages(BOT_USERNAME, limit=500):
            if msg.date < cutoff:
                break
            raw.append(msg)
    raw.reverse()

    mails = group_into_mails(raw)
    ensure_dirs()

    console.print(f"[green][✓][/green] Found {len(mails)} mail(s) from the last {lookback_minutes} minute(s)")
    console.print(f"\n[bold]=== Monitor: {len(mails)} mail(s) from the last {lookback_minutes} minute(s) ===[/bold]\n")
    for mail in mails:
        _display_and_save(mail, header="Recent Mail")

    console.print()
    console.print(
        Panel.fit(
            "[bold green]Live Monitor Active[/bold green]\nPress [bold]Ctrl+C[/bold] to stop",
            border_style="green",
        )
    )

    buffer = []
    flush_task = None

    async def flush_buffer():
        nonlocal buffer
        if buffer:
            mail = "\n".join(buffer)
            buffer = []
            _display_and_save(mail, header="New Mail")

    async def schedule_flush():
        nonlocal flush_task
        if flush_task and not flush_task.done():
            flush_task.cancel()

        async def _delayed():
            await asyncio.sleep(flush_delay)
            await flush_buffer()

        flush_task = asyncio.create_task(_delayed())

    async def handler(event):
        nonlocal buffer
        text = event.message.message
        if text is None:
            return  # skip pure-media fragments (e.g. the .html attachment)

        if is_mail_header(text):
            await flush_buffer()  # previous mail is done — flush it now
            buffer = [text]
        elif buffer:
            buffer.append(text)
        # else: stray text with no header seen yet — ignore

        await schedule_flush()

    client.add_event_handler(handler, events.NewMessage(from_users=BOT_USERNAME))
    try:
        await client.run_until_disconnected()
    except asyncio.CancelledError:
        pass
    finally:
        if flush_task and not flush_task.done():
            flush_task.cancel()
        client.remove_event_handler(handler, events.NewMessage(from_users=BOT_USERNAME))


# ---------------------------------------------------------------------------
# mailbox_mode() / mailbox_inbox()
# ---------------------------------------------------------------------------
async def mailbox_mode(client: TelegramClient):
    """
    Show all mailboxes, let user select one, load last 30 days mails for
    that mailbox, then start live monitoring only for that mailbox.
    """
    await client.send_message(BOT_USERNAME, "/id")
    with console.status("[cyan]Waiting for bot reply...[/cyan]"):
        response = await _safe_wait_for_bot_reply(client)

    if response is None:
        console.print("[red][mailbox] No reply from the bot — it may be slow or down. Try again.[/red]")
        return

    text = response.text or ""

    if LIST_HEADER_HINT not in text.lower():
        console.print("[red][mailbox] Invalid response.[/red]")
        return

    emails = []
    for line in text.splitlines():
        match = re.search(r"([a-zA-Z0-9._%+-]+@(?:hi2\.in|telegmail\.com))", line)
        if match:
            emails.append(match.group(1))

    if not emails:
        console.print("[yellow]No mailboxes found.[/yellow]")
        return

    console.print("\n[bold]=== Mailboxes ===[/bold]\n")
    for i, email in enumerate(emails, 1):
        print(f"{i}. {email}")

    try:
        choice = int(input("\nSelect mailbox: "))
        selected_email = emails[choice - 1]
    except (ValueError, IndexError):
        console.print("[red]Invalid selection.[/red]")
        return

    console.print(f"\nSelected: [bold cyan]{selected_email}[/bold cyan]")

    await mailbox_inbox(client, selected_email)


async def mailbox_inbox(
    client: TelegramClient,
    selected_email: str,
    days: int = 30,
    flush_delay: float = 3.0,
):
    """
    Shows the last `days` days of mail addressed to `selected_email`, then
    live-monitors the bot chat, reconstructing each mail (which may arrive
    across several Telegram messages — same as monitor()) and only
    printing/saving the ones addressed to `selected_email`.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    raw = []
    with console.status("[cyan]Scanning message history...[/cyan]"):
        async for msg in client.iter_messages(BOT_USERNAME, limit=5000):
            if msg.date < cutoff:
                break
            raw.append(msg)
    raw.reverse()

    mails = group_into_mails(raw)

    matched = [m for m in mails if _extract_to_address(m) == selected_email.lower()]

    console.print(f"[green][✓][/green] Found {len(matched)} mail(s) for {selected_email}")
    console.print(f"\n[bold]=== {len(matched)} mails found for {selected_email} ===[/bold]\n")

    ensure_dirs()
    for mail in matched:
        _display_and_save(mail, header=selected_email)

    console.print()
    console.print(
        Panel.fit(
            f"[bold green]Live Monitor Active[/bold green]\n"
            f"Watching for mail to [cyan]{selected_email}[/cyan]\n"
            f"Press [bold]Ctrl+C[/bold] to stop",
            border_style="green",
        )
    )

    buffer = []
    flush_task = None

    async def flush_buffer():
        nonlocal buffer
        if buffer:
            mail = "\n".join(buffer)
            buffer = []
            if _extract_to_address(mail) == selected_email.lower():
                console.print("\n[bold green]=== NEW MAIL ===[/bold green]\n")
                print(mail)
                _display_and_save(mail, header="NEW MAIL")

    async def schedule_flush():
        nonlocal flush_task
        if flush_task and not flush_task.done():
            flush_task.cancel()

        async def _delayed():
            await asyncio.sleep(flush_delay)
            await flush_buffer()

        flush_task = asyncio.create_task(_delayed())

    async def monitor_selected(event):
        nonlocal buffer
        text = event.message.message
        if text is None:
            return

        if is_mail_header(text):
            await flush_buffer()  # previous mail is done — flush it now
            buffer = [text]
        elif buffer:
            buffer.append(text)

        await schedule_flush()

    client.add_event_handler(monitor_selected, events.NewMessage(from_users=BOT_USERNAME))
    try:
        await client.run_until_disconnected()
    except asyncio.CancelledError:
        pass
    finally:
        if flush_task and not flush_task.done():
            flush_task.cancel()
        client.remove_event_handler(monitor_selected, events.NewMessage(from_users=BOT_USERNAME))


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------
def cmd_export():
    """Copies every saved mail in mails/ into exports/<mailbox>/, grouped
    by the address it was addressed to (mails with no parseable To: go
    into exports/unknown/)."""
    ensure_dirs()
    files = sorted(p for p in MAILS_DIR.glob("*.txt") if p.name != "live_inbox.txt")

    if not files:
        console.print("[yellow][export] No saved mail files found in mails/.[/yellow]")
        return

    counts = {}
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        to_addr = _extract_to_address(text) or "unknown"
        safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", to_addr)
        dest_dir = EXPORTS_DIR / safe_name
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest_dir / f.name)
        counts[to_addr] = counts.get(to_addr, 0) + 1

    summary = "\n".join(f"{addr}: {n} mail(s)" for addr, n in sorted(counts.items()))
    console.print(
        Panel.fit(
            summary,
            title=f"[✓] Exported {len(files)} mail(s) to {EXPORTS_DIR}",
            border_style="green",
        )
    )
    log.info(f"Exported {len(files)} mail(s) into {len(counts)} mailbox folder(s).")


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
async def doctor(client: TelegramClient = None):
    """
    Diagnostic check: config file, credentials, saved session, proxy, and
    (if credentials are set) live connectivity to Telegram. If `client` is
    already connected (e.g. called from the interactive menu), it's reused
    for the connectivity check instead of opening a second connection on
    the same session file.
    """
    console.print(Panel.fit("mailgen doctor — system diagnostics", border_style="cyan"))
    cfg = load_config()

    def _status(ok, label, detail):
        if ok is True:
            console.print(f"[green][✓][/green] {label}: {detail}")
        elif ok is False:
            console.print(f"[red][✗][/red] {label}: {detail}")
        else:
            console.print(f"[dim][·][/dim] {label}: {detail}")

    _status(
        CONFIG_PATH.exists(),
        "Config file",
        str(CONFIG_PATH) if CONFIG_PATH.exists() else "not found — run `mailgen setup`",
    )
    _status(bool(cfg.get("phone")), "Phone number", _mask_phone(cfg.get("phone")))
    _status(bool(cfg.get("api_id")), "API ID", cfg.get("api_id") or "not set")
    _status(bool(cfg.get("api_hash")), "API Hash", "configured" if cfg.get("api_hash") else "not set")

    session_file = Path(SESSION_NAME + ".session")
    _status(
        session_file.exists(),
        "Session file",
        str(session_file) if session_file.exists() else "no saved session yet",
    )

    if cfg.get("proxy"):
        _status(True, "Proxy", f"{cfg['proxy'].get('host')}:{cfg['proxy'].get('port')}")
    else:
        _status(None, "Proxy", "not configured (optional)")

    if not (cfg.get("api_id") and cfg.get("api_hash")):
        console.print("[dim][·][/dim] Skipping connectivity check — API credentials not set")
        console.print()
        console.print("[bold yellow]Setup incomplete — see above.[/bold yellow]")
        return

    owns_client = client is None
    if owns_client:
        client = TelegramClient(SESSION_NAME, cfg["api_id"], cfg["api_hash"], proxy=_build_proxy(cfg))
        try:
            with console.status("[cyan]Checking connectivity to Telegram...[/cyan]"):
                await client.connect()
        except (ConnectionError, OSError) as e:
            _status(False, "Telegram reachable", str(e))
            console.print()
            _print_connection_help()
            console.print()
            console.print("[bold yellow]Setup incomplete — see above.[/bold yellow]")
            return

    try:
        authorized = client.is_connected() and await client.is_user_authorized()
        _status(True, "Telegram reachable", "ok")
        _status(bool(authorized), "Session authorized", "yes" if authorized else "no — run `mailgen login`")
    finally:
        if owns_client:
            await client.disconnect()

    console.print()
    console.print("[bold green]System ready.[/bold green]")


# ---------------------------------------------------------------------------
# menu
# ---------------------------------------------------------------------------
async def run_menu():
    client = await login()
    if client is None:
        return

    try:
        while True:
            console.print(BANNER, style="bold green")
            console.print(
                Panel.fit(
                    "[1] Generate Email\n"
                    "[2] Mailboxes\n"
                    "[3] Inbox\n"
                    "[4] Monitor\n"
                    "[5] Export\n"
                    "[6] Show Config\n"
                    "[7] Doctor\n"
                    "[8] Exit",
                    title="mailgen",
                    border_style="cyan",
                )
            )
            choice = input("Select option: ").strip()

            if choice == "1":
                await generate_email(client)
            elif choice == "2":
                await mailbox_mode(client)
            elif choice == "3":
                await inbox(client)
            elif choice == "4":
                await monitor(client)
            elif choice == "5":
                cmd_export()
            elif choice == "6":
                show_config()
            elif choice == "7":
                await doctor(client)
            elif choice == "8":
                break
            else:
                console.print("[red]Invalid choice.[/red]")
    finally:
        await client.disconnect()


# ---------------------------------------------------------------------------
# simple command runner: login -> run one coroutine -> disconnect
# ---------------------------------------------------------------------------
async def _run_simple_command(coro_fn, *args):
    client = await login()
    if client is None:
        return
    try:
        await coro_fn(client, *args)
    finally:
        await client.disconnect()


# ---------------------------------------------------------------------------
# argparse / entry point
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mailgen",
        description="FakeMail Telegram bot automation — Kali/Linux CLI edition.",
    )
    parser.add_argument("--version", action="version", version=f"mailgen {__version__}")

    parser.add_argument("-p", "--phone", metavar="PHONE", help="Save phone number and exit.")
    parser.add_argument(
        "-api", nargs=2, metavar=("API_ID", "API_HASH"), help="Save API credentials and exit."
    )
    parser.add_argument(
        "-proxy",
        nargs=4,
        metavar=("HOST", "PORT", "USER", "PASS"),
        help="Save SOCKS5 proxy settings and exit.",
    )
    parser.add_argument(
        "--show-config", action="store_true", help="Print current configuration (secrets masked)."
    )

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("setup", help="Interactive setup wizard.")
    sub.add_parser("login", help="(Re)authenticate with Telegram.")
    sub.add_parser("generate", help="Generate a new fake email address.")

    inbox_p = sub.add_parser("inbox", help="Show mail from the last N days.")
    inbox_p.add_argument("--days", type=int, default=7)

    monitor_p = sub.add_parser("monitor", help="Live-monitor the chat for new mail.")
    monitor_p.add_argument("--minutes", type=int, default=10)

    sub.add_parser("mailbox", help="Pick a mailbox, see its history, then live-monitor it.")
    sub.add_parser("export", help="Copy saved mail into exports/<mailbox>/.")
    sub.add_parser("doctor", help="Check config, session, and Telegram connectivity.")
    sub.add_parser("menu", help="Open the interactive menu (default with no arguments).")

    return parser


def main():
    setup_logging()
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.phone:
            cmd_set_phone(args.phone)

        if args.api:
            cmd_set_api(*args.api)

        if args.proxy:
            cmd_set_proxy(*args.proxy)

        if args.show_config:
            show_config()

        command = args.command or "menu"

        if command == "setup":
            cmd_setup()
        elif command == "login":
            client = asyncio.run(login())
            if client:
                asyncio.run(client.disconnect())
        elif command == "generate":
            asyncio.run(_run_simple_command(generate_email))
        elif command == "inbox":
            asyncio.run(_run_simple_command(inbox, args.days))
        elif command == "monitor":
            asyncio.run(_run_simple_command(monitor, args.minutes))
        elif command == "mailbox":
            asyncio.run(_run_simple_command(mailbox_mode))
        elif command == "export":
            cmd_export()
        elif command == "doctor":
            asyncio.run(doctor())
        elif command == "menu":
            asyncio.run(run_menu())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted — exiting.[/yellow]")
        sys.exit(0)
    except (ConnectionError, OSError) as e:
        console.print(f"\n[bold red][✗] Connection failed:[/bold red] {e}")
        _print_connection_help()
        log.exception("Connection error")
        sys.exit(1)
    except Exception as e:
        console.print(f"\n[bold red][✗] mailgen hit an unexpected error:[/bold red] {e}")
        console.print(f"[dim]Details were written to {LOG_FILE}[/dim]")
        log.exception("Unhandled exception")
        sys.exit(1)


if __name__ == "__main__":
    main()