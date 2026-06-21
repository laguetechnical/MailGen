# mailgen

FakeMail Telegram bot automation, packaged as a real CLI tool instead of a
script you run with `python fakemail_bot.py`.

## Install

```bash
cd LT-MailGen
pip install --break-system-packages .
# or, inside a venv:
#   python3 -m venv .venv && source .venv/bin/activate && pip install .
```

This registers a `mailgen` command on your PATH (via `pyproject.toml`'s
`[project.scripts]` entry point — no manual symlinking or aliasing needed).

For development (edit the source and have changes picked up immediately):

```bash
pip install --break-system-packages -e .
```

## First-time setup

```bash
mailgen setup
```

Walks you through phone number, Telegram API ID/hash, and an optional SOCKS5
proxy, then offers to log you in right away. Everything is written to
`~/.config/mailgen/config.json` (chmod 600, since it holds credentials).

You can also set things individually without the wizard:

```bash
mailgen -p +911234567890
mailgen -api 9844616 abcdef123456
mailgen -proxy 31.59.20.176 6754 user pass
mailgen --show-config
```

`--show-config` masks secrets:

```
Phone        +91*******
API ID       9844616
API Hash     abcd************
Proxy        31.59.20.176:6754
Proxy Auth   us** / ****
Config file  /home/you/.config/mailgen/config.json
Session dir  /home/you/.config/mailgen/session
```

## Usage

```bash
mailgen              # interactive menu (banner + numbered options)
mailgen login        # force a fresh login / refresh the session
mailgen generate     # request a new fake email address
mailgen inbox --days 7      # reconstruct + save mail from the last 7 days
mailgen monitor --minutes 10  # live-watch the chat for new mail
mailgen mailbox       # pick one mailbox, see its history, then live-monitor it
mailgen export         # copy everything in mails/ into exports/<mailbox>/
```

## Where things live

Everything is kept under `~/.config/mailgen/`:

```
~/.config/mailgen/
├── config.json   credentials + proxy (chmod 600)
├── session/      Telethon session file (created after first login)
├── mails/        every reconstructed mail, one .txt file each
├── logs/         mailgen.log
└── exports/      output of `mailgen export`, one folder per mailbox
```

No environment variables, no hardcoded secrets in source — `mailgen setup`
(or the `-p` / `-api` / `-proxy` flags) is the only place credentials get
entered, and they're written with owner-only file permissions.
