#!/usr/bin/env python3
"""Configuração do agente a partir da entrevista (Etapas 1, 2, 3 e 4).

Subcomandos:
  modelo      grava data/entrevista.json de exemplo (ou imprime com --imprimir)
  validar     confere data/entrevista.json e mostra o resumo
  banco       grava agent_config, schedule_settings, closers e availability_rules no Supabase
  web-js      escreve web/config.js (URL + chave anon; nunca a service_role)
  evolution   conecta a Evolution API e grava o bloco "evolution" do config/config.json
  ia          grava o provedor e a chave de IA no config/config.json

Segredos são lidos sem eco (getpass) ou das variáveis SDR_EVOLUTION_API_KEY e SDR_AI_API_KEY.
Nenhum segredo é impresso.
"""
import argparse
import base64
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _comum  # noqa: E402
import nichos  # noqa: E402
from _comum import (  # noqa: E402
    checar_papeis,
    DATA_DIR, ENTREVISTA_PATH, WEB_CONFIG_JS, _timeout_env, aviso, ask, atualizar_config, cfg, confirmar,
    entrada_externa, erro, gravar_atomico, gravar_json, http_json, info, load_config, ler_json, mascarar, normalizar_telefone,
    ok, q, sair, salvar_estado, segredo, slugificar, supabase_rest, telefone_valido, titulo,
)

# Espera pelo QR code ser lido (segundos). Override: ZX_SDR_QR_WAIT.
QR_WAIT = _timeout_env("ZX_SDR_QR_WAIT", 120)

PROVIDERS = ("gemini", "openai", "anthropic")
# Prefixos dos nomes de modelo de cada provedor: modelo de um provedor nunca é gravado em outro.
_PREFIXOS_MODELO = {"gemini": ("gemini",), "openai": ("gpt", "chatgpt", "o1", "o3", "o4"), "anthropic": ("claude",)}


def provedor_do_modelo(modelo):
    """Provedor a que o nome do modelo pertence, ou None se não for reconhecido."""
    nome = str(modelo or "").strip().lower().split("/")[-1]
    for prov, prefixos in _PREFIXOS_MODELO.items():
        if nome.startswith(prefixos):
            return prov
    return None
DIAS = {"dom": 0, "seg": 1, "ter": 2, "qua": 3, "qui": 4, "sex": 5, "sab": 6}
_HHMM = re.compile(r"^([01]?[0-9]|2[0-3]):([0-5][0-9])$")
_FUSO = re.compile(r"^(UTC|[A-Za-z_]+/[A-Za-z_+\-]+(/[A-Za-z_+\-]+)?)$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_COR = re.compile(r"^#[0-9a-fA-F]{6}$")
CORES_PADRAO = ["#D97706", "#2563EB", "#16A34A", "#9333EA", "#DC2626", "#0891B2"]

PARTES = ("agente", "agenda", "closers")


# ------------------------------------------------------------------ entrevista

def entrevista_modelo():
    """Exemplo comentado do que a Etapa 1 grava em data/entrevista.json."""
    return {
        "negocio": {
            "empresa": "Nome do seu negócio",
            "oferta": "O que você vende, em uma frase",
            "descricao": "Contexto extra sobre o negócio (opcional)",
        },
        "nicho": "servicos-consultoria",
        "agente": {"nome": "Ana", "tom_extra": ""},
        "dono": {"whatsapp": "+5511999990000", "email": "voce@exemplo.com"},
        "conhecimento_extra": "",
        "qualificacao": {"perguntas": None, "desqualificar": None},
        "closers": [
            {
                "nome": "Closer 1",
                "whatsapp": "+5511999990001",
                "email": "",
                "peso": 1,
                "horarios": [{"dias": ["seg", "ter", "qua", "qui", "sex"], "inicio": "09:00", "fim": "18:00"}],
            }
        ],
        "agenda": {
            "duracao_min": 30,
            "intervalo_min": 10,
            "passo_min": 30,
            "antecedencia_h": 2,
            "dias_a_frente": 14,
            "fuso": "America/Sao_Paulo",
            "distribuicao": "rodizio",
            "pagina_publica": True,
            "slug": "agendar",
        },
        "telefones_teste": ["+5511999990000"],
        "ia": {"provider": "gemini"},
    }


def carregar_entrevista(caminho=None):
    caminho = Path(caminho) if caminho else ENTREVISTA_PATH
    if not caminho.exists():
        return None
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _dia(valor):
    """Aceita 0..6 (0 = domingo) ou sigla pt-BR (seg, terça, sab...). Devolve int ou None."""
    if isinstance(valor, bool):
        return None
    if isinstance(valor, int):
        return valor if 0 <= valor <= 6 else None
    texto = unicodedata.normalize("NFKD", str(valor)).encode("ascii", "ignore").decode().lower().strip()
    if texto.isdigit():
        return _dia(int(texto))
    return DIAS.get(texto[:3])


def _hhmm(valor):
    m = _HHMM.match(str(valor or ""))
    return "%02d:%s" % (int(m.group(1)), m.group(2)) if m else None


def _fone(v):
    return re.sub(r"[^\d+]", "", str(v or ""))


def _valores_do_modelo(d):
    """Erros para cada campo que ainda tem o valor de exemplo do modelo (o aluno precisa trocar)."""
    m = entrevista_modelo()
    erros = []
    neg = d.get("negocio") if isinstance(d.get("negocio"), dict) else {}
    for campo in ("empresa", "oferta"):
        if str(neg.get(campo) or "").strip().casefold() == m["negocio"][campo].casefold():
            erros.append("negocio.%s ainda está com o texto de exemplo: troque pelo dado real do negócio" % campo)
    fones_modelo = {_fone(m["dono"]["whatsapp"]), _fone(m["closers"][0]["whatsapp"])}
    email_modelo = m["dono"]["email"].casefold()
    dono = d.get("dono") if isinstance(d.get("dono"), dict) else {}
    if _fone(dono.get("whatsapp")) in fones_modelo:
        erros.append("dono.whatsapp é o número de exemplo (%s): troque pelo WhatsApp real do dono" % dono.get("whatsapp"))
    if str(dono.get("email") or "").strip().casefold() == email_modelo:
        erros.append("dono.email é o e-mail de exemplo: troque pelo e-mail real do dono")
    closers = d.get("closers")
    for i, c in enumerate(closers if isinstance(closers, list) else []):
        if not isinstance(c, dict):
            continue
        if " ".join(str(c.get("nome") or "").split()).casefold() == m["closers"][0]["nome"].casefold():
            erros.append("closers[%d].nome ainda é 'Closer 1': troque pelo nome real de quem atende as calls" % i)
        if _fone(c.get("whatsapp")) in fones_modelo:
            erros.append("closers[%d].whatsapp é o número de exemplo (%s): troque pelo WhatsApp real do closer" % (i, c.get("whatsapp")))
        if str(c.get("email") or "").strip().casefold() == email_modelo:
            erros.append("closers[%d].email é o e-mail de exemplo: troque pelo real" % i)
    tels = d.get("telefones_teste")
    for t in tels if isinstance(tels, list) else []:
        if _fone(t) in fones_modelo:
            erros.append("telefones_teste tem o número de exemplo (%s): troque por um número seu de teste" % t)
    return erros


def _fuso_existe(nome):
    """True/False se o fuso IANA existe; None quando a base de fusos não está disponível neste computador
    (ex.: Windows sem o pacote tzdata), caso em que só o formato é conferido."""
    try:
        import zoneinfo
    except ImportError:
        return None
    try:
        zoneinfo.ZoneInfo("UTC")
    except Exception:  # noqa: BLE001 - base indisponível: não reprovar
        return None
    try:
        zoneinfo.ZoneInfo(nome)
    except Exception:  # noqa: BLE001 - nome inexistente ou inválido
        return False
    return True


def validar_entrevista(d):
    """Lista de erros da entrevista (vazia = válida)."""
    erros = []
    if not isinstance(d, dict):
        return ["entrevista.json não é um objeto JSON"]

    def _bloco(chave):
        valor = d.get(chave)
        return valor if isinstance(valor, dict) else {}

    neg = _bloco("negocio")
    for campo in ("empresa", "oferta"):
        valor = neg.get(campo)
        if not isinstance(valor, str) or not valor.strip():
            erros.append("negocio.%s é obrigatório (texto)" % campo)

    try:
        validos = set(nichos.carregar_todos())
    except SystemExit as exc:
        validos = set()
        erros.append(str(exc))
    if not isinstance(d.get("nicho"), str):
        erros.append("nicho deve ser texto")
    elif validos and d.get("nicho") not in validos:
        erros.append("nicho '%s' inválido. Opções: %s" % (d.get("nicho"), ", ".join(sorted(validos))))

    nome = _bloco("agente").get("nome")
    nome = nome.strip() if isinstance(nome, str) else ""
    if not nome or len(nome) > 40:
        erros.append("agente.nome é obrigatório (texto de até 40 caracteres)")

    dono = _bloco("dono")
    if not telefone_valido(dono.get("whatsapp")):
        erros.append("dono.whatsapp precisa estar no formato internacional, ex.: +5511999990000")
    if dono.get("email") and not _EMAIL.match(str(dono["email"])):
        erros.append("dono.email inválido")

    closers = d.get("closers")
    if not isinstance(closers, list) or not closers:
        erros.append("informe ao menos 1 closer (quem vai atender as calls)")
    else:
        nomes = set()
        for i, c in enumerate(closers):
            rot = "closers[%d]" % i
            if not isinstance(c, dict) or not isinstance(c.get("nome"), str) or not c["nome"].strip():
                erros.append("%s.nome é obrigatório (texto)" % rot)
                continue
            chave_nome = " ".join(str(c["nome"]).split()).casefold()
            if chave_nome in nomes:
                erros.append("%s.nome repetido (%s)" % (rot, str(c["nome"]).strip()))
            nomes.add(chave_nome)
            if c.get("whatsapp") and not telefone_valido(c["whatsapp"]):
                erros.append("%s.whatsapp precisa estar no formato internacional" % rot)
            if c.get("email") and not _EMAIL.match(str(c["email"])):
                erros.append("%s.email inválido" % rot)
            peso = c.get("peso", 1)
            if not isinstance(peso, int) or isinstance(peso, bool) or not 1 <= peso <= 10:
                erros.append("%s.peso deve ser inteiro de 1 a 10" % rot)
            if c.get("cor") and not _COR.match(str(c["cor"])):
                erros.append("%s.cor deve ser #RRGGBB" % rot)
            horarios = c.get("horarios")
            if not isinstance(horarios, list) or not horarios:
                erros.append("%s.horarios: informe ao menos 1 faixa (dias, inicio, fim)" % rot)
                continue
            for j, h in enumerate(horarios):
                hr = "%s.horarios[%d]" % (rot, j)
                if not isinstance(h, dict):
                    erros.append("%s: use um objeto com dias, inicio e fim (ex.: {\"dias\": [\"seg\"], \"inicio\": \"09:00\", \"fim\": \"18:00\"})" % hr)
                    continue
                dias = h.get("dias") if isinstance(h, dict) else None
                if not isinstance(dias, list) or not dias or any(_dia(x) is None for x in dias):
                    erros.append("%s.dias: lista de 0 a 6 (0 = domingo) ou seg, ter, qua, qui, sex, sab, dom" % hr)
                ini, fim = _hhmm(h.get("inicio")), _hhmm(h.get("fim"))
                if not ini or not fim:
                    erros.append("%s: inicio e fim no formato HH:MM" % hr)
                elif ini >= fim:
                    erros.append("%s: inicio precisa ser antes do fim" % hr)

    for chave_bloco in ("ia", "agenda", "qualificacao"):
        if d.get(chave_bloco) is not None and not isinstance(d.get(chave_bloco), dict):
            erros.append("%s deve ser um objeto JSON (chaves e valores), não %s" % (chave_bloco, type(d.get(chave_bloco)).__name__))
    ag = _bloco("agenda")
    limites = {"duracao_min": (5, 480), "intervalo_min": (0, 240), "passo_min": (5, 240),
               "antecedencia_h": (0, 720), "dias_a_frente": (1, 365)}
    for campo, (mn, mx) in limites.items():
        if campo in ag and (not isinstance(ag[campo], int) or isinstance(ag[campo], bool) or not mn <= ag[campo] <= mx):
            erros.append("agenda.%s deve ser inteiro entre %d e %d" % (campo, mn, mx))
    if ag.get("fuso") and not _FUSO.match(str(ag["fuso"])):
        erros.append("agenda.fuso inválido (ex.: America/Sao_Paulo)")
    elif ag.get("fuso"):
        existe = _fuso_existe(str(ag["fuso"]))
        if existe is False:
            erros.append("fuso horário '%s' não existe; use o nome IANA, por exemplo America/Sao_Paulo" % ag["fuso"])
        elif existe is None:
            aviso("não foi possível conferir o fuso neste computador")
    if ag.get("distribuicao", "rodizio") not in ("rodizio", "peso"):
        erros.append("agenda.distribuicao deve ser 'rodizio' ou 'peso'")
    slug = ag.get("slug", "agendar")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,39}", str(slug)):
        erros.append("agenda.slug: use 2 a 40 caracteres minúsculos, números e hífen")

    tels = d.get("telefones_teste")
    if tels is not None and not isinstance(tels, list):
        erros.append("telefones_teste deve ser uma lista de números (ex.: [\"+5511999990000\"]), não %s" % type(tels).__name__)
    for t in tels if isinstance(tels, list) else []:
        if not telefone_valido(t):
            erros.append("telefones_teste: '%s' não está no formato internacional" % t)

    provider = _bloco("ia").get("provider", "gemini")
    if provider not in PROVIDERS:
        erros.append("ia.provider deve ser um de: %s" % ", ".join(PROVIDERS))

    for chave in ("perguntas", "desqualificar"):
        valor = _bloco("qualificacao").get(chave)
        if valor is not None and not isinstance(valor, list):
            erros.append("qualificacao.%s deve ser uma lista (null ou ausente usa a do nicho; lista vazia [] significa nenhuma)" % chave)
        elif chave == "perguntas" and isinstance(valor, list):
            for i, item in enumerate(valor):
                if isinstance(item, str):
                    ok_item = bool(item.strip())
                elif isinstance(item, dict):
                    ok_item = isinstance(item.get("pergunta"), str) and bool(item["pergunta"].strip())
                    if "obrigatoria" in item and not isinstance(item["obrigatoria"], bool):
                        ok_item = False
                    if "key" in item and item["key"] is not None and (not isinstance(item["key"], str) or not item["key"].strip()):
                        erros.append("qualificacao.perguntas[%d].key deve ser um texto não vazio (ou apague o campo para gerar "
                                     "automaticamente)" % i)
                else:
                    ok_item = False
                if not ok_item:
                    erros.append("qualificacao.perguntas[%d]: use um texto não vazio ou um objeto com 'pergunta' (texto não "
                                 "vazio) e, se houver, 'obrigatoria' verdadeiro ou falso" % i)
    erros.extend(_valores_do_modelo(d))
    return erros


# ------------------------------------------------------------------ montagem do payload

def _normalizar_perguntas(lista):
    out, usadas = [], set()
    for i, p in enumerate(lista):
        if isinstance(p, str):
            p = {"pergunta": p}
        chave = p.get("key")
        if not isinstance(chave, str) or not chave.strip():
            chave = re.sub(r"[^a-z0-9]+", "_", slugificar(p.get("pergunta", ""))).strip("_")[:30] or "pergunta_%d" % (i + 1)
        chave = chave.replace("-", "_")
        # O truncamento em 30 caracteres pode fazer perguntas diferentes colidirem: a qualificação é guardada por key,
        # então a segunda sobrescreveria a primeira. Colisão ganha _2, _3... na ordem das perguntas (determinístico).
        final, n = chave, 1
        while final in usadas:
            n += 1
            sufixo = "_%d" % n
            final = chave[:max(1, 30 - len(sufixo))] + sufixo
        usadas.add(final)
        out.append({"key": final, "pergunta": p["pergunta"], "obrigatoria": bool(p.get("obrigatoria", True))})
    return out


def montar(entrevista):
    """Monta o que vai para o banco. Função pura: sem rede, sem disco."""
    neg = entrevista["negocio"]
    empresa, oferta = neg["empresa"].strip(), neg["oferta"].strip()
    agente = entrevista["agente"]["nome"].strip()
    nicho = nichos.aplicar_identidade(nichos.carregar_todos()[entrevista["nicho"]], empresa, agente, oferta)

    tone = nicho["tone"].strip()
    extra_tom = str((entrevista.get("agente") or {}).get("tom_extra") or "").strip()
    if extra_tom:
        tone += "\n\nAjustes de tom pedidos pelo dono: " + extra_tom

    conhecimento = nicho["knowledge"].rstrip()
    descricao = str(neg.get("descricao") or "").strip()
    extra_conhecimento = str(entrevista.get("conhecimento_extra") or "").strip()
    adicional = []
    if oferta:
        adicional.append("- Oferta principal: %s" % oferta)
    if descricao:
        adicional.append("- Contexto do negócio: %s" % descricao)
    if adicional:
        conhecimento += "\n\n## Informações do dono\n" + "\n".join(adicional)
    if extra_conhecimento:
        conhecimento += "\n\n## Informações adicionais\n" + extra_conhecimento

    qual = entrevista.get("qualificacao") or {}
    perguntas = nicho["qualification_questions"] if qual.get("perguntas") is None else _normalizar_perguntas(qual["perguntas"])
    desq = nicho["disqualify_rules"] if qual.get("desqualificar") is None else qual["desqualificar"]

    dono_tel = normalizar_telefone(entrevista["dono"]["whatsapp"])
    testes = [normalizar_telefone(t) for t in (entrevista.get("telefones_teste") or [])] or [dono_tel]
    ag = entrevista.get("agenda") or {}

    # Não enviamos test_only nem paused: o banco começa em modo teste (default true) e
    # reinstalar nunca pode religar o modo teste de quem já liberou a produção.
    # Também não religa o que o dono desligou no painel: em reinstalação, aplicar_no_banco
    # tira `active` (closers) e `public_enabled` (página pública) do que já existe.
    agent_config = {
        "id": 1,
        "agent_name": agente,
        "company": empresa,
        "tone": tone,
        "niche": nicho["niche"],
        "offer": oferta,
        "knowledge": conhecimento,
        "qualification_questions": perguntas,
        "disqualify_rules": desq,
        "objection_playbook": nicho["objection_playbook"],
        "followup_messages": nicho["followup_messages"],
        "confirm_template": nicho["confirm_template"],
        "reminder_24h_template": nicho["reminder_24h_template"],
        "reminder_2h_template": nicho["reminder_2h_template"],
        "owner_phone": dono_tel,
        "test_phones": testes,
        "ai_provider": (entrevista.get("ia") or {}).get("provider", "gemini"),
    }

    schedule = {
        "id": 1,
        "call_minutes": ag.get("duracao_min", 30),
        "buffer_minutes": ag.get("intervalo_min", 10),
        "slot_step_minutes": ag.get("passo_min", 30),
        "min_notice_hours": ag.get("antecedencia_h", 2),
        "days_ahead": ag.get("dias_a_frente", 14),
        "timezone": ag.get("fuso", "America/Sao_Paulo"),
        "public_slug": ag.get("slug", "agendar"),
        "public_enabled": bool(ag.get("pagina_publica", True)),
        "distribution": ag.get("distribuicao", "rodizio"),
    }

    closers = []
    for i, c in enumerate(entrevista["closers"]):
        regras = []
        for h in c["horarios"]:
            for dia in sorted({_dia(x) for x in h["dias"]}):
                regras.append({"weekday": dia, "start_time": _hhmm(h["inicio"]) + ":00", "end_time": _hhmm(h["fim"]) + ":00"})
        informados = [campo for campo, chave in (("whatsapp", "whatsapp"), ("email", "email"), ("weight", "peso"), ("color", "cor")) if c.get(chave)]
        closers.append({
            "_informados": informados,
            "name": c["nome"].strip(),
            "whatsapp": normalizar_telefone(c["whatsapp"]) if c.get("whatsapp") else None,
            "email": c.get("email") or None,
            "weight": c.get("peso", 1),
            "color": c.get("cor") or CORES_PADRAO[i % len(CORES_PADRAO)],
            "active": True,
            "availability": regras,
        })
    return {"agent_config": agent_config, "schedule_settings": schedule, "closers": closers}


# ------------------------------------------------------------------ gravação no banco

def _chave_nome(nome):
    """Identidade de um closer: espaços normalizados e sem diferença de maiúscula/minúscula."""
    return " ".join(str(nome or "").split()).casefold()


def _checar(status, corpo, o_que):
    if status in (200, 201, 204):
        return True
    detalhe = corpo if isinstance(corpo, str) else json.dumps(corpo, ensure_ascii=False)[:300]
    erro("%s falhou (HTTP %s): %s" % (o_que, status, detalhe))
    return False


def _upsert_linha_unica(config, tabela, linha):
    return supabase_rest(config, "POST", "/%s?on_conflict=id" % tabela, [linha],
                         extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"})


def _substituir_regras(config, cid, nome, availability):
    """Troca as faixas de horário sem janela vazia: grava as novas primeiro e só então apaga as antigas.

    Se a gravação das novas falhar, as antigas continuam valendo. Se o apagamento das antigas falhar, as faixas novas
    recém-gravadas são apagadas (rollback): o closer volta ao conjunto anterior, sem janela mista (antigas que o dono
    removeu não ficam valendo junto com as novas)."""
    st, corpo = supabase_rest(config, "GET", "/availability_rules?select=id&closer_id=eq.%s" % q(cid))
    if not _checar(st, corpo, "ler horários de %s" % nome):
        return False
    antigos = [r["id"] for r in corpo or []]
    novos = []
    if availability:
        regras = [dict(r, closer_id=cid) for r in availability]
        st, corpo = supabase_rest(config, "POST", "/availability_rules", regras)
        if not _checar(st, corpo, "gravar horários de %s (os horários antigos foram mantidos)" % nome):
            return False
        novos = [r["id"] for r in corpo or [] if isinstance(r, dict) and r.get("id")]
        if len(novos) != len(regras):
            erro("Horários de %s: o banco confirmou %d de %d faixas. Os antigos foram mantidos; rode de novo." % (nome, len(novos), len(regras)))
            return False
    if antigos:
        lista = ",".join(q(i) for i in antigos)
        st, corpo = supabase_rest(config, "DELETE", "/availability_rules?id=in.(%s)" % lista,
                                  extra_headers={"Prefer": "return=minimal"})
        if not _checar(st, corpo, "remover horários antigos de %s" % nome):
            if novos:
                st2, c2 = supabase_rest(config, "DELETE", "/availability_rules?id=in.(%s)" % ",".join(q(i) for i in novos),
                                        extra_headers={"Prefer": "return=minimal"})
                if st2 in (200, 201, 204):
                    erro("Horários de %s: não consegui remover os antigos, então desfiz as faixas novas. Nada mudou; rode de novo." % nome)
                else:
                    erro("Horários de %s: não consegui remover os antigos NEM desfazer as faixas novas (HTTP %s). O closer pode ter "
                         "faixas repetidas ou mistas: rode este comando de novo antes de liberar a produção." % (nome, st2))
            return False
    return True


def aplicar_no_banco(config, payload, partes, desativar_fora=False):
    """Grava no Supabase. Devolve True se tudo passou."""
    tudo_ok = True
    if "agente" in partes:
        linha_ag = dict(payload["agent_config"])
        st, atual = supabase_rest(config, "GET", "/agent_config?select=ai_provider&id=eq.1&limit=1")
        if not _checar(st, atual, "ler agent_config"):
            return False
        if atual and atual[0].get("ai_provider"):
            # Reinstalar não desfaz o provedor escolhido por 'ia' (a chave em config.json é a dele).
            linha_ag["ai_provider"] = atual[0]["ai_provider"]
        st, corpo = _upsert_linha_unica(config, "agent_config", linha_ag)
        if _checar(st, corpo, "gravar agent_config"):
            ok("Agente '%s' configurado (nicho %s)" % (payload["agent_config"]["agent_name"], payload["agent_config"]["niche"]))
        else:
            tudo_ok = False
    if "agenda" in partes:
        linha = dict(payload["schedule_settings"])
        st, corpo = supabase_rest(config, "GET", "/schedule_settings?select=id&id=eq.1&limit=1")
        if not _checar(st, corpo, "ler schedule_settings"):
            return False
        if corpo:
            # Reinstalar não reabre a página pública que o dono desligou no painel.
            linha.pop("public_enabled", None)
        st, corpo = _upsert_linha_unica(config, "schedule_settings", linha)
        if _checar(st, corpo, "gravar schedule_settings"):
            ok("Agenda configurada (%s min por call, fuso %s)" % (
                payload["schedule_settings"]["call_minutes"], payload["schedule_settings"]["timezone"]))
        else:
            tudo_ok = False
    if "closers" in partes:
        st, corpo = supabase_rest(config, "GET", "/closers?select=id,name,active,created_at")
        if not _checar(st, corpo, "ler closers"):
            return False
        linhas = [c for c in corpo or [] if isinstance(c, dict)]
        nomes_novos = {_chave_nome(c["name"]) for c in payload["closers"]}
        nome_exato = {_chave_nome(c["name"]): c["name"] for c in payload["closers"]}
        grupos = {}
        for c in linhas:
            grupos.setdefault(_chave_nome(c["name"]), []).append(c)
        existentes = {}
        repetidos = []  # linhas ativas a desativar: mesma chave de nome que a escolhida
        for chave, grupo in grupos.items():
            ativos = [c for c in grupo if c.get("active")]
            candidatos = ativos or grupo
            # A de mesmo nome exato; senão a mais antiga (sort estável: sem created_at vale a ordem do banco).
            escolhida = sorted(candidatos, key=lambda c: (c["name"] != nome_exato.get(chave), str(c.get("created_at") or "")))[0]
            existentes[chave] = escolhida["id"]
            if len(ativos) > 1:
                repetidos.append((escolhida, [c for c in ativos if c["id"] != escolhida["id"]]))
        if repetidos and not desativar_fora:
            nomes = []
            for escolhida, outros in repetidos:
                nomes.extend("'%s'" % c["name"] for c in [escolhida] + outros)
            erro("Há mais de um closer ativo com o mesmo nome (%s): há dois closers ativos chamados %s; desative um no painel "
                 "ou rode com --desativar-fora. Nada foi gravado nos closers." % (", ".join(nomes), " e ".join(nomes)))
            return False
        fora = [c for c in linhas if c.get("active") and _chave_nome(c["name"]) not in nomes_novos]
        if fora and not desativar_fora:
            erro("Closers ativos no banco que não estão na entrevista: %s. Se foi troca de nome, a antiga continuaria "
                 "recebendo agendamentos. Nada foi gravado nos closers. Rode de novo com --desativar-fora para desativá-los "
                 "(ou ajuste o nome na entrevista)." % ", ".join(c["name"] for c in fora))
            return False
        a_desativar = list(fora)
        for _, outros in repetidos:
            a_desativar.extend(c for c in outros if c not in a_desativar)
        for c in a_desativar:
            st2, c2 = supabase_rest(config, "PATCH", "/closers?id=eq.%s" % q(c["id"]), {"active": False},
                                    extra_headers={"Prefer": "return=minimal"})
            if _checar(st2, c2, "desativar closer %s" % c["name"]):
                aviso("Closer %s desativado (%s)." % (c["name"], "não está na entrevista" if c in fora else "nome repetido"))
            else:
                tudo_ok = False
        for c in payload["closers"]:
            linha = {k: v for k, v in c.items() if k not in ("availability", "_informados")}
            chave = _chave_nome(c["name"])
            if chave in existentes:
                cid = existentes[chave]
                # Reinstalar não reativa quem o dono desativou no painel nem apaga o que ele editou lá:
                # só vão no PATCH o nome (grafia) e os campos que a entrevista preencheu de fato.
                informados = c.get("_informados")
                linha = {k: v for k, v in linha.items()
                         if k == "name" or (informados is None and k != "active") or (informados is not None and k in informados)}
                st, corpo = supabase_rest(config, "PATCH", "/closers?id=eq.%s" % q(cid), linha,
                                          extra_headers={"Prefer": "return=minimal"})
                if not _checar(st, corpo, "atualizar closer %s" % c["name"]):
                    tudo_ok = False
                    continue
            else:
                st, corpo = supabase_rest(config, "POST", "/closers", linha)
                if not _checar(st, corpo, "criar closer %s" % c["name"]) or not corpo:
                    tudo_ok = False
                    continue
                cid = corpo[0]["id"]
            if _substituir_regras(config, cid, c["name"], c["availability"]):
                ok("Closer %s: %d faixa(s) de horário" % (c["name"], len(c["availability"])))
            else:
                tudo_ok = False
    return tudo_ok


# ------------------------------------------------------------------ subcomandos

def cmd_modelo(args):
    modelo = entrevista_modelo()
    if args.imprimir:
        print(json.dumps(modelo, ensure_ascii=False, indent=2))
        return 0
    if ENTREVISTA_PATH.exists() and not args.sobrescrever:
        erro("%s já existe. Use --sobrescrever para recomeçar." % ENTREVISTA_PATH)
        return 1
    gravar_json(ENTREVISTA_PATH, modelo)
    ok("Modelo gravado em %s. Preencha com as respostas do aluno e rode 'validar'." % ENTREVISTA_PATH)
    return 0


def cmd_validar(args):
    d = carregar_entrevista(args.arquivo)
    if d is None:
        erro("Não encontrei %s (ou o JSON está inválido). Rode 'modelo' ou refaça a Etapa 1." % (args.arquivo or ENTREVISTA_PATH))
        return 1
    erros = validar_entrevista(d)
    if erros:
        erro("A entrevista tem %d problema(s):" % len(erros))
        for e in erros:
            print("      - %s" % e)
        return 1
    payload = montar(d)
    ac, ss = payload["agent_config"], payload["schedule_settings"]
    ok("Entrevista válida")
    print("      Negócio: %s · Nicho: %s · Agente: %s" % (ac["company"], ac["niche"], ac["agent_name"]))
    print("      Closers: %s" % ", ".join("%s (%d faixas)" % (c["name"], len(c["availability"])) for c in payload["closers"]))
    print("      Call de %d min, intervalo %d min, %d dias à frente (%s)" % (
        ss["call_minutes"], ss["buffer_minutes"], ss["days_ahead"], ss["timezone"]))
    print("      Modo teste com: %s" % ", ".join(ac["test_phones"]))
    pend = nichos.contar_pendencias(ac["knowledge"])
    if pend:
        aviso("A base de conhecimento terá %d campo(s) [PREENCHER]. Complete no painel antes de liberar a produção." % pend)
    return 0


def _invalidar_teste(o_que):
    """Mudou o que o teste guiado exercitou (banco, WhatsApp ou IA): a marca teste_ok deixa de valer."""
    try:
        salvar_estado({"teste_ok": False})
    except OSError as exc:
        aviso("Não consegui limpar a marca do teste guiado (%s). Rode: python3 setup/estado.py limpar teste_ok" % exc)
    aviso("%s mudou depois do último teste. O teste guiado precisa rodar de novo antes de liberar a produção: "
          "python3 setup/teste_guiado.py" % o_que)


def cmd_banco(args):
    d = carregar_entrevista()
    if d is None:
        erro("Faça a Etapa 1 primeiro: %s não existe." % ENTREVISTA_PATH)
        return 1
    erros = validar_entrevista(d)
    if erros:
        erro("A entrevista tem problemas. Rode 'validar' e corrija: %s" % "; ".join(erros[:3]))
        return 1
    payload = montar(d)
    partes = [p for p in (args.partes or ",".join(PARTES)).split(",") if p]
    desconhecidas = [p for p in partes if p not in PARTES]
    if desconhecidas:
        erro("Partes desconhecidas: %s. Use: %s" % (", ".join(desconhecidas), ", ".join(PARTES)))
        return 2

    if args.dry_run:
        titulo("Simulação (nada foi gravado). Partes: %s" % ", ".join(partes))
        resumo = {k: payload[k] for k, p in (("agent_config", "agente"), ("schedule_settings", "agenda"), ("closers", "closers")) if p in partes}
        print(json.dumps(resumo, ensure_ascii=False, indent=2))
        return 0

    config = load_config()
    st, corpo = supabase_rest(config, "GET", "/agent_config?select=id,company&limit=1")
    if st != 200:
        erro("Não consegui ler agent_config (HTTP %s). A Etapa 2 (banco) está concluída? Detalhe: %s" % (st, str(corpo)[:200]))
        return 1
    ja_configurado = bool(corpo and corpo[0].get("company"))
    if ja_configurado and not args.sobrescrever:
        aviso("O agente já está configurado (empresa: %s). Regravar substitui o que foi editado no painel." % corpo[0]["company"])
        info("Para regravar tudo: --sobrescrever. Para só uma parte: --partes agenda (ou agente, closers) --sobrescrever.")
        return 1
    # Invalida ANTES da primeira escrita: aplicar_no_banco grava em etapas e pode falhar no meio com o banco já alterado.
    _invalidar_teste("A configuração do banco")
    if not aplicar_no_banco(config, payload, partes, desativar_fora=args.desativar_fora):
        return 1
    return 0


def url_supabase_segura(url):
    """Texto de problema se a URL do Supabase não serve para o painel (sem https, fora localhost), ou '' se está ok."""
    try:
        u = urllib.parse.urlsplit(str(url or "").strip())
        host = (u.hostname or "").lower()
    except ValueError:
        return "O endereço do Supabase (%s) não é uma URL válida." % url
    if u.scheme == "https" and host:
        return ""
    if u.scheme == "http" and host in ("localhost", "127.0.0.1", "::1"):
        return ""
    return ("O endereço do Supabase (%s) precisa começar com https:// (http só vale para localhost). Sem https a senha "
            "do painel trafega sem criptografia. Use o endereço https://... do seu projeto (Project Settings > API)." % url)


def js_config(url, anon, cf_token="", demo=False):
    cfg_js = {"supabaseUrl": url, "supabaseAnonKey": anon, "cfBeaconToken": cf_token, "demo": bool(demo)}
    return ("// Gerado pelo instalador do Setup 18. Contém só a chave pública (anon). Não versionar.\n"
            "window.SDR_CONFIG = %s;\n") % json.dumps(cfg_js, ensure_ascii=False, indent=2)


def cmd_web_js(args):
    if args.demo:
        gravar_atomico(WEB_CONFIG_JS, js_config("", "", args.cf_beacon_token, True), 0o644)
        ok("%s gravado em modo demonstração (sem Supabase)." % WEB_CONFIG_JS)
        return 0
    config = load_config()
    url = cfg(config, "supabase", "url", default="")
    anon = cfg(config, "supabase", "anon_key", default="")
    if not url or not anon:
        erro("Faltam supabase.url e supabase.anon_key em config/config.json. Rode a Etapa 2 (etapa_banco.py).")
        return 1
    if anon == cfg(config, "supabase", "service_role_key"):
        erro("A chave anon é igual à service_role. Use a chave 'anon public' do Supabase (Project Settings > API).")
        return 1
    problema = checar_papeis(None, anon)
    if problema:
        erro(problema)
        return 1
    problema = url_supabase_segura(url)
    if problema:
        erro(problema)
        return 1
    gravar_atomico(WEB_CONFIG_JS, js_config(url, anon, args.cf_beacon_token or ""), 0o644)
    ok("web/config.js gravado (URL %s, anon %s)" % (url, mascarar(anon)))
    return 0


def _estado_instancia(url, chave, instancia):
    st, corpo = http_json("GET", "%s/instance/connectionState/%s" % (url.rstrip("/"), q(instancia)), headers={"apikey": chave})
    estado = None
    if isinstance(corpo, dict):
        estado = (corpo.get("instance") or {}).get("state") or corpo.get("state")
    return st, estado, corpo


def _gravar_qr(destino, dados):
    """PNG do QR de pareamento com modo 0600 desde a criação (quem lê o QR vincula o WhatsApp do aluno)."""
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    try:
        destino.unlink()
    except OSError:
        pass
    fd = os.open(str(destino), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(dados)
    try:
        os.chmod(str(destino), 0o600)
    except OSError:
        pass


def _apagar_qr():
    try:
        (DATA_DIR / "qrcode-whatsapp.png").unlink()
    except OSError:
        pass


def _http_remoto(url):
    """True se a URL é http:// para um host que não é este computador (localhost, 127.0.0.1, ::1)."""
    try:
        partes = urllib.parse.urlsplit(str(url or "").strip())
        host = (partes.hostname or "").lower()
    except ValueError:
        return False
    return partes.scheme.lower() == "http" and host not in ("localhost", "127.0.0.1", "::1")


def _norm_url(url):
    """(esquema, host, porta) normalizados; porta padrão do esquema quando omitida. None se não der para ler."""
    try:
        p = urllib.parse.urlsplit(str(url or "").strip())
        esquema = p.scheme.lower()
        porta = p.port or {"http": 80, "https": 443}.get(esquema)
        return (esquema, (p.hostname or "").lower(), porta) if p.hostname else None
    except ValueError:
        return None


def _host_url(url):
    n = _norm_url(url)
    return "%s:%s" % (n[1], n[2]) if n else ""


def _url_mudou(nova, gravada):
    if not gravada:
        return False
    return _norm_url(nova) != _norm_url(gravada)


def cmd_evolution(args):
    config = load_config()
    atual = config.get("evolution") or {}
    url = args.url or atual.get("url") or ask("URL da Evolution API", "http://localhost:8080", env="SDR_EVOLUTION_URL")
    instancia = args.instance or atual.get("instance") or ask("Nome da instância do WhatsApp (ex.: sdr)", env="SDR_EVOLUTION_INSTANCE")
    if _http_remoto(url) and not getattr(args, "permitir_http", False):
        erro("A URL da Evolution usa http:// em um endereço fora deste computador (%s). A chave (apikey) viajaria sem "
             "criptografia e quem estiver no caminho da rede poderia usá-la para enviar mensagens pelo seu WhatsApp. "
             "Use https://. Se você sabe o que está fazendo (rede privada própria), rode de novo com --permitir-http." % url)
        return 2
    if atual.get("api_key") and not args.trocar_chave and _url_mudou(url, atual.get("url")) \
            and not entrada_externa("SDR_EVOLUTION_API_KEY"):
        erro("A URL mudou para %s; informe a chave da Evolution desse servidor (SDR_EVOLUTION_API_KEY) para não enviar a "
             "chave antiga a outro endereço." % (_host_url(url) or url))
        return 2
    if atual.get("api_key") and not args.trocar_chave and not _url_mudou(url, atual.get("url")):
        chave = atual["api_key"]
    else:
        chave = segredo("Chave (apikey) da Evolution", "SDR_EVOLUTION_API_KEY")

    st, estado, corpo = _estado_instancia(url, chave, instancia)
    if st == 0:
        erro("Não consegui falar com a Evolution em %s: %s" % (url, corpo))
        return 1
    if st in (401, 403):
        erro("A Evolution recusou a chave (HTTP %s). Confira a apikey." % st)
        return 1
    if st == 404:
        if not args.criar:
            erro("A instância '%s' não existe. Rode de novo com --criar para criar e ler o QR code." % instancia)
            return 1
        st2, corpo2 = http_json("POST", url.rstrip("/") + "/instance/create", headers={"apikey": chave},
                                corpo={"instanceName": instancia, "qrcode": True, "integration": "WHATSAPP-BAILEYS"})
        if st2 not in (200, 201):
            erro("Não consegui criar a instância (HTTP %s): %s" % (st2, str(corpo2)[:200]))
            return 1
        ok("Instância '%s' criada" % instancia)
        estado = "close"
    elif st != 200:
        erro("A Evolution respondeu HTTP %s: %s" % (st, str(corpo)[:200]))
        return 1

    atualizar_config({"evolution": {"url": url, "api_key": chave, "instance": instancia}})
    ok("Bloco evolution gravado em config/config.json (chave %s)" % mascarar(chave))
    _invalidar_teste("A configuração do WhatsApp (Evolution)")

    if estado == "open":
        _apagar_qr()
        ok("WhatsApp conectado na instância '%s'" % instancia)
        info("Se o WhatsApp cair depois, reconecte pelo painel: menu WhatsApp.")
        return 0

    titulo("Conectar o WhatsApp")
    st3, qr = http_json("GET", "%s/instance/connect/%s" % (url.rstrip("/"), q(instancia)), headers={"apikey": chave})
    if st3 == 200 and isinstance(qr, dict) and qr.get("base64"):
        bruto = qr["base64"].split(",", 1)[-1]
        try:
            destino = DATA_DIR / "qrcode-whatsapp.png"
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            _gravar_qr(destino, base64.b64decode(bruto))
            info("QR code salvo em %s. Abra a imagem e leia em WhatsApp > Aparelhos conectados." % destino)
        except (OSError, ValueError):
            aviso("Não consegui salvar o QR code. Abra %s/manager e leia por lá." % url.rstrip("/"))
        if qr.get("pairingCode"):
            info("Ou use o código de pareamento: %s" % qr["pairingCode"])
    else:
        info("Abra %s/manager, entre na instância '%s' e leia o QR code." % (url.rstrip("/"), instancia))

    limite = time.monotonic() + QR_WAIT
    while time.monotonic() < limite:
        time.sleep(3)
        st4, estado, _ = _estado_instancia(url, chave, instancia)
        if estado == "open":
            _apagar_qr()
            ok("WhatsApp conectado")
            info("Se o WhatsApp cair depois, reconecte pelo painel: menu WhatsApp.")
            return 0
    erro("O WhatsApp ainda não conectou depois de %ds. Leia o QR code e rode este comando de novo." % QR_WAIT)
    return 1


def _provedor_padrao(config):
    """Sem --provider: o provedor que já está valendo (banco, depois config.json); a entrevista só na 1ª configuração."""
    if cfg(config, "supabase", "url") and cfg(config, "supabase", "service_role_key"):
        st, lido = supabase_rest(config, "GET", "/agent_config?select=ai_provider&id=eq.1&limit=1")
        if st == 200 and isinstance(lido, list) and lido and lido[0].get("ai_provider") in PROVIDERS:
            return lido[0]["ai_provider"]
    atual = cfg(config, "ai", "provider", default="")
    if atual in PROVIDERS:
        return atual
    entrevista = carregar_entrevista()
    bloco_ia = entrevista.get("ia") if isinstance(entrevista, dict) else None
    provedor = bloco_ia.get("provider") if isinstance(bloco_ia, dict) else None
    return provedor if provedor in PROVIDERS else "gemini"


def _restaurar_config_bytes(conteudo):
    """Devolve o config.json ao conteúdo anterior, byte a byte (None = o arquivo não existia). Nunca lança."""
    caminho = Path(_comum.CONFIG_PATH)
    try:
        if conteudo is None:
            caminho.unlink()
            return True
        # Mesma rotina segura de gravar_atomico: mkstemp (0600) na mesma pasta e tmp apagado em qualquer falha.
        fd, tmp = tempfile.mkstemp(dir=str(caminho.parent), prefix=".tmp-", suffix=caminho.suffix or ".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(conteudo)
            os.replace(tmp, str(caminho))
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        try:
            os.chmod(str(caminho), 0o600)
        except OSError:
            pass
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def cmd_ia(args):
    config = load_config()
    provider = args.provider or _provedor_padrao(config)
    if provider not in PROVIDERS:
        erro("Provedor inválido. Use: %s" % ", ".join(PROVIDERS))
        return 2
    atual = cfg(config, "ai", "provider", default="")
    anterior_ia = atual
    if args.model:
        dono_modelo = provedor_do_modelo(args.model)
        if dono_modelo and dono_modelo != provider:
            erro("O modelo '%s' é do provedor %s, não de %s. Use um modelo de %s ou omita --model (usa o padrão do provedor)."
                 % (args.model, dono_modelo, provider, provider))
            return 2
        modelo = args.model
    elif provider == atual:
        modelo = cfg(config, "ai", "model", default="")
    else:
        modelo = ""  # trocou de provedor sem --model: o modelo antigo não vale no novo, o motor usa o padrão do provedor
    chave = segredo("Chave de API de %s" % provider, "SDR_AI_API_KEY")

    # O config.json (provedor + chave) vem PRIMEIRO e o banco depois: o motor lê ai_provider do banco e a chave do config.
    # Com o par novo já no config, o PATCH do banco nunca deixa o motor com provedor novo + chave antiga. Se o PATCH falhar,
    # o config anterior é restaurado byte a byte. Resta uma janela de milissegundos (config novo + banco ainda antigo)
    # em que o motor usaria a chave nova no provedor antigo: risco aceito (r19).
    com_banco = bool(cfg(config, "supabase", "url") and cfg(config, "supabase", "service_role_key"))
    anterior_banco = None
    if _comum._arquivo_invalido(_comum.CONFIG_PATH):
        erro("O arquivo %s está com JSON inválido (vírgula sobrando ou aspas faltando?). Não mexi no banco nem no config: "
             "corrija o arquivo (ou apague-o para recomeçar) e rode de novo." % _comum.CONFIG_PATH)
        return 1
    _invalidar_teste("O provedor/chave de IA")  # antes da primeira escrita (config.json ou PATCH no banco)
    try:
        config_anterior = Path(_comum.CONFIG_PATH).read_bytes()
    except FileNotFoundError:
        config_anterior = None
    except OSError as exc:
        erro("Não consegui ler o config/config.json (%s). Nada foi alterado." % (getattr(exc, "strerror", None) or exc.__class__.__name__))
        return 1
    if com_banco:
        st, ant = supabase_rest(config, "GET", "/agent_config?select=ai_provider&id=eq.1")
        if st == 200 and isinstance(ant, list) and ant and isinstance(ant[0], dict):
            anterior_banco = ant[0].get("ai_provider")

    try:
        atualizar_config({"ai": {"provider": provider, "api_key": chave, "model": modelo}})
    except BaseException as exc:  # noqa: BLE001 - inclui SystemExit: o banco ainda não foi tocado
        detalhe = getattr(exc, "strerror", None) or exc.__class__.__name__
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            _restaurar_config_bytes(config_anterior)
            raise
        _restaurar_config_bytes(config_anterior)
        erro("Não consegui gravar config/config.json (%s). Nada foi alterado." % detalhe)
        return 1

    if com_banco:
        falha = None
        try:
            st, corpo = supabase_rest(config, "PATCH", "/agent_config?id=eq.1", {"ai_provider": provider})
            if st not in (200, 204) or not corpo:
                falha = "O provedor NÃO foi gravado no banco (HTTP %s%s)" % (st, "" if corpo else ", agent_config sem linha")
            else:
                st, lido = supabase_rest(config, "GET", "/agent_config?select=ai_provider&id=eq.1")
                if st != 200 or not lido or lido[0].get("ai_provider") != provider:
                    falha = "Não consegui confirmar o provedor no banco ao reler agent_config"
        except BaseException:  # noqa: BLE001 - interrupção no meio do PATCH também devolve o config anterior
            _restaurar_config_bytes(config_anterior)
            raise
        if falha:
            config_ok = _restaurar_config_bytes(config_anterior)
            banco_ok = True
            if anterior_banco is not None:
                st_r, _corpo = supabase_rest(config, "PATCH", "/agent_config?id=eq.1", {"ai_provider": anterior_banco})
                banco_ok = st_r in (200, 204)
            try:
                st_l, lido = supabase_rest(config, "GET", "/agent_config?select=ai_provider&id=eq.1")
                atual_banco = lido[0].get("ai_provider") if st_l == 200 and isinstance(lido, list) and lido and isinstance(lido[0], dict) else None
            except Exception:  # noqa: BLE001 - a releitura é só conferência
                atual_banco = None
            if anterior_banco is not None:
                banco_ok = banco_ok and atual_banco == anterior_banco
            else:
                banco_ok = atual_banco is not None and atual_banco != provider  # sem valor anterior: só sei que o novo NÃO ficou no banco
            if config_ok and banco_ok:
                erro("%s. Devolvi o config/config.json ao estado anterior: banco e config continuam com o provedor anterior. "
                     "Rode este comando de novo." % falha)
            else:
                erro("%s. ATENÇÃO: banco e config divergem: o banco pode estar com %s e o config com %s. Rode de novo: "
                     "python3 setup/etapa_config.py ia --provider %s"
                     % (falha, provider, anterior_ia or "o provedor anterior", provider))
            return 1

    salvo = load_config()
    if cfg(salvo, "ai", "provider") != provider or cfg(salvo, "ai", "api_key") != chave:
        erro("Gravei, mas ao reler o config/config.json a IA não bateu. Confira se o arquivo está gravável e rode de novo.")
        return 1
    ok("IA configurada: %s (chave %s)" % (provider, mascarar(chave)))
    if not com_banco:
        aviso("O banco ainda não foi atualizado: o Supabase não está configurado em config/config.json (Etapa 2). Quando "
              "estiver, rode de novo: python3 setup/etapa_config.py ia --provider %s" % provider)
    info("Para testar a chave: python3 -m motor check")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("modelo", help="grava data/entrevista.json de exemplo")
    s.add_argument("--imprimir", action="store_true")
    s.add_argument("--sobrescrever", action="store_true")
    s.set_defaults(fn=cmd_modelo)

    s = sub.add_parser("validar", help="valida data/entrevista.json")
    s.add_argument("--arquivo")
    s.set_defaults(fn=cmd_validar)

    s = sub.add_parser("banco", help="grava a configuração no Supabase")
    s.add_argument("--dry-run", action="store_true", help="mostra o que gravaria, sem rede")
    s.add_argument("--partes", help="agente,agenda,closers (padrão: todas)")
    s.add_argument("--sobrescrever", action="store_true", help="regrava mesmo se o agente já estiver configurado")
    s.add_argument("--desativar-fora", action="store_true", help="desativa closers ativos no banco que não estão na entrevista")
    s.set_defaults(fn=cmd_banco)

    s = sub.add_parser("web-js", help="escreve web/config.js")
    s.add_argument("--demo", action="store_true", help="modo demonstração, sem Supabase")
    s.add_argument("--cf-beacon-token", default="")
    s.set_defaults(fn=cmd_web_js)

    s = sub.add_parser("evolution", help="conecta a Evolution API")
    s.add_argument("--url")
    s.add_argument("--instance")
    s.add_argument("--criar", action="store_true", help="cria a instância se não existir")
    s.add_argument("--trocar-chave", action="store_true")
    s.add_argument("--permitir-http", action="store_true",
                   help="aceita http:// para um endereço fora deste computador (a chave viaja sem criptografia)")
    s.set_defaults(fn=cmd_evolution)

    s = sub.add_parser("ia", help="grava provedor e chave de IA")
    s.add_argument("--provider", choices=PROVIDERS)
    s.add_argument("--model")
    s.set_defaults(fn=cmd_ia)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
