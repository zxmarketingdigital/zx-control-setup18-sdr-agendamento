#!/usr/bin/env python3
"""Etapa 3: painel e página pública de agendamento no Cloudflare Pages.

Subcomandos:
  config     escreve web/config.js (URL e chave anon; nunca a service_role)
  demo       escreve web/config.js em modo demonstração (sem banco) e mostra como abrir
  local      serve a pasta web/ no seu computador para você conferir antes de publicar
  deploy     publica a pasta web/ no Cloudflare Pages (wrangler pages deploy)

O login do Cloudflare é feito pelo próprio Wrangler (wrangler login) ou pelas variáveis
CLOUDFLARE_API_TOKEN e CLOUDFLARE_ACCOUNT_ID. Nada disso passa por argumentos nem vai para o git.
"""
import argparse
import functools
import http.server
import os
import re
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import etapa_config  # noqa: E402
from _comum import (  # noqa: E402
    ENTREVISTA_PATH, REPO_ROOT, WEB_CONFIG_JS, WEB_DIR, achar_cli, aviso, ask, cfg, erro, info, ler_json,
    load_config, ok, rodar, salvar_estado, slugificar, supabase_rest, titulo,
)

_PROJETO = re.compile(r"^[a-z0-9][a-z0-9-]{1,56}[a-z0-9]$")
_PAGES_URL = re.compile(r"https://[a-z0-9-]+(?:\.[a-z0-9-]+)*\.pages\.dev")
_SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def slug_publico():
    """Slug da página pública: banco (schedule_settings), depois entrevista, depois 'agendar'."""
    config = load_config()
    if cfg(config, "supabase", "url") and cfg(config, "supabase", "service_role_key"):
        st, corpo = supabase_rest(config, "GET", "/schedule_settings?select=public_slug&id=eq.1")
        if st == 200 and isinstance(corpo, list) and corpo and corpo[0].get("public_slug"):
            return str(corpo[0]["public_slug"]).strip().lower()
    dados = ler_json(ENTREVISTA_PATH)
    ag = (dados.get("agenda") if isinstance(dados, dict) else None) or {}
    slug = str(ag.get("slug") or "agendar").strip().lower() if isinstance(ag, dict) else "agendar"
    return slug if _SLUG_OK.match(slug) else "agendar"


def link_publico(base, slug):
    """Link que o lead abre: o motor da página lê o slug do último trecho depois de /agendar."""
    return "%s/agendar/%s" % (str(base).rstrip("/"), slug)


def escolher_url(saida, projeto):
    """URL de produção do deploy: a do projeto (https://<projeto>.pages.dev) se aparecer na saída; senão a última citada; senão a padrão."""
    achadas = _PAGES_URL.findall(saida or "")
    padrao = "https://%s.pages.dev" % projeto
    if padrao in achadas:
        return padrao
    # Deploy de produção não tem hash antes do projeto (<hash>.<projeto>.pages.dev é preview).
    producao = [u for u in achadas if u.count(".") == 2]
    if producao:
        return producao[-1]
    return padrao


class _ServidorLocal(http.server.SimpleHTTPRequestHandler):
    """Serve web/ e entrega a página de agendamento para /agendar/<slug>, como o _redirects do Cloudflare Pages."""

    def translate_path(self, path):
        caminho = path.split("?", 1)[0].split("#", 1)[0]
        partes = [p for p in caminho.split("/") if p]
        # Igual à regra "/agendar/:slug  /agendar/  200" do web/_redirects: no Cloudflare a regra vale
        # antes dos arquivos, então qualquer /agendar/<algo> vira a página (inclusive "x.js").
        if len(partes) == 2 and partes[0] == "agendar":
            path = "/agendar/index.html"
        return super().translate_path(path)


def nome_projeto_padrao():
    dados = ler_json(ENTREVISTA_PATH)
    empresa = ((dados.get("negocio") or {}).get("empresa") if isinstance(dados, dict) else "") or "meu-negocio"
    return (slugificar(empresa) + "-sdr")[:58].strip("-")


def cmd_config(args):
    return etapa_config.main(["web-js"] + (["--cf-beacon-token", args.cf_beacon_token] if args.cf_beacon_token else []))


def cmd_demo(args):
    codigo = etapa_config.main(["web-js", "--demo"])
    if codigo == 0:
        info("Para abrir: python3 setup/etapa_web.py local   e depois acesse http://localhost:8788")
    return codigo


def cmd_local(args):
    if not WEB_CONFIG_JS.exists():
        erro("web/config.js não existe. Rode 'config' (com banco) ou 'demo' (sem banco).")
        return 1
    slug = slug_publico()
    handler = functools.partial(_ServidorLocal, directory=str(WEB_DIR))
    try:
        servidor = http.server.ThreadingHTTPServer(("127.0.0.1", args.porta), handler)
    except OSError as exc:
        erro("Não consegui abrir a porta %s: %s" % (args.porta, exc))
        return 1
    info("Servindo web/ em http://localhost:%s (Ctrl+C para parar). Painel em /, agendamento em /agendar/%s." % (args.porta, slug))
    timer = threading.Timer(args.minutos * 60, servidor.shutdown)
    timer.daemon = True
    timer.start()
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        timer.cancel()
        servidor.server_close()
    return 0


def cmd_deploy(args):
    if not WEB_CONFIG_JS.exists():
        erro("web/config.js não existe. Rode primeiro: python3 setup/etapa_web.py config")
        return 1
    conteudo = WEB_CONFIG_JS.read_text(encoding="utf-8")
    if '"demo": true' in conteudo and not args.permitir_demo:
        erro("web/config.js está em modo demonstração. Rode 'config' para usar seu banco, ou --permitir-demo.")
        return 1
    if "service_role" in conteudo or "sb_secret_" in conteudo:
        erro("web/config.js parece conter uma chave privada. Use só a chave anon (pública).")
        return 1

    projeto = args.projeto or ler_json(REPO_ROOT / "data" / "estado.json").get("pages_projeto") or ask("Nome do projeto no Cloudflare Pages", nome_projeto_padrao(), env="SDR_PAGES_PROJETO")
    projeto = projeto.strip().lower()
    if not _PROJETO.match(projeto):
        erro("Nome inválido. Use 3 a 58 caracteres: letras minúsculas, números e hífen.")
        return 1

    cli = achar_cli("wrangler")
    if not cli:
        erro("Não encontrei o Wrangler nem o npx. Instale o Node.js (nodejs.org) e rode de novo.")
        return 1

    if args.dry_run:
        titulo("Simulação (nada foi publicado)")
        print("  %s pages project create %s --production-branch main" % (" ".join(cli), projeto))
        print("  %s pages deploy web --project-name %s --branch main" % (" ".join(cli), projeto))
        return 0

    info("Criando o projeto '%s' (se já existir, segue)..." % projeto)
    cod, saida = rodar(cli + ["pages", "project", "create", projeto, "--production-branch", "main"], cwd=REPO_ROOT)
    if cod != 0 and "already exists" not in saida.lower() and "already" not in saida.lower():
        erro("Não consegui criar o projeto no Cloudflare: %s" % " ".join(saida.split())[:300])
        info("Se pediu login, rode 'wrangler login' (ou defina CLOUDFLARE_API_TOKEN e CLOUDFLARE_ACCOUNT_ID) e tente de novo.")
        return 1

    info("Publicando a pasta web/ ...")
    cod, saida = rodar(cli + ["pages", "deploy", "web", "--project-name", projeto, "--branch", "main", "--commit-dirty=true"], cwd=REPO_ROOT)
    if cod != 0:
        erro("O deploy falhou: %s" % " ".join(saida.split())[:400])
        return 1
    url = escolher_url(saida, projeto)
    slug = slug_publico()
    salvar_estado({"pages_projeto": projeto, "pages_url": url, "public_slug": slug})
    ok("Painel publicado: %s" % url)
    print("      Página pública de agendamento (link para os leads): %s" % link_publico(url, slug))
    titulo("Falta uma configuração no Supabase")
    print("  Painel do Supabase > Authentication > URL Configuration:")
    print("    - Site URL: %s" % url)
    print("    - Redirect URLs: %s/**" % url)
    print("  Depois entre no painel com o e-mail e a senha do dono (Etapa 2).")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("config")
    s.add_argument("--cf-beacon-token", default="")
    s.set_defaults(fn=cmd_config)
    s = sub.add_parser("demo")
    s.set_defaults(fn=cmd_demo)
    s = sub.add_parser("local")
    s.add_argument("--porta", type=int, default=8788)
    s.add_argument("--minutos", type=int, default=30, help="desliga sozinho depois de N minutos")
    s.set_defaults(fn=cmd_local)
    s = sub.add_parser("deploy")
    s.add_argument("--projeto")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--permitir-demo", action="store_true")
    s.set_defaults(fn=cmd_deploy)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
