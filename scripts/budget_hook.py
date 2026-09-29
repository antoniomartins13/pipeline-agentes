#!/usr/bin/env python3
"""Advisory Claude PostToolUse hook; does not replace guards or kill workers."""
import json
import os
from pathlib import Path
import sys
from usage_core import ROOT, context


def main():
    try:
        event = json.load(sys.stdin)
        path = Path(event['transcript_path'])
        config = json.loads((ROOT / 'config/pipeline.json').read_text(encoding='utf-8'))['context']
        latest = None
        # Bound memory; transcript may still be streaming its final line.
        with path.open('rb') as f:
            f.seek(max(0, path.stat().st_size - 2_000_000))
            rows = f.read().decode('utf-8', errors='replace').splitlines()
        for line in reversed(rows):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            m = row.get('message') or {}
            if m.get('model') == '<synthetic>' or row.get('isApiErrorMessage'):
                continue
            u = m.get('usage')
            if isinstance(u, dict):
                latest = context(u)
                break
        if latest is None or latest < config['warn_tokens']:
            return
        text = ('Orcamento de contexto observado: %d. Termine esta fatia em ponto seguro e devolva '
                'JSON com handoff, status continue e proximo passo. Nao inicie nova investigacao extensa. '
                'O runner abrira sessao nova automaticamente apos validar progresso.') % latest
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PostToolUse', 'additionalContext': text}}))
        if os.environ.get('PIPELINE_RUN_DIR'):
            target = Path(os.environ['PIPELINE_RUN_DIR']).resolve()
            if target.is_relative_to((ROOT / '.local/runs').resolve()):
                (target / 'budget.json').write_text(json.dumps({'context_tokens': latest, 'advisory_only': True}), encoding='utf-8')
    except (KeyError, OSError, ValueError, TypeError):
        # Unknown telemetry is not an instruction to bypass permissions.
        return


if __name__ == '__main__':
    main()
