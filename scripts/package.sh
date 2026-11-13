#!/usr/bin/env bash
# Build dist/autospec-<version>.tar.gz from the committed tree (git archive: no venvs, no caches).
set -euo pipefail
cd "$(dirname "$0")/.."
ver="$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)"
mkdir -p dist
git archive --format=tar.gz --prefix="autospec/" -o "dist/autospec-${ver}.tar.gz" HEAD
ls -lh "dist/autospec-${ver}.tar.gz"
