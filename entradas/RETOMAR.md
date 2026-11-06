# Retomar tarefa identificada

Exigir ID da tarefa. Ler POLITICA.md e tarefas/<id>/HANDOFF.md; depois apenas plano, findings e trechos necessários. Git/glab são fonte do estado do código.

Tarefa no runner: `python _pipeline/scripts/pipeline.py status <id>` e `run <id>`. Estado awaiting_* exige decisão humana persistente; blocked exige resolver a causa e verificar processos antes de recover. Não reabrir sessão gigante com --resume só para carregar o passado.

Tarefa legada: consultar índice docs/TAREFAS.md e artefatos; registrar handoff por tarefa. Não adivinhar o próximo passo a partir de outro trabalho. Nenhuma aprovação antiga é inventada na migração.
