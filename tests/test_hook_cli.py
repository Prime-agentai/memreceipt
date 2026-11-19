import json
from pathlib import Path

import pytest
from factory import git_repo, rec_instructions, rec_tool, store_for, write_index, write_session

from memreceipt.cli import main, plan_setup
from memreceipt.hook import run


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def _setup(tmp_path: Path, n: int) -> tuple[Path, Path, Path]:
    root = git_repo(tmp_path / "repo")
    cfg = tmp_path / "cfg"
    store = store_for(cfg, root)
    write_index(store, n)
    return root, cfg, store


def test_hook_truncated_gives_receipt_and_cut_lines(tmp_path: Path) -> None:
    root, cfg, _ = _setup(tmp_path, 230)
    out = json.loads(run(json.dumps({"cwd": str(root), "source": "startup"}), cfg=cfg))
    assert "TRUNCATED" in out["systemMessage"] and "lines 201-230 (30 entries)" in out["systemMessage"]
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "L201: - [fact 201](fact-201.md) — fact number 201" in ctx
    assert "L230:" in ctx and "L200:" not in ctx


def test_hook_budget_truncates_list(tmp_path: Path) -> None:
    root, cfg, _ = _setup(tmp_path, 600)
    ctx = json.loads(run(json.dumps({"cwd": str(root)}), cfg=cfg))["hookSpecificOutput"]["additionalContext"]
    assert len(ctx) < 6500 and "more; list them with `memreceipt status --cut`" in ctx


def test_hook_ok_is_one_line_and_silent_with_problems_only(tmp_path: Path) -> None:
    root, cfg, _ = _setup(tmp_path, 3)
    out = json.loads(run(json.dumps({"cwd": str(root)}), cfg=cfg))
    assert out == {"systemMessage": "memreceipt: MEMORY.md loaded whole [3/200 lines, 119/25.0k chars, 3 entries]"}
    assert run(json.dumps({"cwd": str(root)}), always=False, cfg=cfg) == ""


def test_hook_disabled_and_garbage_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, cfg, _ = _setup(tmp_path, 3)
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")
    out = json.loads(run(json.dumps({"cwd": str(root)}), cfg=cfg))
    assert "auto-memory is OFF" in out["systemMessage"] and "hookSpecificOutput" not in out
    assert isinstance(run("not json", cfg=cfg), str)  # never raises


def test_hook_orphans_reach_claude(tmp_path: Path) -> None:
    root, cfg, store = _setup(tmp_path, 2)
    (store / "lost.md").write_text("x", encoding="utf-8")
    out = json.loads(run(json.dumps({"cwd": str(root)}), always=False, cfg=cfg))
    assert "1 unindexed topic file" in out["systemMessage"]
    assert "lost.md" in out["hookSpecificOutput"]["additionalContext"]


def test_status_exit_codes_and_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, cfg, store = _setup(tmp_path, 3)
    assert main(["status", "--cwd", str(root), "--config-dir", str(cfg)]) == 0
    write_index(store, 170)
    assert main(["status", "--cwd", str(root), "--config-dir", str(cfg)]) == 0
    assert main(["status", "--cwd", str(root), "--config-dir", str(cfg), "--strict"]) == 1
    write_index(store, 205)
    capsys.readouterr()
    assert main(["status", "--cwd", str(root), "--config-dir", str(cfg), "--json"]) == 2
    d = json.loads(capsys.readouterr().out)
    assert d["status"] == "truncated" and d["index"]["loaded_lines"] == 200
    assert [c["line"] for c in d["cut_entries"]] == [201, 202, 203, 204, 205]


def test_verify_and_audit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, cfg, store = _setup(tmp_path, 3)
    idx = str(store / "MEMORY.md")
    body = (store / "MEMORY.md").read_text(encoding="utf-8")
    write_session(cfg, root, "s1", [rec_instructions("2026-11-01T00:00:00Z", root, [("AutoMem", idx, body)]),
                                    rec_tool("2026-11-01T00:01:00Z", "Read", {"file_path": str(store / "fact-002.md")})])
    cut = "- a\n" * 200 + "\n> WARNING: MEMORY.md is 260 lines (limit: 200). Only part of it was loaded: " \
          "60 of 260 lines were cut off, starting at line 201 (\"x\")."
    write_session(cfg, root, "s2", [rec_instructions("2026-11-02T00:00:00Z", root, [("AutoMem", idx, cut)])])
    write_session(cfg, root, "s3", [rec_instructions("2026-11-03T00:00:00Z", root, [("AutoMem", "/elsewhere/MEMORY.md", "z")])])
    write_session(cfg, root, "s4", [rec_instructions("2026-11-04T00:00:00Z", root, [("Project", "/c", "y")])])
    capsys.readouterr()
    assert main(["verify", "--cwd", str(root), "--config-dir", str(cfg), "--json"]) == 2
    d = json.loads(capsys.readouterr().out)
    assert [(s["session"], s["verdict"]) for s in d["sessions"]] == [
        ("s1", "whole"), ("s2", "TRUNCATED"), ("s3", "other-store"), ("s4", "not-loaded")]
    assert "60 of 260 cut from line 201" in d["sessions"][1]["detail"]
    assert main(["audit", "--cwd", str(root), "--config-dir", str(cfg), "--json"]) == 0
    a = json.loads(capsys.readouterr().out)
    by = {r["file"]: r for r in a["files"]}
    assert by["fact-002.md"]["read_sessions"] == 1 and by["fact-001.md"]["read_sessions"] == 0
    assert a["summary"]["never_read"] == 2 and a["summary"]["sessions_scanned"] == 4


def test_where_find_flags_other_store(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, cfg, _ = _setup(tmp_path, 2)
    other = git_repo(tmp_path / "elsewhere")
    s2 = store_for(cfg, other)
    (s2 / "MEMORY.md").write_text("- [rule](rule.md) — never deploy on Fridays\n", encoding="utf-8")
    capsys.readouterr()
    assert main(["where", "--cwd", str(root), "--config-dir", str(cfg), "--find", "fridays", "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert len(d["hits"]) == 1 and d["hits"][0]["this_store"] is False and len(d["stores"]) == 2


def test_plan_setup_is_idempotent_and_keeps_existing_hooks() -> None:
    existing = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}, "model": "x"}
    new, why = plan_setup(existing, "memreceipt hook")
    assert new is not None and len(new["hooks"]["SessionStart"]) == 2 and new["model"] == "x"
    assert existing["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "echo hi"
    assert len(existing["hooks"]["SessionStart"]) == 1  # input not mutated
    again, why2 = plan_setup(new, "memreceipt hook")
    assert again is None and "already installed" in why2


def test_setup_write_backs_up(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "settings.json").write_text('{"theme": "dark"}')
    assert main(["setup", "--config-dir", str(cfg)]) == 0
    assert json.loads((cfg / "settings.json").read_text()) == {"theme": "dark"}  # dry run
    assert main(["setup", "--config-dir", str(cfg), "--write"]) == 0
    s = json.loads((cfg / "settings.json").read_text())
    assert s["theme"] == "dark" and "memreceipt" in s["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert list(cfg.glob("settings.json.memreceipt-*.bak"))


def test_verify_through_symlinked_config_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # macOS: /var -> /private/var. Claude Code records resolved paths; the user may pass the link.
    root, cfg, store = _setup(tmp_path, 3)
    link = tmp_path / "cfg-link"
    link.symlink_to(cfg)
    idx = str(store / "MEMORY.md")  # real path, as written into the transcript
    body = (store / "MEMORY.md").read_text(encoding="utf-8")
    write_session(cfg, root, "s1", [rec_instructions("2026-11-01T00:00:00Z", root, [("AutoMem", idx, body)]),
                                    rec_tool("2026-11-01T00:01:00Z", "Bash", {"command": f"cat {store}/fact-001.md"})])
    capsys.readouterr()
    assert main(["verify", "--cwd", str(root), "--config-dir", str(link), "--json"]) == 0
    assert [s["verdict"] for s in json.loads(capsys.readouterr().out)["sessions"]] == ["whole"]
    assert main(["audit", "--cwd", str(root), "--config-dir", str(link), "--json"]) == 0
    by = {r["file"]: r for r in json.loads(capsys.readouterr().out)["files"]}
    assert by["fact-001.md"]["read_sessions"] == 1
