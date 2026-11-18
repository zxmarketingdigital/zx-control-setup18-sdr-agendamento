#!/usr/bin/env python3
"""Etapa 7: tira o agente do modo teste para ele atender qualquer lead que chamar no WhatsApp.

Antes de liberar, confere:
  - o teste guiado foi aprovado (marca teste_ok);
  - nenhum campo do agent_config (conhecimento, tom, perguntas, regras, objeções, follow-ups, modelos de mensagem) tem [PREENCHER]:
    esses textos viram resposta do agente e poderiam chegar ao lead com o marcador dentro;
  - o motor está instalado como serviço (só avisa).

Uso:
  python3 setup/liberar_producao.py [--sim] [--aceitar-pendencias] [--forcar]
  python3 setup/liberar_producao.py --voltar-ao-teste      volta ao modo teste, sem apagar nada
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _comum import (  # noqa: E402
    aviso, cfg, confirmar, erro, info, load_config, load_estado, ok, salvar_estado, supabase_rest, titulo,
)

_PREENCHER = re.compile(r"\[PREENCHER\]")


def contar_pendencias(texto):
    return len(_PREENCHER.findall(texto or ""))


def pendencias_por_campo(registro):
    """{campo: quantidade} de [PREENCHER] em TODOS os campos do agent_config (texto ou JSON aninhado)."""
    achadas = {}
    for campo, valor in (registro or {}).items():
        texto = valor if isinstance(valor, str) else json.dumps(valor, ensure_ascii=False)
        n = contar_pendencias(texto)
        if n:
            achadas[campo] = n
    return achadas


def _patch(config, corpo):
    return supabase_rest(config, "PATCH", "/agent_config?id=eq.1", corpo, extra_headers={"Prefer": "return=minimal"})


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--sim", action="store_true", help="confirma sem perguntar")
    p.add_argument("--aceitar-pendencias", action="store_true", help="libera mesmo com [PREENCHER] em campos do agente")
    p.add_argument("--forcar", action="store_true", help="libera mesmo sem o teste guiado aprovado")
    p.add_argument("--voltar-ao-teste", action="store_true")
    args = p.parse_args(argv)

    config = load_config()
    if not cfg(config, "supabase", "url") or not cfg(config, "supabase", "service_role_key"):
        erro("Faltam as credenciais do Supabase. Faça a Etapa 2 primeiro.")
        return 1

    if args.voltar_ao_teste:
        st, corpo = _patch(config, {"test_only": True})
        if st not in (200, 204):
            erro("Não consegui voltar ao modo teste (HTTP %s): %s" % (st, str(corpo)[:200]))
            return 1
        salvar_estado({"producao_liberada": False})
        ok("O agente voltou ao modo teste: só os telefones de teste recebem resposta.")
        return 0

    st, corpo = supabase_rest(config, "GET", "/agent_config?select=*&id=eq.1")
    if st != 200 or not corpo:
        erro("Não consegui ler agent_config (HTTP %s)." % st)
        return 1
    ac = corpo[0]
    if not ac.get("company"):
        erro("O agente ainda não foi configurado. Rode: python3 setup/etapa_config.py banco")
        return 1
    if not ac.get("test_only"):
        ok("O agente já está em produção. Nada a fazer.")
        salvar_estado({"producao_liberada": True})
        return 0

    problemas = []
    if not load_estado().get("teste_ok") and not args.forcar:
        problemas.append("O teste guiado ainda não foi aprovado. Rode: python3 setup/teste_guiado.py")
    pend = pendencias_por_campo(ac)
    if pend and not args.aceitar_pendencias:
        onde = ", ".join("%s (%d)" % (c, n) for c, n in sorted(pend.items()))
        problemas.append("Há [PREENCHER] em: %s. Esses textos podem chegar ao lead. Complete no painel (tela de configuração do agente) "
                         "ou rode com --aceitar-pendencias." % onde)
    if ac.get("paused"):
        problemas.append("O agente está pausado. Reative no painel antes de liberar.")
    if problemas:
        titulo("Ainda não dá para liberar")
        for x in problemas:
            erro(x)
        return 1
    if not load_estado().get("servico_instalado"):
        aviso("O motor ainda não roda como serviço (Etapa 5). Sem ele o agente só responde enquanto você deixar o motor aberto.")

    titulo("Liberar produção")
    print("  Depois disto, %s (%s) responde QUALQUER pessoa que chamar no WhatsApp conectado." % (ac.get("agent_name"), ac.get("company")))
    if not args.sim and not confirmar("Liberar agora?", False):
        info("Nada foi alterado.")
        return 1
    st, corpo = _patch(config, {"test_only": False})
    if st not in (200, 204):
        erro("Não consegui liberar (HTTP %s): %s" % (st, str(corpo)[:200]))
        return 1
    salvar_estado({"producao_liberada": True})
    ok("Produção liberada. Para voltar ao modo teste: python3 setup/liberar_producao.py --voltar-ao-teste")
    return 0


if __name__ == "__main__":
    sys.exit(main())
