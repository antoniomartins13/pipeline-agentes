# [ID] Título curto da tarefa

> Tamanho: **P / M / G**  (define os checkpoints — ver README)

## 1. Contexto e objetivo
Por que isso existe? Qual dor resolve? (o "porquê", não o "como")

## 2. Regras de negócio e fluxo
Passo a passo do comportamento esperado. Estados, transições, exceções de negócio.
Quem dispara, o que acontece, qual o resultado.

## 3. Escopo — fronteiras
- ✅ Dentro: ...
- ❌ Fora (explicitamente não fazer agora): ...

## 4. Repositórios / módulos afetados
Ex.: erp-back-end (Módulo/X), erp-front-end (pages/x). Conexões entre repos.

## 5. Dados (visão de negócio)
Quais entidades/informações a funcionalidade lida. (Migrations, índices e detalhe
técnico ficam no `plan.md`.)

## 6. Autorização e segurança (regra de negócio)
- Quem pode ver / alterar? (permissão Spatie: `Modulo.acao`)
- Algum dado sensível envolvido?

## 7. Edge cases e erro
Coleção vazia, nulo, valor fora de faixa, falha de serviço externo.
**O que o usuário vê quando dá errado?**

## 8. Critérios de aceite (Definition of Done)
Lista verificável do que precisa estar verdadeiro pra tarefa ser considerada pronta.

## 9. Impacto técnico conhecido (opcional)
Só se você JÁ souber que mexe em contrato de API, performance ou migration.
Senão, deixe em branco — o Plano define.
