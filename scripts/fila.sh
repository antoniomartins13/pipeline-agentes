#!/usr/bin/env bash
# Fila do runner: roda as tarefas uma por vez, cada uma num cgroup do systemd com teto de memoria,
# e so comeca quando ha memoria livre. Motivo (04/11/2026): `next build` da validacao em cima dos
# servidores de revisao e dos apps do desktop travou a maquina (swap de 512 MB, sem OOM a tempo).
# Com o teto, quem passa do limite e o run (que bloqueia e aparece no Habblaud), nao o desktop.
# CPU e disco com peso baixo (04/11/2026): a suite paralela deixava o navegador lento.
#
# Uso: fila.sh <tarefa> [<tarefa>...]
# Ajustes: FILA_MEM_LIVRE_MB (padrao 4096), FILA_MEMORY_HIGH (6G), FILA_MEMORY_MAX (8G).
set -uo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
LIVRE_MB="${FILA_MEM_LIVRE_MB:-4096}"
HIGH="${FILA_MEMORY_HIGH:-6G}"
MAX="${FILA_MEMORY_MAX:-8G}"

livre_mb() { awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo; }

for t in "$@"; do
  [[ "$t" =~ ^[A-Za-z0-9_-]{1,80}$ ]] || { echo "tarefa invalida: $t"; continue; }
  until [ "$(livre_mb)" -ge "$LIVRE_MB" ]; do
    echo "--- $t aguardando memoria: $(livre_mb) MB livres (precisa de $LIVRE_MB)"
    sleep 60
  done
  echo "=== $t $(date +%T) ($(livre_mb) MB livres)"
  # CPU com prioridade baixa: a suite paralela (6 PHPs) deixava o navegador lento; assim usa os nucleos
  # livres, mas cede na hora para o desktop.
  systemd-run --user --scope --quiet --collect -p MemoryHigh="$HIGH" -p MemoryMax="$MAX" -p MemorySwapMax=0 \
    -p CPUWeight=10 -p IOWeight=10 --nice=15 \
    --unit="pipeline-$t-$(date +%s)" python3 "$DIR/scripts/pipeline.py" run "$t" > "$DIR/.local/fila-$t.log" 2>&1
  echo "exit=$?"
  python3 -c "import json,sys;s=json.load(open(sys.argv[1]));print(s['phase'],s.get('blocked_phase') or '',str(s.get('reason') or '')[:200])" \
    "$DIR/.local/tasks/$t/state.json"
done
