#!/usr/bin/env python3
"""Build a self-contained demo: a git repo plus a Claude config dir whose memory index is over the limit.

    python3 scripts/make-demo.py /tmp/memreceipt-demo
    memreceipt status --cwd /tmp/memreceipt-demo/repo --config-dir /tmp/memreceipt-demo/claude

Nothing outside the target directory is touched.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

TOPICS = ["deploy", "testing", "style", "infra", "release", "oncall", "api", "db", "auth", "ci"]


def key(p: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(p))


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    base = Path(sys.argv[1]).resolve()
    repo, cfg = base / "repo", base / "claude"
    (repo / "services" / "api").mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    store = cfg / "projects" / key(repo) / "memory"
    store.mkdir(parents=True, exist_ok=True)

    lines = []
    for i in range(1, 216):
        t = TOPICS[i % len(TOPICS)]
        name = f"{t}-rule-{i:03d}.md"
        lines.append(f"- [{t} rule {i}]({name}) — {t}: rule number {i} learned from a correction")
        if i != 42:  # one dangling link
            (store / name).write_text(f"---\nname: {t}-rule-{i:03d}\n---\nRule {i} about {t}.\n", encoding="utf-8")
    (store / "MEMORY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (store / "forgotten-note.md").write_text("---\nname: forgotten-note\n---\nNo index line.\n", encoding="utf-8")

    def write(cwd: Path, sid: str, ts: str, content: str | None, reads: list[str]) -> None:
        d = cfg / "projects" / key(cwd)
        d.mkdir(parents=True, exist_ok=True)
        recs: list[dict[str, object]] = []
        files = [{"path": str(repo / "CLAUDE.md"), "type": "Project", "content": "# demo"}]
        if content is not None:
            files.append({"path": str(store / "MEMORY.md"), "type": "AutoMem", "content": content})
        recs.append({"type": "attachment", "timestamp": ts, "cwd": str(cwd), "sessionId": sid,
                     "attachment": {"type": "instructions", "files": files}})
        for n, r in enumerate(reads):
            recs.append({"type": "assistant", "timestamp": ts[:-3] + f"{n + 10}Z", "message": {"role": "assistant",
                         "content": [{"type": "tool_use", "id": f"t{n}", "name": "Read",
                                      "input": {"file_path": str(store / r)}}]}})
        (d / f"{sid}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in recs), encoding="utf-8")

    first = "\n".join(lines[:150])
    cut = "\n".join(lines[:200]) + (
        f"\n\n> WARNING: MEMORY.md is 215 lines (limit: 200). Only part of it was loaded: 15 of 215 lines were "
        f"cut off, starting at line 201 (\"{lines[200]}\"). Keep index entries to one line under ~200 chars; "
        f"move detail into topic files.")
    write(repo, "3f2a91c4-6b1e-4c0a-9d51-0d6e2b7a1101", "2026-11-10T09:00:00Z", first,
          ["deploy-rule-010.md", "testing-rule-001.md"])
    write(repo / "services" / "api", "8c47d0e2-91f3-4b6a-a2c8-5e1f9b3d2202", "2026-11-12T09:00:00Z", cut,
          ["deploy-rule-010.md"])
    write(repo, "d15e6a7b-2c84-4f19-b7e0-a93c4f8e3303", "2026-11-14T09:00:00Z", None, [])
    print(f"demo repo:   {repo}\nclaude dir:  {cfg}\nstore:       {store}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
