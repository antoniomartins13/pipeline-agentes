# Review — [ID] Título

> Auditoria do branch/diff contra `plan.md` e `ia_guidelines.md`.
> Review **não implementa** — reporta e devolve snippets. **CP3 antes do MR.**

## Escopo revisado
Branch / commits / arquivos.

## Aderência arquitetural
- Controller no padrão do `make:crud` (`BaseController`, Service injetado, `setDefaultPermissions` + `set*Permissions`, Form Request na assinatura)? Sem query, sem `middleware('permission:...')` manual, sem `PermissionHelper` direto?
- Mapa rota → permissão conferido método a método (e comparado antes/depois em refatoração)?
- Arquivos compartilhados fora do módulo (`git diff origin/homologacao --stat`): cada um justificado? Quem testa foi avisado de quais outras rotas testar?
- Métodos longos quebrados; filtros repetidos extraídos?
- Regra em Service/Action; persistência em Repository?
- Resposta via `JsonResource` com `whenLoaded()`?
- Frontend: componente "burro", lógica em Hooks/Context?
- Frontend: `PageCardHeader` + primeiro card sem nada solto entre eles; "Voltar" em detalhe/formulário; cadastros sem coluna ID?
- Frontend: lint `react/jsx-no-undef`/`no-undef` limpo **e** cada rota aberta no navegador sem exceção no console?

## Segurança
- Autorização Spatie aplicada (usuário logado pode mesmo isso)?
- Input validado por Form Request?
- Dado sensível exposto em resposta/log?

## Performance
- N+1 visível? Eager loading presente?
- Índice para filtro/join?  - Paginação?  - `SELECT *`?

## Edge cases
Coleção vazia / nulo / falha externa tratados? O que o usuário vê no erro?

## Testes
- Testes novos presentes (Unit/Feature/Integration)?
- Padrão AAA, anotações `#[Test]` (não `testNome`)?
- **Suíte COMPLETA do back** (`php artisan test`) — não só o grupo do módulo. (colar resumo)
- **Build do front** (`npm run build`) do repo afetado. (colar resumo)
- Falha fora do escopo? Reportar + MR separado (não misturar). Não aprovar MR com pipeline vermelha.

## Veredito
- [ ] ✅ Aprovado
- [ ] 🔁 Mudanças necessárias → lista + snippets pro Dev aplicar:
