import json
from pathlib import Path

import pytest
from factory import FIXTURES, git_repo, rec_instructions, rec_tool, store_for, write_index, write_session

from memreceipt.store import project_key, resolve
from memreceipt.transcripts import load_session, parse_delivery, sessions_for


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def test_project_key_matches_claude_code() -> None:
    assert project_key("/home/u/autonomous-agent") == "-home-u-autonomous-agent"
    assert project_key("/tmp/cb-matrix/add-mcp-server") == "-tmp-cb-matrix-add-mcp-server"
    assert project_key("/a/b.c/d_e") == "-a-b-c-d-e"


def test_subdirectory_loads_repo_root_store(tmp_path: Path) -> None:
    root = git_repo(tmp_path / "repo")
    (root / "sub" / "deep").mkdir(parents=True)
    cfg = tmp_path / "cfg"
    res = resolve(root / "sub" / "deep", cfg)
    assert res.root == root and res.root_how == "git"
    assert res.store == cfg / "projects" / project_key(root) / "memory"
    assert res.transcripts.name == project_key(root / "sub" / "deep")
    assert res.key_note is not None


def test_non_git_dir_is_its_own_root(tmp_path: Path) -> None:
    d = tmp_path / "plain"
    d.mkdir()
    res = resolve(d, tmp_path / "cfg")
    if res.root_how.startswith("git"):
        pytest.skip("tmp dir is inside a git repository on this machine")
    assert res.root == d.resolve() and res.key_note is None


def test_auto_memory_directory_and_disable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = git_repo(tmp_path / "repo")
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "settings.json").write_text(json.dumps({"autoMemoryDirectory": str(tmp_path / "mem")}))
    res = resolve(root, cfg)
    assert res.store == tmp_path / "mem" and res.store_source.endswith("settings.json")
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.local.json").write_text(json.dumps({"autoMemoryEnabled": False}))
    res = resolve(root, cfg)
    assert not res.enabled and "autoMemoryEnabled=false" in str(res.disabled_by)
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")
    assert resolve(root, cfg).disabled_by == "CLAUDE_CODE_DISABLE_AUTO_MEMORY"


def test_parse_real_truncated_delivery(tmp_path: Path) -> None:
    line = (FIXTURES / "instructions-truncated-2.1.323.jsonl").read_text(encoding="utf-8")
    rec = json.loads(line)
    f = rec["attachment"]["files"][0]
    d = parse_delivery(rec["timestamp"], f["path"], f["content"])
    assert d.truncated and d.lines == 200
    assert (d.cut_lines, d.total_lines, d.cut_start) == (50, 250, 201)
    assert d.warning is not None and d.warning.startswith("> WARNING: MEMORY.md is 250 lines")
    whole = parse_delivery("t", "p", "- a\n- b\n")
    assert not whole.truncated and whole.warning is None and whole.lines == 2


def test_session_deliveries_reads_writes_and_subagents(tmp_path: Path) -> None:
    root = git_repo(tmp_path / "repo")
    cfg = tmp_path / "cfg"
    store = store_for(cfg, root)
    write_index(store, 3)
    idx = str(store / "MEMORY.md")
    f = write_session(cfg, root, "aaa", [
        rec_instructions("2026-11-01T00:00:00Z", root, [("AutoMem", idx, "- [a](fact-001.md)\n")]),
        rec_tool("2026-11-01T00:01:00Z", "Read", {"file_path": str(store / "fact-001.md")}),
        rec_tool("2026-11-01T00:02:00Z", "Bash", {"command": f"cat {store}/fact-002.md | head"}),
        rec_tool("2026-11-01T00:03:00Z", "Edit", {"file_path": str(store / "MEMORY.md")}),
        rec_tool("2026-11-01T00:04:00Z", "Read", {"file_path": str(root / "unrelated.md")}),
    ])
    sub = f.with_suffix("") / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-1.jsonl").write_text(json.dumps(
        rec_tool("2026-11-01T00:05:00Z", "Read", {"file_path": str(store / "fact-003.md")})) + "\n")
    s = load_session(f, store, {"fact-001.md", "fact-002.md", "fact-003.md"})
    assert s.cwd == str(root) and s.started == "2026-11-01T00:00:00Z" and s.ended == "2026-11-01T00:05:00Z"
    assert len(s.deliveries) == 1 and not s.deliveries[0].truncated
    assert dict(s.reads) == {"fact-001.md": 1, "fact-002.md": 1, "fact-003.md": 1}
    assert dict(s.writes) == {"MEMORY.md": 1}


def test_sessions_for_includes_subdir_sessions_and_excludes_neighbours(tmp_path: Path) -> None:
    root = git_repo(tmp_path / "repo")
    other = git_repo(tmp_path / "repo-other")  # shares the key prefix "-...-repo"
    (root / "sub").mkdir()
    cfg = tmp_path / "cfg"
    store = store_for(cfg, root)
    write_index(store, 2)
    idx = str(store / "MEMORY.md")
    write_session(cfg, root / "sub", "insub", [rec_instructions("2026-11-02T00:00:00Z", root / "sub",
                                                                 [("AutoMem", idx, "x")])])
    write_session(cfg, root, "noauto", [rec_instructions("2026-11-01T00:00:00Z", root, [("Project", "/c", "y")])])
    write_session(cfg, other, "neighbour", [rec_instructions("2026-11-03T00:00:00Z", other, [("Project", "/c", "y")])])
    got = sessions_for(store, root, cfg / "projects", project_key(root))
    assert [s.id for s in got] == ["noauto", "insub"]
