# Automação de sessões e limites reais

O runner `scripts/pipeline.py` é um processo Python determinístico. Claude e agy (Gemini) são subprocessos com sessões novas (o adapter do Codex segue no código, fora de uso desde 03/11/2026): não usa resume/continue nem transporta o histórico da conversa. Os resultados entram num handoff curto, e a próxima fase começa automaticamente. Apenas tarefas explicitamente cadastradas participam; nenhuma sessão antiga ou servidor da workspace é encerrado.

## Pré-requisitos

Python 3.10+, Git, CLIs autenticados e permissões normais/guards funcionando. O runner não instala bypass. Para dev headless, configurar as permissões benignas necessárias na máquina; uma negação vira bloqueio, não um sucesso falso. Claude usa a configuração existente mais um hook consultivo PostToolUse desta execução; agy roda sem `--dangerously-skip-permissions`: `accept-edits` no dev, `plan` nas fases de leitura; comando negado vem em `denied_actions` e bloqueia. Não existe promessa de que os guards de um provedor protejam outro.

Cadastre alias/caminho em `config/local.json`. Comece num checkout limpo, na branch da tarefa. O runner não cria/move worktrees automaticamente. Se usar worktrees geridos pelo Orca, prepare-os pelo CLI do Orca antes e mapeie o alias para o checkout exato. Não pode existir outro escritor no mesmo checkout.

## Cadastrar e executar

Exemplo ilustrativo — adapte os comandos de teste ao repo. Não usar comando de migração/produção como check.

```text
python _pipeline/scripts/pipeline.py init minha-tarefa --repo erp-back-end --spec pedido.md --base origin/homologacao --scope app/Services/MeuModulo --scope tests/Feature/MeuModulo --check '["php","artisan","test"]' --size P
python _pipeline/scripts/pipeline.py run minha-tarefa
```

No PowerShell use aspas simples para o JSON de `--check`, como acima. Em cmd.exe o escaping difere; preferir PowerShell/bash. `--check` e `--scope` podem se repetir. Backend completo e build do front afetado continuam requisitos; o MVP opera um repo por tarefa. Full-stack usa duas tarefas com contrato explícito e integração revisada, não presume validar os dois repos pelo teste de um.

O plano termina em `awaiting_CP1`. O agente mostra os hashes com `pipeline.py gate <tarefa-ou-entrega>` (somente leitura). O dono revisa critérios, plano, base, escopo e checks e **aprova pelo chat**, numa linha própria da mensagem:

```text
APROVAR CP1 <tarefa-ou-entrega> <hash-combinado (8+ caracteres)>
```

O hook `UserPromptSubmit` (`scripts/chat_approval_hook.py`, registrado em `~/.claude/settings.json`) grava a aprovação de todas as tarefas da entrega, tudo ou nada. Primeiro grava todas as aprovações, depois os estados; o `run` conclui uma transição que tenha sido interrompida. O registro leva a origem `chat-usuario`, a linha, a sessão e o sha256 da mensagem.

Regras do hook:
- a mensagem inteira tem de ser só o comando: nada antes nem depois, inclusive bloco de código, citação ou `<pasted_content>`;
- todas as tarefas da entrega precisam ter o registro publicado com o mesmo hash combinado (uma falha no meio não libera nenhuma);
- tarefas de uma mesma entrega têm de ter o mesmo `--prototype`;
- tarefa que pertence a uma entrega não é aprovada sozinha.

Origem: são recusados os workers do runner (`PIPELINE_RUN_DIR`/`PIPELINE_PHASE`), o `claude -p`, uma sessão aberta por outro agente (claude/codex) e o hook rodado fora do Claude Code (o pai precisa ser o `claude`, direto ou via `sh -c` com o comando exato do settings). A verificação é feita por `/proc` (Linux); em outro SO, use o terminal.

A aprovação de entrega guarda o hash combinado. No `run`, ela só vale se o hash atual de **todas** as tarefas da entrega (e a composição dela) continuar igual. Depois o agente dispara `run` em segundo plano e acompanha.

`read_dirs` no manifesto (alias de repo ou caminho relativo ao `_pipeline`) libera leitura extra às fases somente leitura do Claude (plan/review/docs), por exemplo o repo irmão da entrega e o `erp-agent`. Faz parte do hash do CP1.

Alternativa equivalente, **num terminal interativo**:

```text
python _pipeline/scripts/pipeline.py approve minha-tarefa
```

O comando mostra o hash e pede `APROVAR CP1`. Uma vez aprovado, já chama o runner: dev → checks → revisão → correções quando necessárias → docs. Você não precisa encerrar/abrir sessões entre essas fases. Não há mais CP0: o CP1 cobre critérios e plano. Tarefas antigas em `awaiting_CP0` voltam para `plan` no próximo `run`. `--prototype` acrescenta aprovação do HTML antes do dev. A aprovação é persistida e invalida se o input protegido mudar.

## Estado e retomada

- Portátil/versionável: `tarefas/<id>/task.json`, critérios, plano, handoff e pareceres. O manifesto fixa base, branch, paths, providers e checks.
- Local: `.local/tasks/<id>/state.json`, lock e aprovações; `.local/runs/<id>/<run>/` com prompt, saída, recibo, métricas e logs. Não enviar logs brutos para o repositório.
- `status <id>` mostra fase/razão; `run <id>` continua uma fase já autorizada. Estado `blocked` não redispara sozinho.
- `recover <id>` exige decisão humana, pelo terminal ou pela linha `RECUPERAR <id> [dev|validate]` no chat. As checagens de processo e PID são as mesmas nos dois caminhos. Não oferece limpeza destrutiva. Um lock cujo dono está comprovadamente encerrado nesta máquina é **arquivado automaticamente** em `recoveries/`, com registro. Dono vivo, de outro host ou desconhecido continua bloqueando. O script nunca rouba lock por timeout.
- No notebook, após trazer código/artefatos e mapear o repo: `attach <id> --phase dev` ou `--phase validate`. Aprovações são revalidadas localmente; processos e PIDs não são portáveis.

## Evidência e independência

Cada worker retorna JSON validado com summary, decisions, next_step, document, artifacts, status e verdict. Isso é uma alegação: o runner verifica existência/caminhos, mudança na árvore, escopo, branch, integridade das entradas/aprovações, e executa os checks aprovados em processo separado. Não aceita testes alegados pelo worker.

Plan/review/docs/prototype não podem alterar o código; mudanças detectadas bloqueiam. O piloto exige provedores diferentes para dev e review. Revisão se refere à árvore validada; alteração posterior invalida a conclusão. Falha pré-existente requer classificação/decisão humana; o MVP bloqueia na falha em vez de inventar um waiver.

## O que o MVP garante e o que não garante

**Implementado e testado com workers simulados e checks reais em Git temporário:** sessão/fatia independente no protocolo; transições automáticas; CP vinculado ao plano/base/manifesto; lock local; bloqueio em falta de progresso, escopo indevido, testes vermelhos, timeout e entrada adulterada. Não disparado um ciclo ERP real nesta alteração, pois não houve tarefa de aplicação selecionada.

**Contexto dentro de uma fatia:** Claude recebe aviso consultivo via hook e alvos em prompt. Isso não é um limite rígido de tokens e depende de o agente devolver o handoff. Codex fornece uso via eventos ao fim do turno; não foi implementado um hook de limite rígido de contexto nele. Não confundir tokens acumulados do turno com tamanho do contexto. `config/pipeline.json` concentra limiares, timeout, máximo de fatias e rodadas.

**Timeout:** bloqueia sem redisparo e sem matar um processo no meio da escrita. Registra PID porque o processo pode continuar vivo. Só recuperar depois de verificar. Limites de fatias/rodadas impedem loops ilimitados, mas podem exigir reavaliar uma tarefa grande.

**Aprovações:** comando interativo e hashes impedem erros de fluxo; não são uma fronteira criptográfica contra um processo malicioso sob o mesmo usuário do SO. Worker recebe ordem de não alterar estado e o runner verifica mudanças; isolamento forte exige identidade/permissões separadas. Não foi declarado ou instalado esse isolamento.

**Concorrência:** lock por tarefa nesta máquina; não existe lease distribuído entre desktop/notebook. Uma tarefa tem um dono por vez. Workers Orca usados em consultas não avançam esse estado. Não executar a mesma tarefa simultaneamente em dois runtimes ou máquinas.

**Final:** `ready_for_draft` produz documentação/parecer privado. Publicar BookStack, abrir draft e merge não são automatizados pelo MVP. Os checkpoints e ferramentas explícitas continuam; merge é humano. Revisão avulsa de colega permanece na entrada REVISAR, fora do ciclo automático de implementação.

Tarifas do medidor se referem ao Claude. Uso bruto do agy (e do Codex, em recibos antigos) fica nos recibos, sem conversão fictícia de assinatura. Para sessão iniciada dentro do repo, use `custo.py --workspace <caminho-do-repo>`; o escopo padrão é a workspace e não toda a conta.

## Validação de 23/09/2026

Validação inicial de 23/09: 19 testes passaram em 32.188s. Depois das correções aprovadas, a suíte passou com 37 testes em 75.860s e, na revisão independente, em 98,1s. Os agentes são simulados. O [parecer do delta](../relatorios/REVISAO-CORRECOES-RUNNER-2026-09-23.md) aprovou com ressalvas o preparo do smoke; provedores reais ainda não foram validados. Evidências e próximo passo em [continuidade](../relatorios/CONTINUIDADE-FLUXO-2026-09-23.md).

`recover <id> --to dev` permite corrigir uma falha de validação depois de inspeção humana. `--to validate` solicita novos checks. Sem `--to`, falhas em validate/review/docs voltam para validação; as outras fases retomam a etapa original. Recuperar invalida a evidência anterior, registra a decisão em `.local/tasks/<id>/recoveries/` e não chama `run` automaticamente. Voltar para dev conta uma rodada; o limite continua bloqueante. Gates são conferidos novamente em `run`. Não existe aprovação de exceção para ignorar checks vermelhos.

`recover <id> --stale-lock` pode arquivar um lock abandonado, sob o mesmo mutex do runner, somente com confirmação humana e proprietário comprovadamente encerrado nesta máquina. Processo vivo, host diferente ou estado desconhecido impedem a recuperação. O mutex do sistema operacional é liberado quando o processo morre; o arquivo de metadados continua exigindo inspeção. Nunca se rouba lock por expiração.

Recibos sem desfecho e processos pendentes impedem novo dispatch. Cada spawn registra intenção, PID e desfecho; uma interrupção entre spawn e registro do PID permanece desconhecida e bloqueante. Nesse caso é necessária investigação externa, não edição do estado para forçar retomada. A confirmação humana inclui examinar processos filhos, porque encerrar o PID principal não prova que todos os descendentes terminaram.

O limite de seis fatias conta invocações de agentes; checks determinísticos não o consomem. Assim, uma rodada normal de correção pode chegar à documentação. Loops de agentes e limite de rodadas continuam bloqueantes.

`HANDOFF.md` é um resumo parcial: confira manifesto e recibos para branch/base, entregáveis e testes antes de transferir uma tarefa.

## Limitações que permanecem antes do piloto real

- Arquivos ignorados, inclusive ambiente local, não entram na impressão digital. A evidência ainda não cobre alterações nesses arquivos.
- O avanço da referência de base bloqueia a tarefa; não há comando de rebase com reaprovação.
- No PowerShell 5.1, o JSON inline de `--check` pode perder aspas ao chegar ao Python. Conferir o argv recebido; o exemplo acima não está validado nesse shell. Checks Windows precisam de executáveis resolvíveis (por exemplo, `npm.cmd` em vez de `npm`).
- O hook consultivo do Claude no Windows ainda precisa de validação real. Não há garantia de interrupção intrafase por tokens.
- `attach` M/G pode replanejar em vez de retomar o plano existente. Não usar essa transferência como piloto desta rodada.
- A passagem dos diretórios extras ao Claude e os sandboxes do Codex têm testes locais com CLIs simulados. O smoke real dos provedores permanece necessário.

## Ressalvas da revisão do delta

O smoke deve ser pequeno e sem recuperação planejada. Antes de aplicar no ERP, tratar os achados A–C do parecer:

- Duas rodadas de correção podem consumir seis fatias e bloquear em docs. Hoje a recuperação exige novos checks/review, mesmo com a árvore já aprovada.
- Retomar uma pausa em dev também incrementa `rounds`; muitas fatias ou erros transitórios podem esgotar esse limite. Não editar o estado para contornar o bloqueio: reavaliar o desenho antes de uma tarefa longa.
- Se a causa for alteração de código por uma fase somente leitura, **não recuperar diretamente para validate**. Inspecionar o diff, rejeitar a alteração indevida ou registrar transição para desenvolvimento com autoria definida e nova revisão independente. O runner ainda não impõe essa distinção; validação verde não autoriza autoaprovação do código do revisor.

As demais notas do parecer incluem retomada de gates com lock abandonado, interrupção dentro de recover e investigação de liveness desconhecida. Elas continuam abertas e não foram apresentadas como resolvidas por esta entrega.
