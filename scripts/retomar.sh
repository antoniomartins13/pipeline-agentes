#!/usr/bin/env bash
# Retomada identificada; nao repete prompt global nem contorna quota.
set -euo pipefail
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
python "$RAIZ/scripts/pipeline.py" run "${1:?informe o id da tarefa}"
