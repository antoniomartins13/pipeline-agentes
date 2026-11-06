# Workspace — pipeline

Para trabalho de desenvolvimento/revisão, leia `_pipeline/POLITICA.md` e a entrada específica de `_pipeline/entradas/`. Leia apenas os conhecimentos relevantes ao domínio.

Regras de processo da política v2 substituem memórias antigas: coordenador determinístico, sessões por fase/fatia, Claude/Codex como pares, CP1 humano, CP3 draft sem novo gate prévio. Revisor não implementa; parecer privado, nunca publicar no MR/Trello. Banco compartilhado não é etapa do dev; preservar guards e aprovações destrutivas.

Retomada exige tarefa identificada: `_pipeline/tarefas/<id>/HANDOFF.md`. Estado de código/CI vem de git/glab atual. Não carregar HANDOFF global nem histórico completo. Limiares e provedores estão em `_pipeline/config/pipeline.json`, não neste arquivo.

Medidor: `python _pipeline/scripts/custo.py --sessoes`. Preço equivalente de API não é quota. Runner: `python _pipeline/scripts/pipeline.py --help`. Sessões legadas não são encerradas automaticamente; as transições automáticas valem para tarefas cadastradas.
