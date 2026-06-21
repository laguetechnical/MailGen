# mailgen

**FakeMail Telegram Bot Automation — Kali/Linux CLI edition**

`mailgen` wraps interaction with Telegram's `@fakemailbot` behind a proper, scriptable command-line tool. Generate temporary email addresses, pull your inbox, live-monitor incoming mail, and manage everything from your terminal — no manual back-and-forth in the Telegram app required.

```
███╗   ███╗ █████╗ ██╗██╗      ██████╗ ███████╗███╗   ██╗
████╗ ████║██╔══██╗██║██║     ██╔════╝ ██╔════╝████╗  ██║
██╔████╔██║███████║██║██║     ██║  ███╗█████╗  ██╔██╗ ██║
██║╚██╔╝██║██╔══██║██║██║     ██║   ██║██╔══╝  ██║╚██╗██║
██║ ╚═╝ ██║██║  ██║██║███████╗╚██████╔╝███████╗██║ ╚████║
╚═╝     ╚═╝╚═╝  ╚═╝╚═╝╚══════╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝
```

---

## Introduction

`@fakemailbot` is a Telegram bot that hands out disposable email addresses and forwards any mail they receive straight into your chat with it. That's great for quick throwaway signups, but doing it by hand means scrolling through chat history, manually copy-pasting message fragments back together, and babysitting the chat for new mail.

`mailgen` automates all of that. It logs into Telegram on your behalf (via your own API credentials, using [Telethon](https://github.com/LonamiWebs/Telethon)), talks to the bot, reconstructs full mail bodies from the message fragments Telegram splits them into, and saves everything to disk in a clean, browsable structure — all driven by a single CLI with both direct-argument and interactive modes.

**Core features:**

- 🔑 One-time setup, persistent authenticated session (no repeated OTP)
- 📬 Generate new fake addresses on demand
- 📥 Pull and reconstruct mail history for any time window
- 👁 Live-monitor the chat and catch new mail as it arrives
- 📦 Per-mailbox view: pick one address, see its history, then watch just that one
- 🗂 Export saved mail into per-mailbox folders
- 🩺 Built-in diagnostics (`doctor`) for config/session/connectivity issues
- 🌐 Optional SOCKS5 proxy support
- 🔒 Config stored with `chmod 600` — credentials aren't world-readable

---

## How It Works

```
 You ──▶ mailgen ──▶ Telethon client ──▶ Telegram ──▶ @fakemailbot
                          │
                          ▼
                  reconstructs mail
                  fragments into full
                  messages, then saves
                  to ~/.config/mailgen/
```

1. **Authentication.** `mailgen` uses your personal Telegram API ID/hash (from [my.telegram.org](https://my.telegram.org)) and phone number to open a Telethon session. The first login asks for an OTP (and 2FA password, if enabled); after that, the session file is reused automatically — no repeated logins.
2. **Talking to the bot.** Commands like `generate` and `mailbox` send the right text (e.g. `/id`, a new address request) to `@fakemailbot` and wait for its reply, with retry/timeout handling if the bot is slow.
3. **Mail reconstruction.** The bot doesn't send "one mail = one message" — long mails get split across several Telegram messages, with a `➖➖➖➖➖` separator line marking where a new mail starts. `mailgen` walks the chat history (or live event stream), groups fragments back into complete mails, and filters out bot chrome (command echoes, `/id` listings, "new mail id" confirmations) so it never gets mistaken for actual content.
4. **Saving.** Every reconstructed mail is written to its own timestamped file under `mails/`, with a running log in `mails/live_inbox.txt`. `export` later sorts those files into per-mailbox folders based on the `To:` address.

---

## Installation

### Requirements

- Linux (developed for Kali, works on any modern distro)
- Python 3.9+
- A Telegram account
- Telegram API credentials — free, from [my.telegram.org](https://my.telegram.org) → **API development tools**

### Install

```bash
git clone <your-repo-url> mailgen
cd mailgen
pip install -e .
```

This installs `mailgen` as a CLI command (via the project's `setup.py`/`pyproject.toml`) and pulls in its dependencies: `telethon` and `rich`.

> If you're not packaging it yet and just want to run it directly:
> ```bash
> pip install telethon rich
> python -m mailgen
> ```

---

## Basic Usage

### 1. First-time setup

Run the interactive wizard — it'll ask for your phone number, API ID/hash, and (optionally) a SOCKS5 proxy, then walk you through the Telegram login:

```bash
mailgen setup
```

Or configure pieces directly without the wizard:

```bash
mailgen -p +911234567890                      # save phone number
mailgen -api 9844616 abcdef123456              # save API ID + hash
mailgen -proxy 31.59.20.176 6754 user pass      # save SOCKS5 proxy
mailgen --show-config                           # view current config (secrets masked)
```

### 2. Log in

```bash
mailgen login
```

Only needed the first time, or if your saved session ever expires — `mailgen` reuses the saved session automatically on every other command.

### 3. Generate a fake email

```bash
mailgen generate
```

Prompts for a username and a domain choice, sends the request to the bot, and prints the address it gave you back.

### 4. Check your inbox

```bash
mailgen inbox --days 7
```

Reconstructs and saves every mail from the last 7 days (default).

### 5. Just run it with no arguments

```bash
mailgen
```

Drops you into a Rich-powered interactive menu covering every feature below — handy if you don't want to remember flags.

---

## Deep Usage Guide

### Subcommands at a glance

| Command | What it does |
|---|---|
| `mailgen setup` | Interactive first-time configuration |
| `mailgen login` | (Re)authenticate with Telegram |
| `mailgen generate` | Request a new fake address |
| `mailgen inbox [--days N]` | Reconstruct + save the last N days of mail (default: 7) |
| `mailgen monitor [--minutes N]` | Live-watch the chat for new mail (default lookback: 10 min) |
| `mailgen mailbox` | Pick one mailbox, see its history, then live-monitor just that one |
| `mailgen export` | Copy saved mail into `exports/<mailbox>/`, grouped by recipient |
| `mailgen doctor` | Run diagnostics: config, session, connectivity |
| `mailgen` (no args) | Open the interactive menu |

### Quick-config flags (no subcommand needed)

These can be combined with each other or with a subcommand in the same invocation — they're applied before whatever subcommand runs:

```bash
mailgen -p +911234567890                       # set/update phone
mailgen -api <API_ID> <API_HASH>                # set/update API credentials
mailgen -proxy <HOST> <PORT> <USER> <PASS>      # set/update SOCKS5 proxy
mailgen --show-config                           # print config (secrets masked)
mailgen --version                               # print version
```

### Inbox vs. Monitor vs. Mailbox

These three commands all reconstruct mail the same way, but differ in scope:

- **`inbox --days N`** — one-shot dump. Scans the last N days of chat history, reconstructs every mail, prints + saves each one, then exits.
- **`monitor --minutes N`** — shows recent history (last N minutes) the same way, then **stays connected** and live-prints/saves any new mail as it comes in. Press `Ctrl+C` to stop. Because live messages don't have an explicit "end of mail" marker, `mailgen` uses a short flush delay: once a few seconds pass with no new fragment, whatever's buffered is treated as a complete mail.
- **`mailbox`** — lists all your fake addresses (via `/id`), lets you pick one, shows its last 30 days of history, and then live-monitors the chat but **only surfaces mail addressed to that one address** — everything else is filtered out.

### Exporting

```bash
mailgen export
```

Walks every saved file in `mails/`, parses out the `To:` address, and copies each into `exports/<address>/`. Anything without a parseable recipient lands in `exports/unknown/`. Useful once you've accumulated mail across several generated addresses and want them sorted.

### Diagnostics

```bash
mailgen doctor
```

Checks, in order: config file presence, phone/API credentials, saved session file, proxy settings, and (if credentials are set) a live connectivity + authorization check against Telegram. Run this first whenever something isn't working.

### Directory layout

Everything lives under `~/.config/mailgen/`:

```
~/.config/mailgen/
├── config.json     # credentials + proxy, chmod 600
├── session/         # Telethon session file
├── mails/           # every reconstructed mail, one file each
│   └── live_inbox.txt   # running append-only log
├── exports/         # output of `mailgen export`, grouped by mailbox
└── logs/
    └── mailgen.log   # full execution log, including stack traces
```

### Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| "Missing configuration" error | Phone/API ID/API hash not set | `mailgen setup` or the `-p` / `-api` flags |
| Connection failed on login | Proxy down, Telegram blocked, bad credentials, no internet | `mailgen doctor` for a full breakdown |
| No reply from the bot | Bot is slow/down, or rate-limiting | Commands already retry/timeout gracefully — just try again shortly |
| Session keeps asking for OTP | Session file missing or invalidated | Re-run `mailgen login`; check `~/.config/mailgen/session/` permissions |
| Need full error details | — | Check `~/.config/mailgen/logs/mailgen.log` — unhandled errors are logged there with full tracebacks |

---

## Notes

- All secrets (`config.json`) are written with `chmod 600` — owner read/write only.
- This tool only automates *your own* Telegram session against a public bot you already use; it doesn't touch anyone else's account or bypass any authentication.
- Domain options for generated addresses are currently `@hi2.in` and `@telegmail.com`, matching whatever `@fakemailbot` offers — these are bot-side and not configurable from `mailgen` itself.
