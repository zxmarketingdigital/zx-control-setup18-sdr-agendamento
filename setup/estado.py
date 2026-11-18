#!/usr/bin/env python3
"""Mostra em que etapa do Setup 18 você está, sem precisar de rede nem de configuração pronta.

Uso:
  python3 setup/estado.py status [--json]    situação das 7 etapas e a próxima ação
  python3 setup/estado.py proxima            só a próxima etapa pendente
  python3 setup/estado.py marcar <chave>     grava uma marca manual (ex.: teste_ok, producao_liberada)
  python3 setup/estado.py limpar <chave>     remove uma marca

Funciona mesmo antes da Etapa 1: sem config/config.json e sem data/entrevista.json, tudo aparece como pendente.
Não depende de PyYAML.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _comum import (  # noqa: E402
    ENTREVISTA_PATH, ESTADO_PATH, ETAPAS, WEB_CONFIG_JS, aviso, cfg, erro, gravar_json, ler_json, load_config,
    load_estado, ok, salvar_estado, titulo,
)

MARCAS_VALIDAS = ("banco_migrado", "dono_email", "pages_url", "servico_instalado", "teste_ok", "producao_liberada")

PROXIMO_PASSO = {
    "1": 'Diga "INICIAR SETUP" no Claude Code e responda a entrevista (ou rode: python3 setup/etapa_config.py modelo).',
    "2": "python3 setup/etapa_banco.py tudo",
    "3": "python3 setup/etapa_config.py banco\n     depois: python3 setup/etapa_web.py config\n     depois: python3 setup/etapa_web.py deploy",
    "4": "python3 setup/etapa_config.py evolution --criar\n     depois: python3 setup/etapa_config.py ia",
    "5": "python3 setup/servico.py install",
    "6": "python3 setup/teste_guiado.py",
    "7": "python3 setup/liberar_producao.py",
}


def _entrevista_ok():
    dados = ler_json(ENTREVISTA_PATH)
    if not isinstance(dados, dict) or not dados:
        return False, "ainda não há data/entrevista.json"
    faltam = [c for c in ("negocio", "nicho", "agente", "dono", "closers") if not dados.get(c)]
    if faltam:
        return False, "entrevista incompleta (falta: %s)" % ", ".join(faltam)
    return True, "%s, nicho %s" % ((dados.get("negocio") or {}).get("empresa", "?"), dados.get("nicho"))


def avaliar():
    """Devolve lista de (id, nome, concluida, detalhe). Só lê arquivos locais."""
    config = load_config()
    estado = load_estado()
    resultado = []

    feito, det = _entrevista_ok()
    resultado.append(("1", feito, det))

    tem_chaves = all(cfg(config, "supabase", c) for c in ("url", "service_role_key", "anon_key"))
    if not tem_chaves:
        resultado.append(("2", False, "faltam credenciais do Supabase em config/config.json"))
    elif not estado.get("banco_migrado"):
        resultado.append(("2", False, "credenciais ok; falta aplicar e verificar a migration"))
    elif not estado.get("dono_email"):
        resultado.append(("2", False, "banco pronto; falta criar o usuário dono"))
    else:
        resultado.append(("2", True, "banco pronto, dono %s" % estado["dono_email"]))

    if not WEB_CONFIG_JS.exists():
        resultado.append(("3", False, "web/config.js ainda não foi gerado"))
    elif not estado.get("pages_url"):
        resultado.append(("3", False, "web/config.js gerado; falta publicar no Cloudflare Pages"))
    else:
        resultado.append(("3", True, estado["pages_url"]))

    evo = all(cfg(config, "evolution", c) for c in ("url", "api_key", "instance"))
    ia = bool(cfg(config, "ai", "api_key"))
    if evo and ia:
        resultado.append(("4", True, "Evolution (%s) e IA (%s) configuradas" % (cfg(config, "evolution", "instance"), cfg(config, "ai", "provider", default="?"))))
    else:
        falta = [n for n, v in (("Evolution", evo), ("chave de IA", ia)) if not v]
        resultado.append(("4", False, "falta: %s" % ", ".join(falta)))

    resultado.append(("5", bool(estado.get("servico_instalado")), "serviço instalado" if estado.get("servico_instalado") else "o motor ainda não roda sozinho"))
    resultado.append(("6", bool(estado.get("teste_ok")), "teste guiado aprovado" if estado.get("teste_ok") else "teste guiado ainda não aprovado"))
    resultado.append(("7", bool(estado.get("producao_liberada")), "produção liberada" if estado.get("producao_liberada") else "agente ainda em modo teste"))

    nomes = dict(ETAPAS)
    return [(i, nomes[i], c, d) for i, c, d in resultado]


def cmd_status(args):
    etapas = avaliar()
    proxima = next((e for e in etapas if not e[2]), None)
    if args.json:
        print(json.dumps({
            "etapas": [{"id": i, "nome": n, "concluida": c, "detalhe": d} for i, n, c, d in etapas],
            "proxima": proxima[0] if proxima else None,
        }, ensure_ascii=False, indent=2))
        return 0
    titulo("Setup 18 — Agente SDR de Agendamento")
    for i, nome, concluida, detalhe in etapas:
        marca = "✅" if concluida else ("👉" if proxima and proxima[0] == i else "⬜")
        print(" %s Etapa %s — %s" % (marca, i, nome))
        print("      %s" % detalhe)
    print()
    if proxima:
        print("Próxima etapa: %s — %s" % (proxima[0], proxima[1]))
        print("Comando: %s" % PROXIMO_PASSO[proxima[0]])
    else:
        ok("Todas as etapas concluídas. O agente está no ar.")
    return 0


def cmd_proxima(args):
    proxima = next((e for e in avaliar() if not e[2]), None)
    if not proxima:
        print("nenhuma")
        return 0
    print("%s\t%s\t%s" % (proxima[0], proxima[1], PROXIMO_PASSO[proxima[0]]))
    return 0


def cmd_marcar(args):
    if args.chave not in MARCAS_VALIDAS:
        erro("Marca desconhecida. Use uma destas: %s" % ", ".join(MARCAS_VALIDAS))
        return 2
    salvar_estado({args.chave: args.valor if args.valor else True})
    ok("Marca '%s' gravada em %s" % (args.chave, ESTADO_PATH))
    return 0


def cmd_limpar(args):
    estado = load_estado()
    if args.chave not in estado:
        aviso("A marca '%s' não existe." % args.chave)
        return 0
    estado.pop(args.chave)
    gravar_json(ESTADO_PATH, estado, 0o600)
    ok("Marca '%s' removida" % args.chave)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("proxima")
    s.set_defaults(fn=cmd_proxima)
    s = sub.add_parser("marcar")
    s.add_argument("chave")
    s.add_argument("valor", nargs="?")
    s.set_defaults(fn=cmd_marcar)
    s = sub.add_parser("limpar")
    s.add_argument("chave")
    s.set_defaults(fn=cmd_limpar)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
