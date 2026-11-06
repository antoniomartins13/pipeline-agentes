# Instruções para agentes

Leia `_pipeline/POLITICA.md` para autorizações e fluxo. Use a entrada pertinente de `_pipeline/entradas/` e o HANDOFF da tarefa; não reabra todas as tarefas da workspace.

Código das aplicações segue as diretrizes do repositório e de `erp-agent/projeto/ia_guidelines.md` quando aplicável ao ERP. Se a arquitetura não se aplicar ao repo, não a imponha.

Claude e Codex são pares com papéis configuráveis. Quem implementa não revisa o próprio trabalho; em revisão, não corrigir silenciosamente. Não publicar parecer no MR/card. CP1 humano persistente, draft conforme política e merge humano. Banco/destrutivo exigem as aprovações explícitas; não contornar guards.

Sem aprovação fictícia ou quota inferida de preço API. Mudanças no pipeline autorizadas diretamente pelo dono não precisam ser recadastradas como tarefas de aplicação; manter evidência, testes e relatório.
