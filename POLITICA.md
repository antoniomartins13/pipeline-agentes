# Regras mínimas do exemplo público

- Cada demanda tem critérios, plano, escopo, branch, base e checks definidos.
- Uma pessoa aprova plano e base no CP1 antes do desenvolvimento.
- Quem implementa não revisa o próprio trabalho; o revisor não corrige.
- O runner executa e registra checks; a resposta do agente não prova resultado.
- Falha, timeout ou execução sem desfecho bloqueiam a tarefa.
- Banco compartilhado, mudanças destrutivas, publicação e merge exigem decisão humana.
- Estado local não é fronteira de segurança contra processos do mesmo usuário.
