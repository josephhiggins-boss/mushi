# Mushi

A Telegram interface to Claude Code. Run `mushi.py` on your laptop, message the bot from your phone, and Claude does real work where your files actually live.

Most "Claude integrations" you'll see are webapp wrappers around the Anthropic API. Those can chat. They can't read your files, run commands, or use any of Claude's agent tools. Mushi is a bridge to the [Claude Code CLI](https://docs.claude.com/en/docs/claude-code/overview) instead, so the Claude that responds to your texts has the same powers as the one you'd talk to at your desk.

I built it because I wanted to give Claude real tasks while I was away from my laptop. Now I do most of my work this way.

The name comes from One Piece. A Den Den Mushi is a small snail people carry to talk to others across the Grand Line. Same idea here: phone in your hand, laptop running Claude, work happening in the world.

## What you can do

| Command | What it does |
|---|---|
| Plain message | Auto-classified into your memory dir as a markdown file (`/remember` for explicit) |
| Voice message | Transcribed with Whisper, then routed |
| `/ask <q>` | One-shot Q&A against your accumulated memories |
| `/think <prompt>` | Multi-turn deep mode (Opus via your Claude Code subscription) |
| `/cc <prompt>` | Headless Claude Code job with full agent tools, live-streaming output. Multiple jobs run in parallel. |
| `/cd <path>` | Set the working directory for subsequent `/cc` jobs in this chat |
| `/cc-stop [id]` | Kill a running job (or all of them) |
| `/list` | Show the `MEMORY.md` index |
| `/cost` | 7-day API cost breakdown |
| `/lock` / `/unlock <pwd>` | Password gate for destructive ops |

Files you send to the chat (images, PDFs) drop into your inbox folder and can be referenced in later prompts.

Claude can send files back too. If it prints `[[SEND_IMAGE: /abs/path]]` or `[[SEND_FILE: /abs/path]]` on a line of its output, the bridge strips that line and forwards the file to chat. Handy for generated charts, screenshots, PDFs.

## Setup

```bash
git clone https://github.com/josephhiggins-boss/mushi.git
cd mushi
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Fill in TELEGRAM_BOT_TOKEN, TELEGRAM_USER_ID, and at least one LLM key
python mushi.py
```

Then message your bot `/help` from your phone.

You'll need:

1. **A Telegram bot.** Message [@BotFather](https://t.me/BotFather), `/newbot`, follow the prompts. Copy the HTTP API token.
2. **Your numeric Telegram user ID.** Message [@userinfobot](https://t.me/userinfobot). It replies with your ID. Only this user gets served. Everyone else is silently ignored.
3. **Claude access.** Easiest: install [Claude Code](https://claude.ai/code) and log in. Mushi invokes the `claude` CLI as a subprocess, routing through your subscription. No API credit burn. If you'd rather use the API, set `ANTHROPIC_API_KEY` in `.env`.
4. **Whisper (optional).** Set `OPENAI_API_KEY` to enable voice messages.

To keep it running, drop a `mushi.bat` shortcut into `shell:startup` on Windows, write a launchd plist on macOS, or a systemd unit on Linux.

## Storage

By default everything lives under `~/.claude-bridge/`:

```
~/.claude-bridge/
├── memory/           # markdown memories (MEMORY_DIR)
│   ├── MEMORY.md     # one-line index. Claude reads this every conversation.
│   ├── user_*.md     # who you are
│   ├── feedback_*.md # how you want to work
│   ├── project_*.md  # ongoing work / decisions
│   └── reference_*.md# pointers to external systems
└── inbox/            # files / images dropped from chat (INBOX_DIR)
```

The memory dir is plain markdown with YAML frontmatter. No DB, no proprietary format. Grep it, edit in your IDE, version-control it, sync via Dropbox, whatever.

## Things worth knowing before you deploy this

Two warnings, both load-bearing.

**Don't expose this publicly.** Mushi gives Claude full file access on the host machine. The `TELEGRAM_USER_ID` check is the only thing keeping strangers out of your filesystem. If you share the bot's token or run it on a shared machine, you've given that audience read/write access to everything Claude can reach. Treat the token like an SSH key.

**Watch your `ANTHROPIC_API_KEY` shell env.** If the variable is set in your shell, the Claude CLI quietly prefers it over your subscription OAuth. You can end up burning API credits even though you're paying for a Claude Code subscription. Mushi strips the key from the child environment before invoking the CLI to force OAuth to win. If you call Claude CLI from other scripts, do the same.

## License

MIT. See [LICENSE](LICENSE).

## Contact

Telegram: [@nftscreenshotter](https://t.me/nftscreenshotter)
Email: admin@jo3vo.io
