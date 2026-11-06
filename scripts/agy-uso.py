#!/usr/bin/env python3
"""Consumo do agy/Gemini — proxy de quota.

Por que invocacoes e nao dolares: o agy NAO expoe contagem de tokens (verificado em 15/09 —
nem no transcript, nem no SQLite, nem no payload do PostInvocation). E como o plano e assinatura,
USD nao e a metrica: o que importa e quanto da janela foi consumido.

Fonte: ~/.gemini/config/agy-uso.jsonl, alimentado pelo hook PostInvocation.

Uso:  python agy-uso.py [--dias N]
"""
import argparse, collections, json, os, sys
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8")
ARQ = os.path.expanduser("~/.gemini/config/agy-uso.jsonl")

ap = argparse.ArgumentParser()
ap.add_argument("--dias", type=int, default=0, help="considerar apenas os ultimos N dias")
a = ap.parse_args()

if not os.path.exists(ARQ):
    sys.exit(f"Sem dados ainda: {ARQ}\nO hook PostInvocation grava a partir da proxima execucao do agy.")

registros = []
for linha in open(ARQ, encoding="utf-8-sig"):
    linha = linha.strip()
    if not linha:
        continue
    try:
        registros.append(json.loads(linha))
    except Exception:
        continue

if a.dias:
    limite = datetime.now().astimezone() - timedelta(days=a.dias)
    def recente(r):
        try:
            return datetime.fromisoformat(r["ts"]) >= limite
        except Exception:
            return True
    registros = [r for r in registros if recente(r)]

if not registros:
    sys.exit("Nenhum registro no periodo.")

por_dia     = collections.Counter()
por_modelo  = collections.Counter()
por_ws      = collections.Counter()
conversas   = collections.defaultdict(int)

for r in registros:
    por_dia[(r.get("ts") or "?")[:10]] += 1
    por_modelo[r.get("modelName") or "?"] += 1
    ws = r.get("workspace") or "(sem workspace)"
    por_ws[os.path.basename(ws.rstrip("\\/")) or ws] += 1
    conversas[r.get("conversationId")] += 1

print("=" * 66)
print(f"CONSUMO DO AGY/GEMINI — {len(registros)} invocações em {len(conversas)} conversas")
print("=" * 66)

print("\nPor modelo (modelos diferentes pesam diferente na quota):")
for m, n in por_modelo.most_common():
    print(f"  {m:<28} {n:>5}")

print("\nPor dia:")
for d in sorted(por_dia):
    print(f"  {d}   {por_dia[d]:>5}")

print("\nPor workspace (aproxima invocações por tarefa):")
for w, n in por_ws.most_common(15):
    print(f"  {w:<40} {n:>5}")

med = len(registros) / max(len(conversas), 1)
print(f"\nMédia de invocações por conversa: {med:.1f}")
print("Conversas mais pesadas:")
for c, n in sorted(conversas.items(), key=lambda x: -x[1])[:5]:
    print(f"  {str(c)[:8]}…  {n:>5}")
