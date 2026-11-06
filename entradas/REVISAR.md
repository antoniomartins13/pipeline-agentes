# Revisar MR de colega

Leia POLITICA.md, o card indicado e o papel papeis/review.md. Fixe repo, base e SHA do MR; confira MR irmão quando houver integração.

Revise o diff, critérios e áreas impactadas. Execute/reaproveite evidência válida da suíte completa e build afetado. Resultado deve separar veredito do diff, testes/CI e condição para merge. Falhas pré-existentes exigem evidência na base, não suposição.

Entregue tarefas/<id>/review-report.md ao dono, sem publicar no MR/card. Para correções autorizadas, transicione para tarefa de implementação com CP1; um novo revisor confere o resultado. Não assumir o papel de autor silenciosamente.

O runner v2 cobre ciclos de implementação e correção cadastrados. Revisão avulsa de MR permanece uma sessão independente com handoff; não é automaticamente convertida em desenvolvimento.

A entrega inclui sempre o roteiro de teste manual (fluxo, edge cases, regressão em código compartilhado e pré-condições), conforme papeis/review.md.
