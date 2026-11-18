#!/usr/bin/env python3
"""Etapa 6: teste guiado. Confere cada peça do agente antes de você liberar a produção.

Passos:
  1. modo-teste   o agente está em modo teste e pausado = não (só os números de teste recebem resposta)
  2. motor        python -m motor check (Supabase, Evolution e IA respondem)
  3. simulacao    python -m motor simulate (uma conversa de ponta a ponta, sem enviar WhatsApp)
  4. horarios     a página pública enxerga horários livres (chave anon, só leitura)
  5. whatsapp     (opcional, --whatsapp) você manda "oi" de um número de teste e o agente responde

Uso:
  python3 setup/teste_guiado.py [--passos modo-teste,motor,...] [--whatsapp] [--sem-pagina-publica] [--dry-run]
A marca "teste_ok" só é gravada quando TODOS os passos do teste (modo-teste, motor, simulacao, horarios e, se
pedido, whatsapp) rodaram nesta execução e passaram. Execução parcial (--passos) não libera a Etapa 7.
"""
import argparse
import sys
import re
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _comum  # noqa: E402
from _comum import (  # noqa: E402
    REPO_ROOT, _timeout_env, aviso, ask, cfg, erro, http_json, info, load_config, ok, python_cmd, q, rodar,
    salvar_estado, supabase_rest, telefone_valido, titulo, normalizar_telefone,
)

# Tempo de espera pela resposta real do WhatsApp (segundos). Override: ZX_SDR_TEST_WAIT.
ESPERA_WHATSAPP = _timeout_env("ZX_SDR_TEST_WAIT", 180)
TEMPO_MOTOR = _timeout_env("ZX_SDR_TEST_MOTOR_TIMEOUT", 180)
PASSOS = ["modo-teste", "motor", "simulacao", "horarios", "whatsapp"]
PADRAO = ["modo-teste", "motor", "simulacao", "horarios"]
# Sem --sem-pagina-publica, o passo "horarios" exige a página pública ativa.



def _linha(texto):
    return " ".join(str(texto).split())[:300]


def passo_modo_teste(config, ctx):
    st, corpo = supabase_rest(config, "GET", "/agent_config?select=test_only,paused,test_phones,company,agent_name&id=eq.1")
    if st != 200 or not corpo:
        erro("Não consegui ler agent_config (HTTP %s). A Etapa 2 e a configuração (etapa_config.py banco) estão feitas?" % st)
        return False
    ac = corpo[0]
    if not ac.get("company"):
        erro("O agente ainda não foi configurado. Rode: python3 setup/etapa_config.py banco")
        return False
    ctx["telefones"] = ac.get("test_phones") or []
    if ac.get("paused"):
        erro("O agente está pausado. Reative no painel (tela de configuração do agente) antes de testar.")
        return False
    if ac.get("test_only") is False:
        aviso("O agente já está FORA do modo teste (produção liberada). O teste segue, mas qualquer lead real pode responder.")
    elif not ctx["telefones"]:
        erro("Nenhum telefone de teste cadastrado. Adicione ao menos um número no painel ou na entrevista.")
        return False
    ok("Agente '%s' em modo teste, telefones liberados: %s" % (ac.get("agent_name"), ", ".join(ctx["telefones"]) or "nenhum"))
    return True


def passo_motor(config, ctx):
    codigo, saida = rodar([python_cmd(), "-m", "motor", "check"], cwd=REPO_ROOT, timeout=TEMPO_MOTOR)
    if codigo != 0:
        erro("motor check falhou: %s" % _linha(saida))
        info("Veja a configuração em config/config.json e rode: python3 -m motor check")
        return False
    ok("motor check passou")
    return True


def passo_simulacao(config, ctx):
    telefone = ctx.get("telefone_simulacao") or (ctx.get("telefones") or [""])[0]
    if not telefone_valido(telefone):
        telefone = normalizar_telefone(ask("Número de teste (formato internacional, ex.: +5511999990000)"))
    if not telefone_valido(telefone):
        erro("Número inválido.")
        return False
    codigo, saida = rodar([python_cmd(), "-m", "motor", "simulate", "--phone", telefone, "--text", "Oi, quero saber mais"],
                          cwd=REPO_ROOT, timeout=TEMPO_MOTOR)
    if codigo != 0:
        erro("motor simulate falhou: %s" % _linha(saida))
        return False
    print("      Resposta simulada do agente:")
    for linha in saida.splitlines()[-12:]:
        print("        " + linha)
    ok("Simulação concluída")
    return True


def passo_horarios(config, ctx):
    anon = cfg(config, "supabase", "anon_key")
    url = str(cfg(config, "supabase", "url", default="")).rstrip("/")
    if not anon or not url:
        erro("Falta supabase.anon_key em config/config.json. Rode: python3 setup/etapa_banco.py credenciais --trocar-chaves")
        return False
    st, corpo = supabase_rest(config, "GET", "/schedule_settings?select=public_slug,public_enabled,days_ahead&id=eq.1")
    if st != 200 or not corpo:
        erro("Não consegui ler schedule_settings (HTTP %s)." % st)
        return False
    ss = corpo[0]
    if not ss.get("public_enabled") or not ss.get("public_slug"):
        if ctx.get("sem_pagina_publica"):
            aviso("Página pública desativada e você escolheu não usá-la (--sem-pagina-publica): este passo foi dispensado.")
            return True
        erro("A página pública está desativada, então os horários não foram testados. Ative a página pública na tela de agenda "
             "do painel e rode de novo; se você não vai usar a página, rode com --sem-pagina-publica.")
        return False
    agora = datetime.now(timezone.utc)
    st, corpo = http_json(
        "POST", url + "/rest/v1/rpc/public_slots",
        headers={"apikey": anon, "Authorization": "Bearer " + anon},
        corpo={"p_slug": ss["public_slug"], "p_from": agora.isoformat(), "p_to": (agora + timedelta(days=int(ss.get("days_ahead") or 14))).isoformat()},
    )
    if st != 200 or not isinstance(corpo, list):
        erro("A consulta pública de horários falhou (HTTP %s): %s" % (st, _linha(corpo)))
        return False
    if not corpo:
        erro("A página pública não tem nenhum horário livre. Confira os horários dos closers e a antecedência mínima.")
        return False
    ok("A página pública enxerga %d horário(s) livre(s); o primeiro é %s" % (len(corpo), corpo[0].get("starts_at")))
    return True


def _marca(valor):
    """ISO 8601 do banco -> datetime UTC; vazio ou inválido -> None."""
    try:
        texto = str(valor or "").strip().replace("Z", "+00:00")
        # Python < 3.11 só aceita fração de 3 ou 6 dígitos: normaliza qualquer fração de 1 a 9 dígitos para 6.
        texto = re.sub(r"\.(\d{1,9})", lambda m: "." + m.group(1)[:6].ljust(6, "0"), texto, count=1)
        dt = datetime.fromisoformat(texto)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _lead(config, telefone):
    st, corpo = supabase_rest(config, "GET", "/leads?select=id,stage,last_inbound_at,last_outbound_at&phone=eq.%s" % q(telefone))
    return corpo[0] if st == 200 and isinstance(corpo, list) and corpo else None


def resposta_nova(lead, inicio, mensagens=None):
    """True só se o lead tem mensagem recebida E resposta do agente DEPOIS de `inicio` (nunca histórico antigo).

    Com `mensagens` (linhas de messages do lead), exige também uma mensagem 'out' com author 'agente' posterior à
    mensagem do aluno: last_outbound_at sozinho não distingue a resposta do agente de um aviso antigo ou do dono."""
    if not lead:
        return False
    entrou, saiu = _marca(lead.get("last_inbound_at")), _marca(lead.get("last_outbound_at"))
    if not (entrou and saiu and entrou > inicio and saiu > inicio and saiu >= entrou):
        return False
    if mensagens is None:
        return True
    for m in mensagens:
        if not isinstance(m, dict):
            continue
        quando = _marca(m.get("created_at"))
        if m.get("direction") == "out" and m.get("author") == "agente" and quando and quando >= entrou:
            return True
    return False


def _mensagens_do_agente(config, lead_id, inicio):
    st, corpo = supabase_rest(config, "GET", "/messages?select=direction,author,created_at&lead_id=eq.%s&direction=eq.out"
                              "&author=eq.agente&created_at=gte.%s&order=created_at.desc&limit=20" % (q(lead_id), q(inicio.isoformat())))
    return corpo if st == 200 and isinstance(corpo, list) else []


def _data_do_servidor(config):
    """Instante do cabeçalho HTTP `Date` da resposta do Supabase (relógio do servidor), ou None."""
    base = str(cfg(config, "supabase", "url", default="")).rstrip("/")
    chave = cfg(config, "supabase", "service_role_key", default="")
    if not base or not chave:
        return None
    req = urllib.request.Request(base + "/rest/v1/", headers={"apikey": chave, "Authorization": "Bearer " + chave}, method="GET")
    try:
        with _comum._OPENER.open(req, timeout=_comum.HTTP_TIMEOUT) as resp:
            cabecalho = resp.headers.get("Date")
    except urllib.error.HTTPError as e:
        cabecalho = e.headers.get("Date") if e.headers else None
    except (urllib.error.URLError, OSError, ValueError):
        return None
    try:
        dt = parsedate_to_datetime(cabecalho) if cabecalho else None
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def inicio_pelo_banco(config):
    """Marca de início do teste pelo relógio do BANCO (nunca só o do computador, que pode estar adiantado ou atrasado).

    1) maior created_at de messages; 2) sem mensagens, o cabeçalho Date da resposta do Supabase; 3) último caso, o relógio
    local, com aviso. Devolve (datetime UTC, fonte)."""
    st, corpo = supabase_rest(config, "GET", "/messages?select=created_at&order=created_at.desc&limit=1")
    if st == 200 and isinstance(corpo, list) and corpo and isinstance(corpo[0], dict):
        marca = _marca(corpo[0].get("created_at"))
        if marca:
            return marca, "banco"
    if st == 200 and isinstance(corpo, list) and not corpo:
        servidor = _data_do_servidor(config)
        if servidor:
            return servidor, "servidor"
    aviso("Não consegui ler o relógio do banco; usei o relógio deste computador. Se ele estiver com a hora errada, o teste "
          "pode falhar ou passar por engano: acerte a hora do computador e rode de novo.")
    return datetime.now(timezone.utc), "local"


def passo_whatsapp(config, ctx):
    telefone = ctx.get("telefone_simulacao") or (ctx.get("telefones") or [""])[0]
    if not telefone_valido(telefone):
        telefone = normalizar_telefone(ask("Número de teste que vai enviar a mensagem (internacional)", env="SDR_TEST_PHONE"))
    if not telefone_valido(telefone):
        erro("Número inválido.")
        return False
    instancia = cfg(config, "evolution", "instance", default="a instância do WhatsApp")
    titulo("Teste real no WhatsApp")
    print("  Do número %s, mande a mensagem 'oi' para o WhatsApp conectado em '%s'." % (telefone, instancia))
    print("  O motor precisa estar rodando (Etapa 5 ou: python3 -m motor run em outro terminal).")
    # Só vale mensagem recebida DEPOIS daqui (a simulação e conversas antigas não contam).
    inicio, _fonte = inicio_pelo_banco(config)
    limite = time.monotonic() + ESPERA_WHATSAPP
    while time.monotonic() < limite:
        lead = _lead(config, telefone)
        if resposta_nova(lead, inicio, _mensagens_do_agente(config, lead.get("id"), inicio) if lead else None):
            ok("O agente respondeu à mensagem que você acabou de enviar (lead em '%s')." % lead.get("stage"))
            return True
        time.sleep(5)
    erro("Não vi mensagem sua seguida de resposta do agente em %ds. Confira se o motor está rodando e rode: python3 -m motor check" % ESPERA_WHATSAPP)
    return False


FUNCOES = {
    "modo-teste": passo_modo_teste,
    "motor": passo_motor,
    "simulacao": passo_simulacao,
    "horarios": passo_horarios,
    "whatsapp": passo_whatsapp,
}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--passos", help="lista separada por vírgula (padrão: %s)" % ",".join(PADRAO))
    p.add_argument("--whatsapp", action="store_true", help="inclui o teste real no WhatsApp")
    p.add_argument("--telefone", help="número de teste para a simulação")
    p.add_argument("--sem-pagina-publica", action="store_true", help="não vou usar a página pública: dispensa o passo horarios se ela estiver desativada")
    p.add_argument("--dry-run", action="store_true", help="só lista os passos")
    args = p.parse_args(argv)

    passos = [x.strip() for x in args.passos.split(",")] if args.passos else list(PADRAO)
    if args.whatsapp and "whatsapp" not in passos:
        passos.append("whatsapp")
    desconhecidos = [x for x in passos if x not in FUNCOES]
    if desconhecidos:
        erro("Passos desconhecidos: %s. Use: %s" % (", ".join(desconhecidos), ", ".join(PASSOS)))
        return 2
    if args.dry_run:
        titulo("Simulação: o teste rodaria estes passos")
        for i, x in enumerate(passos, 1):
            print("  %d. %s" % (i, x))
        return 0

    config = load_config()
    if not cfg(config, "supabase", "url") or not cfg(config, "supabase", "service_role_key"):
        erro("Faltam as credenciais do Supabase. Faça a Etapa 2 primeiro.")
        return 1
    ctx = {"telefone_simulacao": args.telefone, "sem_pagina_publica": args.sem_pagina_publica}
    titulo("Teste guiado")
    # Marcador invalidado ANTES de qualquer passo: interrupção nunca deixa um True antigo valendo.
    salvar_estado({"teste_ok": False})
    resultados = []
    for nome in passos:
        print("\n▶ %s" % nome)
        try:
            passou = FUNCOES[nome](config, ctx)
        except (KeyboardInterrupt, SystemExit) as e:
            salvar_estado({"teste_ok": False})
            erro("Teste interrompido. A produção continua bloqueada; rode o teste de novo.")
            if isinstance(e, SystemExit) and e.code not in (None, 0):
                return e.code if isinstance(e.code, int) else 1
            return 130
        resultados.append((nome, passou))

    print()
    for nome, passou in resultados:
        (ok if passou else erro)("%s: %s" % (nome, "passou" if passou else "falhou"))
    if not all(r for _, r in resultados):
        salvar_estado({"teste_ok": False})
        erro("Há passos com falha. Corrija e rode de novo.")
        return 1
    faltaram = [x for x in PADRAO if x not in passos]
    if faltaram:
        salvar_estado({"teste_ok": False})
        aviso("Teste parcial: não rodaram %s. Isso não libera a produção; rode python3 setup/teste_guiado.py sem --passos." % ", ".join(faltaram))
        return 1
    salvar_estado({"teste_ok": True})
    ok("Teste aprovado. Próximo: python3 setup/liberar_producao.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
