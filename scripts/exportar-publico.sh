#!/usr/bin/env bash
# Atualiza a distribuicao publica (pipeline-agentes) a partir deste _pipeline. So copia a lista abaixo (o motor):
# nunca tarefas/, relatorios/, historico/, .local/ nem config/local.json. Recusa se achar dado interno.
#
#   bash scripts/exportar-publico.sh <clone-do-pipeline-agentes>
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:?informe a pasta do clone do pipeline-agentes}"
[[ -d "$DEST/.git" ]] || { echo "ERRO: $DEST nao e um clone git" >&2; exit 1; }

FILES=(
  POLITICA.md .gitignore .gitattributes .claude/settings.json
  config/pipeline.json config/pricing.json config/local.example.json
  docs/AUTOMACAO.md docs/DECISOES.md docs/ECONOMIA.md docs/PORTABILIDADE.md docs/TAREFAS.md
  conhecimento/INDICE.md conhecimento/OPERACAO.md
  integracoes/workspace/AGENTS.md integracoes/workspace/CLAUDE.md integracoes/claude/db-guard.py
  integracoes/systemd/pipeline-auto.service.in integracoes/systemd/pipeline-auto.timer.in
  integracoes/systemd/habblaud-console.service.in
  scripts/pipeline.py scripts/chat_approval_hook.py scripts/ambiente_revisao.py scripts/usage_core.py
  scripts/bootstrap.py scripts/custo.py scripts/budget_hook.py scripts/dashboard.py scripts/retrospectiva.py
  scripts/agy-uso.py scripts/agy-run.sh scripts/instalar-context7.sh scripts/retomar.sh
  scripts/fila.sh scripts/auto.sh scripts/console.sh scripts/instalar.sh scripts/exportar-publico.sh
)
DIRS=(entradas papeis templates tests)

# Remove o que a versao anterior tinha e saiu da lista (menos .git, README e licenca, que sao da distribuicao).
find "$DEST" -mindepth 1 -maxdepth 1 ! -name .git ! -name README.md ! -name LICENSE ! -name AGENTS.md ! -name CLAUDE.md \
  -exec rm -r -- {} +
for f in "${FILES[@]}"; do install -D -m "$( [[ -x "$SRC/$f" || "$f" == *.sh ]] && echo 755 || echo 644)" "$SRC/$f" "$DEST/$f"; done
for d in "${DIRS[@]}"; do
  git -C "$SRC" ls-files -z -- "$d" ':!entradas/docs' | while IFS= read -r -d '' f; do install -D -m 644 "$SRC/$f" "$DEST/$f"; done
done
find "$DEST" -name __pycache__ -prune -exec rm -r -- {} +

# Regras e comentarios falam do dono, nao de uma pessoa (o nome de verdade vem de config/local.json "dono").
grep -rlI --exclude-dir=.git --exclude=exportar-publico.sh 'Antonio' "$DEST" | while IFS= read -r f; do
  sed -i -E 's/\b(pelo|do|ao|o|O) Antonio\b/\1 dono/g; s/\bde Antonio\b/do dono/g; s/\bpor Antonio\b/pelo dono/g; s/\bpro Antonio\b/pro dono/g;
             s/(^|[.!?:] |\*\* |- )Antonio\b/\1O dono/g; s/\bAntonio\b/o dono/g' "$f"
done

# Dado interno nunca vai: IP da rede, tailnet, token, caminho de usuario, webhook real, card real.
PADRAO='172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.|\.ts\.net|tail[0-9a-f]{5}|glpat-|ghp_|/home/[a-z][a-z0-9_-]{2,}|conecta\.jucese|@jucese\.se\.gov\.br|[Mm]ascarenhas|MASCARENHAS|trello\.com/c/[A-Za-z0-9]{8}'
if grep -rIEn --exclude-dir=.git --exclude=exportar-publico.sh "$PADRAO" "$DEST"; then
  echo "ERRO: dado interno acima; nada foi commitado" >&2; exit 1
fi
echo "ok: $(git -C "$DEST" status --porcelain | wc -l) arquivos mudaram em $DEST"
