#!/usr/bin/env python3
"""Operação do dia a dia do agente, sem abrir o painel.

Uso:
  python3 setup/operar.py status      leads, agendamentos e se o agente está pausado ou em modo teste
  python3 setup/operar.py pausar      o agente para de responder (as conversas ficam guardadas)
  python3 setup/operar.py retomar     o agente volta a responder
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _comum import aviso, cfg, erro, load_config, ok, supabase_rest, titulo  # noqa: E402


def _total(config, tabela, filtro):
    # PostgREST devolve o total no cabeçalho; como http_json não expõe cabeçalhos, contamos pelas linhas (limite 1000).
    st, corpo = supabase_rest(config, "GET", "/%s?select=id%s&limit=1000" % (tabela, filtro))
    return len(corpo) if st == 200 and isinstance(corpo, list) else None


def cmd_status(config):
    st, corpo = supabase_rest(config, "GET", "/agent_config?select=agent_name,company,paused,test_only&id=eq.1")
    if st != 200 or not corpo:
        erro("Não consegui ler agent_config (HTTP %s)." % st)
        return 1
    ac = corpo[0]
    titulo("%s (%s)" % (ac.get("agent_name"), ac.get("company") or "sem nome"))
    print("  Estado: %s" % ("PAUSADO" if ac.get("paused") else ("modo teste" if ac.get("test_only") else "em produção")))
    for rotulo, tabela, filtro in (
        ("Leads (até 1000)", "leads", ""),
        ("Agendados", "appointments", "&status=in.(agendado,confirmado)"),
        ("Mensagens na fila", "outbox", "&status=eq.pendente"),
        ("Mensagens com erro", "outbox", "&status=eq.erro"),
    ):
        n = _total(config, tabela, filtro)
        print("  %-22s %s" % (rotulo + ":", "?" if n is None else n))
    return 0


def cmd_pausa(config, pausar):
    st, corpo = supabase_rest(config, "PATCH", "/agent_config?id=eq.1", {"paused": pausar}, extra_headers={"Prefer": "return=minimal"})
    if st not in (200, 204):
        erro("Não consegui alterar (HTTP %s): %s" % (st, str(corpo)[:200]))
        return 1
    (aviso if pausar else ok)("Agente %s." % ("pausado: não responde ninguém até você retomar" if pausar else "retomado"))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("cmd", choices=("status", "pausar", "retomar"))
    args = p.parse_args(argv)
    config = load_config()
    if not cfg(config, "supabase", "url") or not cfg(config, "supabase", "service_role_key"):
        erro("Faltam as credenciais do Supabase. Faça a Etapa 2 primeiro.")
        return 1
    if args.cmd == "status":
        return cmd_status(config)
    return cmd_pausa(config, args.cmd == "pausar")


if __name__ == "__main__":
    sys.exit(main())
