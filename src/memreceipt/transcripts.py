"""Read Claude Code session transcripts for what auto-memory actually delivered and what was used.

Two record kinds matter (observed on Claude Code 2.1.323):

* ``{"type": "attachment", "attachment": {"type": "instructions", "files": [...]}}``: the files
  injected at session start. The auto-memory index appears as ``{"type": "AutoMem", "path": ...,
  "content": ...}``, and ``content`` is exactly what the model saw, including the
  ``> WARNING: MEMORY.md is ... Only part of it was loaded: N of M lines were cut off, starting at
  line K`` note Claude Code appends when it truncates.
* assistant ``tool_use`` blocks: ``Read``/``Write``/``Edit`` with a ``file_path`` inside the store,
  or a ``Bash`` command that names a file in it, are reads and writes of individual memories.

Parsing is defensive: unknown shapes are skipped, never fatal.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .index import INDEX_NAME, split_lines, u16len

_WARN_SPLIT = "\n\n> WARNING:"
_CUT = re.compile(r"(\d+) of (\d+) lines were cut off")
_START = re.compile(r"starting at line (\d+)")
_TS = re.compile(r'"timestamp":\s*"([^"]+)"')
_SKIP_PREFIX = '{"type":"api-request'

READ_TOOLS = {"Read", "NotebookRead"}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


@dataclass(frozen=True)
class Delivery:
    """One injection of MEMORY.md into a session's context."""

    ts: str
    path: str
    body: str  # the index text as delivered, warning stripped
    warning: str | None
    cut_lines: int | None
    total_lines: int | None
    cut_start: int | None

    @property
    def lines(self) -> int:
        return len(split_lines(self.body))

    @property
    def chars(self) -> int:
        return u16len(self.body)

    @property
    def truncated(self) -> bool:
        return self.warning is not None and bool(self.cut_lines)


def parse_delivery(ts: str, path: str, content: str) -> Delivery:
    body, warning = content, None
    i = content.find(_WARN_SPLIT)
    if i >= 0:
        body, warning = content[:i], content[i + 2:].strip()
    cut = total = start = None
    if warning:
        m = _CUT.search(warning)
        if m:
            cut, total = int(m.group(1)), int(m.group(2))
        s = _START.search(warning)
        if s:
            start = int(s.group(1))
    return Delivery(ts=ts, path=path, body=body, warning=warning, cut_lines=cut, total_lines=total, cut_start=start)


@dataclass
class Session:
    id: str
    file: Path
    cwd: str | None = None
    started: str | None = None
    ended: str | None = None
    instructions_records: int = 0
    deliveries: list[Delivery] = field(default_factory=list)
    reads: Counter[str] = field(default_factory=Counter)  # memory file name -> tool calls
    writes: Counter[str] = field(default_factory=Counter)

    @property
    def memory_paths(self) -> set[str]:
        return {d.path for d in self.deliveries}


def _memory_name(path: str, store: str) -> str | None:
    p = path.replace("\\", "/")
    if p.startswith(store + "/"):
        rest = p[len(store) + 1:]
        if "/" not in rest and rest.endswith(".md"):
            return rest
    return None


def _bash_names(cmd: str, store: str, names: set[str]) -> set[str]:
    """Memory files a shell command names, either by full path or by bare name after a cd."""
    hit: set[str] = set()
    if store in cmd:
        for m in re.finditer(re.escape(store) + r"/([^\s'\"/;|&)]+\.md)", cmd):
            hit.add(m.group(1))
        if "memory" in cmd:
            for n in names:
                if re.search(r"(^|[\s'\"/])" + re.escape(n) + r"($|[\s'\";|&)])", cmd):
                    hit.add(n)
    return hit


def _tool_uses(rec: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return
    content = msg.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            inp = block.get("input")
            yield str(block.get("name", "")), inp if isinstance(inp, dict) else {}


def _scan_file(path: Path, sess: Session, store: str, names: set[str], deliveries: bool) -> None:
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            if line.startswith(_SKIP_PREFIX):  # raw API request copies: large, and never what we want
                continue
            t = _TS.search(line)
            if t:
                ts_raw = t.group(1)
                if sess.started is None or ts_raw < sess.started:
                    sess.started = ts_raw
                if sess.ended is None or ts_raw > sess.ended:
                    sess.ended = ts_raw
            want_instr = deliveries and '"instructions"' in line
            want_tool = '"tool_use"' in line
            want_meta = sess.cwd is None and '"cwd"' in line
            if not (want_instr or want_tool or want_meta):
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            ts = rec.get("timestamp")
            if sess.cwd is None and isinstance(rec.get("cwd"), str):
                sess.cwd = rec["cwd"]
            att = rec.get("attachment")
            if want_instr and isinstance(att, dict) and att.get("type") == "instructions":
                sess.instructions_records += 1
                for f in att.get("files") or []:
                    if isinstance(f, dict) and f.get("type") == "AutoMem" and isinstance(f.get("content"), str):
                        sess.deliveries.append(parse_delivery(str(ts or ""), str(f.get("path", "")), f["content"]))
            if want_tool and rec.get("type") == "assistant":
                for name, inp in _tool_uses(rec):
                    if name in READ_TOOLS or name in WRITE_TOOLS:
                        fp = inp.get("file_path") or inp.get("notebook_path")
                        n = _memory_name(str(fp), store) if isinstance(fp, str) else None
                        if n:
                            (sess.reads if name in READ_TOOLS else sess.writes)[n] += 1
                    elif name == "Bash" and isinstance(inp.get("command"), str):
                        for n in _bash_names(inp["command"], store, names):
                            sess.reads[n] += 1


def load_session(path: Path, store: Path, names: set[str] | None = None) -> Session:
    """Parse one transcript plus its subagent transcripts (reads only) into a Session."""
    s = str(store).rstrip("/")
    nm = names or set()
    sess = Session(id=path.stem, file=path)
    _scan_file(path, sess, s, nm, deliveries=True)
    sub = path.with_suffix("") / "subagents"
    if sub.is_dir():
        for f in sorted(sub.glob("*.jsonl")):
            _scan_file(f, sess, s, nm, deliveries=False)
    return sess


def candidate_dirs(projects: Path, root_key: str, every: bool) -> list[Path]:
    """Transcript dirs worth scanning: those whose key starts with the repo root's key."""
    if not projects.is_dir():
        return []
    return sorted(d for d in projects.iterdir() if d.is_dir() and (every or d.name.startswith(root_key)))


def _under(cwd: str | None, root: Path) -> bool:
    if not cwd:
        return False
    try:
        Path(cwd).relative_to(root)
    except ValueError:
        return False
    return True


def sessions_for(store: Path, root: Path, projects: Path, root_key: str, every: bool = False,
                 names: set[str] | None = None) -> list[Session]:
    """Sessions that loaded this store, or that ran inside this repository, oldest first."""
    index = str(store / INDEX_NAME)
    out: list[Session] = []
    for d in candidate_dirs(projects, root_key, every):
        for f in sorted(d.glob("*.jsonl")):
            sess = load_session(f, store, names)
            if index in sess.memory_paths or _under(sess.cwd, root) or sess.reads or sess.writes:
                out.append(sess)
    out.sort(key=lambda s: s.started or "")
    return out
