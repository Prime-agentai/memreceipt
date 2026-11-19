#!/usr/bin/env bash
# The one gate: strict types, tests, and runs of the real entrypoint against a generated demo.
# Locally:  uv run --no-project --with pytest --with mypy bash scripts/check.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
"$PY" -m mypy --strict src
"$PY" -m pytest -q -p no:cacheprovider
export PYTHONPATH=src
DEMO="$(mktemp -d)"
trap 'rm -rf "$DEMO"' EXIT
"$PY" scripts/make-demo.py "$DEMO" > /dev/null
MR=("$PY" -m memreceipt)
A=(--cwd "$DEMO/repo" --config-dir "$DEMO/claude")
"${MR[@]}" --version

# run CMD... with an expected exit code; stdout goes to $OUT for the assertions that follow
OUT="$DEMO/out.json"
expect() {
  local want="$1"; shift
  set +e; "${MR[@]}" "$@" > "$OUT"; local rc=$?; set -e
  [ "$rc" = "$want" ] || { echo "memreceipt $*: expected exit $want, got $rc"; cat "$OUT"; exit 1; }
}

expect 2 status "${A[@]}"
expect 2 status "${A[@]}" --json
"$PY" - "$OUT" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["status"] == "truncated", d["status"]
assert d["index"]["lines"] == 215 and d["index"]["loaded_lines"] == 200, d["index"]
assert [c["line"] for c in d["cut_entries"]] == list(range(201, 216))
assert [x["line"] for x in d["dangling"]] == [42], d["dangling"]
assert d["orphans"] == ["forgotten-note.md"], d["orphans"]
PYEOF

expect 2 verify "${A[@]}" --json
"$PY" - "$OUT" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
assert [s["verdict"] for s in d["sessions"]] == ["whole", "TRUNCATED", "not-loaded"], d["sessions"]
assert d["sessions"][1]["cwd"].endswith("services/api")
assert d["counts"]["TRUNCATED"] == 1
PYEOF

expect 0 audit "${A[@]}" --json
"$PY" - "$OUT" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
by = {r["file"]: r for r in d["files"]}
assert by["deploy-rule-010.md"]["read_sessions"] == 2, by["deploy-rule-010.md"]
assert by["forgotten-note.md"]["state"] == "unindexed"
assert d["summary"]["cut"] == 15 and d["summary"]["sessions_scanned"] == 3, d["summary"]
PYEOF

printf '{"cwd": "%s", "source": "startup"}' "$DEMO/repo/services/api" \
  | "${MR[@]}" hook --config-dir "$DEMO/claude" | "$PY" -c '
import json, sys
d = json.load(sys.stdin)
assert "TRUNCATED" in d["systemMessage"], d
ctx = d["hookSpecificOutput"]["additionalContext"]
assert "L201:" in ctx and "L215:" in ctx and "forgotten-note.md" in ctx, ctx
'
expect 0 where "${A[@]}" --find "rule number 42"
grep -q "rule number 42" "$OUT"
echo "check.sh: all green"
