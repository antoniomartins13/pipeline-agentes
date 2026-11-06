# Armadilhas operacionais atuais

- agy headless já retornou exit 0 sem entregar por falta de permissão. Verificar arquivos/commit e checks; não confiar em texto “OK”. Log por execução.
- Permissões do agy e hooks diferem das do Claude/Codex. A comprovação é teste seguro do guard, não relato do agente. Não copiar bypass entre provedores.
- O sync:model já encontrou permissões criadas fora do seeder e deixou dados incompletos. Dev apenas registra necessidade; etapa de ambiente aplica com aprovação.
- Worktrees isolam código, não o banco configurado no .env. Ambiente compartilhado pode gerar interferência e falsos diagnósticos.
- Testes do DocumentGenerator podem alterar fixture; inspecionar diff antes de commit. Não restaurar edições de terceiros.
- Browser exploratório serve para investigar; regressão repetível deve produzir asserções e resumo. Prints sob demanda, sem devolver todos ao contexto.
- Caminhos ../agent do layout antigo não são universais. Usar mapa local de repositórios e instruções portáteis.
- Estado de MR/branch vem de git/glab atual, não de HANDOFF global. Arquivos em comum não significam conflito.

Detalhes, incidentes e datas: histórico arquivado. Este arquivo descreve fatos operacionais; regras vigentes ficam em POLITICA.md.
- **Suíte completa do erp-back-end apaga `storage/app/private` real.** Quatro testes antigos em `tests/Feature/Command/Move*`
  fazem `File::deleteDirectory(storage_path('app/private'))`. Rodar a suíte no repositório de trabalho destrói os arquivos
  privados locais (perdemos fotos de teste do EVENTOS em 22/09). Suíte completa só em worktree isolado ou sandbox.
- **agy: regras `allow`/`deny` com `.*` não casam.** A barreira efetiva é o `db-guard-agy.ps1` (bloqueia tinker, php -r,
  sync:model, migrate, db:*). Prova de bloqueio = efeito observável (arquivo não criado), nunca a resposta do modelo.
- **`vite build` verde não prova que a tela carrega.** Identificador sem import (ex.: ícone do `react-feather`) só quebra
  ao abrir a página (ErrorBoundary). Antes de entregar ao dono: lint `react/jsx-no-undef`/`no-undef` no módulo e cada
  rota aberta no navegador com o console. Sessão do ERP expirada: pedir login ao dono, nunca digitar a senha dele.
- **O `erp-agent` mandava usar `BaseCrudController` + `setStoreRequest`** — contrário ao `make:crud` e aos 264 controllers
  reais. Corrigido em 06/10 no MR erp/agent !3 (branch `docs/padrao-controller-e-checklist-front`). Até o merge, a referência é
  `erp-back-end/stubs/controller.crud.stub`.
- **Dev tende a mexer em arquivo compartilhado para resolver caso do módulo** (Handler, `validation.php`, `SystemMultiSelect`,
  `PermissionHelper` — este último gravava toda permissão nova no módulo "Events"). Review confere
  `git diff origin/homologacao --stat` fora do módulo em toda entrega.
- **Vite dev: dependência nova (ex.: `@fullcalendar/core/locales/pt-br`) dispara reotimização e pode deixar o cache
  `node_modules/.vite/deps` inconsistente** — um dep passa a responder `504 Outdated Optimize Dep` e toda tela que o
  importa cai no ErrorBoundary ("Failed to fetch dynamically imported module"), mesmo recarregando. Correção: reiniciar
  o vite com `--force`. Depois de adicionar import novo de pacote, reabrir as telas no navegador antes de devolver.
