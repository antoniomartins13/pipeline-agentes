#!/usr/bin/env bash
# Instala o pipeline numa maquina nova (Linux). Rode de dentro da workspace, com o repo clonado como _pipeline:
#
#   git clone https://github.com/antoniomartins13/pipeline-agentes.git _pipeline
#   bash _pipeline/scripts/instalar.sh --dono "Seu Nome" [--gitlab git.exemplo.gov.br] [--sem-systemd]
#
# O que faz (idempotente; tudo que ja existe ganha backup antes de mudar):
#   1. confere python3, git e claude (agy, glab, tmux e tailscale sao opcionais e so avisam);
#   2. cria config/local.json a partir do exemplo, com o dono e o GitLab;
#   3. instala CLAUDE.md/AGENTS.md e as skills /iniciar /retomar /revisar /backlog na workspace (bootstrap.py);
#   4. registra no ~/.claude/settings.json o hook dos gates (UserPromptSubmit) e o db-guard (PreToolUse);
#   5. (sem --sem-systemd) instala e liga o timer pipeline-auto e o console habblaud-console do systemd do usuario.
# Nao copia segredo nenhum: login do claude/agy/glab fica com cada CLI.
set -euo pipefail

PIPE="$(cd "$(dirname "$0")/.." && pwd)"
WS="$(dirname "$PIPE")"
DONO=""; GITLAB=""; SYSTEMD=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dono) DONO="$2"; shift 2 ;;
    --gitlab) GITLAB="$2"; shift 2 ;;
    --sem-systemd) SYSTEMD=0; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "opcao desconhecida: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERRO: %s\n' "$*" >&2; exit 1; }

[[ "$(basename "$PIPE")" == "_pipeline" ]] || die "clone o repositorio como _pipeline dentro da workspace (ex.: ~/Orca-workspace/_pipeline)."

say "Pre-requisitos"
for bin in python3 git claude; do command -v "$bin" >/dev/null || die "falta '$bin'."; done
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || die "precisa do Python 3.10+."
for bin in agy glab tmux tailscale; do
  command -v "$bin" >/dev/null && echo "  $bin: ok" || echo "  $bin: ausente (opcional; ver README)"
done

say "config/local.json"
python3 - "$PIPE" "$DONO" "$GITLAB" <<'PY'
import json, sys
from pathlib import Path
pipe, dono, gitlab = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
path = pipe / 'config/local.json'
data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else \
    json.loads((pipe / 'config/local.example.json').read_text(encoding='utf-8'))
if dono:
    data['dono'] = dono
if gitlab:
    data['gitlab_host'] = gitlab
if not data.get('dono') or data['dono'] == 'Seu Nome':
    sys.exit('Informe --dono "Seu Nome" (quem aprova os gates).')
path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
print(f"  dono: {data['dono']} | gitlab: {data.get('gitlab_host')} | repos: {', '.join(data.get('repositories', {}))}")
PY

say "Workspace ($WS): CLAUDE.md, AGENTS.md e skills"
python3 "$PIPE/scripts/bootstrap.py" --install >/dev/null && echo "  ok"

say "Hooks do Claude Code (~/.claude/settings.json)"
mkdir -p "$HOME/.claude/hooks"
[[ -f "$HOME/.claude/hooks/db-guard.py" ]] || cp "$PIPE/integracoes/claude/db-guard.py" "$HOME/.claude/hooks/db-guard.py"
python3 - "$PIPE" <<'PY'
import json, shutil, sys, time
from pathlib import Path
pipe = Path(sys.argv[1])
path = Path.home() / '.claude/settings.json'
data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
if path.exists():
    shutil.copy2(path, path.with_name(f'settings.json.bak-{time.strftime("%Y%m%d%H%M%S")}'))
hooks = data.setdefault('hooks', {})

def ensure(event, command, matcher=None, **extra):
    for entry in hooks.get(event, []):
        if (entry.get('matcher') == matcher) and any(h.get('command') == command for h in entry.get('hooks', [])):
            return False
    entry = {'hooks': [{'type': 'command', 'command': command, **extra}]}
    if matcher:
        entry = {'matcher': matcher, **entry}
    hooks.setdefault(event, []).append(entry)
    return True

gate = f'python3 -I "{pipe / "scripts/chat_approval_hook.py"}"'
guard = f'python3 -I "{Path.home() / ".claude/hooks/db-guard.py"}"'
changed = [ensure('UserPromptSubmit', gate, timeout=30),
           ensure('PreToolUse', guard, 'Bash', statusMessage='Verificando risco ao banco de dados...'),
           ensure('PreToolUse', guard, 'Write|Edit|MultiEdit|NotebookEdit', statusMessage='Verificando gates do pipeline...')]
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
print('  hooks registrados' if any(changed) else '  hooks ja estavam registrados')
PY

if [[ $SYSTEMD == 1 ]]; then
  say "systemd do usuario: pipeline-auto.timer e habblaud-console.service"
  command -v tmux >/dev/null || die "o console precisa do tmux (apt install tmux) ou rode com --sem-systemd."
  UNITS="$HOME/.config/systemd/user"; mkdir -p "$UNITS"
  for f in pipeline-auto.service pipeline-auto.timer habblaud-console.service; do
    sed -e "s|@WORKSPACE@|$WS|g" -e "s|@PIPELINE@|$PIPE|g" -e "s|@PATH@|$PATH|g" -e "s|@TMUX@|$(command -v tmux)|g" \
      "$PIPE/integracoes/systemd/$f.in" > "$UNITS/$f"
  done
  chmod +x "$PIPE"/scripts/*.sh
  systemctl --user daemon-reload
  systemctl --user enable --now pipeline-auto.timer habblaud-console.service
  loginctl enable-linger "$USER" 2>/dev/null || echo "  (sem linger: o timer so roda com voce logado)"
  echo "  ok — console: tmux attach -t habblaud-console"
fi

cat <<FIM

============================================================
 Pipeline instalado em $PIPE

 Habblaud (fork com o pipeline): no .env do Habblaud ponha
     HABBLAUD_PIPELINE_DIR=$PIPE
 e rode  npm run docker:up  (ou npm start).

 Proximos passos:
   - ajuste "repositories" em config/local.json (alias -> pasta do clone);
   - abra o claude na workspace ($WS) e use /iniciar <pedido>;
   - testes do runner: python3 -m unittest discover -s _pipeline/tests
============================================================
FIM
