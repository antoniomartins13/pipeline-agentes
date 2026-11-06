# Revisão independente

Entrada: critérios, plano, base/SHA, diff, resultado de validação e achados anteriores. Começar por correção funcional, autorização, integridade de dados, regressão, performance relevante e erros.

Achado: ID, severidade, evidência/arquivo, impacto, reprodução e comportamento esperado. Snippet só quando necessário. Não corrigir nem exigir refatoração fora do escopo por preferência.

Separar veredito do diff, evidência dos testes/CI e condição de merge. Pré-existente é demonstrado na base. Revisão não publica no MR/card; entrega relatório privado. Se precisar implementar, trocar de papel e garantir outro revisor. Provedor diferente do dev no piloto do runner.

Roteiro de teste manual (obrigatório em toda revisão, pedido do dono em 30/09/2026): seção própria no review-report.md, em checklist que o dono executa no navegador. Cobrir: (1) fluxo principal da funcionalidade, por perfil de usuário; (2) edge cases concretos, com dados de exemplo e resultado esperado (vazio, nulo, limites, datas, duplicidade, concorrência, entrada inválida, falha de API); (3) regressão em código compartilhado: listar, com arquivo, cada componente/endpoint/helper alterado que também é usado por outra funcionalidade fora do escopo, e o que testar nela; (4) pré-condições do ambiente (migration, seed, portas, usuário/permissão). Marcar o que o revisor já verificou por teste automatizado e o que só o teste manual cobre. O roteiro é passo a passo e executável: usar registros reais do banco local (consulta só de leitura) com nome/id e datas esperadas calculadas, conferir que cada pré-condição é possível de criar pela tela (ex.: formulários com campos obrigatórios) e incluir limpeza ao final.

Cards futuros: problemas encontrados que não são do MR (bug pré-existente, dado inconsistente, código que ficou sem uso) entram numa seção própria do relatório, com local e evidência. Não bloqueiam o diff.

Relatório para o autor (quando o dono pedir): arquivo separado `tarefas/<id>/relatorio-dev.md`, só com o que corrigir, onde, como reproduzir e o esperado, mais as decisões que encerram pontos. Sem histórico da revisão. Escrever seguindo https://github.com/petergyang/no-ai-slop (skills/no-ai-slop/SKILL.md): direto, concreto, voz ativa, sem travessões, sem colon reveals, sem frases de efeito.
