"""The SessionStart hook: a one-line load receipt for the user, and the missing pointers for Claude.

Claude Code already appends a model-facing warning when it truncates MEMORY.md, but that warning
names only the first line that was cut, and nothing at all reaches the user. This hook:

* shows the user a one-line receipt (``systemMessage``) on every session start, and
* when entries were cut, gives Claude their titles and file names (``additionalContext``) so a
  cut memory is still one Read away instead of silently unknown.

It must never break a session: any failure prints nothing and exits 0.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .index import CHAR_LIMIT, LINE_LIMIT, IndexReport, analyze_store
from .store import Resolution, resolve

#: Budget for the list of cut entries given to Claude, in characters.
CONTEXT_BUDGET = 6000
#: Each cut index line is passed through up to this many characters.
ITEM_CHARS = 220


def _k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def receipt_line(res: Resolution, rep: IndexReport) -> str:
    """The user-facing one-liner."""
    if not res.enabled:
        return f"memreceipt: auto-memory is OFF ({res.disabled_by}); no memory index was loaded"
    if not rep.exists:
        n = len(rep.topic_files)
        extra = f", but {n} topic file(s) sit in the store with no index" if n else ""
        return f"memreceipt: no MEMORY.md at {rep.store}{extra}"
    size = (f"{rep.total_lines}/{LINE_LIMIT} lines, {_k(rep.total_chars)}/{_k(CHAR_LIMIT)} chars, "
            f"{len(rep.entries)} entries")
    problems = []
    if rep.dangling:
        problems.append(f"{len(rep.dangling)} dangling link(s)")
    if rep.orphans:
        problems.append(f"{len(rep.orphans)} unindexed topic file(s)")
    tail = f"; {', '.join(problems)}" if problems else ""
    if rep.truncated:
        cut = rep.cut_entries
        return (f"memreceipt: MEMORY.md TRUNCATED by the {rep.binding_limit} limit: lines "
                f"{rep.loaded_lines + 1}-{rep.total_lines} ({len(cut)} entries) were NOT loaded "
                f"[{size}{tail}]. Run `memreceipt status`.")
    if rep.near_limit:
        pct = round(100 * max(rep.line_fraction, rep.char_fraction))
        return f"memreceipt: MEMORY.md loaded whole but at {pct}% of the {rep.binding_limit} limit [{size}{tail}]"
    return f"memreceipt: MEMORY.md loaded whole [{size}{tail}]"


def context_text(res: Resolution, rep: IndexReport, budget: int = CONTEXT_BUDGET) -> str:
    """What Claude needs to know that Claude Code did not tell it. Empty when there is nothing."""
    if not res.enabled or not rep.exists:
        return ""
    parts: list[str] = []
    if rep.truncated:
        cut = rep.cut_entries
        head = (f"memreceipt: your auto-memory index ({rep.index_path}) was cut after line {rep.loaded_lines}; "
                f"{len(cut)} indexed memories below it were NOT in your context. These are their index lines. "
                f"Before saying you have no memory of something, check them, and Read the topic file from "
                f"{rep.store}/ when you need the detail:")
        lines: list[str] = []
        used = len(head)
        for e in cut:
            text = e.text.strip()
            text = text[:ITEM_CHARS - 3] + "..." if len(text) > ITEM_CHARS else text
            item = f"L{e.line}: {text}" + ("" if e.exists else " (topic file missing on disk)")
            if used + len(item) + 1 > budget:
                lines.append(f"- ... and {len(cut) - len(lines)} more; list them with `memreceipt status --cut`")
                break
            lines.append(item)
            used += len(item) + 1
        parts.append("\n".join([head, *lines]))
    if rep.orphans:
        names = ", ".join(rep.orphans[:20]) + (" ..." if len(rep.orphans) > 20 else "")
        parts.append(f"memreceipt: {len(rep.orphans)} topic file(s) in {rep.store} have no index line, so "
                     f"nothing points to them: {names}")
    return "\n\n".join(parts)


def build_output(payload: dict[str, Any], always: bool = True, cfg: Path | None = None) -> dict[str, Any]:
    cwd = payload.get("cwd")
    res = resolve(cwd if isinstance(cwd, str) and cwd else None, cfg)
    rep = analyze_store(res.store)
    out: dict[str, Any] = {}
    problem = (not res.enabled) or rep.status != "ok" or bool(rep.dangling or rep.orphans)
    if always or problem:
        out["systemMessage"] = receipt_line(res, rep)
    ctx = context_text(res, rep)
    if ctx:
        out["hookSpecificOutput"] = {"hookEventName": "SessionStart", "additionalContext": ctx}
    return out


def run(stdin_text: str, always: bool = True, cfg: Path | None = None) -> str:
    """Hook entrypoint; returns the JSON to print (possibly empty). Never raises."""
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        out = build_output(payload, always=always, cfg=cfg)
        return json.dumps(out) if out else ""
    except Exception as e:  # noqa: BLE001 - a receipt must never take a session down
        print(f"memreceipt hook: {type(e).__name__}: {e}", file=sys.stderr)
        return ""
