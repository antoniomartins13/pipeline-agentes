#!/usr/bin/env bash
# Console sem janela do pipeline (habblaud-console.service): sempre a MESMA sessao do Claude, para o Habblaud manter
# o nome do personagem e nao nascer um novo a cada reinicio. O id fica em .local/console-session (gerado na primeira
# vez); existe a conversa: retoma; nao existe: cria com esse id. O titulo "Console do pipeline" e o que o Habblaud usa
# para mandar os botoes Aprovar/Recuperar/Concluir para ca.
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
FILE="$DIR/.local/console-session"
if [[ -n "${HABBLAUD_CONSOLE_SESSION:-}" ]]; then
  ID="$HABBLAUD_CONSOLE_SESSION"
else
  mkdir -p "$DIR/.local"
  [[ -s "$FILE" ]] || python3 -c 'import uuid; print(uuid.uuid4())' > "$FILE"
  ID="$(tr -d '[:space:]' < "$FILE")"
fi
CLAUDE="${CLAUDE_BIN:-$(command -v claude || echo "$HOME/.local/bin/claude")}"
if compgen -G "$HOME/.claude/projects/*/$ID.jsonl" > /dev/null; then
  exec "$CLAUDE" --resume "$ID" -n "Console do pipeline"
fi
exec "$CLAUDE" --session-id "$ID" -n "Console do pipeline"
