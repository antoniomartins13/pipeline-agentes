# Decisões vigentes

Todas subordinadas a ../POLITICA.md. Motivo e supersessão registrados em 22/09/2026 após duas rodadas Codex/Claude solicitadas pelo dono.

| Decisão | Estado | Motivo |
|---|---|---|
| Coordenador determinístico, modelos como pares | vigente | Evitar acumular execução e coordenação numa conversa |
| Sessão nova por fase/fatia, handoff persistido | vigente | Contextos observados de 600–900k; transições não ficam a cargo do usuário |
| Claude e Codex configuráveis por fase | vigente | Preferência inicial é experimento; independência e qualidade importam mais que marca |
| Jules apenas verificador | vigente | Implementação em clone não conectado não entregou branch no teste local |
| CP3 = draft sem novo gate prévio | vigente | o dono já autorizou commit/push/MR de tarefa; retirar draft continua humano |
| Banco compartilhado fora do dev automático | vigente | Interferência real de tinker/sync:model documentada no histórico |
| Revisor não corrige; nova fase de dev | vigente | Evitar autoaprovação e medir retrabalho corretamente |
| Tokens, preço API e quota separados | vigente | Quota não deriva de soma bruta de tokens ou calendário presumido |
| Obsidian/RAG não implantados agora | vigente | Primeiro corrigir seleção/validade de conhecimento e medir |
| Uma conversa faz todos os papéis | substituída | Era descrição do processo, não requisito; provocava crescimento de contexto |
| Cache nunca compartilhado / >50% prova desperdício | substituída | Afirmações absolutas incorretas; observar prefixos, chamadas e contexto |
| Parar só em 400k / alertar em 200k | substituída | Configuração única atual em config/pipeline.json |

Relatos antigos preservados em ../historico/2026-09-22, sem autoridade operacional. Decisões de negócio de uma tarefa continuam válidas até serem explicitamente substituídas.
