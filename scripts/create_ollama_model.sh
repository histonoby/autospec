#!/usr/bin/env bash
# Create the Ollama alias AutoSpec talks to (default: local-coder-64k = <base> with num_ctx 65536).
# Usage: scripts/create_ollama_model.sh <base-model> [alias] [num_ctx]
#   e.g. scripts/create_ollama_model.sh qwen3.8:<tag> local-coder-64k 65536
set -euo pipefail
BASE="${1:?base model (e.g. the Qwen3.8 tag shown by 'ollama list')}"
ALIAS="${2:-local-coder-64k}"
CTX="${3:-65536}"
command -v ollama >/dev/null || { echo "ollama not found"; exit 1; }
if ! ollama show "$BASE" >/dev/null 2>&1; then
  echo "base model '$BASE' is not pulled. Run: ollama pull $BASE"; exit 1
fi
tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
printf 'FROM %s\nPARAMETER num_ctx %s\n' "$BASE" "$CTX" > "$tmp"
ollama create "$ALIAS" -f "$tmp"
ollama show "$ALIAS" --parameters
