#!/usr/bin/env bash
# AutoSpec setup on Ubuntu (no sudo is run by this script; missing system packages are listed for you).
# Usage: scripts/setup_ubuntu.sh [qwen-base-model-tag]
#   With a tag, it also creates the Ollama alias local-coder-64k (num_ctx 65536) from it.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
missing=()

pick_python() {
  for py in python3.13 python3.12 python3.11 python3; do
    if command -v "$py" >/dev/null && "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
      echo "$py"; return 0
    fi
  done
  return 1
}
PY="$(pick_python)" || { echo "Python 3.11+ が必要です（Ubuntu 22.04 なら: sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.12 python3.12-venv）"; exit 1; }
"$PY" -c 'import venv, ensurepip' 2>/dev/null || missing+=("$(basename "$PY")-venv")
for tool in git gcc g++ make; do command -v "$tool" >/dev/null || missing+=("$tool"); done
if ((${#missing[@]})); then
  echo "不足しているパッケージ: ${missing[*]}"
  echo "  sudo apt install ${missing[*]/gcc/build-essential}"
  exit 1
fi

echo "== AutoSpec venv ($PY)"
"$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e ".[dev]"

echo "== aider venv (isolated: aider pins many libraries)"
"$PY" -m venv .venv-aider
.venv-aider/bin/pip install -q --upgrade pip
.venv-aider/bin/pip install -q aider-chat
.venv-aider/bin/aider --version

if [ ! -f config.yaml ]; then
  sed "s#  bin: \"aider\".*#  bin: \"$ROOT/.venv-aider/bin/aider\"#" config.example.yaml > config.yaml
  echo "== config.yaml を作成しました（aider.bin=$ROOT/.venv-aider/bin/aider）"
fi

echo "== optional tools"
for tool in iverilog verilator node npx docker ctags; do
  printf '  %-10s %s\n' "$tool" "$(command -v "$tool" || echo '(なし)')"
done
echo "  verilog: sudo apt install iverilog   / web: node 20+ と npm i -D typescript tsx（対象プロジェクト側）"

if command -v ollama >/dev/null; then
  if [ -n "${1:-}" ]; then
    scripts/create_ollama_model.sh "$1" local-coder-64k 65536
  fi
else
  echo "ollama が見つかりません: curl -fsSL https://ollama.com/install.sh | sh"
fi

echo "== self test (Ollama なし・FakeLLM)"
.venv/bin/pytest -q
echo "== health"
.venv/bin/autospec health || echo "（Ollama/モデル/aider のどれかが未準備です。上の JSON を確認してください）"
echo
echo "次の手順: README.md の「Ubuntu での受入確認」を参照"
