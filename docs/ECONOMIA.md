# Economia baseada em evidência

1. Uma sessão por fase/fatia; handoff curto e input específico. O runner coordena a transição, não uma conversa acumulando todas as fases.
2. Logs em arquivo; devolver código de saída, resumo e falhas. Testes não precisam de LLM acompanhando cada linha.
3. Buscar por domínio e arquivo, sem varrer cópias de worktrees, dumps e dependências. Não carregar histórico completo para responder uma dúvida pontual.
4. Limiares somente em `config/pipeline.json`. São metas experimentais de contexto, não limites oficiais nem percentual do plano.
5. Cache hit não prova desperdício. Medir contexto médio/pico, número de chamadas, criação/leitura de cache, output e qualidade da entrega.
6. `scripts/custo.py`, dashboard e retrospectiva usam `usage_core.py`. A escrita é calculada pelo TTL observado; modelo/TTL desconhecidos não recebem preço zero silencioso.
7. Sem âncora explícita, `--semana` significa últimos sete dias. Não indica reset ou quota disponível. Usar o indicador oficial do provedor para quota.
8. Paralelismo custa pelo trabalho executado. Sessão ociosa não executa inferência por existir. Subagentes que fazem chamadas têm custo e são incluídos.
9. Não abrir outra IA para repetir a mesma análise sem uma finalidade de revisão independente. Trocar provedor alivia uma assinatura, mas não elimina trabalho redundante.
10. Não disparar tarefas automaticamente para “queimar quota”. Uma tarefa precisa de prioridade e escopo autorizados; o medidor não conhece o saldo oficial.

Fonte das tarifas: config/pricing.json, com data e URL. O equivalente de API não inclui serviços extras, contratos ou conversão da assinatura.
