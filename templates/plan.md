# Plano — [ID] Título

> Baseado em `task-spec.md`. Ancorar em `erp-agent/projeto/ia_guidelines.md`.
> **CP1: este arquivo precisa da tua aprovação antes de o Gemini codar.**

## 1. Abordagem técnica
Resumo da estratégia. Qual padrão (Service/Repository/Action), qual camada.

## 2. Passo a passo (ordem de execução)
Sequência concreta por arquivo/camada. Base: full_implementation
(database_modeling → backend → frontend → test → code_review).
1. ...
2. ...

## 3. Modelo de dados / migrations
Entidades novas/alteradas. **Migration incremental — nunca fresh/wipe/rollback.**
- Índice necessário para algum filtro/join novo? Quais colunas?

## 4. Contratos / API  *(movido do intake — só dá pra saber aqui)*
- Endpoints afetados, método, URI (snake_case).
- Payload entrada (Form Request) e saída (JsonResource).
- Quebra algum consumidor existente? (breaking change)

## 5. Performance  *(movido do intake — só dá pra saber aqui)*
- Índice para o filtro/join?
- Risco de N+1 → usar `whenLoaded()` / eager loading.
- `SELECT *` em tabela grande?  - Listagem paginada?

## 6. Autorização
Permissões Spatie a criar/usar (`Modulo.acao`). Onde é checada.

## 7. Testes a gerar
Cenários mínimos (Unit / Feature / Integration), padrão AAA, incluindo casos de erro.

## 8. Riscos e pontos de decisão
O que pode dar errado, dependências, onde o Dev deve parar e devolver se sair do plano.
