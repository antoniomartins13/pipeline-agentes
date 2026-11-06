# Iniciar tarefa

Leia POLITICA.md e somente os conhecimentos do domínio necessário. Registre critérios em tarefas/<id>/task-spec.md (modelo em templates/task-spec.md); ambiguidades relevantes voltam ao dono. A spec começa pelo **Guardanapo** (seção 0): é o que o dono lê para aprovar. Ao apresentar o CP1, mostre o guardanapo e a linha de aprovação, não a spec inteira. Correção que amplia escopo reescreve o guardanapo antes do novo CP1.

Cadastre repo, branch da tarefa, base, paths de escopo, tamanho e comandos de validação no runner (docs/AUTOMACAO.md). O plano enumera fatias com critérios de aceite. CP1 sempre humano: cobre critérios e plano, e não há mais CP0. A aprovação é feita pelo chat, com `APROVAR CP1 <tarefa-ou-entrega>`. Tarefas da mesma demanda em repos diferentes usam `--entrega <id>`, para ter um gate único. O agente dispara e acompanha o `run`; não peça ao dono para rodar comandos.

O runner abre sessões novas por fase/fatia, persiste handoff e executa validações. Não conduza todo o desenvolvimento numa única conversa. Parecer é privado; draft e revisão humana conforme política. Publicação de documentação e abertura do MR permanecem etapas explícitas depois de ready_for_draft.
