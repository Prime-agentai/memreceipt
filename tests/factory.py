"""Build throwaway Claude config dirs, memory stores and transcripts for tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from memreceipt.store import project_key

FIXTURES = Path(__file__).parent / "fixtures"


def git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path.resolve()


def store_for(cfg: Path, root: Path) -> Path:
    s = cfg / "projects" / project_key(root) / "memory"
    s.mkdir(parents=True, exist_ok=True)
    return s


def write_index(store: Path, n: int, files: bool = True, pad: int = 0, start: int = 1) -> list[str]:
    lines = []
    for i in range(start, start + n):
        lines.append(f"- [fact {i}](fact-{i:03d}.md) — fact number {i}" + ("x" * pad))
        if files:
            (store / f"fact-{i:03d}.md").write_text(f"---\nname: fact-{i:03d}\n---\nbody {i}\n", encoding="utf-8")
    (store / "MEMORY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lines


def rec_instructions(ts: str, cwd: Path, files: list[tuple[str, str, str]]) -> dict[str, Any]:
    return {"type": "attachment", "timestamp": ts, "cwd": str(cwd), "sessionId": "s",
            "attachment": {"type": "instructions",
                           "files": [{"path": p, "type": t, "content": c} for t, p, c in files]}}


def rec_tool(ts: str, name: str, inp: dict[str, Any]) -> dict[str, Any]:
    return {"type": "assistant", "timestamp": ts,
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": name, "input": inp}]}}


def write_session(cfg: Path, cwd: Path, sid: str, records: list[dict[str, Any]]) -> Path:
    d = cfg / "projects" / project_key(cwd)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    with f.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "api-request-blob", "body": '"tool_use" "instructions"'}) + "\n")
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return f
