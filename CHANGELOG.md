# Changelog

## 0.1.0 (2026-11-19)

First release.

- `memreceipt hook`: a SessionStart hook. It gives you a one-line load receipt (`systemMessage`) and, when the index was cut, gives Claude the cut index lines (`additionalContext`). It also flags unindexed topic files. It never fails a session.
- `memreceipt status`: what loads, what is cut and by which limit, headroom, dangling links, unindexed topic files and over-long lines. Exits 2 when truncated; `--strict` exits 1 on warnings.
- `memreceipt verify`: per session, from transcripts: `whole` / `TRUNCATED` / `not-loaded` / `other-store` / `no-record`.
- `memreceipt audit`: per memory file, the sessions that opened it, reads, writes and last read.
- `memreceipt where [--find]`: store resolution (git root, worktrees, `autoMemoryDirectory`, disable switches) and a search across every store.
- `memreceipt setup [--project] [--write]`: an idempotent hook install with a backup.
- The cut rule (200 lines / 25,000 characters, whole lines) was measured on Claude Code 2.1.323.
- `scripts/ab-claude.sh`: a reproducible A/B against a real Claude Code session.
