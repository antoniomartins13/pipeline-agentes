#!/usr/bin/env bash
# Executa o agy headless sobrevivendo a limite de quota.
#
# Problema: em `agy -p`, se a assinatura bate o limite, o processo morre e o trabalho se perde.
# Solucao: detecta erro de quota/limite e re-tenta com backoff, ate o teto de espera.
# Nao mascara erro real: falha de codigo sai na hora, sem retry.
#
# Uso:  agy-run.sh "<prompt>" [--add-dir DIR] [outras flags do agy...]
# Env:  AGY_MAX_TENTATIVAS (default 6)   AGY_ESPERA_INICIAL (default 300s = 5min)
#       AGY_ESPERA_MAX (default 3600s = 1h)
set -uo pipefail

PROMPT="${1:?uso: agy-run.sh \"<prompt>\" [flags do agy]}"; shift
MAX="${AGY_MAX_TENTATIVAS:-6}"
ESPERA="${AGY_ESPERA_INICIAL:-300}"
ESPERA_MAX="${AGY_ESPERA_MAX:-3600}"
LOG="${AGY_LOG:-./agy-run.log}"

# Padroes que indicam limite de uso (nao erro de codigo)
PADRAO_LIMITE='quota|rate.?limit|resource.?exhausted|429|too many requests|limit reached|usage limit|try again later'

for (( t=1; t<=MAX; t++ )); do
  echo "[$(date -Is)] tentativa $t/$MAX" | tee -a "$LOG"
  SAIDA=$(agy -p "$PROMPT" "$@" 2>&1)
  CODIGO=$?
  echo "$SAIDA" >> "$LOG"

  if [ $CODIGO -eq 0 ] && ! echo "$SAIDA" | grep -qiE "$PADRAO_LIMITE"; then
    echo "$SAIDA"
    echo "[$(date -Is)] OK na tentativa $t" | tee -a "$LOG"
    exit 0
  fi

  if echo "$SAIDA" | grep -qiE "$PADRAO_LIMITE"; then
    if [ $t -eq $MAX ]; then
      echo "[$(date -Is)] LIMITE persistente apos $MAX tentativas — parando." | tee -a "$LOG"
      echo "Estado preservado: atualize o HANDOFF.md e retome depois." | tee -a "$LOG"
      exit 75   # EX_TEMPFAIL: falha temporaria, nao erro de codigo
    fi
    echo "[$(date -Is)] limite detectado — aguardando ${ESPERA}s antes de re-tentar" | tee -a "$LOG"
    sleep "$ESPERA"
    ESPERA=$(( ESPERA * 2 )); [ $ESPERA -gt $ESPERA_MAX ] && ESPERA=$ESPERA_MAX
    continue
  fi

  # Erro que NAO e limite: falha na hora, sem retry (nao mascarar bug)
  echo "$SAIDA"
  echo "[$(date -Is)] erro nao relacionado a limite (exit $CODIGO) — sem retry." | tee -a "$LOG"
  exit $CODIGO
done
