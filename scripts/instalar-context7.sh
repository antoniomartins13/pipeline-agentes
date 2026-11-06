#!/usr/bin/env bash
# Instala o Context7 (MCP de documentacao de bibliotecas) no agy — e opcionalmente no Claude Code.
#
# Por que no agy: e o Gemini quem ESCREVE o codigo, e e onde a alucinacao de API custa caro.
# A chave sai de ~/.context7.env, nunca da linha de comando (evita gravar no historico do shell).
#
# Uso: instalar-context7.sh [--com-claude]
set -uo pipefail

ENVF="$HOME/.context7.env"
[ -f "$ENVF" ] || { echo "ERRO: $ENVF nao existe."; exit 1; }

KEY=$(grep -E '^CONTEXT7_API_KEY=' "$ENVF" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '[:space:]')
if [ -z "$KEY" ]; then
  echo "ERRO: CONTEXT7_API_KEY vazio em $ENVF"
  echo "Gere a chave gratuita em https://context7.com/dashboard e preencha o arquivo."
  exit 1
fi
echo "chave carregada (${#KEY} chars) — nao sera exibida."

echo "=== agy: adicionando context7 ==="
agy mcp add --header "Authorization: Bearer ${KEY}" context7 https://mcp.context7.com/mcp \
  && echo "  adicionado" || { echo "  FALHOU"; exit 1; }

echo "=== agy: MCPs configurados ==="
agy mcp list

if [ "${1:-}" = "--com-claude" ]; then
  echo "=== Claude Code: adicionando context7 ==="
  claude mcp add --transport http --header "Authorization: Bearer ${KEY}" context7 https://mcp.context7.com/mcp \
    && echo "  adicionado" || echo "  FALHOU (confira 'claude mcp add --help')"
fi

cat <<'FIM'

Pronto. Como usar no dia a dia:
  O agente chama o Context7 sozinho quando precisa de doc de biblioteca.
  Para forcar, basta pedir no prompt: "use context7" ou citar a lib e a versao.

Bibliotecas do ERP que mais se beneficiam (versoes recentes, pouco material de treino):
  Laravel 12 · Vite 7 · PhpOffice/PhpWord · spatie/laravel-permission

Plano Free: 1.000 chamadas/mes. Se atrapalhar (latencia, consulta desnecessaria):
  agy mcp disable context7    (desliga sem remover)
  agy mcp remove  context7    (remove de vez)
FIM
