# Documentação Técnica — {NOME DO MÓDULO / FUNCIONALIDADE}

> **Como usar este template**
>
> - Documento de **referência**: descreve como o módulo **funciona hoje**, para quem vai mexer depois. Não é plano de mudança — para especificar implementação nova, use a Especificação Técnica.
> - Escreva no presente ("o sistema calcula"), nunca no futuro ("o sistema deve calcular"). Se está descrevendo algo que ainda não existe, não é esse o template.
> - Blocos em `> [orientação]` devem ser removidos na versão final. Placeholders `{assim}` são para substituir.
> - Regras de negócio mantêm o mesmo identificador (`RN-01`…) da especificação — é isso que dá rastreabilidade entre os dois documentos.
> - Seções que não se aplicam ficam com "Não se aplica — {motivo}". Não apague a seção.
> - Documento **vivo**: atualize quando o comportamento mudar. Documentação desatualizada engana em vez de admitir que falta.

---

## 1. Visão geral

### O que é
> [Uma ou duas frases, sem jargão interno.]

### Para quem serve
> [Quais perfis usam e para qual finalidade.]

### Onde vive no sistema

| Camada | Localização |
|---|---|
| Backend | `{paths principais}` |
| Frontend | `{paths principais}` |
| Tabelas | `{tabelas principais}` |

---

## 2. Glossário

| Termo | Definição |
|---|---|
| | |

---

## 3. O que o módulo faz e não faz

### 3.1 Cobre
-

### 3.2 Não cobre
> [Fronteiras reais, hoje. Se algo relacionado existe em outro módulo, referencie aqui.]
-

---

## 4. Atores e permissões

### Perfis

| Perfil | Descrição |
|---|---|
| | |

### Matriz perfil × ação (como está implementado hoje)

| Ação | {Perfil A} | {Perfil B} | {Perfil C} |
|---|:---:|:---:|:---:|
| | | | |

**Escopo de visibilidade:**

---

## 5. Modelo de dados

### 5.1 Entidades

#### `{nome_da_tabela}`

| Campo | Tipo | Obrig. | Observação |
|---|---|:---:|---|
| `id` | BIGINT UNSIGNED PK | ✔ | |

> [Se souber o porquê de uma decisão de modelagem, registre; se não souber, marque "motivo não documentado" em vez de inventar.]

**Índices:**

### 5.2 Diagrama de relacionamento

```
{esboço em ASCII ou referência ao anexo}
```

---

## 6. Regras de negócio vigentes

> [O que o sistema faz hoje, com base no código — não o que "deveria" fazer.]

### [Grupo]
- **RN-xx** —

### Ciclo de vida

```
[estado A] ──ação──> [estado B] ──ação──> [estado C]
```

| De | Para | Perfil | Condição |
|---|---|---|---|
| | | | |

---

## 7. Navegação

```
{Menu}
├── {item}      {descrição}
└── {item}      {descrição}
```

---

## 8. Telas

### 8.1 {Nome da tela}

**O que faz:**

**Campos / colunas:**

**Ações disponíveis:**

*Exemplo ilustrativo:*

---

## 9. Como usar — passo a passo

> [Fluxo operacional real de quem usa: "para fazer X, faça Y, depois Z".]

### {Tarefa comum}
1.
2.

---

## 10. Integrações

| Ponto | Descrição |
|---|---|
| | |

---

## 11. Limitações e dívida técnica conhecidas

> [Bugs conhecidos, comportamento inconsistente, gambiarra documentada, decisão questionável mas deliberada. Para o próximo dev não redescobrir o mesmo problema do zero.]

| # | Limitação | RN / seção relacionada | Desde quando se sabe |
|---|---|---|---|
| | | | |

---

## 12. Histórico de mudanças relevantes

> [Não é changelog de commit — é o resumo de decisões/mudanças de comportamento que alguém debugando precisaria saber. Mais recente primeiro.]

| Data | Mudança | Referência |
|---|---|---|
| | | |

---

## 13. Anexos

| Item | Status |
|---|---|
| Especificação técnica original (se existir) | |
| Wireframes / protótipo | |
| Auditorias ou levantamentos relacionados | |
