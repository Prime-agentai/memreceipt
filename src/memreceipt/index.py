"""Parse a MEMORY.md index and compute exactly which part of it Claude Code loads at session start.

The cut rule was measured against Claude Code 2.1.323 (see README, "How the limits were measured"):

* at most ``LINE_LIMIT`` (200) lines are loaded, and
* whole lines only, while the loaded text (lines joined by ``\\n``) is at most ``CHAR_LIMIT``
  (25,000) characters. Characters, not bytes: an index of 117 lines that is 24,812 characters but
  25,046 UTF-8 bytes loads in full; adding line 118 (25,025 characters) does not.

Characters are counted as UTF-16 code units, which is how a JavaScript runtime measures string
length. For text without astral-plane characters (most emoji are astral) this equals ``len(str)``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

LINE_LIMIT = 200
CHAR_LIMIT = 25_000
#: Claude Code's own advice for an index entry ("one line under ~200 chars").
ENTRY_SOFT_LIMIT = 200
#: Fraction of either limit at which an index is reported as "near the limit".
NEAR_FRACTION = 0.8

INDEX_NAME = "MEMORY.md"

_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def u16len(s: str) -> int:
    """String length as a JavaScript runtime counts it (UTF-16 code units)."""
    return len(s) + sum(1 for ch in s if ord(ch) > 0xFFFF)


def split_lines(text: str) -> list[str]:
    """Lines as Claude Code counts them: a trailing newline does not add an empty last line."""
    if text == "":
        return []
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return [ln[:-1] if ln.endswith("\r") else ln for ln in lines]


def loaded_line_count(lines: list[str], line_limit: int = LINE_LIMIT, char_limit: int = CHAR_LIMIT) -> int:
    """How many leading lines fit under both limits (whole lines only)."""
    total = 0
    for i, line in enumerate(lines):
        if i >= line_limit:
            return i
        total += u16len(line) + (1 if i else 0)
        if total > char_limit:
            return i
    return len(lines)


@dataclass(frozen=True)
class Entry:
    """One index line that links to a topic file."""

    line: int  # 1-based
    title: str
    target: str  # as written in the link
    text: str  # the whole line
    loaded: bool
    exists: bool

    @property
    def target_name(self) -> str:
        return Path(self.target.split("#", 1)[0]).name


@dataclass
class IndexReport:
    """Everything memreceipt knows about one memory store, from disk alone."""

    store: Path
    exists: bool
    total_lines: int = 0
    total_chars: int = 0
    loaded_lines: int = 0
    loaded_chars: int = 0
    entries: list[Entry] = field(default_factory=list)
    topic_files: list[str] = field(default_factory=list)  # *.md in the store, minus MEMORY.md
    long_lines: list[int] = field(default_factory=list)  # 1-based lines over ENTRY_SOFT_LIMIT

    @property
    def index_path(self) -> Path:
        return self.store / INDEX_NAME

    @property
    def truncated(self) -> bool:
        return self.loaded_lines < self.total_lines

    @property
    def cut_lines(self) -> int:
        return self.total_lines - self.loaded_lines

    @property
    def cut_entries(self) -> list[Entry]:
        return [e for e in self.entries if not e.loaded]

    @property
    def dangling(self) -> list[Entry]:
        return [e for e in self.entries if not e.exists]

    @property
    def orphans(self) -> list[str]:
        """Topic files that no index line links to: Claude has no pointer to them at startup."""
        linked = {e.target_name for e in self.entries}
        return [t for t in self.topic_files if t not in linked]

    @property
    def line_fraction(self) -> float:
        return self.total_lines / LINE_LIMIT

    @property
    def char_fraction(self) -> float:
        return self.total_chars / CHAR_LIMIT

    @property
    def near_limit(self) -> bool:
        return not self.truncated and max(self.line_fraction, self.char_fraction) >= NEAR_FRACTION

    @property
    def binding_limit(self) -> str:
        """Which limit cut (or would cut first): 'lines' or 'chars'."""
        return "lines" if self.line_fraction >= self.char_fraction else "chars"

    @property
    def status(self) -> str:
        if not self.exists:
            return "missing"
        if self.truncated:
            return "truncated"
        if self.near_limit:
            return "near-limit"
        return "ok"


def parse_entries(lines: list[str], loaded: int, store: Path) -> list[Entry]:
    out: list[Entry] = []
    for i, line in enumerate(lines):
        m = _LINK.search(line)
        if not m:
            continue
        target = m.group(2)
        if "://" in target or target.startswith("#"):
            continue
        local = target.split("#", 1)[0]
        out.append(Entry(
            line=i + 1, title=m.group(1).strip(), target=target, text=line,
            loaded=i < loaded, exists=(store / local).is_file(),
        ))
    return out


def analyze_text(text: str, store: Path, topic_files: list[str] | None = None) -> IndexReport:
    lines = split_lines(text)
    n = loaded_line_count(lines)
    rep = IndexReport(
        store=store, exists=True,
        total_lines=len(lines), total_chars=u16len("\n".join(lines)),
        loaded_lines=n, loaded_chars=u16len("\n".join(lines[:n])),
        entries=parse_entries(lines, n, store),
        topic_files=sorted(topic_files or []),
        long_lines=[i + 1 for i, ln in enumerate(lines) if u16len(ln) > ENTRY_SOFT_LIMIT],
    )
    return rep


def list_topic_files(store: Path) -> list[str]:
    if not store.is_dir():
        return []
    return sorted(p.name for p in store.glob("*.md") if p.is_file() and p.name != INDEX_NAME)


def analyze_store(store: Path) -> IndexReport:
    index = store / INDEX_NAME
    topics = list_topic_files(store)
    try:
        text = index.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return IndexReport(store=store, exists=False, topic_files=topics)
    return analyze_text(text, store, topics)
