"""Rotinas periódicas: follow-up, retomada combinada, anti-no-show, repasse ao closer e vigia da instância."""

from __future__ import annotations

import json
import re
from datetime import datetime, time, timedelta

from .conversa import (AGUARDA_HUMANO_KEY, _directed_chat_read, BOOKED_STAGES, FINAL_STAGES, NAME_MAX, PROFILE_MAX, handle_turn, has_pending_opt_out,
                       sanitize_lead_text)
from .db import TERMINAL_STAGES
from .evolution import EvolutionError
from .outbox import _instance_not_open, notify_owner, phone_set, queue_owner_alert, record_uncertain_send
from .util import first_name, fmt_date, fmt_time, get_tz, iso, parse_ts, render, to_e164

DEFAULT_REMINDER_24H = ("Oi {nome}! Passando para lembrar da nossa conversa marcada para {data} às {hora}. "
                        "Você consegue confirmar presença? Se precisar mudar o horário, é só me avisar.")
DEFAULT_REMINDER_2H = "Oi {nome}! Nossa conversa começa hoje às {hora}. Te espero lá!"


def _tz(ctx):
    return get_tz((ctx.state.get("settings") or {}).get("timezone"))


def _agent(ctx) -> dict:
    return ctx.state.get("agent") or {}


def _clean(text: str) -> str:
    return text.replace(" ,", ",").replace(" !", "!").replace("  ", " ").strip()


def _hours(step: dict):
    """apos_horas válido (número >= 0) ou None: valor inválido na configuração ignora o passo, não derruba o job."""
    try:
        value = float(step.get("apos_horas") or 0)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _hhmm(value, default: time) -> time:
    try:
        parts = str(value).split(":")
        return time(int(parts[0]), int(parts[1]))
    except (ValueError, IndexError):
        return default


def in_followup_window(ctx) -> bool:
    agent = _agent(ctx)
    start = _hhmm(agent.get("followup_window_start") or "08:00", time(8, 0))
    end = _hhmm(agent.get("followup_window_end") or "20:00", time(20, 0))
    now = ctx.now().astimezone(_tz(ctx)).time().replace(second=0, microsecond=0)
    return start <= now <= end if start <= end else (now >= start or now <= end)


def _takeover_active(lead: dict, now: datetime) -> bool:
    until = parse_ts(lead.get("takeover_until"))
    return bool(until and until > now)


MIDIA_NAO_OUVIDA_KEY = "_midia_nao_ouvida"   # qualification: carimbo do áudio/imagem que o agente não ouviu


def _midia_nao_ouvida(lead: dict) -> bool:
    """O último áudio/imagem do lead não foi transcrito e ele ainda não escreveu texto depois: automação proativa
    (cadência, retomada) não sai, porque o pedido dele pode ser de saída."""
    qualification = lead.get("qualification") if isinstance(lead.get("qualification"), dict) else {}
    return bool(qualification.get(MIDIA_NAO_OUVIDA_KEY))


def _changed(ctx, snapshot: dict, internal: bool = False, block_midia: bool = False) -> bool:
    """Relê o lead logo antes de um envio de rotina: True se ele não pode mais receber (opt-out, estágio mudou,
    escreveu depois do snapshot ou tem um 'pare' ainda não processado). Falha de leitura = não envia.
    internal=True (repasse ao closer): o opt-out do lead não conta, só o envio AO LEAD é bloqueado por ele.
    Envio AO LEAD: antes de reler o banco faz a leitura DIRECIONADA do chat (o dono pode ter respondido pelo celular
    durante o laço, só na Evolution); falha nessa leitura só é registrada. Takeover ativo bloqueia o envio ao lead."""
    if not internal:
        _directed_chat_read(ctx, snapshot)
    try:
        fresh = ctx.db.get_lead(snapshot["id"])
        if not fresh or (fresh.get("opt_out") and not internal):
            return True
        if not internal and _takeover_active(fresh, ctx.now()):
            return True
        if block_midia and _midia_nao_ouvida(fresh):
            return True
        if fresh.get("stage") != snapshot.get("stage") or fresh.get("last_inbound_at") != snapshot.get("last_inbound_at"):
            return True
        return not internal and has_pending_opt_out(ctx.db.recent_messages(snapshot["id"], 20), fresh)
    except Exception as exc:  # noqa: BLE001
        ctx.log.error("não consegui reler o lead %s antes do envio: %s", snapshot.get("id"), exc)
        return True


def _qualification(lead: dict):
    """qualification como dict; None se a linha está malformada (string, lista...). Ausente/vazia = {}."""
    value = lead.get("qualification")
    if value is None or value == "":
        return {}
    return value if isinstance(value, dict) else None


def _bad_row(ctx, routine: str, key, who, detail) -> None:
    """Linha malformada pulada: log e UM alerta ao dono por (rotina, linha). Nunca para as demais."""
    ctx.log.error("%s: %s pulado (dado inválido): %s", routine, who, detail)
    done = ctx.state.setdefault("jobs_bad_row_alerted", set())
    if (routine, key) in done:
        return
    done.add((routine, key))
    try:
        queue_owner_alert(ctx, f"⚠️ Pulei {who} no {routine}: há um dado inválido no cadastro ({str(detail)[:100]}). "
                               "As demais pessoas seguem normais; corrija esse registro no painel.")
    except Exception as exc:  # noqa: BLE001
        ctx.log.error("alerta de dado inválido (%s) não enfileirado: %s", routine, exc)


# ---------------------------------------------------------------- follow-up
def followups(ctx) -> int:
    """Cadência do nicho: agent_config.followup_messages = [{apos_horas, texto}], contada desde a última
    mensagem do lead. Respeita janela, pausa, opt-out, takeover e quem já agendou."""
    agent = _agent(ctx)
    steps = sorted((s for s in agent.get("followup_messages") or []
                    if isinstance(s, dict) and s.get("texto") and _hours(s) is not None), key=_hours)
    if agent.get("paused") or not steps or not in_followup_window(ctx):
        return 0
    now, sent = ctx.now(), 0
    for lead in ctx.db.active_leads():
        try:
            sent += _followup_lead(ctx, lead, steps, now)
        except Exception as exc:  # noqa: BLE001 - um lead com dado ruim não derruba a cadência dos outros
            _bad_row(ctx, "follow-up", lead.get("id"), f"o lead {lead.get('phone')}", exc)
    return sent


def _followup_lead(ctx, lead: dict, steps: list, now) -> int:
    stage = lead.get("stage")
    if lead.get("opt_out") or stage in BOOKED_STAGES or stage in FINAL_STAGES:
        return 0
    if _takeover_active(lead, now) or not ctx.messenger.allowed(lead["phone"]):
        return 0
    qualification = _qualification(lead)
    if qualification is None:
        raise ValueError("qualification não é um objeto")
    if qualification.get("_retomar"):
        return 0  # o lead pediu para falar depois: quem acorda é a retomada, não a cadência
    if _midia_nao_ouvida(lead):
        return 0  # áudio/imagem que o agente não ouviu: pode ser pedido de saída; só texto novo do lead libera
    step = int(lead.get("followup_step") or 0)
    if step >= len(steps):
        return 0
    last_in = parse_ts(lead.get("last_inbound_at")) or parse_ts(lead.get("created_at"))
    last_out = parse_ts(lead.get("last_outbound_at"))
    if not last_in or not last_out or last_out < last_in:
        if last_in and last_out:
            # a cadência só cobra quem ficou sem resposta DEPOIS de uma mensagem nossa; saída "anterior" à entrada
            # costuma ser relógio do computador atrasado: registra 1x por lead para não ser um silêncio invisível
            warned = ctx.state.setdefault("followup_clock_warned", set())
            if lead.get("id") not in warned:
                warned.add(lead.get("id"))
                ctx.log.warning("follow-up de %s pulado: última saída (%s) é anterior à última entrada (%s); "
                                "confira o relógio do computador", lead.get("phone"), lead.get("last_outbound_at"),
                                lead.get("last_inbound_at"))
        return 0  # só cobra quem ficou sem resposta DEPOIS de uma mensagem nossa
    if now < last_in + timedelta(hours=_hours(steps[step])):
        return 0
    text = _clean(render(steps[step]["texto"], nome=first_name(lead.get("name"))))
    if _changed(ctx, lead, block_midia=True):
        return 0
    # estado ANTES do envio: se a gravação falhar nada sai; se o envio falhar, reverte (exceto timeout incerto)
    try:
        ctx.db.update_lead(lead["id"], {"followup_step": step + 1})
    except Exception as exc:  # noqa: BLE001
        ctx.log.error("follow-up de %s não iniciado (estado não gravado): %s", lead.get("phone"), exc)
        return 0
    snapshot_inbound = lead.get("last_inbound_at")

    def revert() -> None:
        # compare-and-set: se o lead respondeu durante o envio, a ingestão já zerou followup_step; devolver o passo
        # antigo pularia etapas da cadência da conversa nova
        fresh = ctx.db.get_lead(lead["id"])
        if (fresh and int(fresh.get("followup_step") or 0) == step + 1
                and fresh.get("last_inbound_at") == snapshot_inbound):
            ctx.db.update_lead(lead["id"], {"followup_step": step})
        else:
            ctx.log.info("follow-up de %s: passo não revertido (o lead mudou durante o envio)", lead.get("phone"))

    return int(bool(_send_or_revert(ctx, lead["phone"], text, lead["id"], revert,
                                    proactive=True, routine="follow-up")))


def _permanent_failure(ctx, phone: str) -> str:
    """Motivo se a falha do último envio é PERMANENTE (HTTP 4xx exceto 408/429, ou telefone inválido); '' se
    transitória (5xx, rede, 408, 429, limitador)."""
    if not to_e164(phone):
        return "telefone_invalido"
    status = getattr(ctx.messenger, "last_status", None)
    if ctx.messenger.last_failure == "evolution" and isinstance(status, int) and 400 <= status < 500 \
            and status not in (408, 429):
        if _instance_not_open(ctx):
            return ""   # 4xx com a instância fora de 'open' (ou estado ilegível) é transitório: reverte e tenta depois
        return f"HTTP {status}"
    return ""


def _alert_permanent_failure(ctx, phone: str, routine: str, reason: str) -> None:
    """Um alerta por rotina e DESTINATÁRIO por dia: o mesmo número repetido não inunda o WhatsApp do dono, mas
    um destinatário diferente (outro closer, outro lead) nunca fica sem aviso."""
    today = ctx.now().date().isoformat()
    alerted = ctx.state.setdefault("alerted_day", {})
    key = f"falha_permanente:{routine}:{to_e164(phone) or str(phone or '').strip()}"
    if alerted.get(key) == today:
        ctx.log.info("falha permanente do %s para %s já avisada hoje (%s)", routine, phone, reason)
        return
    detail = str(getattr(ctx.messenger, "last_error", "") or "")[:120]
    try:
        if queue_owner_alert(ctx, f"⚠️ Não consegui enviar o {routine} para {phone}: {reason}"
                                  f"{' (' + detail + ')' if detail and reason != 'telefone_invalido' else ''}. "
                                  "Esse erro não se resolve sozinho e o motor NÃO vai repetir: confira o número "
                                  "cadastrado e avise a pessoa por outro caminho. Repetições para este mesmo "
                                  "número hoje ficam só no log."):
            alerted[key] = today   # só depois de enfileirar
    except Exception as exc:  # noqa: BLE001
        ctx.log.error("alerta de falha permanente do %s não enfileirado: %s", routine, exc)


def _send_or_revert(ctx, phone: str, text: str, lead_id, revert, *, proactive: bool = False,
                    internal: bool = False, routine: str = "envio da rotina", context: str = "") -> bool:
    """Envia depois do estado já gravado. Falha transitória => reverte o estado (tenta de novo no próximo ciclo).
    Timeout incerto: mantém o estado e NÃO reenvia. Falha PERMANENTE (4xx, telefone inválido): mantém o estado
    (não repete) e avisa o dono UMA vez."""
    key = None
    if not to_e164(phone):
        ctx.messenger.last_failure, ctx.messenger.last_error = "telefone_invalido", "telefone_invalido"
        ctx.messenger.last_status = None
    else:
        try:
            key = ctx.messenger.send(phone, text, lead_id=lead_id, proactive=proactive, internal=internal)
        except Exception as exc:  # noqa: BLE001
            ctx.log.error("envio para %s falhou: %s", phone, exc)
            key = None
    if key:
        return True
    if ctx.messenger.last_failure in ("timeout_incerto", "sem_key_id"):
        if lead_id and not internal and to_e164(phone):
            record_uncertain_send(ctx, to_e164(phone), text, lead_id, ctx.messenger.last_failure)
        elif internal:
            # repasse interno com resultado incerto: a flag fica (sem reenvio), o dono confere se chegou
            try:
                queue_owner_alert(ctx, f"⚠️ Não tenho certeza de que o {routine} chegou a {phone}"
                                       f"{' (' + context + ')' if context else ''}. Não reenviei para não duplicar: "
                                       "confira com o closer se ele recebeu.")
            except Exception as exc:  # noqa: BLE001
                ctx.log.error("alerta de repasse incerto não enfileirado: %s", exc)
        return False
    reason = _permanent_failure(ctx, phone)
    if reason:
        ctx.log.error("envio permanente para %s falhou (%s): o estado não é revertido", phone, reason)
        _alert_permanent_failure(ctx, phone, routine, reason)
        return False
    try:
        revert()
    except Exception as exc:  # noqa: BLE001
        ctx.log.error("não consegui reverter o estado após falha de envio para %s: %s", phone, exc)
    return False


def retomar(ctx) -> int:
    """Micropacto: o lead pediu para falar depois (qualification._retomar.em); na hora combinada o agente volta."""
    agent = _agent(ctx)
    if agent.get("paused") or not in_followup_window(ctx):
        return 0
    now, done = ctx.now(), 0
    for lead in ctx.db.active_leads():
        try:
            done += _retomar_lead(ctx, lead, now)
        except Exception as exc:  # noqa: BLE001 - um lead com dado ruim não derruba a retomada dos outros
            _bad_row(ctx, "retomar", lead.get("id"), f"o lead {lead.get('phone')}", exc)
    return done


MAX_RETOMAR_TENTATIVAS = 3   # rodadas de retomada com a IA falhando antes de desistir e avisar o dono
# status de handle_turn que contam como tentativa da retomada (falha da IA, da agenda ou do próprio turno)
_RETOMAR_CONTA = ("ia_falhou", "agenda_falhou", "erro", "erro_releitura")
# status que ENCERRAM a retomada sem alerta de IA: o turno não tinha mais o que fazer
_RETOMAR_ENCERRA = ("takeover", "estagio_mudou", "laco_pausado", "estagio_final", "autoresponder",
                    "ja_respondido", "opt_out", "lead_inexistente")
_REMOVE = object()


def _set_qualification_key(ctx, lead: dict, key: str, value, extra: dict | None = None) -> None:
    """Relê o lead imediatamente antes do update e mexe SÓ na chave `key` da qualification (value=_REMOVE tira a
    chave): edições do dono em outras chaves, feitas enquanto o laço rodava, nunca são sobrescritas."""
    current = ctx.db.get_lead(lead["id"]) or lead
    qualification = dict(current.get("qualification") if isinstance(current.get("qualification"), dict) else {})
    if value is _REMOVE:
        qualification.pop(key, None)
    else:
        qualification[key] = value
    ctx.db.update_lead(lead["id"], {**(extra or {}), "qualification": qualification})


def _saiu_mensagem_do_agente(ctx, lead: dict, since) -> bool:
    """True se o agente já mandou (ou enfileirou) algo a este lead desde `since`. Consulta que falha conta como
    'pode ter saído': o pior caso é não retomar (o dono é avisado pelo fluxo normal), nunca mandar duas vezes."""
    try:
        if ctx.db.agent_messages_since(lead["id"], since):
            return True
        return bool(ctx.db.outbox_for_lead_since(lead["id"], since))
    except Exception as exc:  # noqa: BLE001
        ctx.log.warning("retomada de %s: não consegui conferir se houve envio na rodada (%s)", lead.get("phone"), exc)
        return True


def _retomar_lead(ctx, lead: dict, now) -> int:
    qualification = _qualification(lead)
    if qualification is None:
        raise ValueError("qualification não é um objeto")
    raw = qualification.get("_retomar")
    if raw is not None and not isinstance(raw, dict):
        ctx.log.warning("lead %s: _retomar inválido (%s) removido", lead.get("phone"), type(raw).__name__)
        _set_qualification_key(ctx, lead, "_retomar", _REMOVE)
        return 0
    due = (raw or {}).get("em")
    due_at = parse_ts(due)
    if raw is not None and not due_at:
        # 'em' que não vira data nunca vence: sem remover o marcador a cadência do lead ficaria bloqueada para sempre
        ctx.log.warning("lead %s: _retomar com 'em' inválido (%r) removido", lead.get("phone"), due)
        _set_qualification_key(ctx, lead, "_retomar", _REMOVE)
        who = lead.get("name") or lead.get("phone")
        try:
            queue_owner_alert(ctx, f"⚠️ A retomada combinada com {who} ({lead.get('phone')}) não pôde ser feita: "
                                   "o horário combinado não é uma data válida. Retome a conversa manualmente.")
        except Exception as exc:  # noqa: BLE001
            ctx.log.error("alerta de retomada inválida de %s não enfileirado: %s", lead.get("phone"), exc)
        return 0
    if not due_at or due_at > now or lead.get("opt_out") or _takeover_active(lead, now):
        return 0
    if not ctx.messenger.allowed(lead["phone"]):
        return 0
    if _midia_nao_ouvida(lead):
        return 0   # o _retomar fica intacto: volta a valer quando o lead escrever texto (ou o dono resolver)
    if _changed(ctx, lead, block_midia=True):
        return 0
    # estado ANTES do envio: tira o _retomar; se o turno não enviar nada, restaura
    original_marker = dict(raw or {})
    neutral_sent = bool(original_marker.get("neutra"))   # esta retomada já mandou a mensagem neutra antes
    try:
        _set_qualification_key(ctx, lead, "_retomar", _REMOVE, {"followup_step": 0})
    except Exception as exc:  # noqa: BLE001
        ctx.log.error("retomada de %s não iniciada (estado não gravado): %s", lead.get("phone"), exc)
        return 0
    try:
        result = handle_turn(ctx, lead["id"], trigger="retomar", allow_neutral=not neutral_sent)
    except Exception as exc:  # noqa: BLE001
        ctx.log.error("retomada de %s falhou: %s", lead.get("phone"), exc)
        result = {"status": "erro"}
    status = result.get("status")
    if status != "ok":
        if status in _RETOMAR_ENCERRA or str(status).startswith("opt_out"):
            return 0   # o marcador já foi removido: o dono assumiu, o lead saiu ou mudou de estágio; sem alerta de IA
        if not result.get("neutral") and _saiu_mensagem_do_agente(ctx, lead, now):
            # o turno falhou DEPOIS de enviar: restaurar o marcador reenviaria a resposta no próximo ciclo
            ctx.log.warning("lead %s: retomada com status %s mas já houve envio nesta rodada; _retomar removido "
                            "sem reenvio", lead.get("phone"), status)
            return 0
        counts = status in _RETOMAR_CONTA
        attempts = int(original_marker.get("tentativas") or 0) + (1 if counts else 0)
        if counts and attempts >= MAX_RETOMAR_TENTATIVAS:
            # desiste: o marcador já foi removido acima; avisa o dono (alerta próprio, independente de ai_alerted)
            who = lead.get("name") or lead.get("phone")
            cause = "a agenda está indisponível" if status == "agenda_falhou" else "a IA está fora do ar"
            try:
                queue_owner_alert(ctx, f"⚠️ A retomada combinada com {who} ({lead.get('phone')}) não pôde ser feita: "
                                       f"{cause} e tentei {attempts} vezes. Retome a conversa manualmente.")
            except Exception as exc:  # noqa: BLE001
                ctx.log.error("alerta de retomada desistida de %s não enfileirado: %s", lead.get("phone"), exc)
            return 0
        # mantém _retomar para a próxima rodada (até o limite), com no máximo UMA mensagem neutra por pedido
        marker = {**original_marker, "tentativas": attempts}
        if result.get("neutral") or neutral_sent:
            marker["neutra"] = True
        try:
            _set_qualification_key(ctx, lead, "_retomar", marker,
                                   {"followup_step": int(lead.get("followup_step") or 0)})
        except Exception as exc:  # noqa: BLE001
            ctx.log.error("não consegui restaurar _retomar de %s: %s", lead.get("phone"), exc)
        return 0
    return 1


# ---------------------------------------------------------------- anti-no-show
def reminders(ctx) -> int:
    """Lembrete 24h e 2h antes. Idempotente pelas colunas reminder_*_at; só marca depois de enviar de verdade."""
    agent = _agent(ctx)
    if agent.get("paused"):
        return 0
    tz, now, sent = _tz(ctx), ctx.now(), 0
    for appt in ctx.db.upcoming_appointments(now):
        try:
            sent += _reminder_appt(ctx, appt, agent, tz, now)
        except Exception as exc:  # noqa: BLE001 - um agendamento com dado ruim não derruba os outros lembretes
            _bad_row(ctx, "lembrete", appt.get("id"), f"o agendamento {appt.get('id')}", exc)
    return sent


def _reminder_appt(ctx, appt: dict, agent: dict, tz, now) -> int:
    start = parse_ts(appt.get("starts_at"))
    if not start:
        return 0
    remaining, created = start - now, parse_ts(appt.get("created_at"))
    kind = None
    if remaining <= timedelta(hours=2):
        if not appt.get("reminder_2h_at") and not (created and created >= start - timedelta(hours=2)):
            kind = "2h"
    elif remaining <= timedelta(hours=24):
        # agendado já dentro da janela de 24h: a própria confirmação cumpre o papel do lembrete
        if not appt.get("reminder_24h_at") and not (created and created >= start - timedelta(hours=24)):
            kind = "24h"
    if not kind:
        return 0
    lead = ctx.db.get_lead(appt["lead_id"])
    if not lead or lead.get("opt_out") or not ctx.messenger.allowed(lead["phone"]):
        return 0
    if lead.get("stage") in TERMINAL_STAGES:
        return 0   # venda/perdido/desqualificado/compareceu não recebem WhatsApp de rotina: não marca a flag
    if _takeover_active(lead, now):
        return 0   # o dono conversa com o lead: não marca a flag, reavalia no próximo ciclo
    closer = ctx.db.get_closer(appt["closer_id"]) if appt.get("closer_id") else None
    template = (agent.get("reminder_24h_template") if kind == "24h" else agent.get("reminder_2h_template")) \
        or (DEFAULT_REMINDER_24H if kind == "24h" else DEFAULT_REMINDER_2H)
    text = _clean(render(template, nome=first_name(lead.get("name")), data=fmt_date(start, tz),
                         hora=fmt_time(start, tz), closer=(closer or {}).get("name", "")))
    field = f"reminder_{kind}_at"
    if _changed(ctx, lead):
        return 0
    try:
        ctx.db.update_appointment(appt["id"], {field: iso(now)})   # marca ANTES; reverte se o envio falhar
    except Exception as exc:  # noqa: BLE001
        ctx.log.error("lembrete %s de %s não iniciado: %s", kind, lead.get("phone"), exc)
        return 0
    return int(bool(_send_or_revert(ctx, lead["phone"], text, lead["id"],
                                    lambda a=appt, f=field: ctx.db.update_appointment(a["id"], {f: None}),
                                    routine=f"lembrete de {kind}")))


# ---------------------------------------------------------------- repasse ao closer
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


def _lead_text(value, limit: int) -> str:
    """Texto do lead no repasse: dado, não instrução: sem marcação, sem link, truncado."""
    return _URL_RE.sub("[link removido]", sanitize_lead_text(value, limit * 2))[:limit].strip()


def handoff_summary(ctx, appt: dict, lead: dict, title: str) -> str:
    tz = _tz(ctx)
    start = parse_ts(appt["starts_at"])
    lines = [title, f"Lead: {_lead_text(lead.get('name'), NAME_MAX) or 'sem nome'} ({lead.get('phone')})",
             f"Quando: {fmt_date(start, tz)} às {fmt_time(start, tz)}",
             f"Origem: {lead.get('source') or 'whatsapp'} · marcado via {appt.get('source') or 'agente'}",
             "Informação do lead, não verificada:"]
    if lead.get("profile"):
        lines.append(f"Perfil: {_lead_text(lead['profile'], PROFILE_MAX)}")
    raw_q = lead.get("qualification")
    # qualification malformada (string, lista) não derruba o repasse: segue sem esse bloco (o alerta sai em handoffs)
    qualification = ({k: v for k, v in raw_q.items() if not str(k).startswith("_")}
                     if isinstance(raw_q, dict) else {})
    if qualification:
        lines.append("Qualificação: " + "; ".join(f"{_lead_text(k, 60)}: {_lead_text(v, 300)}"
                                                   for k, v in qualification.items()))
    if lead.get("score") is not None:
        lines.append(f"Score: {lead['score']}/100")
    objections = ctx.db.list_objections(lead["id"])
    if objections:
        lines.append("Objeções: " + "; ".join(f"{_lead_text(o.get('category'), 60)} (\"{_lead_text(o.get('excerpt'), 80)}\")"
                                              for o in objections[:5]))
    if appt.get("notes"):
        lines.append(f"Notas: {_lead_text(appt['notes'], 900)}")
    return "\n".join(lines)


def handoffs(ctx) -> int:
    """Resumo ao closer no agendamento e 1h antes (handoff_sent_at / handoff_1h_sent_at evitam duplicar)."""
    agent = _agent(ctx)
    now, sent = ctx.now(), 0
    warned = ctx.state.setdefault("handoff_warned", set())
    test_only = agent.get("test_only") is not False   # fail-closed: NULL/ausente = modo teste
    test_phones = phone_set(agent.get("test_phones"))
    for appt in ctx.db.upcoming_appointments(now):
        try:
            sent += _handoff_appt(ctx, appt, agent, now, warned, test_only, test_phones)
        except Exception as exc:  # noqa: BLE001 - um agendamento com dado ruim não derruba os outros repasses
            _bad_row(ctx, "repasse", appt.get("id"), f"o agendamento {appt.get('id')}", exc)
    return sent


def _handoff_appt(ctx, appt: dict, agent: dict, now, warned: set, test_only: bool, test_phones: set) -> int:
    start = parse_ts(appt.get("starts_at"))
    if not start:
        return 0
    first, hour_before = parse_ts(appt.get("handoff_sent_at")), parse_ts(appt.get("handoff_1h_sent_at"))
    if not first:
        remarcado = str(appt.get("notes") or "").lower().startswith("reagendado de")
        field, title = "handoff_sent_at", "🔁 Agendamento remarcado" if remarcado else "📌 Novo agendamento"
    elif not hour_before and start - now <= timedelta(hours=1) and first < start - timedelta(hours=1):
        field, title = "handoff_1h_sent_at", "⏰ Sua conversa começa em cerca de 1 hora"
    else:
        return 0
    lead = ctx.db.get_lead(appt["lead_id"])
    closer = ctx.db.get_closer(appt["closer_id"]) if appt.get("closer_id") else None
    dest = to_e164((closer or {}).get("whatsapp")) or to_e164(agent.get("owner_phone"))
    if not lead or not dest:
        if appt["id"] not in warned:
            warned.add(appt["id"])
            ctx.log.warning("repasse sem destino: cadastre o WhatsApp do closer ou o owner_phone (agendamento %s)", appt["id"])
        return 0
    redirected = False
    if test_only:
        # modo teste: o repasse só sai para número de teste; os demais vão ao dono, sempre com "[TESTE] "
        title = "[TESTE] " + title
        if dest not in test_phones:
            dest = to_e164(agent.get("owner_phone"))
            if not dest:
                return 0
            redirected = True
    previewed = ctx.state.setdefault("handoff_test_previewed", set())
    if redirected and (appt["id"], field) in previewed:
        return 0   # o dono já viu a prévia [TESTE] deste repasse neste processo
    if _changed(ctx, lead, internal=True):
        return 0
    tz = _tz(ctx)
    context = (f"{fmt_date(start, tz)} às {fmt_time(start, tz)}, closer {(closer or {}).get('name') or 'não identificado'}, "
               f"lead {lead.get('name') or lead.get('phone')}")
    if _qualification(lead) is None:
        # dado ruim no lead: o repasse sai sem a qualificação e o dono é avisado UMA vez
        _bad_row(ctx, "repasse", lead.get("id"), f"a qualificação do lead {lead.get('phone')}",
                 "qualification não é um objeto")
    summary = handoff_summary(ctx, appt, lead, title)   # monta ANTES de gravar a flag: falha aqui não a deixa presa
    if redirected:
        # desvio [TESTE] ao dono NÃO é o repasse real: a flag fica livre e o closer recebe o repasse (1 vez) quando
        # o modo ao vivo voltar. A prévia vai uma vez por repasse e processo.
        try:
            ok = bool(ctx.messenger.send(dest, summary, lead_id=None, internal=True))
        except Exception as exc:  # noqa: BLE001
            ctx.log.error("prévia [TESTE] do repasse %s não enviada: %s", appt["id"], exc)
            ok = False
        if ok:
            previewed.add((appt["id"], field))
        return int(ok)
    try:
        ctx.db.update_appointment(appt["id"], {field: iso(now)})   # marca ANTES; reverte se o envio falhar
    except Exception as exc:  # noqa: BLE001
        ctx.log.error("repasse do agendamento %s não iniciado: %s", appt["id"], exc)
        return 0
    return int(bool(_send_or_revert(ctx, dest, summary, None,
                                    lambda a=appt, f=field: ctx.db.update_appointment(a["id"], {f: None}),
                                    internal=True, routine="repasse ao closer", context=context)))


# ---------------------------------------------------------------- lead que escreveu durante a pausa da IA
def aguardando_humano(ctx) -> int:
    """O lead escreveu enquanto o dono tinha assumido (takeover). Quando a pausa expira: se o dono respondeu depois,
    só limpa a marca; se ninguém respondeu, avisa o dono UMA vez e reabre o turno do agente."""
    agent = _agent(ctx)
    now, reopened = ctx.now(), 0
    for lead in ctx.db.active_leads():
        qualification = lead.get("qualification") if isinstance(lead.get("qualification"), dict) else {}
        raw = qualification.get(AGUARDA_HUMANO_KEY)
        if not raw or _takeover_active(lead, now):
            continue
        since = parse_ts(raw)
        try:
            answered = since is not None and any(
                m.get("direction") == "out" and (parse_ts(m.get("created_at")) or since) > since
                for m in ctx.db.recent_messages(lead["id"], 20))
            if answered or since is None:
                _set_qualification_key(ctx, lead, AGUARDA_HUMANO_KEY, _REMOVE)
                continue
            if agent.get("paused") or not ctx.messenger.allowed(lead["phone"]):
                continue   # a marca fica: o aviso sai quando o agente puder falar
            who = lead.get("name") or lead.get("phone")
            alerted = ctx.state.setdefault("aguarda_humano_alerted", set())
            if (lead["id"], raw) not in alerted:   # nova tentativa após falha do turno não repete o aviso
                if not queue_owner_alert(ctx, f"ℹ️ {who} ({lead.get('phone')}) mandou mensagem durante a pausa da IA e "
                                              "ninguém respondeu. A pausa acabou: o agente vai retomar a conversa agora."):
                    continue
                alerted.add((lead["id"], raw))
            _set_qualification_key(ctx, lead, AGUARDA_HUMANO_KEY, _REMOVE)
            try:
                handle_turn(ctx, lead["id"])
            except Exception:
                # o turno não rodou: devolve a marca (mesmo valor) para o próximo run tentar de novo
                try:
                    _set_qualification_key(ctx, lead, AGUARDA_HUMANO_KEY, raw)
                except Exception as restore_exc:  # noqa: BLE001
                    ctx.log.error("não consegui restaurar a marca de aguardando humano de %s: %s",
                                  lead.get("phone"), restore_exc)
                raise
            reopened += 1
        except Exception as exc:  # noqa: BLE001 - um lead com problema não derruba os outros
            ctx.log.error("lead %s aguardando humano: %s", lead.get("phone"), exc)
    return reopened


# ---------------------------------------------------------------- vigia
def _vigia_path(ctx):
    return ctx.cfg.data_dir / "vigia.json"


def _load_vigia(ctx) -> dict:
    try:
        return json.loads(_vigia_path(ctx).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_vigia(ctx, state: dict) -> None:
    path = _vigia_path(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


def vigia(ctx) -> str:
    """Instância caída: avisa o dono UMA vez por queda e avisa de novo quando volta."""
    state = _load_vigia(ctx)
    try:
        status = ctx.evo.connection_state()
    except Exception as exc:  # noqa: BLE001 - sem confirmar o estado não se envia nada
        status = f"sem resposta ({str(exc)[:80]})"
    up = status == "open"
    if not up and not state.get("down_since"):
        ctx.log.error("instância do WhatsApp fora do ar: %s", status)
        # o estado de queda só é gravado DEPOIS do aviso enfileirado: se o aviso falhar, o próximo ciclo tenta de novo
        try:
            notify_owner(ctx, f"⚠️ O WhatsApp do agente está desconectado (estado: {status}). O agente não recebe nem "
                              "envia mensagens até reconectar. Abra a Evolution e leia o QR code de novo se precisar.")
        except Exception as exc:  # noqa: BLE001 - instância fora: as rotinas de envio são puladas neste ciclo
            ctx.log.error("aviso de instância caída não enfileirado: %s", exc)
            return "fora"
        _save_vigia(ctx, {"down_since": iso(ctx.now())})
        return "caiu"
    if up and state.get("down_since"):
        _save_vigia(ctx, {})
        ctx.log.info("instância do WhatsApp voltou")
        try:
            notify_owner(ctx, "✅ O WhatsApp do agente voltou a funcionar. As mensagens pendentes serão processadas.")
        except Exception as exc:  # noqa: BLE001
            ctx.log.error("aviso de instância de volta não enfileirado: %s", exc)
        return "voltou"
    return "ok" if up else "fora"


def run_all(ctx) -> dict:
    results = {}
    for name, fn in (("vigia", vigia), ("followups", followups), ("retomar", retomar),
                     ("lembretes", reminders), ("repasse", handoffs), ("aguardando_humano", aguardando_humano)):
        if name in ("followups", "retomar", "lembretes", "aguardando_humano") \
                and ctx.state.get("ignored", set()) is None:
            results[name] = "pulado (lista de closers não carregada)"   # fail-closed: sem releitura não envia a lead
            continue
        if name != "vigia" and results.get("vigia") in ("caiu", "fora"):
            results[name] = "pulado (WhatsApp fora do ar)"   # tentaria enviar e só geraria erro
            continue
        try:
            results[name] = fn(ctx)
        except Exception as exc:  # noqa: BLE001 - uma rotina com problema não derruba as outras
            ctx.log.exception("rotina %s falhou: %s", name, exc)
            results[name] = f"erro: {exc}"
    return results
