# Pipeline de agentes com checkpoints humanos

Exemplo público de um runner de desenvolvimento e revisão. Um processo Python
abre uma sessão por fase, mantém handoff curto, executa checks e para nos
checkpoints humanos. Provedores são configuráveis; revisão é independente.

## Requisitos

- Python 3.10 ou superior e Git. O núcleo não requer pacotes pip.
- Para agentes reais: CLIs claude e codex instalados, autenticados e com
  permissões adequadas. Os testes usam agentes simulados.

Clone como _pipeline dentro de uma workspace. Copie
config/local.example.json para config/local.json e ajuste caminhos.
Leia POLITICA.md, entradas/INICIAR.md e docs/USO.md.

No diretório da workspace:

    python -m unittest discover -s _pipeline/tests -v
    python _pipeline/scripts/pipeline.py --help

Fluxo: critérios, plano, CP1 humano, dev, checks, revisão independente,
documentação. Tarefas médias e grandes também usam CP0. O estado local
fica em .local/; publicação e merge dependem de pessoas.

Este repositório contém código, testes e documentos genéricos, sem dados
internos. É uma distribuição de estudo, sem validação dos provedores
reais em outro ambiente. Tarifas de API não representam quota oficial.
Nenhuma licença de reutilização foi escolhida para esta distribuição.
