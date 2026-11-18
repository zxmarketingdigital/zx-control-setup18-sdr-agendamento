#!/usr/bin/env python3
"""Confere o computador antes de instalar o Setup 18: Python, Supabase CLI, Wrangler/npx, Git e PyYAML.

Sai com código 0 se tudo que é obrigatório está disponível e 1 se falta algo.
Não altera nada no computador (a instalação do PyYAML é só sugerida).
"""
import os
import platform
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _comum import CONFIG_PATH, REPO_ROOT, _timeout_env, rodar  # noqa: E402

PYTHON_MINIMO = (3, 10)
COMANDO_TIMEOUT = _timeout_env("ZX_SDR_PREREQ_TIMEOUT", 30)


def _linha(status, nome, detalhe, instrucao=""):
    simbolo = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}[status]
    print("%-3s %-16s %s" % (simbolo, nome, detalhe))
    if status != "ok" and instrucao:
        print("      Resolva: %s" % instrucao)


def _versao(candidatos):
    """Tenta cada comando de versão; devolve (ok, texto)."""
    for comando in candidatos:
        if not shutil.which(comando[0]):
            continue
        codigo, saida = rodar(comando, timeout=COMANDO_TIMEOUT)
        if codigo == 0:
            return True, "%s: %s" % (comando[0], " ".join(saida.split())[:80] or "disponível")
    return False, "não encontrado ou não respondeu"


def verificar():
    """Devolve a lista de itens obrigatórios que faltam (vazia = ok)."""
    faltando = []
    so = platform.system()
    py_cmd = "python" if so == "Windows" else "python3"

    atual = sys.version_info[:2]
    if atual >= PYTHON_MINIMO:
        _linha("ok", "Python", "%d.%d (%s)" % (atual[0], atual[1], sys.executable))
    else:
        _linha("fail", "Python", "versão %d.%d" % atual,
               "instale o Python %d.%d ou mais recente" % PYTHON_MINIMO)
        faltando.append("Python")

    itens = [
        ("Git", [("git", "--version")], "instale o Git (git-scm.com) e abra um novo terminal"),
        ("Node/npx", [("node", "--version"), ("npx", "--version")], "instale o Node.js LTS (nodejs.org), que já inclui o npx"),
        ("Supabase CLI", [("supabase", "--version"), ("npx", "--yes", "supabase", "--version")],
         "instale a Supabase CLI (supabase.com/docs/guides/cli) ou deixe o npx disponível"),
        ("Wrangler", [("wrangler", "--version"), ("npx", "--yes", "wrangler", "--version")],
         "instale o Wrangler (npm install -g wrangler) ou deixe o npx disponível"),
    ]
    for nome, candidatos, instrucao in itens:
        ok_, detalhe = _versao(candidatos)
        _linha("ok" if ok_ else "fail", nome, detalhe, instrucao)
        if not ok_:
            faltando.append(nome)

    try:
        import yaml  # noqa: F401
        _linha("ok", "PyYAML", "instalado")
    except ImportError:
        _linha("fail", "PyYAML", "não instalado",
               "rode: %s -m pip install pyyaml" % py_cmd)
        faltando.append("PyYAML")

    # Opcionais (só avisam).
    if (REPO_ROOT / "supabase" / "migrations").is_dir() and any((REPO_ROOT / "supabase" / "migrations").glob("*.sql")):
        _linha("ok", "Migration", "supabase/migrations encontrada")
    else:
        _linha("warn", "Migration", "supabase/migrations/*.sql ausente",
               "atualize o repositório (git pull) antes da Etapa 2")
    if CONFIG_PATH.exists():
        _linha("ok", "config.json", "já existe (será mesclado, nunca apagado)")
    else:
        _linha("warn", "config.json", "ainda não existe (normal antes da Etapa 2)")
    if os.environ.get("CLAUDECODE") or shutil.which("claude"):
        _linha("ok", "Claude Code", "disponível")
    return faltando


def main():
    inicio = time.monotonic()
    print("Pré-requisitos do Setup 18 — Agente SDR de Agendamento (%s)\n" % platform.system())
    faltando = verificar()
    print("\nConferência concluída em %.1fs." % (time.monotonic() - inicio))
    if faltando:
        print("Faltam itens obrigatórios: %s." % ", ".join(faltando))
        return 1
    print("Tudo pronto para a Etapa 1.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
