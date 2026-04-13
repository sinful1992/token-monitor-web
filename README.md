# Token Monitor Web

A web dashboard for monitoring Claude Code token usage in real time. Reads `~/.claude/projects/` JSONL session files, tracks per-session and aggregate usage, and serves a live dashboard accessible locally or via Tailscale from any device (Windows laptop, phone, etc.).

Built with **FastAPI** + server-sent events (SSE). Single Python file. Cross-platform (Linux + Windows).

## What it does

- **Live session list** — detects running Claude Code processes via `/proc` (Linux) or process listing (Windows), shows which session each is using, current context window fill, tokens used/remaining
- **5-hour plan window** — tracks output tokens in a rolling 5h window against the plan's output token limit (calibrated to 237K output tokens per 5h)
- **Per-session breakdown** — input, cache write, cache read, output tokens per session; cost estimate in USD
- **Aggregate stats** — total tokens and cost across all active sessions
- **SSE push updates** — dashboard updates are pushed instantly when Claude writes new tokens; no polling loop
- **Tailscale accessible** — binds to `0.0.0.0:8765`, reachable from Windows laptop at `http://home-server.tail7bde5d.ts.net:8765`

## File locations

| Path | Purpose |
|------|---------|
| `~/token-monitor-web.py` | The entire app — single file |
| `~/.claude/projects/` | Claude Code session source data (read-only) |

## Install

```bash
pip install fastapi uvicorn watchfiles --break-system-packages
```

## Run

```bash
python3 ~/token-monitor-web.py
# Local:     http://localhost:8765
# Tailscale: http://home-server.tail7bde5d.ts.net:8765
```

### As a systemd user service (auto-start on login)

```ini
# ~/.config/systemd/user/token-monitor.service
[Unit]
Description=Claude Code Token Monitor

[Service]
ExecStart=/usr/bin/python3 /home/giedrius/token-monitor-web.py
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now token-monitor
```

## Architecture

```
FastAPI app
  GET  /        HTML dashboard page (full render)
  GET  /stream  SSE endpoint — pushes updated snapshot JSON on file change + 30s heartbeat
  GET  /data    JSON snapshot on demand (used by dashboard JS on initial load)
```

### Background tasks (started at app startup)

| Task | Interval | What |
|------|----------|------|
| `file_watcher_loop` | event-driven | Watches `~/.claude/projects/` with `watchfiles.awatch`; broadcasts snapshot to all SSE clients when any `.jsonl` changes |
| `proc_refresh_loop` | every 60s | Refreshes live session list via `/proc` scan |
| `heartbeat_loop` | every 30s | Sends a snapshot so countdown timers stay fresh even when Claude is idle |

### Session discovery

1. Walk `/proc` for all running PIDs
2. For each PID, read `/proc/{pid}/cmdline` — look for `claude` processes
3. Match the process's working directory (`/proc/{pid}/cwd`) to a `~/.claude/projects/` subdirectory
4. Read the corresponding JSONL session file, parse token usage from `usage` fields on `assistant` messages

This scan runs every 60s in the background — not on every SSE tick.

### Caching

Two levels of file-read caching:

| Cache | Key | What |
|-------|-----|------|
| `_parse_cache` | file size | Per-file parse result — on growth, seeks to previous size and reads only new bytes |
| `_plan_cache` | 10s TTL | 5h plan window calculation |

The parse cache is append-aware: when a JSONL file grows, only the newly appended bytes are read and the new turns are merged with the cached result.

### Key constants

```python
CONTEXT_LIMIT   = 200_000   # Claude's context window
AUTOCOMPACT_BUF = 33_000    # reserved for autocompact trigger
USABLE_LIMIT    = 167_000   # context actually usable before compaction
WARN_PCT        = 0.80      # yellow warning threshold
CRITICAL_PCT    = 0.90      # red critical threshold
PLAN_WINDOW     = 5 * 3600  # rolling window for plan limit (seconds)
PLAN_LIMIT      = 280_000   # max output tokens per 5h window (peak hours)
CACHE_TTL       = 300       # prompt cache TTL (seconds) — used for cache_expires_in display
```

```python
PRICES = {          # per million tokens, USD
    "input":       3.00,
    "cache_write": 3.75,
    "cache_read":  0.30,
    "output":      15.00,
}
```

### Token extraction from JSONL

```python
# Each assistant message has a usage block:
{
  "type": "assistant",
  "message": {
    "usage": {
      "input_tokens": 1234,
      "cache_creation_input_tokens": 567,
      "cache_read_input_tokens": 890,
      "output_tokens": 42
    }
  }
}
# Sum these across all assistant messages in the session file.
# The last message's cumulative totals are used for context fill %.
```

## Notes

- **Read-only** — never writes to `~/.claude/projects/`, only reads JSONL files
- **Cross-platform** — `watchfiles` uses inotify on Linux and ReadDirectoryChanges on Windows; `/proc` session discovery is Linux-only (sessions show as inactive on Windows but plan usage still works)
- **Tailscale hostname** — `home-server.tail7bde5d.ts.net` — update if the Tailscale network changes
- **Plan limit calibration** — `PLAN_LIMIT = 237_000` was measured empirically from actual 5h sessions. Adjust if hitting limits earlier or later than expected.
