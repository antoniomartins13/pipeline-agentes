# Usar o runner

Requisitos: Python 3.10+, Git e CLIs dos provedores escolhidos em
config/pipeline.json. O núcleo usa somente a biblioteca padrão do Python.
Clone como _pipeline dentro da workspace. Copie config/local.example.json
para config/local.json e configure os repositórios.

No diretório da workspace:

    python -m unittest discover -s _pipeline/tests -v
    python _pipeline/scripts/pipeline.py --help
    python _pipeline/scripts/pipeline.py init --help

init requer ID, alias do repositório, especificação, base, escopo e checks
como listas JSON argv. O checkout deve estar limpo numa branch da tarefa.
No PowerShell 5.1, JSON inline pode perder aspas; confira os argumentos
recebidos ou passe-os como argv de Python.

run <id> avança até checkpoint, bloqueio ou fim. CP1 é sempre humano.
O aprovador lê plano e base e usa approve <id> no terminal interativo.
status <id> é leitura. recover <id> requer inspeção da causa e processos.
Estado e logs ficam em .local/ e não entram no Git.

Limitações conhecidas para demandas longas: duas rodadas podem esgotar
fatias na documentação; retomar pausa de dev consome rodadas; alteração
de código por revisor exige inspeção humana antes de recuperar. Os testes
usam provedores simulados. ready_for_draft não é aprovação de publicação.
