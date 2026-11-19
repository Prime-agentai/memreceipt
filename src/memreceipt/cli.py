"""memreceipt command line: status, verify, audit, where, hook, setup."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from . import __version__
from .hook import receipt_line
from .hook import run as run_hook
from .index import CHAR_LIMIT, LINE_LIMIT, IndexReport, analyze_store, split_lines
from .store import Resolution, all_stores, project_key, resolve
from .transcripts import Session, sessions_for


def _warn(msg: str) -> None:
    print(f"memreceipt: {msg}", file=sys.stderr)


def _home(p: Path | str) -> str:
    s, h = str(p), str(Path.home())
    return "~" + s[len(h):] if s == h or s.startswith(h + os.sep) else s


def _cfg(ns: argparse.Namespace) -> Path | None:
    return Path(ns.config_dir).expanduser() if ns.config_dir else None


def _ctx(ns: argparse.Namespace) -> tuple[Resolution, IndexReport]:
    res = resolve(ns.cwd, _cfg(ns))
    return res, analyze_store(res.store)


def _sessions(ns: argparse.Namespace, res: Resolution, rep: IndexReport) -> list[Session]:
    names = set(rep.topic_files) | {e.target_name for e in rep.entries} | {"MEMORY.md"}
    return sessions_for(rep.store, res.root, res.config / "projects", project_key(res.root),
                        every=ns.all_projects, names=names)


# --------------------------------------------------------------------------- status

def report_json(res: Resolution, rep: IndexReport) -> dict[str, Any]:
    return {
        "cwd": str(res.cwd), "root": str(res.root), "root_how": res.root_how,
        "store": str(rep.store), "store_source": res.store_source,
        "enabled": res.enabled, "disabled_by": res.disabled_by,
        "transcripts": str(res.transcripts),
        "status": rep.status if res.enabled else "disabled",
        "limits": {"lines": LINE_LIMIT, "chars": CHAR_LIMIT},
        "index": {
            "exists": rep.exists, "lines": rep.total_lines, "chars": rep.total_chars,
            "loaded_lines": rep.loaded_lines, "loaded_chars": rep.loaded_chars,
            "truncated": rep.truncated, "binding_limit": rep.binding_limit,
            "entries": len(rep.entries), "long_lines": rep.long_lines,
        },
        "cut_entries": [{"line": e.line, "title": e.title, "target": e.target} for e in rep.cut_entries],
        "dangling": [{"line": e.line, "title": e.title, "target": e.target} for e in rep.dangling],
        "orphans": rep.orphans,
        "topic_files": len(rep.topic_files),
        "receipt": receipt_line(res, rep),
    }


def cmd_status(ns: argparse.Namespace) -> int:
    res, rep = _ctx(ns)
    if ns.json:
        print(json.dumps(report_json(res, rep), indent=2))
    else:
        print(receipt_line(res, rep))
        print(f"  store      {_home(rep.store)}  ({res.store_source}; root via {res.root_how})")
        if rep.exists:
            print(f"  loaded     {rep.loaded_lines}/{rep.total_lines} lines, {rep.loaded_chars:,}/{rep.total_chars:,} chars "
                  f"(limits {LINE_LIMIT} lines, {CHAR_LIMIT:,} chars)")
            print(f"  headroom   {max(0, LINE_LIMIT - rep.total_lines)} lines, "
                  f"{max(0, CHAR_LIMIT - rep.total_chars):,} chars")
            print(f"  entries    {len(rep.entries)} indexed, {len(rep.topic_files)} topic files on disk")
        if res.key_note:
            print(f"  note       {res.key_note}")
        cut = rep.cut_entries
        if cut:
            show = cut if ns.cut else cut[:10]
            print(f"\n  NOT loaded at session start ({len(cut)} entries, from line {rep.loaded_lines + 1}):")
            for e in show:
                print(f"    L{e.line:<4} {e.title}  -> {e.target_name}")
            if len(show) < len(cut):
                print(f"    ... {len(cut) - len(show)} more (--cut to list all)")
        if rep.dangling:
            print(f"\n  dangling links ({len(rep.dangling)}): the file they point to does not exist")
            for e in rep.dangling[:20]:
                print(f"    L{e.line:<4} {e.title}  -> {e.target}")
        if rep.orphans:
            print(f"\n  unindexed topic files ({len(rep.orphans)}): no index line points to them")
            for o in rep.orphans[:20]:
                print(f"    {o}")
        if rep.long_lines:
            print(f"\n  {len(rep.long_lines)} index line(s) over 200 chars (Claude Code asks for one line under ~200): "
                  + ", ".join(f"L{n}" for n in rep.long_lines[:15]))
    if res.enabled and rep.truncated:
        return 2
    if ns.strict and (rep.near_limit or rep.dangling or rep.orphans or (res.enabled and not rep.exists)):
        return 1
    return 0


# --------------------------------------------------------------------------- verify

def verdict(sess: Session, store: Path, current_prefix: str) -> tuple[str, str]:
    """(verdict, detail) for one session: what actually reached the model."""
    index = str(store / "MEMORY.md")
    if not sess.deliveries:
        if sess.instructions_records == 0:
            return "no-record", "no instructions record (older build, --bare, or a non-interactive path)"
        return "not-loaded", "instructions were injected without an auto-memory index (auto-memory off for that run?)"
    mine = [d for d in sess.deliveries if d.path == index]
    if not mine:
        others = sorted({d.path for d in sess.deliveries})
        return "other-store", "loaded " + ", ".join(_home(o) for o in others)
    last = mine[-1]
    n = f" ({len(mine)} deliveries)" if len(mine) > 1 else ""
    if last.truncated:
        return "TRUNCATED", (f"{last.lines} lines delivered, {last.cut_lines} of {last.total_lines} cut "
                             f"from line {last.cut_start}{n}")
    drift = "" if last.body.rstrip("\n") == current_prefix else "; index has changed since"
    return "whole", f"{last.lines} lines, {last.chars:,} chars{n}{drift}"


def cmd_verify(ns: argparse.Namespace) -> int:
    res, rep = _ctx(ns)
    sessions = _sessions(ns, res, rep)
    if ns.session:
        sessions = [s for s in sessions if s.id.startswith(ns.session)]
    shown = sessions[-ns.last:] if ns.last else sessions
    prefix = ""
    if rep.exists:
        lines = split_lines(rep.index_path.read_text(encoding="utf-8", errors="replace"))
        prefix = "\n".join(lines[:rep.loaded_lines])
    rows = []
    for s in shown:
        v, detail = verdict(s, rep.store, prefix)
        rows.append({"session": s.id, "started": s.started, "cwd": s.cwd, "verdict": v, "detail": detail,
                     "transcript": str(s.file)})
    counts = Counter(r["verdict"] for r in rows)
    if ns.json:
        print(json.dumps({"store": str(rep.store), "sessions": rows, "counts": dict(counts)}, indent=2))
    else:
        print(f"memreceipt verify: {_home(rep.store / 'MEMORY.md')}")
        if not rows:
            print("  no sessions found for this store (try --all-projects)")
        for r in rows:
            where = r["cwd"] or "?"
            try:
                where = "./" + str(Path(where).relative_to(res.root)) if where != "?" else where
            except ValueError:
                where = _home(where)
            print(f"  {str(r['started'] or '')[:16]:16} {str(r['session'])[:8]}  {r['verdict']:<11} {r['detail']}  [{where}]")
        print("  " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) +
              f"  ({len(rows)} of {len(sessions)} sessions shown)")
    bad = counts.get("TRUNCATED", 0) + counts.get("other-store", 0)
    return 2 if bad else 0


# --------------------------------------------------------------------------- audit

def cmd_audit(ns: argparse.Namespace) -> int:
    res, rep = _ctx(ns)
    sessions = _sessions(ns, res, rep)
    reads: Counter[str] = Counter()
    read_sessions: Counter[str] = Counter()
    writes: Counter[str] = Counter()
    last_read: dict[str, str] = {}
    for s in sessions:
        for name, n in s.reads.items():
            reads[name] += n
            read_sessions[name] += 1
            if s.ended and s.ended > last_read.get(name, ""):
                last_read[name] = s.ended
        for name, n in s.writes.items():
            writes[name] += n
    by_name = {e.target_name: e for e in rep.entries}
    names = sorted(set(rep.topic_files) | set(by_name))
    rows = []
    for name in names:
        e = by_name.get(name)
        state = ("dangling" if e and not e.exists else "CUT" if e and not e.loaded
                 else "unindexed" if not e else "loaded")
        rows.append({"file": name, "title": e.title if e else None, "line": e.line if e else None,
                     "state": state, "read_sessions": read_sessions[name], "read_calls": reads[name],
                     "writes": writes[name], "last_read": last_read.get(name)})
    order = {"CUT": 0, "dangling": 1, "unindexed": 2, "loaded": 3}
    rows.sort(key=lambda r: (order[str(r["state"])], r["read_sessions"], str(r["file"])))
    never = [r for r in rows if r["read_sessions"] == 0 and r["state"] != "dangling"]
    summary = {"sessions_scanned": len(sessions), "files": len(rows), "never_read": len(never),
               "cut": sum(1 for r in rows if r["state"] == "CUT"),
               "unindexed": sum(1 for r in rows if r["state"] == "unindexed"),
               "dangling": sum(1 for r in rows if r["state"] == "dangling"),
               "index_reads": read_sessions["MEMORY.md"]}
    if ns.json:
        print(json.dumps({"store": str(rep.store), "summary": summary, "files": rows}, indent=2))
        return 0
    first = sessions[0].started[:10] if sessions and sessions[0].started else "?"
    print(f"memreceipt audit: {_home(rep.store)}  ({len(sessions)} sessions since {first})")
    print(f"  {'state':<10} {'sessions':>8} {'reads':>6} {'writes':>6}  {'last read':<10}  file")
    for r in rows[: ns.limit or None]:
        print(f"  {r['state']:<10} {r['read_sessions']:>8} {r['read_calls']:>6} {r['writes']:>6}  "
              f"{str(r['last_read'] or '-')[:10]:<10}  {r['file']}")
    if ns.limit and len(rows) > ns.limit:
        print(f"  ... {len(rows) - ns.limit} more (--limit 0 for all)")
    print(f"\n  {summary['never_read']} of {summary['files']} memory files were never opened in "
          f"{len(sessions)} sessions; {summary['cut']} are below the load cut; {summary['unindexed']} have "
          f"no index line; {summary['dangling']} index links are dangling.")
    print("  'never opened' is not 'useless': an index line can carry the whole fact. It does mean the "
          "topic file itself cost nothing and taught nothing.")
    return 0


# --------------------------------------------------------------------------- where

def cmd_where(ns: argparse.Namespace) -> int:
    res, rep = _ctx(ns)
    stores = all_stores(res.config)
    if rep.store not in stores and rep.store.is_dir():
        stores.append(rep.store)
    hits: list[dict[str, Any]] = []
    if ns.find:
        needle = ns.find.lower()
        for st in stores:
            for f in sorted(st.glob("*.md")):
                try:
                    text = f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for i, line in enumerate(text.splitlines(), 1):
                    if needle in line.lower():
                        hits.append({"store": str(st), "file": f.name, "line": i, "text": line.strip()[:160],
                                     "this_store": st == rep.store})
    if ns.json:
        print(json.dumps({
            "cwd": str(res.cwd), "root": str(res.root), "root_how": res.root_how,
            "store": str(res.store), "store_source": res.store_source, "enabled": res.enabled,
            "disabled_by": res.disabled_by, "transcripts": str(res.transcripts),
            "stores": [str(s) for s in stores], "hits": hits,
        }, indent=2))
        return 0
    print(f"  cwd          {_home(res.cwd)}")
    print(f"  memory root  {_home(res.root)}  ({res.root_how})")
    print(f"  store        {_home(res.store)}  ({res.store_source})")
    print(f"  auto-memory  {'on' if res.enabled else 'OFF: ' + str(res.disabled_by)}")
    print(f"  transcripts  {_home(res.transcripts)}")
    if res.key_note:
        print(f"  note         {res.key_note}")
    if ns.find:
        print(f"\n  '{ns.find}' found {len(hits)} time(s):")
        for h in hits[:50]:
            mark = "this store" if h["this_store"] else "OTHER STORE"
            print(f"    [{mark}] {_home(h['store'])}/{h['file']}:{h['line']}  {h['text']}")
        others = {h["store"] for h in hits if not h["this_store"]}
        if others:
            print(f"\n  {len(others)} other store(s) hold it. Sessions started under {_home(res.root)} will not see those.")
    else:
        print(f"\n  {len(stores)} memory store(s) on this machine:")
        for st in stores:
            r = analyze_store(st)
            size = f"{r.total_lines} lines, {len(r.topic_files)} files" if r.exists else f"no index, {len(r.topic_files)} files"
            print(f"    {'*' if st == rep.store else ' '} {_home(st)}  ({size}, {r.status})")
    return 0


# --------------------------------------------------------------------------- hook / setup

def cmd_hook(ns: argparse.Namespace) -> int:
    out = run_hook(sys.stdin.read(), always=not ns.problems_only, cfg=_cfg(ns))
    if out:
        print(out)
    return 0


def _self_command() -> str:
    """How a hook should invoke this install: the console script, or this interpreter plus our path."""
    if shutil.which("memreceipt"):
        return "memreceipt"
    cmd = f"{shlex.quote(sys.executable)} -m memreceipt"
    pkg_parent = Path(__file__).resolve().parent.parent
    if "site-packages" not in pkg_parent.parts and "dist-packages" not in pkg_parent.parts:
        cmd = f"PYTHONPATH={shlex.quote(str(pkg_parent))} {cmd}"  # running from a source checkout
    return cmd


def plan_setup(settings: dict[str, Any], command: str) -> tuple[dict[str, Any] | None, str]:
    """Return (new settings or None if already installed, explanation). Pure; never mutates input."""
    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
    assert isinstance(hooks, dict)
    groups = hooks.get("SessionStart") if isinstance(hooks.get("SessionStart"), list) else []
    assert isinstance(groups, list)
    for g in groups:
        for h in (g.get("hooks") or []) if isinstance(g, dict) else []:
            if isinstance(h, dict) and "memreceipt" in str(h.get("command", "")) and " hook" in str(h.get("command", "")):
                return None, "a memreceipt SessionStart hook is already installed"
    new = json.loads(json.dumps(settings))
    new.setdefault("hooks", {})
    if not isinstance(new["hooks"], dict):
        new["hooks"] = {}
    ss = new["hooks"].get("SessionStart")
    if not isinstance(ss, list):
        ss = []
    ss.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
    new["hooks"]["SessionStart"] = ss
    return new, f"adds one SessionStart hook ({len(groups)} existing SessionStart group(s) kept)"


def cmd_setup(ns: argparse.Namespace) -> int:
    cfg = _cfg(ns) or resolve(ns.cwd).config
    if ns.project:
        res = resolve(ns.cwd, _cfg(ns))
        path = res.root / ".claude" / "settings.json"
    else:
        path = cfg / "settings.json"
    try:
        settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError) as e:
        _warn(f"cannot read {path}: {e}")
        return 1
    if not isinstance(settings, dict):
        _warn(f"{path} is not a JSON object")
        return 1
    command = f"{_self_command()} hook" + (" --problems-only" if ns.problems_only else "")
    new, why = plan_setup(settings, command)
    if new is None:
        print(f"{path}: {why}. Nothing to do.")
        return 0
    print(f"{path}:\n  + SessionStart hook: {command}\n  ({why})")
    if not ns.write:
        print("Dry run. Re-run with --write to apply (a timestamped backup is kept).")
        return 0
    if path.exists():
        backup = path.with_name(f"{path.name}.memreceipt-{time.strftime('%Y%m%d-%H%M%S')}.bak")
        shutil.copy2(path, backup)
        print(f"  backup: {backup}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".memreceipt.tmp")
    tmp.write_text(json.dumps(new, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    print("Written. New sessions start with a memory load receipt.")
    return 0


# --------------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="memreceipt",
        description="A load receipt for Claude Code auto-memory: what loaded, what was cut, what was ever used.",
    )
    p.add_argument("--version", action="version", version=f"memreceipt {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cwd", help="project directory to resolve (default: current directory)")
    common.add_argument("--config-dir", help="Claude config dir (default: $CLAUDE_CONFIG_DIR or ~/.claude)")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", parents=[common], help="what this project's MEMORY.md loads (exit 2 if truncated)")
    s.add_argument("--cut", action="store_true", help="list every entry below the cut")
    s.add_argument("--strict", action="store_true", help="exit 1 on near-limit, dangling links or unindexed files")
    s.set_defaults(fn=cmd_status)

    v = sub.add_parser("verify", parents=[common], help="what each past session actually received, from transcripts")
    v.add_argument("--last", type=int, default=15, help="show the N most recent sessions (0 = all)")
    v.add_argument("--session", help="only sessions whose id starts with this")
    v.add_argument("--all-projects", action="store_true", help="scan every transcript dir, not just this repo's")
    v.set_defaults(fn=cmd_verify)

    a = sub.add_parser("audit", parents=[common], help="which memory files sessions actually open")
    a.add_argument("--limit", type=int, default=40, help="rows to show (0 = all)")
    a.add_argument("--all-projects", action="store_true", help="scan every transcript dir, not just this repo's")
    a.set_defaults(fn=cmd_audit)

    w = sub.add_parser("where", parents=[common], help="which store this directory maps to, and every store on disk")
    w.add_argument("--find", metavar="TEXT", help="search every store for TEXT and flag hits outside this one")
    w.set_defaults(fn=cmd_where)

    h = sub.add_parser("hook", help="SessionStart hook: reads the hook JSON on stdin")
    h.add_argument("--config-dir", help=argparse.SUPPRESS)
    h.add_argument("--problems-only", action="store_true", help="stay silent when the index loaded whole")
    h.set_defaults(fn=cmd_hook)

    st = sub.add_parser("setup", help="add the SessionStart hook to settings.json (dry run unless --write)")
    st.add_argument("--cwd", help=argparse.SUPPRESS)
    st.add_argument("--config-dir", help=argparse.SUPPRESS)
    st.add_argument("--project", action="store_true", help="write the repo's .claude/settings.json instead of user settings")
    st.add_argument("--problems-only", action="store_true", help="install the hook in --problems-only mode")
    st.add_argument("--write", action="store_true", help="apply the change")
    st.set_defaults(fn=cmd_setup)
    return p


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    try:
        rc: int = ns.fn(ns)
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130
    return rc
