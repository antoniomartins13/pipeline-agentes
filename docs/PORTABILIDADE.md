# Desktop, notebook e versionamento

`_pipeline` agora é um repositório Git local independente. O primeiro commit preserva o estado anterior à reorganização; ele já inclui a primeira implementação do coletor corrigido, feita antes do snapshot. Não há remoto, push nem publicação. Código das aplicações permanece em seus próprios repositórios.

## O que acompanha você

Regras atuais, scripts, templates, entradas, papéis, conhecimento, critérios/planos/handoffs e relatórios de tarefas. `integracoes/workspace` contém pontes mínimas para Claude e Codex. Históricos preservam decisões antigas, claramente marcadas como substituídas.

## O que fica em cada máquina

`config/local.json` (paths), `.local` (estado, aprovações, logs, registros de ambientes/Jules), credenciais, CLIs autenticados, dependências, guards, portas e processos. `.gitignore` exclui esses itens, dumps, `.env`, logs e imagens de execução. `_backups`, `_ambientes` e `_review-wt` são pastas irmãs e nunca fazem parte deste repo.

Não sincronizar `.git` de worktrees ou junctions entre computadores. Trazer branches das aplicações pelo Git, configurar dependências/banco local e mapear os caminhos. Não copiar o banco de trabalho ou segredos como parte do pipeline.

## Preparar o notebook

1. Quando definir um remoto **privado**, enviar o repo após revisar os documentos internos. Histórico antigo também contém nomes de projetos, hosts e relatos internos; .gitignore não remove dados de commits anteriores.
2. Clonar o pipeline como `<workspace>/_pipeline` e os repos das aplicações em pastas irmãs, ou mapear outro layout em `config/local.json`.
3. Instalar Python 3.10+, Git e os CLIs necessários. Autenticar cada provedor e configurar guards/permissões; instalação não copia tokens nem desativa hooks.
4. Executar `python _pipeline/scripts/bootstrap.py --check`, depois `--install`. O instalador cria os adapters na workspace, preserva backups e cria local.json se faltar. Inspecionar overrides antes de reutilizar numa workspace com instruções próprias.
5. Opcional, para reparar memórias antigas desse projeto no Claude: `python _pipeline/scripts/bootstrap.py --install-memory`. Atualiza somente o índice e a ponte do pipeline; não muda as regras globais de banco/destrutivo.
6. Executar `python -m unittest discover -s _pipeline/tests -v`. Para medir, usar `custo.py --workspace <path>` ou configurar `claude_transcripts` quando a descoberta não corresponder ao layout do CLI.
7. Para tarefa já versionada, fazer checkout da branch/base corretas, ler o handoff e usar `pipeline.py attach <id> --phase dev` (ou validate). Confirme que o desktop não está executando a mesma tarefa. Aprovação é revalidada na máquina nova.

## Compatibilidade

O núcleo Python, coletor e runner usam biblioteca padrão e paths relativos. O fuso default é -03:00; pode ser UTC, offset ou IANA (IANA depende da base tzdata do sistema).

Ferramentas legadas de infraestrutura têm limites: `ambiente.sh` ainda é um helper Windows/Git Bash com junctions/taskkill, e assume o layout ERP para localizar o front. Ele não foi anunciado como multiplataforma. `review-mr.sh` requer bash, glab e a stack instalada. Em outro sistema, executar o núcleo portátil e adaptar o provisionamento; nenhuma migração de banco é automática por padrão.

Para medir consumo consolidado em vários computadores, seriam necessários snapshots sanitizados ou um coletor central. Transcripts locais e quota oficial não são sincronizados por este repo.

## Validação de 23/09/2026

Em 23/09, a reorganização ainda está sem commit após o snapshot local `96852fd`. Evidências e pendências em [continuidade](../relatorios/CONTINUIDADE-FLUXO-2026-09-23.md).

`bootstrap.py --check` informa executáveis e configuração local; não valida autenticação ou guards. Os campos `installed_workspace` e `installed_memory` indicam as opções usadas naquela chamada, não uma auditoria da instalação existente.

Revise os templates antes de instalar as pontes numa workspace com instruções próprias. Confira também manifesto e recibos: o HANDOFF gerado contém somente parte das informações necessárias à transferência.
