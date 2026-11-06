# Especificação Técnica — {NOME DA FUNCIONALIDADE / MÓDULO}

> **Como usar este template**
>
> - Documento **interno de engenharia**. Traduz a solicitação da área em decisões implementáveis. Não é o formulário que o solicitante preenche.
> - Blocos em `> [orientação]` explicam o que escrever e **devem ser removidos** na versão final.
> - Placeholders no formato `{assim}` são para substituir.
> - Todo conteúdo é **normativo** por padrão. O que for exemplo deve ser marcado explicitamente como *ilustrativo*, senão será implementado ao pé da letra.
> - Lacuna sem resposta vira `PENDENTE` com a decisão a tomar e quem responde. Nunca preencha por suposição — pendência visível custa menos que improviso do dev.
> - Regras de negócio recebem identificador (`RN-01`, `RN-02`…) e são referenciadas por telas, fluxos e critérios de aceite.
> - Seções que não se aplicam ficam com "Não se aplica — {motivo}". Não apague a seção.

---

## 1. Objetivo e contexto

### Problema
> [Situação atual e o que ela custa — retrabalho, risco, demora, perda de informação. Se não dá pra escrever o problema sem citar a solução, a discovery ainda não aconteceu.]

### Valor
> [O que muda quando isso existir. Uma ou duas frases.]

### Objetivo funcional
> [O que o sistema deve permitir fazer, em uma frase objetiva.]

---

## 2. Glossário

> [Só termos do domínio que geram ambiguidade. Vocabulário duplicado bifurca o modelo de dados.]

| Termo | Definição |
|---|---|
| | |

---

## 3. Escopo

### 3.1 Dentro do escopo
-

### 3.2 Fora do escopo
-

---

## 4. Atores e matriz de permissões

### Perfis

| Perfil | Descrição |
|---|---|
| | |

### Matriz perfil × ação

> [Célula vazia = negado explicitamente. Não existe permissão implícita. Liste toda ação com efeito colateral, não só as telas.]

| Ação | {Perfil A} | {Perfil B} | {Perfil C} |
|---|:---:|:---:|:---:|
| | | | |

**Escopo de visibilidade**
> [Vê registros de todos os setores ou só do próprio? Retrofit de escopo é caro.]

---

## 5. Modelo de dados

### 5.1 Entidades

#### `{nome_da_tabela}`

| Campo | Tipo | Obrig. | Observação |
|---|---|:---:|---|
| `id` | BIGINT UNSIGNED PK | ✔ | |

> [Para cada decisão não óbvia de modelagem, registre o porquê técnico em uma linha.]

**Índices:**

#### Relacionamentos N:N

| Pivot | Colunas | Uso |
|---|---|---|
| | | |

### 5.2 Diagrama de relacionamento

```
{esboço em ASCII ou referência ao anexo}
```

---

## 6. Regras de negócio

> [Uma regra por item, numerada e testável por quem não participou da conversa.]

### [Grupo]
- **RN-xx** —

### Ciclo de vida

> [Estado sem transição documentada vira comportamento inventado na implementação.]
- **RN-xx** —
```
[estado A] ──ação──> [estado B] ──ação──> [estado C]
```

| De | Para | Perfil | Condição |
|---|---|---|---|
| | | | |

---

## 7. Navegação e ponto de entrada

```
{Menu}
├── {item}      {descrição}
└── {item}      {descrição}
```

---

## 8. Telas

> [Toda tela implementa quatro estados: carregando, vazio, erro e sem permissão. Especificar só o caminho feliz é a principal fonte de retrabalho em homologação.]

### 8.1 {Nome da tela}

**Layout:**

**Campos / colunas:**

**Ações:**

*Exemplo ilustrativo:*

**Estados**

| Estado | Comportamento |
|---|---|
| Carregando | |
| Vazio (sem dados) | |
| Vazio (filtro sem resultado) | |
| Erro | |
| Sem permissão | |

> [Distinguir "não há nada" de "seu filtro não achou nada" evita chamado de suporte.]

---

## 9. Fluxos

> [Fluxo tem ator, pré-condição, passos, alternativos e exceções. Setas entre telas é navegação, não fluxo.]

### F-01 — {Nome do fluxo}

- **Ator:**
- **Pré-condição:**

**Fluxo principal**
1.
2.

**Alternativos**
- 2a.

**Exceções**
-

---

## 10. Requisitos não-funcionais

### 10.1 Volumetria
> [Sem número, a arquitetura é escolhida no chute e falha no primeiro uso real.]

| Item | Estimativa |
|---|---|
| | |

### 10.3 Performance

| Operação | Meta |
|---|---|
| | |

> [Riscos conhecidos: N+1, operação síncrona que deveria ser assíncrona, listagem sem paginação, consulta sem índice.]

### 10.4 Retenção e privacidade
> [Em órgão público isso é requisito, não recomendação.]

- **Dados pessoais tratados:**
- **Base legal:**
- **Prazo de retenção:**
- **Retenção da auditoria:**

### 10.5 Disponibilidade e limites
-

---

## 11. Integrações e impacto no existente

| Ponto | Descrição | Impacto |
|---|---|---|
| | | |

**Riscos de impacto**
> [Recurso compartilhado — fila, storage, banco — é o suspeito habitual.]
-

---

## 12. Migração de dados legados

> [Se existe base anterior e não for migrada, o novo sistema nasce parcial. Causa mais comum de baixa adoção.]

**A levantar com a área**
1. Onde está o dado atual e qual o volume?
2. Qual a qualidade e a estrutura?
3. Migra tudo ou um recorte?
4. Quem valida o resultado?

**Abordagem**
-

---

## 13. Critérios de aceite

> [Dado/Quando/Então, rastreado a uma RN. Cubra também os caminhos infelizes — sem permissão, dado inválido, falha parcial, estado vazio.]

### {Agrupamento}

**CA-01** (`RN-xx`)
Dado que {contexto}
Quando {ação}
Então {resultado observável}

---

## 14. Anexos

| Item | Status |
|---|---|
| Solicitação original | |
| Wireframes | |
| Protótipo | |
| Documento normativo de referência | |

---

## Apêndice — Pendências bloqueantes

> [Itens que alteram modelo de dados, infraestrutura ou base legal vêm primeiro.]

| # | Pendência | Seção | Quem responde | Prazo |
|---|---|---|---|---|
| 1 | | | | |
