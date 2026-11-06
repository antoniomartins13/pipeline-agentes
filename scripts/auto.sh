#!/usr/bin/env bash
# Pipeline sem sessao nenhuma (decisao do dono, 05/11/2026): chamado pelo timer pipeline-auto do systemd do
# usuario. Roda pelo fila.sh (teto de memoria, CPU baixa) toda tarefa em fase que anda sozinha e sem run em
# andamento. Gates, bloqueios e "a concluir" ficam parados esperando o dono (botoes do Habblaud -> console).
set -uo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
exec 9>"$DIR/.local/auto.lock"
flock -n 9 || exit 0  # a rodada anterior ainda esta trabalhando

pendentes() {
  python3 - "$DIR" <<'EOF'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
for state in sorted((root / '.local' / 'tasks').glob('*/state.json')):
    task = state.parent
    try:
        s = json.loads(state.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        continue
    if s.get('phase') in ('plan', 'prototype', 'dev', 'validate', 'review', 'docs') \
            and not s.get('active_run') and not (task / 'runner.lock').exists():
        print(task.name)
EOF
}

for t in $(pendentes); do
  "$DIR/scripts/fila.sh" "$t"
done

# Espelho Trello -> GitLab (so com a chave em ~/.config/pipeline/trello.env), no maximo a cada 10 minutos.
python3 "$DIR/scripts/trello_gitlab.py" sync --se-passou 600 >> "$DIR/.local/gestao.log" 2>&1 || true
