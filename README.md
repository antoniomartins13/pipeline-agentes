# Pipeline de agentes com checkpoints humanos

Um runner em Python que leva uma funcionalidade do pedido ao MR em draft com agentes de IA. Cada fase roda numa
sessão própria (plano, dev, validação, revisão, documentação), e o humano só entra em dois momentos:

1. **CP1**: aprova a especificação (`task-spec.md`) e o manifesto (`task.json`: escopo, checks, base e provedores).
2. **Concluir**: testa a funcionalidade pronta (servidor de revisão, roteiro e MR em draft) e conclui ou pede correção.

Entre esses dois momentos o runner trabalha sozinho:
- falha de check volta ao dev com o log, até o limite de rodadas;
- quando a cota de um modelo acaba, a fase passa para o próximo modelo da lista de fallback;
- a revisão roda numa sessão independente da sessão que escreveu o código.

Ele funciona com o **[Habblaud](https://github.com/antoniomartins13/habblaud/tree/agy)** (fork, branch `agy`), um
escritório em pixel art onde você vê as sessões, lê a spec, o plano e o roteiro, e aprova pelos botões.

## O que precisa

- Linux, Python 3.10+ e Git.
- [Claude Code](https://claude.com/claude-code) (`claude`) autenticado.
- Opcionais:
  - `agy` (Antigravity CLI / Gemini): segundo provedor;
  - `glab` autenticado: MR em draft no GitLab;
  - `tmux`: console sem janela;
  - `tailscale`: servidores de revisão acessíveis pelo celular.

## Instalação

A workspace é uma pasta com os clones dos projetos lado a lado e este repositório como `_pipeline`:

```bash
mkdir -p ~/Orca-workspace && cd ~/Orca-workspace
git clone <url-do-back-end> erp-back-end        # os projetos em que você trabalha
git clone <url-do-front-end> erp-front-end
git clone https://github.com/antoniomartins13/pipeline-agentes.git _pipeline
bash _pipeline/scripts/instalar.sh --dono "Seu Nome" --gitlab git.seu-gitlab.gov.br
```

O instalador faz o seguinte (o que já existe ganha backup antes de mudar):
- cria `config/local.json`;
- instala `CLAUDE.md`, `AGENTS.md` e as skills `/iniciar`, `/retomar`, `/revisar` e `/backlog` na workspace;
- registra no `~/.claude/settings.json` o hook dos gates (`chat_approval_hook.py`) e o `db-guard`, que barra comando
  destrutivo de banco e autoaprovação;
- liga dois serviços no systemd do usuário:
  - `pipeline-auto.timer`: a cada 3 minutos roda as tarefas que andam sozinhas;
  - `habblaud-console.service`: uma sessão do Claude em tmux, sem janela, que recebe os botões do Habblaud. Ela não
    gasta cota, porque o hook registra a linha e encerra a mensagem.

Use `--sem-systemd` para pular os serviços.

Depois:

1. Em `config/local.json`, aponte `repositories` para os seus clones (alias -> pasta, relativa a `_pipeline`).
2. No Habblaud, ponha `HABBLAUD_PIPELINE_DIR=<workspace>/_pipeline` no `.env` e rode `npm run docker:up`.
3. Abra o `claude` na workspace e peça a funcionalidade com `/iniciar <pedido>`.

Testes do runner, sem chamar nenhum modelo:

```bash
python3 -m unittest discover -s _pipeline/tests
```

## Como é o dia a dia

- `/iniciar` gera a spec e o manifesto da tarefa e mostra a linha do CP1. Uma funcionalidade com back e front vira uma
  **entrega**, com uma tarefa por repositório, aprovadas juntas.
- Para aprovar, use o botão no Habblaud ou mande a linha sozinha numa mensagem:
  `APROVAR CP1 <tarefa-ou-entrega> <hash>`. O hook só aceita a linha numa sessão interativa sua; agente nenhum
  consegue aprovar por você.
- O runner vai sozinho pelas fases `plan`, `dev`, `validate`, `review` e `docs`.
- Quando fica pronto, você recebe:
  - MR em draft por repositório;
  - servidores de revisão (`ambiente_revisao.py`, com teto de memória);
  - migrations não destrutivas aplicadas **só** no banco local;
  - `roteiro-teste.md`;
  - uma sessão coordenadora na sala da funcionalidade no Habblaud.
- Depois de testar, mande `CONCLUIR <entrega>` ou `CORRIGIR <entrega>: <o que mudar>`. Pedido que muda o escopo
  amplia a spec e volta ao CP1.
- Se algo travar, use `RECUPERAR <tarefa> [dev|validate|plan]`. Só param para você: o limite de rodadas, a falta de
  cota em todos os modelos, mudança de escopo e banco destrutivo.

Regras completas em [`POLITICA.md`](POLITICA.md). Provedores, limites e a lista de fallback ficam em
[`config/pipeline.json`](config/pipeline.json).

## Segurança

- O revisor não implementa, e o parecer dele é privado: nunca vai para o MR nem para o Trello.
- O agy roda em sandbox, só com leitura no shell.
- Nenhum agente faz merge, push com força ou mexe em banco que não seja local.
- O hook dos gates e o `db-guard` evitam erro de fluxo e autoaprovação, mas não são fronteira contra um processo
  malicioso do mesmo usuário do sistema (POLITICA, item 12).

## Atualizar esta distribuição

Ela é gerada a partir de um `_pipeline` de uso real, com `scripts/exportar-publico.sh <clone>`. O script copia só o
motor e recusa a exportação se achar dado interno (IP, tailnet, token, caminho de usuário, e-mail institucional).
