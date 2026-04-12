# Token Monitor Web

A web dashboard for monitoring Claude Code token usage in real time. Reads `~/.claude/projects/` JSONL session files, tracks per-session and aggregate usage, and serves a live dashboard accessible locally or via Tailscale from any device (Windows laptop, phone, etc.).

Built with **FastAPI** + server-sent events (SSE). Single Python file.

## What it does

- **Live session list** — detects running Claude Code processes via `/proc`, shows which session each is using, current context window fill, tokens used/remaining
- **5-hour plan window** — tracks output tokens in a rolling 5h window against the plan's output token limit (calibrated to 237K output tokens per 5h)
- **Per-session breakdown** — input, cache write, cache read, output tokens per session; cost estimate in USD
- **Aggregate stats** — total tokens and cost across all active sessions
- **SSE auto-refresh** — dashboard updates every 3 seconds via server-sent events, no polling needed on the client
- **Tailscale accessible** — binds to `0.0.0.0:8765`, reachable from Windows laptop at `http://home-server.tail7bde5d.ts.net:8765`

## File locations

| Path | Purpose |
|------|---------|
| `~/token-monitor-web.py` | The entire app — single file |
| `~/.claude/projects/` | Claude Code session source data (read-only) |

## Install

```bash
pip install fastapi uvicorn --break-system-packages
```

## Run

```bash
python3 ~/token-monitor-web.py
# Local:     http://localhost:8765
# Tailscale: http://home-server.tail7bde5d.ts.net:8765
```

### As a systemd user service (auto-start on login)

```ini
# ~/.config/systemd/user/token-monitor-web.service
[Unit]
Description=Claude Token Monitor Web Dashboard

[Service]
ExecStart=/usr/bin/python3 /home/giedrius/token-monitor-web.py
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now token-monitor-web
```

## Architecture

```
FastAPI app
  GET  /           HTML dashboard page (full render)
  GET  /stream     SSE endpoint — pushes updated snapshot JSON every 3s
  GET  /api/data   JSON snapshot on demand (used by dashboard JS on load)
```

### Session discovery

1. Walk `/proc` for all running PIDs
2. For each PID, read `/proc/{pid}/cmdline` — look for `claude` processes
3. Match the process's working directory (`/proc/{pid}/cwd`) to a `~/.claude/projects/` subdirectory
4. Read the corresponding JSONL session file, parse token usage from `usage` fields on `assistant` messages

### Caching (important — this is why it doesn't kill CPU)

Three levels:

| Cache | TTL | What |
|-------|-----|------|
| `_parse_cache` | mtime+size keyed | Per-file parse result — only re-parsed if file changed |
| `_snapshot_cache` | 3 seconds | Full dashboard data snapshot |
| `_plan_cache` | 10 seconds | 5h plan window calculation |

The parse cache is keyed on `(mtime, size)` — same trick as conv-browser. A file that hasn't changed since last read is returned from cache without any I/O. Only the active session file (being appended to) gets re-parsed on each refresh cycle.

### Key constants

```python
CONTEXT_LIMIT   = 200_000   # Claude's context window
AUTOCOMPACT_BUF = 33_000    # reserved for autocompact trigger
USABLE_LIMIT    = 167_000   # context actually usable before compaction
WARN_PCT        = 0.80      # yellow warning threshold
CRITICAL_PCT    = 0.90      # red critical threshold
PLAN_WINDOW     = 5 * 3600  # rolling window for plan limit (seconds)
PLAN_LIMIT      = 237_000   # max output tokens per 5h window (calibrated empirically)
CACHE_TTL       = 300       # parse cache max age (seconds)
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

- **Linux only** — uses `/proc` for process discovery. The Windows version (`token-monitor-web-win.py`) uses a different process detection approach.
- **Read-only** — never writes to `~/.claude/projects/`, only reads JSONL files
- **Tailscale hostname** — `home-server.tail7bde5d.ts.net` — update if the Tailscale network changes
- **Plan limit calibration** — `PLAN_LIMIT = 237_000` was measured empirically from actual 5h sessions. Adjust if hitting limits earlier or later than expected.
