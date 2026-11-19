#!/usr/bin/env bash
# Reproduce the README's A/B: does Claude know a fact whose index line was cut from MEMORY.md?
#
#   bash scripts/ab-claude.sh [runs-per-arm] [model]      (defaults: 2, haiku)
#
# Needs the `claude` CLI, logged in. Makes 2*runs short calls. Builds two throwaway git repos
# under $TMPDIR with identical 250-line memory stores in your Claude config dir, asks the same
# question in both, and passes memreceipt's hook ONLY to the "hook" arm via `claude --settings`
# (no settings file of yours is modified). Removes the two stores again on exit.
set -euo pipefail
cd "$(dirname "$0")/.."
RUNS="${1:-2}"; MODEL="${2:-haiku}"
CFG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SRC="$(pwd)/src"
WORK="$(mktemp -d)"
key() { python3 -c 'import re,sys; print(re.sub(r"[^A-Za-z0-9]", "-", sys.argv[1]))' "$1"; }
STORES=()
cleanup() { for s in "${STORES[@]}"; do rm -rf -- "${s:?}"; done; rm -rf -- "${WORK:?}"; }
trap cleanup EXIT

for arm in hook control; do
  repo="$WORK/$arm"; mkdir -p "$repo"; git init -q "$repo"
  repo="$(cd "$repo" && pwd -P)"
  store="$CFG/projects/$(key "$repo")"
  STORES+=("$store")
  mkdir -p "$store/memory"
  python3 - "$store/memory" <<'PY'
import os, random, sys
random.seed(7)
words = ["amber", "basalt", "cobalt", "delta", "ember", "fjord", "garnet", "harbor", "indigo", "juniper"]
lines = []
for i in range(1, 251):
    w = f"{random.choice(words)}-{random.randint(100, 999)}"
    lines.append(f"- [fact-{i:03d}](fact-{i:03d}.md) — project fact {i}: the deploy codeword for service s{i:03d} is {w}")
    open(os.path.join(sys.argv[1], f"fact-{i:03d}.md"), "w").write(f"Service s{i:03d} deploy codeword: {w}\n")
open(os.path.join(sys.argv[1], "MEMORY.md"), "w").write("\n".join(lines) + "\n")
print("planted:", lines[229].split(" is ")[-1], "(line 230, below the 200-line cut)")
PY
done

printf '{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"PYTHONPATH=%s python3 -m memreceipt hook","timeout":10}]}]}}\n' \
  "$SRC" > "$WORK/hook-settings.json"
Q="From your memory only, what is the deploy codeword for service s230? If it is not in your context, reply exactly NOT-IN-CONTEXT. Do not use any tools."
for i in $(seq 1 "$RUNS"); do
  for arm in hook control; do
    extra=(); [ "$arm" = hook ] && extra=(--settings "$WORK/hook-settings.json")
    ans="$(cd "$WORK/$arm" && claude -p "$Q" --model "$MODEL" "${extra[@]}" 2>/dev/null | head -c 200 | tr '\n' ' ')"
    echo "$arm run $i: $ans"
  done
done
