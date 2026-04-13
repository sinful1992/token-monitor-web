#!/usr/bin/env python3
"""
token-monitor-web.py — Claude Code token usage web dashboard.

Run:  python3 ~/token-monitor-web.py
Open: http://localhost:8765   (or via Tailscale from Windows)
"""

import json
import os
import re
import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from watchfiles import awatch
import uvicorn

# ── constants ──────────────────────────────────────────────────────────────
CONTEXT_LIMIT   = 200_000
AUTOCOMPACT_BUF = 33_000
USABLE_LIMIT    = CONTEXT_LIMIT - AUTOCOMPACT_BUF
WARN_PCT        = 0.80
CRITICAL_PCT    = 0.90
CACHE_TTL       = 300
PLAN_WINDOW     = 5 * 3600
PLAN_LIMIT      = 280_000  # observed off-peak (~23:00); actual peak limit unknown

PRICES = {
    "input":       3.00,
    "cache_write": 3.75,
    "cache_read":  0.30,
    "output":      15.00,
}

BOOT_TIME = None

# ── caches ─────────────────────────────────────────────────────────────────
# parse_session cache: path -> (mtime, size, result)
_parse_cache: dict = {}
# plan usage cache: (result, expiry_ts)
_plan_cache: tuple = (None, 0.0)
_PLAN_CACHE_TTL = 10.0      # recompute plan usage at most every 10s

# ── live state (updated by background tasks) ───────────────────────────────
_live_sessions: list = []           # updated every 60s by proc_refresh_loop
_client_queues: set = set()         # one asyncio.Queue per connected SSE client


# ── process helpers ────────────────────────────────────────────────────────
def get_boot_time() -> float:
    global BOOT_TIME
    if BOOT_TIME is None:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime "):
                    BOOT_TIME = float(line.split()[1])
                    break
    return BOOT_TIME


def proc_start_time(pid: int) -> float:
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().split()
        ticks = int(fields[21])
        clk   = os.sysconf("SC_CLK_TCK")
        return get_boot_time() + (ticks / clk)
    except Exception:
        return 0.0


# ── session discovery ──────────────────────────────────────────────────────
def find_claude_sessions() -> list[dict]:
    sessions      = []
    projects_base = Path.home() / ".claude" / "projects"

    try:
        pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
    except PermissionError:
        return []

    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().decode(errors="replace").replace("\x00", " ").strip()
        except (OSError, PermissionError):
            continue

        if not re.match(r"^claude(\s|$)", cmdline):
            continue

        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except (OSError, PermissionError):
            continue

        pts = "?"
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                try:
                    t = os.readlink(f"/proc/{pid}/fd/{fd}")
                    if "/dev/pts/" in t:
                        pts = t.split("/dev/pts/")[-1]
                        break
                except OSError:
                    pass
        except (OSError, PermissionError):
            pass

        start_time = proc_start_time(pid)

        # Try exact session file first
        jsonl_path = None
        meta_file  = Path.home() / ".claude" / "sessions" / f"{pid}.json"
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text())
                sid  = meta.get("sessionId")
                if sid:
                    candidate = projects_base / cwd.replace("/", "-") / f"{sid}.jsonl"
                    if candidate.exists():
                        jsonl_path = candidate
            except Exception:
                pass

        if jsonl_path is None:
            proj_dir = projects_base / cwd.replace("/", "-")
            if not proj_dir.exists():
                continue
            candidates = []
            for jf in proj_dir.glob("*.jsonl"):
                try:
                    candidates.append((jf.stat().st_mtime, jf))
                except OSError:
                    pass
            if not candidates:
                continue
            jsonl_path = max(candidates)[1]

        sessions.append({
            "pid":        pid,
            "pts":        pts,
            "cwd":        cwd,
            "jsonl":      jsonl_path,
            "start_time": start_time,
        })

    seen = {}
    for s in sessions:
        key = str(s["jsonl"])
        if key not in seen:
            seen[key] = s
    return list(seen.values())


# ── data parsing ───────────────────────────────────────────────────────────
def parse_session(path: Path) -> list[dict]:
    global _parse_cache
    try:
        st = path.stat()
        key = str(path)
        cached = _parse_cache.get(key)
        if cached and cached[0] == st.st_size:
            return cached[1]
        # If file shrank (truncated/replaced), discard cache and re-read from start
        if cached and st.st_size < cached[0]:
            cached = None
        read_from = cached[0] if cached else 0
        prior_turns = cached[1] if cached else []
    except OSError:
        return []

    new_turns = []
    try:
        with open(path, "rb") as f:
            f.seek(read_from)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "assistant":
                    continue
                usage = entry.get("message", {}).get("usage", {})
                if not usage:
                    continue
                new_turns.append({
                    "input":       usage.get("input_tokens", 0),
                    "cache_read":  usage.get("cache_read_input_tokens", 0),
                    "cache_write": usage.get("cache_creation_input_tokens", 0),
                    "output":      usage.get("output_tokens", 0),
                    "timestamp":   entry.get("timestamp"),
                })
    except (OSError, PermissionError):
        pass

    turns = prior_turns + new_turns
    _parse_cache[key] = (st.st_size, turns)
    return turns


def compute_stats(turns: list[dict]) -> dict:
    if not turns:
        return {}
    last         = turns[-1]
    total_input  = sum(t["input"]       for t in turns)
    total_cr     = sum(t["cache_read"]  for t in turns)
    total_cw     = sum(t["cache_write"] for t in turns)
    total_output = sum(t["output"]      for t in turns)

    cost = (
        total_input  / 1e6 * PRICES["input"] +
        total_cr     / 1e6 * PRICES["cache_read"] +
        total_cw     / 1e6 * PRICES["cache_write"] +
        total_output / 1e6 * PRICES["output"]
    )

    context_tokens = last["input"] + last["cache_read"] + last["cache_write"]
    fill_pct       = context_tokens / CONTEXT_LIMIT
    usable_pct     = context_tokens / USABLE_LIMIT
    cacheable      = last["cache_read"] + last["input"]

    cache_expires_in = None
    for turn in reversed(turns):
        if turn["cache_write"] > 0 and turn.get("timestamp"):
            try:
                ts      = datetime.fromisoformat(turn["timestamp"].replace("Z", "+00:00"))
                elapsed = (datetime.now(timezone.utc) - ts).total_seconds()
                cache_expires_in = max(0.0, CACHE_TTL - elapsed)
            except Exception:
                pass
            break

    return {
        "turns":            len(turns),
        "context_tokens":   context_tokens,
        "fill_pct":         fill_pct,
        "usable_pct":       usable_pct,
        "usable_left":      USABLE_LIMIT - context_tokens,
        "cache_hit":        last["cache_read"] / cacheable if cacheable else 0,
        "total_input":      total_input,
        "total_output":     total_output,
        "cost":             cost,
        "last_input":       last["input"],
        "last_cr":          last["cache_read"],
        "last_cw":          last["cache_write"],
        "last_output":      last["output"],
        "cache_expires_in": cache_expires_in,
    }


def compute_plan_usage() -> dict:
    """Find the current 5h session start, sum output tokens since then."""
    global _plan_cache
    now = time.monotonic()
    if _plan_cache[0] is not None and now < _plan_cache[1]:
        return _plan_cache[0]

    projects_base = Path.home() / ".claude" / "projects"
    now_ts = datetime.now(timezone.utc).timestamp()

    # Re-use parse_session() cache — avoids re-reading files that haven't changed
    events = []
    for jsonl in projects_base.glob("**/*.jsonl"):
        for turn in parse_session(jsonl):
            ts_str = turn.get("timestamp")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            events.append((ts, turn["output"]))

    if not events:
        return {"used": 0, "limit": PLAN_LIMIT, "pct": 0.0, "reset_in": None}

    events.sort(key=lambda x: x[0])

    # Walk forward: a new session begins whenever a message arrives after
    # the current session's 5h window has expired.
    session_start = events[0][0]
    for i in range(1, len(events)):
        if events[i][0] >= session_start + PLAN_WINDOW:
            session_start = events[i][0]

    total_out = sum(out for ts, out in events if ts >= session_start)
    window_end = session_start + PLAN_WINDOW
    reset_in = window_end - now_ts  # negative = window expired, resets on next message

    result = {
        "used":     total_out,
        "limit":    PLAN_LIMIT,
        "pct":      min(1.0, total_out / PLAN_LIMIT),
        "reset_in": reset_in if reset_in > 0 else 0,
    }
    _plan_cache = (result, time.monotonic() + _PLAN_CACHE_TTL)
    return result


# ── snapshot builder ───────────────────────────────────────────────────────
def build_snapshot() -> dict:
    """Build snapshot using cached live sessions (no /proc scan here)."""
    sessions_out = []
    agg = {"turns": 0, "cost": 0.0, "input": 0, "output": 0, "cache_hit_num": 0.0, "cache_hit_den": 0}

    for sess in _live_sessions:
        turns    = parse_session(sess["jsonl"])
        stats    = compute_stats(turns)
        cwd_name = Path(sess["cwd"]).name or sess["cwd"]
        if stats:
            agg["turns"]        += stats["turns"]
            agg["cost"]         += stats["cost"]
            agg["input"]        += stats["total_input"]
            agg["output"]       += stats["total_output"]
            agg["cache_hit_num"] += stats["cache_hit"] * stats["turns"]
            agg["cache_hit_den"] += stats["turns"]
        sessions_out.append({
            "pid":      sess["pid"],
            "pts":      sess["pts"],
            "cwd":      cwd_name,
            "has_data": bool(stats),
            "stats":    stats,
        })

    agg["cache_hit"] = (agg["cache_hit_num"] / agg["cache_hit_den"]
                        if agg["cache_hit_den"] else 0.0)

    return {
        "time":     datetime.now().strftime("%H:%M:%S"),
        "plan":     compute_plan_usage(),
        "agg":      agg,
        "sessions": sessions_out,
    }


# ── background tasks ────────────────────────────────────────────────────────
async def proc_refresh_loop():
    """Refresh live session list (slow /proc scan) every 60s."""
    global _live_sessions
    while True:
        _live_sessions = find_claude_sessions()
        await asyncio.sleep(60)


async def _broadcast(snap: dict) -> None:
    msg = f"data: {json.dumps(snap)}\n\n"
    for q in list(_client_queues):
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass


async def file_watcher_loop():
    """Watch ~/.claude/projects/**/*.jsonl; broadcast snapshot on any change."""
    global _live_sessions
    projects_base = Path.home() / ".claude" / "projects"
    projects_base.mkdir(parents=True, exist_ok=True)
    async for changes in awatch(str(projects_base)):
        if any(p.endswith(".jsonl") for _, p in changes):
            _live_sessions = find_claude_sessions()  # always fresh on activity
            await _broadcast(build_snapshot())


async def heartbeat_loop():
    """Send a snapshot every 30s so countdown timers stay fresh in the browser."""
    while True:
        await asyncio.sleep(30)
        await _broadcast(build_snapshot())


# ── FastAPI app ────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _live_sessions
    _live_sessions = find_claude_sessions()  # initial sync scan
    tasks = [
        asyncio.create_task(proc_refresh_loop()),
        asyncio.create_task(file_watcher_loop()),
        asyncio.create_task(heartbeat_loop()),
    ]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan)

HTML = r"""<!DOCTYPE html>
<!-- v3 -->
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Token Monitor</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@400;500;600;700&family=JetBrains+Mono:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:           #080b14;
    --bg-panel:     #0c1022;
    --card:         rgba(14, 20, 40, 0.85);
    --card-border:  rgba(255,255,255,0.06);
    --cyan:         #00d4ff;
    --cyan-dim:     rgba(0,212,255,0.15);
    --green:        #10b981;
    --green-dim:    rgba(16,185,129,0.15);
    --amber:        #f59e0b;
    --amber-dim:    rgba(245,158,11,0.15);
    --red:          #ef4444;
    --red-dim:      rgba(239,68,68,0.15);
    --purple:       #7c3aed;
    --purple-dim:   rgba(124,58,237,0.15);
    --text:         #cbd5e1;
    --text-bright:  #f1f5f9;
    --text-muted:   #7ab0cc;
    --text-dim:     #5080a0;
    --mono:         'JetBrains Mono', monospace;
    --display:      'Rajdhani', sans-serif;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background-color: var(--bg);
    background-image:
      linear-gradient(rgba(0,212,255,0.025) 1px, transparent 1px),
      linear-gradient(90deg, rgba(0,212,255,0.025) 1px, transparent 1px);
    background-size: 48px 48px;
    color: var(--text);
    font-family: var(--mono);
    min-height: 100vh;
    overflow-x: hidden;
  }

  /* scanline overlay */
  body::before {
    content: '';
    position: fixed;
    inset: 0;
    background: repeating-linear-gradient(
      0deg,
      transparent,
      transparent 2px,
      rgba(0,0,0,0.04) 2px,
      rgba(0,0,0,0.04) 4px
    );
    pointer-events: none;
    z-index: 1000;
  }

  /* ── header ── */
  header {
    position: sticky;
    top: 0;
    z-index: 100;
    background: rgba(8,11,20,0.95);
    backdrop-filter: blur(20px);
    border-bottom: 1px solid rgba(0,212,255,0.12);
    box-shadow: 0 4px 32px rgba(0,0,0,0.5);
    padding: 0 28px;
  }

  /* row 1 — logo + plan % + clock */
  .header-top {
    max-width: 1400px;
    margin: 0 auto;
    display: flex;
    align-items: center;
    gap: 24px;
    padding: 16px 0 10px;
  }
  .logo {
    font-family: var(--display);
    font-size: 18px;
    font-weight: 700;
    letter-spacing: 4px;
    text-transform: uppercase;
    color: var(--cyan);
    text-shadow: 0 0 24px rgba(0,212,255,0.5);
    white-space: nowrap;
    flex-shrink: 0;
  }
  .logo span { color: rgba(0,212,255,0.35); }

  /* big plan % number */
  .plan-pct-big {
    font-family: var(--display);
    font-size: 36px;
    font-weight: 700;
    line-height: 1;
    flex-shrink: 0;
    transition: color 0.4s;
  }

  /* plan bar block */
  .plan-block { flex: 1; min-width: 0; }
  .plan-bar-label {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    margin-bottom: 6px;
  }
  .plan-bar-label-left {
    font-family: var(--display);
    font-size: 10px;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: #4ec8e0;
  }
  .plan-bar-label-right {
    font-size: 11px;
    color: var(--text-muted);
  }
  .plan-bar-label-right strong { color: var(--text-bright); font-weight: 500; }
  .plan-track {
    height: 8px;
    background: rgba(255,255,255,0.06);
    border-radius: 4px;
    overflow: hidden;
  }
  .plan-fill {
    height: 100%;
    border-radius: 4px;
    transition: width 0.8s cubic-bezier(0.4,0,0.2,1), background 0.5s ease;
  }
  .plan-fill.c-green { background: linear-gradient(90deg,#0d9e6e,#10b981); box-shadow: 0 0 10px rgba(16,185,129,0.4); }
  .plan-fill.c-amber { background: linear-gradient(90deg,#d97706,#f59e0b); box-shadow: 0 0 10px rgba(245,158,11,0.4); }
  .plan-fill.c-red   { background: linear-gradient(90deg,#dc2626,#ef4444); box-shadow: 0 0 12px rgba(239,68,68,0.5); }

  /* reset + clock */
  .header-clock-block {
    flex-shrink: 0;
    text-align: right;
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 4px;
  }
  .clock {
    font-family: var(--display);
    font-size: 28px;
    font-weight: 700;
    color: var(--text-bright);
    letter-spacing: 3px;
    line-height: 1;
  }
  .reset-badge {
    font-size: 11px;
    color: var(--text-muted);
    white-space: nowrap;
  }
  .reset-badge strong { color: var(--cyan); font-weight: 600; }

  /* live indicator */
  .live-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--green);
    box-shadow: 0 0 10px var(--green);
    animation: pulse-dot 2s ease-in-out infinite;
    flex-shrink: 0;
    align-self: center;
  }
  @keyframes pulse-dot {
    0%,100% { opacity: 1; transform: scale(1); }
    50%      { opacity: 0.4; transform: scale(0.75); }
  }

  /* row 2 — aggregate stat tiles */
  .header-stats {
    max-width: 1400px;
    margin: 0 auto;
    display: flex;
    gap: 8px;
    padding: 0 0 14px;
    flex-wrap: wrap;
  }
  .stat-tile {
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 7px;
    padding: 7px 14px;
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 80px;
  }
  .stat-tile-label {
    font-family: var(--display);
    font-size: 10px;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: #4ec8e0;
  }
  .stat-tile-val {
    font-size: 16px;
    font-weight: 600;
    color: var(--text-bright);
    line-height: 1.1;
  }
  .stat-tile-val.green  { color: var(--green); }
  .stat-tile-val.cyan   { color: var(--cyan); }
  .stat-tile-val.amber  { color: var(--amber); }

  /* ── main layout ── */
  main {
    max-width: 1400px;
    margin: 0 auto;
    padding: 28px 24px 48px;
  }

  .sessions-grid {
    display: grid;
    gap: 20px;
    grid-template-columns: repeat(auto-fill, minmax(520px, 1fr));
  }

  /* ── session card ── */
  .session-card {
    background: var(--card);
    border: 1px solid var(--card-border);
    border-radius: 12px;
    backdrop-filter: blur(12px);
    overflow: hidden;
    transition: border-color 0.4s ease, box-shadow 0.4s ease;
    position: relative;
  }
  .session-card.state-ok     { border-left: 3px solid var(--green);  box-shadow: -4px 0 20px rgba(16,185,129,0.12); }
  .session-card.state-warn   { border-left: 3px solid var(--amber);  box-shadow: -4px 0 20px rgba(245,158,11,0.15); }
  .session-card.state-crit   { border-left: 3px solid var(--red);    box-shadow: -4px 0 20px rgba(239,68,68,0.2); animation: crit-pulse 2s ease-in-out infinite; }
  @keyframes crit-pulse {
    0%,100% { box-shadow: -4px 0 20px rgba(239,68,68,0.2); }
    50%      { box-shadow: -4px 0 28px rgba(239,68,68,0.4); }
  }

  .card-header {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 14px 18px 10px;
    border-bottom: 1px solid var(--card-border);
  }
  .card-indicator {
    width: 6px; height: 6px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .state-ok   .card-indicator { background: var(--green); box-shadow: 0 0 8px var(--green); }
  .state-warn .card-indicator { background: var(--amber); box-shadow: 0 0 8px var(--amber); animation: pulse-dot 1.5s ease-in-out infinite; }
  .state-crit .card-indicator { background: var(--red);   box-shadow: 0 0 8px var(--red);   animation: pulse-dot 0.8s ease-in-out infinite; }

  .card-title {
    font-family: var(--display);
    font-size: 14px;
    font-weight: 600;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    color: var(--text-bright);
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .card-pid {
    font-size: 10px;
    color: var(--text-dim);
    letter-spacing: 1px;
    flex-shrink: 0;
  }

  .card-body { padding: 16px 18px 14px; }
  .card-footer {
    display: flex;
    gap: 6px;
    padding: 10px 18px 14px;
    border-top: 1px solid var(--card-border);
  }
  .card-footer .stat-tile {
    flex: 1;
    min-width: 0;
  }

  /* ── bars ── */
  .bars-section { display: flex; flex-direction: column; gap: 10px; margin-bottom: 18px; }

  .bar-row {
    display: grid;
    grid-template-columns: 80px 1fr 90px;
    align-items: center;
    gap: 10px;
  }
  .bar-label {
    font-family: var(--display);
    font-size: 11px;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    color: var(--text);
    text-align: right;
    white-space: nowrap;
  }
  .bar-track {
    height: 6px;
    background: rgba(255,255,255,0.05);
    border-radius: 3px;
    overflow: visible;
    position: relative;
  }
  .bar-fill {
    height: 100%;
    border-radius: 3px;
    transition: width 0.6s cubic-bezier(0.4, 0, 0.2, 1),
                background 0.4s ease,
                box-shadow 0.4s ease;
    position: relative;
  }
  .bar-fill.c-green  { background: linear-gradient(90deg,#0d9e6e,#10b981); box-shadow: 0 0 10px rgba(16,185,129,0.5); }
  .bar-fill.c-amber  { background: linear-gradient(90deg,#d97706,#f59e0b); box-shadow: 0 0 10px rgba(245,158,11,0.5); }
  .bar-fill.c-red    { background: linear-gradient(90deg,#dc2626,#ef4444); box-shadow: 0 0 12px rgba(239,68,68,0.6); }
  .bar-fill.c-cyan   { background: linear-gradient(90deg,#0099bb,#00d4ff); box-shadow: 0 0 10px rgba(0,212,255,0.4); }
  .bar-fill.c-purple { background: linear-gradient(90deg,#5b21b6,#7c3aed); box-shadow: 0 0 10px rgba(124,58,237,0.5); }

  .bar-value {
    font-size: 11px;
    font-weight: 500;
    text-align: right;
    white-space: nowrap;
  }
  .bar-value.c-green  { color: var(--green); }
  .bar-value.c-amber  { color: var(--amber); }
  .bar-value.c-red    { color: var(--red); }
  .bar-value.c-cyan   { color: var(--cyan); }

  /* bigger plan bar in header */
  .plan-track { height: 5px; background: rgba(255,255,255,0.06); border-radius: 3px; overflow: hidden; }
  .plan-fill  {
    height: 100%;
    border-radius: 3px;
    transition: width 0.8s cubic-bezier(0.4,0,0.2,1), background 0.5s ease;
  }
  .plan-fill.c-green  { background: linear-gradient(90deg,#0d9e6e,#10b981); box-shadow: 0 0 8px rgba(16,185,129,0.4); }
  .plan-fill.c-amber  { background: linear-gradient(90deg,#d97706,#f59e0b); box-shadow: 0 0 8px rgba(245,158,11,0.4); }
  .plan-fill.c-red    { background: linear-gradient(90deg,#dc2626,#ef4444); box-shadow: 0 0 8px rgba(239,68,68,0.5); }

  .bar-sub {
    font-size: 10px;
    color: var(--text-muted);
    margin-top: 3px;
    text-align: right;
  }

  /* ── stat tables ── */
  .stats-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
  }
  .stat-block {
    background: rgba(255,255,255,0.02);
    border: 1px solid var(--card-border);
    border-radius: 8px;
    padding: 12px 14px;
  }
  .stat-block-title {
    font-family: var(--display);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: rgba(0,212,255,0.5);
    margin-bottom: 10px;
  }
  .stat-row {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 8px;
    padding: 3px 0;
    border-bottom: 1px solid rgba(255,255,255,0.03);
  }
  .stat-row:last-child { border-bottom: none; }
  .stat-key {
    font-size: 10px;
    color: var(--text);
    white-space: nowrap;
    flex-shrink: 0;
  }
  .stat-val {
    font-size: 11px;
    font-weight: 500;
    color: var(--text);
    text-align: right;
    white-space: nowrap;
  }
  .stat-sub {
    font-size: 10px;
    color: var(--text-muted);
    text-align: right;
    white-space: nowrap;
  }
  .cost-val { color: var(--green); font-weight: 600; }
  .hit-val  { color: var(--cyan); }

  /* ── warning banner ── */
  .warn-banner {
    margin-top: 12px;
    padding: 8px 14px;
    border-radius: 6px;
    font-family: var(--display);
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 2px;
    text-transform: uppercase;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .warn-banner.w-warn { background: var(--amber-dim); color: var(--amber); border: 1px solid rgba(245,158,11,0.2); }
  .warn-banner.w-crit { background: var(--red-dim);   color: var(--red);   border: 1px solid rgba(239,68,68,0.3); animation: crit-pulse 1.5s ease-in-out infinite; }

  /* cache miss cost banner */
  .cache-miss-banner {
    margin-top: 10px;
    padding: 10px 14px;
    border-radius: 6px;
    background: rgba(239,68,68,0.08);
    border: 1px solid rgba(239,68,68,0.25);
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
  }
  .cache-miss-label {
    font-family: var(--display);
    font-size: 10px;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    color: var(--red);
  }
  .cache-miss-costs {
    display: flex;
    gap: 14px;
    align-items: baseline;
    flex-shrink: 0;
  }
  .cache-miss-item { text-align: right; }
  .cache-miss-val  { font-size: 13px; font-weight: 600; color: var(--red); }
  .cache-miss-sub  { font-size: 10px; color: var(--text-muted); letter-spacing: 0.5px; }
  .cache-warm-val  { font-size: 13px; font-weight: 600; color: var(--green); }
  .penalty-val     { font-size: 13px; font-weight: 600; color: var(--amber); }

  /* ── empty / waiting state ── */
  .no-sessions {
    grid-column: 1 / -1;
    text-align: center;
    padding: 80px 20px;
  }
  .no-sessions-icon {
    font-size: 48px;
    margin-bottom: 20px;
    opacity: 0.2;
    animation: float 3s ease-in-out infinite;
  }
  @keyframes float {
    0%,100% { transform: translateY(0); }
    50%      { transform: translateY(-8px); }
  }
  .no-sessions h2 {
    font-family: var(--display);
    font-size: 18px;
    font-weight: 600;
    letter-spacing: 3px;
    text-transform: uppercase;
    color: var(--text-dim);
    margin-bottom: 8px;
  }
  .no-sessions p {
    font-size: 12px;
    color: var(--text-dim);
  }

  /* ── waiting card ── */
  .waiting-state {
    padding: 28px;
    text-align: center;
    color: var(--text-dim);
    font-size: 11px;
    letter-spacing: 1px;
  }
  .waiting-state .pulse-bar {
    height: 3px;
    background: linear-gradient(90deg, transparent, var(--cyan), transparent);
    border-radius: 2px;
    margin-bottom: 14px;
    animation: scan 2s linear infinite;
    background-size: 200% 100%;
  }
  @keyframes scan {
    0%   { background-position: -100% 0; }
    100% { background-position: 200% 0; }
  }

  /* ── scrollbar ── */
  ::-webkit-scrollbar { width: 4px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--text-dim); border-radius: 2px; }
  /* ── aggregate chips row ── */
  .agg-row {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 8px 0 2px;
    flex-wrap: wrap;
  }
  .agg-chip {
    display: flex;
    align-items: baseline;
    gap: 5px;
    background: rgba(255,255,255,0.03);
    border: 1px solid var(--card-border);
    border-radius: 5px;
    padding: 3px 9px;
  }
  .agg-chip-label {
    font-family: var(--display);
    font-size: 10px;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    color: #4ec8e0;
  }
  .agg-chip-val {
    font-size: 12px;
    font-weight: 500;
    color: var(--text-bright);
  }
  .agg-chip-val.green { color: var(--green); }
  .agg-chip-val.cyan  { color: var(--cyan); }
  .agg-sep { color: var(--text-dim); font-size: 10px; }
</style>
</head>
<body>

<header>
  <!-- row 1: logo · plan % · bar · clock -->
  <div class="header-top">
    <div class="logo">Claude<span>/</span>Monitor</div>
    <div class="plan-pct-big c-green" id="plan-pct-big">—%</div>
    <div class="plan-block">
      <div class="plan-bar-label">
        <span class="plan-bar-label-left">5h Window</span>
        <span class="plan-bar-label-right" id="plan-values">—</span>
      </div>
      <div class="plan-track"><div class="plan-fill c-green" id="plan-fill" style="width:0%"></div></div>
    </div>
    <div class="header-clock-block">
      <div class="clock" id="clock">—</div>
      <div class="reset-badge" id="reset-badge"></div>
    </div>
    <div class="live-dot"></div>
  </div>
  <!-- row 2: aggregate stat tiles -->
  <div class="header-stats" id="header-stats"></div>
</header>

<main>
  <div class="sessions-grid" id="sessions-grid"></div>
</main>

<script>
const fmt    = n => Number(n).toLocaleString();
const fmtPct = p => (p * 100).toFixed(1) + '%';
const fmtCost = c => '$' + Number(c).toFixed(4);

function barColor(pct, warn=0.80, crit=0.90) {
  return pct >= crit ? 'c-red' : pct >= warn ? 'c-amber' : 'c-green';
}
function cardState(pct) {
  return pct >= 0.90 ? 'state-crit' : pct >= 0.80 ? 'state-warn' : 'state-ok';
}
function fmtTime(sec) {
  if (sec == null || sec <= 0) return 'expired';
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  if (h > 0) return `${h}h ${m}m`;
  return m > 0 ? `${m}m ${s.toString().padStart(2,'0')}s` : `${s}s`;
}

function renderBar(label, pct, valueText, subText='', colorOverride=null) {
  const col = colorOverride || barColor(pct);
  return `
    <div class="bar-row">
      <span class="bar-label">${label}</span>
      <div class="bar-track">
        <div class="bar-fill ${col}" style="width:${Math.min(100,pct*100).toFixed(2)}%"></div>
      </div>
      <div>
        <div class="bar-value ${col}">${valueText}</div>
        ${subText ? `<div class="bar-sub">${subText}</div>` : ''}
      </div>
    </div>`;
}

function chip(label, val, cls='') {
  return `<div class="agg-chip"><span class="agg-chip-label">${label}</span><span class="agg-chip-val ${cls}">${val}</span></div>`;
}

function renderSession(s) {
  if (!s.has_data) {
    return `
      <div class="session-card state-ok" id="card-${s.pid}">
        <div class="card-header">
          <div class="card-indicator"></div>
          <div class="card-title">pts/${s.pts} · ${s.cwd}</div>
          <div class="card-pid">pid ${s.pid}</div>
        </div>
        <div class="waiting-state">
          <div class="pulse-bar"></div>
          Awaiting first assistant turn…
        </div>
      </div>`;
  }

  const st = s.stats;
  const state = cardState(st.usable_pct);

  const cacheColor = st.cache_expires_in == null ? 'c-cyan'
    : st.cache_expires_in < 60  ? 'c-red'
    : st.cache_expires_in < 120 ? 'c-amber' : 'c-cyan';
  const cacheVal = st.cache_expires_in == null ? 'no write'
    : st.cache_expires_in <= 0 ? 'EXPIRED'
    : fmtTime(st.cache_expires_in);
  const cachePct = st.cache_expires_in == null || st.cache_expires_in <= 0
    ? 0 : st.cache_expires_in / 300;

  const warn = st.usable_pct >= 0.90
    ? `<div class="warn-banner w-crit">⚠ compaction imminent</div>`
    : st.usable_pct >= 0.80
    ? `<div class="warn-banner w-warn">⚠ approaching context limit</div>` : '';

  // cache miss cost estimate
  const ctx = st.context_tokens;
  const coldCost = ctx / 1e6 * (3.00 + 3.75); // fresh input + re-write cache
  const warmCost = ctx / 1e6 * 0.30;           // cache read only
  const penalty  = coldCost - warmCost;
  const cacheMiss = (st.cache_expires_in !== null && st.cache_expires_in <= 0)
    ? `<div class="cache-miss-banner">
        <span class="cache-miss-label">⚡ Next prompt cost · cache expired</span>
        <div class="cache-miss-costs">
          <div class="cache-miss-item">
            <div class="cache-miss-val">${fmtCost(coldCost)}</div>
            <div class="cache-miss-sub">cold</div>
          </div>
          <div class="cache-miss-item">
            <div class="cache-warm-val">${fmtCost(warmCost)}</div>
            <div class="cache-miss-sub">if warm</div>
          </div>
          <div class="cache-miss-item">
            <div class="penalty-val">+${fmtCost(penalty)}</div>
            <div class="cache-miss-sub">penalty</div>
          </div>
        </div>
      </div>`
    : st.cache_expires_in !== null && st.cache_expires_in < 120
    ? `<div class="cache-miss-banner" style="background:rgba(245,158,11,0.06);border-color:rgba(245,158,11,0.2)">
        <span class="cache-miss-label" style="color:var(--amber)">⚡ Cache expiring · next prompt cost</span>
        <div class="cache-miss-costs">
          <div class="cache-miss-item">
            <div class="penalty-val">${fmtCost(coldCost)}</div>
            <div class="cache-miss-sub">if cold</div>
          </div>
          <div class="cache-miss-item">
            <div class="cache-warm-val">${fmtCost(warmCost)}</div>
            <div class="cache-miss-sub">if warm</div>
          </div>
        </div>
      </div>`
    : '';

  // per-session footer chips
  const chips = [
    tile('Turns',   st.turns),
    tile('Output',  fmt(st.total_output)),
    tile('Hit',     fmtPct(st.cache_hit), 'cyan'),
    tile('Cost',    fmtCost(st.cost),     'green'),
  ].join('');

  return `
    <div class="session-card ${state}" id="card-${s.pid}">
      <div class="card-header">
        <div class="card-indicator"></div>
        <div class="card-title">pts/${s.pts} · ${s.cwd}</div>
        <div class="card-pid">pid ${s.pid}</div>
      </div>
      <div class="card-body">
        <div class="bars-section">
          ${renderBar('Context', st.fill_pct,   fmtPct(st.fill_pct),   fmt(st.context_tokens)+' / 200K')}
          ${renderBar('Usable',  st.usable_pct, fmtPct(st.usable_pct), fmt(st.usable_left)+' left')}
          ${renderBar('Cache',   cachePct,       cacheVal,              '5 min TTL', cacheColor)}
        </div>
        <div class="stat-block">
          <div class="stat-block-title">Last Turn</div>
          <div class="stat-row">
            <span class="stat-key">fresh input</span>
            <div><div class="stat-val">${fmt(st.last_input)}</div><div class="stat-sub">${fmtCost(st.last_input/1e6*3.00)}</div></div>
          </div>
          <div class="stat-row">
            <span class="stat-key">cache read</span>
            <div><div class="stat-val">${fmt(st.last_cr)}</div><div class="stat-sub">${fmtCost(st.last_cr/1e6*0.30)}</div></div>
          </div>
          <div class="stat-row">
            <span class="stat-key">cache write</span>
            <div><div class="stat-val">${fmt(st.last_cw)}</div><div class="stat-sub">${fmtCost(st.last_cw/1e6*3.75)}</div></div>
          </div>
          <div class="stat-row">
            <span class="stat-key">output</span>
            <div><div class="stat-val">${fmt(st.last_output)}</div><div class="stat-sub">${fmtCost(st.last_output/1e6*15.00)}</div></div>
          </div>
        </div>
        ${cacheMiss}
        ${warn}
      </div>
      <div class="card-footer">${chips}</div>
    </div>`;
}

function tile(label, val, cls='') {
  return `<div class="stat-tile"><span class="stat-tile-label">${label}</span><span class="stat-tile-val ${cls}">${val}</span></div>`;
}

function applyData(data) {
  document.getElementById('clock').textContent = data.time;

  // plan bar
  const plan = data.plan;
  const planCol = barColor(plan.pct, 0.70, 0.90);
  const fill = document.getElementById('plan-fill');
  fill.style.width = Math.min(100, plan.pct * 100).toFixed(2) + '%';
  fill.className = `plan-fill ${planCol}`;

  const pctBig = document.getElementById('plan-pct-big');
  pctBig.textContent = (plan.pct * 100).toFixed(1) + '%';
  pctBig.className = `plan-pct-big ${planCol}`;

  document.getElementById('plan-values').innerHTML =
    `<strong>${fmt(plan.used)}</strong> / ${fmt(plan.limit)} output tokens`;

  // reset countdown
  if (plan.reset_in != null && plan.reset_in > 0) {
    document.getElementById('reset-badge').innerHTML =
      `resets in <strong>${fmtTime(plan.reset_in)}</strong>`;
  } else {
    document.getElementById('reset-badge').textContent =
      plan.reset_in === null ? '' : 'window expired';
  }

  // aggregate stat tiles
  const agg = data.agg || {};
  const costCls = (agg.cost || 0) > 5 ? 'amber' : 'green';
  document.getElementById('header-stats').innerHTML = [
    tile('Sessions',   data.sessions.length),
    tile('Turns',      fmt(agg.turns    || 0)),
    tile('Input tok',  fmt(agg.input    || 0)),
    tile('Output tok', fmt(agg.output   || 0)),
    tile('Cache hit',  fmtPct(agg.cache_hit || 0), 'cyan'),
    tile('Total cost', fmtCost(agg.cost || 0), costCls),
  ].join('');

  // sessions grid
  const grid = document.getElementById('sessions-grid');

  if (!data.sessions.length) {
    grid.innerHTML = `
      <div class="no-sessions">
        <div class="no-sessions-icon">◎</div>
        <h2>No Active Sessions</h2>
        <p>Start a claude session in your terminal</p>
      </div>`;
    return;
  }

  // remove initial placeholder / stale cards
  const incoming = new Set(data.sessions.map(s => s.pid));
  // clear any non-card children (placeholder divs)
  [...grid.children].forEach(el => {
    if (!el.classList.contains('session-card')) el.remove();
  });
  // remove stale session cards
  [...grid.querySelectorAll('.session-card')].forEach(el => {
    const pid = parseInt(el.id.replace('card-', ''));
    if (!incoming.has(pid)) el.remove();
  });
  // add/update
  for (const s of data.sessions) {
    const html = renderSession(s);
    const existing = document.getElementById(`card-${s.pid}`);
    if (existing) existing.outerHTML = html;
    else grid.insertAdjacentHTML('beforeend', html);
  }
}

function connect() {
  const src = new EventSource('/stream');
  const dot = document.querySelector('.live-dot');
  src.onmessage = e => { try { applyData(JSON.parse(e.data)); } catch {} };
  src.onerror   = () => {
    src.close(); setTimeout(connect, 3000);
    dot.style.cssText = 'background:var(--red);box-shadow:0 0 8px var(--red)';
  };
  src.onopen    = () => {
    dot.style.cssText = 'background:var(--green);box-shadow:0 0 8px var(--green)';
  };
}

fetch('/data').then(r => r.json()).then(applyData).finally(connect);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    from fastapi.responses import Response
    return Response(content=HTML, media_type="text/html",
                    headers={"Cache-Control": "no-store"})


@app.get("/data")
async def data():
    return build_snapshot()


@app.get("/stream")
async def stream():
    queue: asyncio.Queue = asyncio.Queue(maxsize=20)
    _client_queues.add(queue)

    async def generator():
        # Send current snapshot immediately on connect
        try:
            yield f"data: {json.dumps(build_snapshot())}\n\n"
        except Exception:
            pass
        try:
            while True:
                msg = await queue.get()
                yield msg
        finally:
            _client_queues.discard(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


PIDFILE = Path("/tmp/token-monitor-web.pid")

def acquire_pidfile():
    """Write pidfile, or exit if another live instance holds it."""
    if PIDFILE.exists():
        try:
            existing = int(PIDFILE.read_text().strip())
            # Check if that PID is actually running
            os.kill(existing, 0)
            print(f"Already running as PID {existing}, exiting.", flush=True)
            raise SystemExit(1)
        except (ValueError, ProcessLookupError):
            pass  # stale pidfile — overwrite it
    PIDFILE.write_text(str(os.getpid()))

def release_pidfile():
    try:
        if PIDFILE.exists() and int(PIDFILE.read_text().strip()) == os.getpid():
            PIDFILE.unlink()
    except Exception:
        pass

if __name__ == "__main__":
    acquire_pidfile()
    try:
        print("Claude Token Monitor")
        print("  Local:     http://localhost:8765")
        print("  Tailscale: http://home-server.tail7bde5d.ts.net:8765")
        print()
        uvicorn.run("token-monitor-web:app", host="0.0.0.0", port=8765, log_level="warning")
    finally:
        release_pidfile()
