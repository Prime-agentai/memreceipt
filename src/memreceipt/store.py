"""Resolve which auto-memory store a working directory maps to, and why.

Two different keys are in play, and mixing them up is how facts end up in a store nobody reads:

* **memory** is keyed by the git repository (all worktrees and subdirectories share one store;
  outside a repo, the directory itself), per the Claude Code memory docs, and
* **transcripts** are keyed by the exact working directory the session started in.

So a session started in ``repo/sub`` writes its transcript to ``projects/-repo-sub/`` but loads
``projects/-repo/memory/MEMORY.md``. ``autoMemoryDirectory`` in any settings scope overrides the
store location, and ``autoMemoryEnabled: false`` or ``CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`` turns
auto-memory off.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_UNSAFE = re.compile(r"[^A-Za-z0-9]")
#: Keys longer than this are shortened by Claude Code with a hash suffix (not reproduced here).
MAX_KEY = 200


def config_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base).expanduser() if base else Path.home() / ".claude"


def project_key(path: Path | str) -> str:
    """Directory name Claude Code uses under ``projects/`` for an absolute path."""
    return _UNSAFE.sub("-", str(path))


def find_project_dir(projects: Path, key: str) -> Path:
    """The ``projects/<key>`` dir; for over-long keys, the existing dir sharing the prefix."""
    direct = projects / key
    if len(key) <= MAX_KEY or direct.exists():
        return direct
    prefix = key[:MAX_KEY]
    matches = sorted(projects.glob(prefix + "*")) if projects.is_dir() else []
    return matches[0] if matches else direct


def repo_root(cwd: Path) -> tuple[Path, str]:
    """(root, how): the main worktree's root for a git checkout, else ``cwd`` itself."""
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--path-format=absolute", "--git-common-dir", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return cwd, "cwd (git unavailable)"
    if out.returncode != 0:
        return cwd, "cwd (not a git repository)"
    parts = out.stdout.strip().splitlines()
    if len(parts) < 2:
        return cwd, "cwd (git gave no toplevel)"
    common, top = Path(parts[0]), Path(parts[1])
    if common.name == ".git" and common.parent != top:
        return common.parent, "git (linked worktree, main checkout)"
    return top, "git"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def managed_settings_path() -> Path:
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    if sys.platform.startswith("win"):
        return Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "ClaudeCode" / "managed-settings.json"
    return Path("/etc/claude-code/managed-settings.json")


def settings_files(root: Path, cwd: Path, cfg: Path) -> list[Path]:
    """Settings files, highest precedence first: managed, local, project, user."""
    files = [managed_settings_path()]
    for base in dict.fromkeys([cwd, root]):
        files.append(base / ".claude" / "settings.local.json")
    for base in dict.fromkeys([cwd, root]):
        files.append(base / ".claude" / "settings.json")
    files.append(cfg / "settings.json")
    return files


def _first_setting(files: list[Path], key: str) -> tuple[Any, Path | None]:
    for f in files:
        data = _read_json(f)
        if data is not None and key in data:
            return data[key], f
    return None, None


@dataclass(frozen=True)
class Resolution:
    cwd: Path
    root: Path
    root_how: str
    config: Path
    store: Path
    store_source: str  # "default" or the settings file that set autoMemoryDirectory
    enabled: bool
    disabled_by: str | None
    transcripts: Path  # where this cwd's session transcripts are written

    @property
    def key_note(self) -> str | None:
        if self.root != self.cwd:
            return (f"transcripts for this directory go to {self.transcripts.name}/ but memory loads from "
                    f"the repository root's store")
        return None


def resolve(cwd: Path | str | None = None, cfg: Path | None = None) -> Resolution:
    here = Path(cwd or os.getcwd()).expanduser().resolve()
    cfg = cfg or config_dir()
    root, how = repo_root(here)
    files = settings_files(root, here, cfg)
    projects = cfg / "projects"

    custom, src = _first_setting(files, "autoMemoryDirectory")
    if isinstance(custom, str) and custom.strip():
        store = Path(custom).expanduser()
        if not store.is_absolute():
            store = (root / store).resolve()
        source = str(src)
    else:
        store = find_project_dir(projects, project_key(root)) / "memory"
        source = "default"

    enabled, disabled_by = True, None
    if os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "").strip().lower() in ("1", "true", "yes"):
        enabled, disabled_by = False, "CLAUDE_CODE_DISABLE_AUTO_MEMORY"
    else:
        flag, fsrc = _first_setting(files, "autoMemoryEnabled")
        if flag is False:
            enabled, disabled_by = False, f"autoMemoryEnabled=false in {fsrc}"

    return Resolution(
        cwd=here, root=root, root_how=how, config=cfg, store=store, store_source=source,
        enabled=enabled, disabled_by=disabled_by,
        transcripts=find_project_dir(projects, project_key(here)),
    )


def all_stores(cfg: Path | None = None) -> list[Path]:
    """Every default-location memory store on this machine."""
    projects = (cfg or config_dir()) / "projects"
    if not projects.is_dir():
        return []
    return sorted(p for p in projects.glob("*/memory") if p.is_dir())
