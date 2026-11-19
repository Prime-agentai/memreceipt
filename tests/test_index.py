from pathlib import Path

from factory import FIXTURES, write_index

from memreceipt.index import (
    CHAR_LIMIT,
    LINE_LIMIT,
    analyze_store,
    analyze_text,
    loaded_line_count,
    split_lines,
    u16len,
)


def test_split_lines_trailing_newline_and_crlf() -> None:
    assert split_lines("") == []
    assert split_lines("a\nb\n") == ["a", "b"]
    assert split_lines("a\r\nb") == ["a", "b"]
    assert split_lines("a\n\n") == ["a", ""]


def test_u16len_counts_astral_as_two() -> None:
    assert u16len("abc") == 3
    assert u16len("—") == 1  # BMP: one unit even though it is 3 UTF-8 bytes
    assert u16len("🙂") == 2


def test_line_limit_is_200() -> None:
    lines = [f"line {i}" for i in range(250)]
    assert loaded_line_count(lines) == LINE_LIMIT


def test_char_limit_counts_characters_not_bytes() -> None:
    # Measured on Claude Code 2.1.323: 117 lines of 24,812 chars / 25,046 bytes loaded; line 118 did not.
    line = "- [fact-001](fact-001.md) — planted fact 1 codeword WORD001 " + "x" * 150
    n = u16len(line)
    k = (CHAR_LIMIT + 1) // (n + 1)
    lines = [line] * 150
    got = loaded_line_count(lines)
    assert u16len("\n".join(lines[:got])) <= CHAR_LIMIT < u16len("\n".join(lines[: got + 1]))
    assert got in (k, k + 1)
    assert len("\n".join(lines[:got]).encode()) > CHAR_LIMIT  # bytes would have cut earlier


def test_exact_boundary() -> None:
    assert loaded_line_count(["a" * CHAR_LIMIT]) == 1
    assert loaded_line_count(["a" * (CHAR_LIMIT + 1)]) == 0
    assert loaded_line_count(["a" * (CHAR_LIMIT - 2), "b"]) == 2  # 24,998 + "\n" + "b" = 25,000 fits
    assert loaded_line_count(["a" * (CHAR_LIMIT - 1), "b"]) == 1  # 25,001 does not


def test_real_250_line_index_matches_claude_code(tmp_path: Path) -> None:
    text = (FIXTURES / "MEMORY-250.md").read_text(encoding="utf-8")
    rep = analyze_text(text, tmp_path)
    assert rep.total_lines == 250 and rep.loaded_lines == 200 and rep.truncated
    assert rep.binding_limit == "lines"
    assert [e.line for e in rep.cut_entries] == list(range(201, 251))
    assert rep.status == "truncated"


def test_store_report_dangling_orphans_and_near(tmp_path: Path) -> None:
    write_index(tmp_path, 170)
    (tmp_path / "fact-005.md").unlink()
    (tmp_path / "stray.md").write_text("x", encoding="utf-8")
    rep = analyze_store(tmp_path)
    assert rep.status == "near-limit" and not rep.truncated
    assert [e.line for e in rep.dangling] == [5]
    assert rep.orphans == ["stray.md"]
    assert rep.near_limit


def test_missing_index(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    rep = analyze_store(tmp_path)
    assert not rep.exists and rep.status == "missing" and rep.topic_files == ["a.md"]


def test_links_skip_urls_and_anchors(tmp_path: Path) -> None:
    rep = analyze_text("- [site](https://x.y/a.md)\n- [sec](#top)\n- [ok](ok.md#part)\nplain line\n", tmp_path)
    assert [e.target for e in rep.entries] == ["ok.md#part"]
    assert rep.entries[0].target_name == "ok.md"


def test_long_lines_flagged(tmp_path: Path) -> None:
    rep = analyze_text("short\n" + "y" * 201 + "\n", tmp_path)
    assert rep.long_lines == [2]
