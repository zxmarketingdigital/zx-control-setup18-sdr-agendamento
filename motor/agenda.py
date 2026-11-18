"""Agenda: horários vêm SÓ das RPCs available_slots/book_slot (mesma regra do painel e da página pública)."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta

from .config import env_positive
from .http import HttpError, NetworkError, http_json
from .outbox import alert_closer, notify_owner, queue_owner_alert, send_row
from .util import first_name, fmt_date, fmt_time, get_tz, iso, parse_ts, render, slot_label, to_e164

# Teto da chamada à ponte do Google Agenda. Override: ZX_SDR_GCAL_TIMEOUT.
GCAL_TIMEOUT = env_positive("ZX_SDR_GCAL_TIMEOUT", 40)
OFFER_COUNT = 2          # quantos horários o agente oferece por vez
SLOTS_IN_PROMPT = 12     # quantos horários a IA enxerga para escolher

DEFAULT_CONFIRM = "Olá {nome}! Sua conversa está confirmada para {data} às {hora}."
# Cobre "14h", "16h30", "15:30", "16hs", "14 horas", "às 9" e "3 da tarde/manhã/noite".
# "N horas" solto é duração ("em 2 horas"): só vale como horário entre 7 e 22 e fora de expressões de duração.
TIME_RE = re.compile(
    r"(?<![\d:.,/-])(?:\b(?:as|às)\s+(?P<h0>\d{1,2})(?![\d:hH]|\s*(?:min|reais|r\$|%))(?:\s*(?:horas?|hs?))?(?![a-zà-ú])|"
    r"(?P<h1>\d{1,2})(?:\s*h(?:s(?![a-zà-ú])|(?![a-zà-ú]))|:)\s*(?P<m1>\d{2})?(?!\d)|"
    r"(?P<h3>\d{1,2})\s+horas?(?![a-zà-ú])|"
    r"(?P<h2>\d{1,2})\s+da\s+(?P<p2>manh[ãa]|tarde|noite))",
    re.IGNORECASE)
DURATION_BEFORE_RE = re.compile(r"(?:\b(?:em|de|por|ate|até|cerca de|umas?|uns|dentro de|a cada|durante)\s+)$", re.IGNORECASE)
# "agendei/marquei": promessa de ação, vale mesmo com agendamento ativo (remarcação)
CLAIM_VERB_RE = re.compile(r"\b(agendei|reagendei|marquei|remarquei|reservei|confirmei (?:(?:o|a) )?(?:(?:seu|sua) )?(?:agendamento|horario|horário|conversa|reuniao|reunião|call))\b", re.IGNORECASE)
# "está agendado/marcado": estado, só é mentira se não existir agendamento
_BOOKED_WORD = r"(?:agendad|marcad|confirmad|reservad)[oa]"
BOOKING_CLAIM_RE = re.compile(
    r"\b(?:(?:ficou|fica|est[áa]|foi|j[áa] est[áa])\s+" + _BOOKED_WORD + r"|" +
    _BOOKED_WORD + r"\s+(?:para|pra|no|na|às|as|com)|"
    r"fica(?:ou)?\s+para|"
    r"sua\s+(?:conversa|call|reuni[ãa]o)\s+(?:est[áa]|fica(?:ou)?))\b",
    re.IGNORECASE)


def tz_of(ctx):
    return get_tz((ctx.state.get("settings") or {}).get("timezone"))


def load_slots(ctx, exclude: datetime | None = None) -> list[datetime]:
    """Horários distintos disponíveis (sem repetir por closer), do mais próximo ao mais distante."""
    settings = ctx.state.get("settings") or {}
    now = ctx.now()
    rows = ctx.db.available_slots(now, now + timedelta(days=int(settings.get("days_ahead") or 14)))
    seen, out = set(), []
    for row in rows:
        start = parse_ts(row.get("starts_at"))
        if not start or start in seen or (exclude and start == exclude):
            continue
        seen.add(start)
        out.append(start)
    return sorted(out)


def prompt_slots(slots: list[datetime], tz) -> list[datetime]:
    """Amostra enxuta para a IA: até 3 por dia, no máximo SLOTS_IN_PROMPT."""
    per_day: dict = {}
    chosen = []
    for start in slots:
        day = start.astimezone(tz).date()
        if per_day.get(day, 0) >= 3:
            continue
        per_day[day] = per_day.get(day, 0) + 1
        chosen.append(start)
        if len(chosen) >= SLOTS_IN_PROMPT:
            break
    return chosen


def pick_offers(slots: list[datetime], tz, prefer: list[datetime] | None = None, count: int = OFFER_COUNT):
    """Escolhe os horários a oferecer: os preferidos válidos; depois o mais próximo e um de outro dia."""
    def day(moment):
        return moment.astimezone(tz).date()

    chosen: list[datetime] = [w for w in dict.fromkeys(prefer or []) if w in slots][:count]
    for start in slots:
        if len(chosen) >= count:
            break
        if start in chosen:
            continue
        if len(chosen) == 1:  # segunda opção: tenta outro dia para dar escolha de verdade
            other = next((s for s in slots if s not in chosen and day(s) != day(chosen[0])), None)
            chosen.append(other or start)
        else:
            chosen.append(start)
    return sorted(chosen)


def offer_message(ctx, lead_in: str, offers: list[datetime]) -> str:
    tz, now = tz_of(ctx), ctx.now()
    lines = "\n".join(f"• {slot_label(s, tz, now)}" for s in offers)
    return f"{lead_in.strip()}\n{lines}\nQual fica melhor para você?"


def no_slots_message() -> str:
    return "No momento não tenho horários livres na agenda. Vou avisar a equipe e te retornamos assim que abrir um horário."


def alert_no_slots(ctx, lead: dict) -> None:
    """Sem horário livre: o agente prometeu retorno da equipe, então o dono recebe um alerta de verdade (1x por lead)."""
    alerted = ctx.state.setdefault("no_slots_alerted", set())
    if lead.get("id") in alerted:
        return
    who = lead.get("name") or lead.get("phone")
    try:
        queue_owner_alert(ctx, f"⚠️ {who} ({lead.get('phone')}) quer agendar, mas não há horário livre na agenda. "
                               "Avisei que a equipe retorna: abra horários ou fale com a pessoa.")
    except Exception as exc:  # noqa: BLE001 - sem marcar: o próximo turno tenta o alerta de novo
        ctx.log.error("alerta de agenda sem horário para %s não enfileirado: %s", lead.get("phone"), exc)
        return
    alerted.add(lead.get("id"))   # só depois do enfileiramento bem-sucedido


_WEEKDAYS = {"segunda": 0, "terca": 1, "terça": 1, "quarta": 2, "quinta": 3, "sexta": 4, "sabado": 5, "sábado": 5,
             "domingo": 6}
# referência de dia colada ao horário: "quinta às 15h", "amanhã 10h", "dia 20 às 9h", "20/11 às 14h"
_DAY_REF_RE = re.compile(
    r"(?<![\wà-ú])(?:(?P<rel>depois de amanh[ãa]|amanh[ãa]|hoje)|"
    r"(?P<wd>segunda|ter[çc]a|quarta|quinta|sexta|s[áa]bado|domingo)(?:-feira)?(?!\s+(?:op[cç][ãa]o|vez|parte|via|chance))|"
    r"dia\s+(?P<d1>\d{1,2})(?![\d:hH/])|(?P<d2>\d{1,2})/(?P<m2>\d{1,2})(?!\d))(?![\wà-ú])", re.IGNORECASE)
_DAY_REF_AFTER_RE = re.compile(r"\s*(?:,\s*)?(?:d[aeo]|n[ao]|em)\s+", re.IGNORECASE)
_DAY_REF_REACH = 25   # distância máxima (caracteres) entre a referência de dia e o horário


def _day_ref_near(text: str, start: int, end: int):
    """Referência de dia (match de _DAY_REF_RE) que acompanha o horário em text[start:end]: a mais próxima antes
    dele, na mesma frase, ou 'de/na/em <dia>' logo depois. None se o horário vem sem dia."""
    before = text[max(0, start - 40):start]
    cut = max(before.rfind(c) for c in ".!?\n")
    if cut >= 0:
        before = before[cut + 1:]
    found = None
    for m in _DAY_REF_RE.finditer(before):
        found = m
    if found is not None and len(before) - found.end() <= _DAY_REF_REACH:
        return found
    glue = _DAY_REF_AFTER_RE.match(text, end)
    if glue:
        after = _DAY_REF_RE.match(text, glue.end())
        if after:
            return after
    return None


def _day_matches(ref, local, today) -> bool:
    """O dia local do horário `local` é o dia citado em `ref`?"""
    if ref.group("rel"):
        word = ref.group("rel").lower().replace("ã", "a")
        offset = 2 if word.startswith("depois") else 1 if word.startswith("amanh") else 0
        return local.date() == today + timedelta(days=offset)
    if ref.group("wd"):
        return local.weekday() == _WEEKDAYS[ref.group("wd").lower()]
    if ref.group("d1"):
        return local.day == int(ref.group("d1"))
    return local.day == int(ref.group("d2")) and local.month == int(ref.group("m2"))


def _match_times(match, text: str):
    """(horas possíveis, minuto) de um casamento de TIME_RE, ou None quando não é horário (duração, minuto inválido)."""
    if match.group("h0") is not None:
        hours, minute = [int(match.group("h0"))], 0
    elif match.group("h1") is not None:
        hours, minute = [int(match.group("h1"))], int(match.group("m1") or 0)
    elif match.group("h3") is not None:
        hour = int(match.group("h3"))
        if not 7 <= hour <= 22 or DURATION_BEFORE_RE.search(text[:match.start()]):
            return None
        hours, minute = [hour], 0
    else:
        hour, period = int(match.group("h2")), match.group("p2").lower()
        minute = 0
        # "3 da tarde" = 15h; "9 da noite" = 21h; "9 da manhã" = 9h; "12 da tarde" = 12h
        hours = [hour + 12 if period != "manhã" and period != "manha" and 1 <= hour <= 11 else hour]
    if minute > 59:
        return None
    return hours, minute


def _unlisted_spans(text: str, allowed: list[datetime], tz, now: datetime | None = None) -> list:
    """Trechos (início, fim) do texto que citam um horário (14h, 15:30) fora dos horários reais. Horário com dia
    ("quinta às 15h") só vale se existir um horário livre NAQUELE dia e hora; sem dia, vale só hora e minuto."""
    locals_ = [s.astimezone(tz) for s in allowed]
    ok = {(s.hour, s.minute) for s in locals_}
    today = (now or datetime.now(tz)).astimezone(tz).date()
    spans = []
    for match in TIME_RE.finditer(text or ""):
        parsed = _match_times(match, text)
        if parsed is None:
            continue
        hours, minute = parsed
        ref = _day_ref_near(text, match.start(), match.end())
        for hour in hours:
            if hour > 23:
                continue
            if (hour, minute) not in ok:
                spans.append((match.start(), match.end()))
                break
            if ref is not None and not any(
                    (l.hour, l.minute) == (hour, minute) and _day_matches(ref, l, today) for l in locals_):
                spans.append((match.start(), match.end()))   # a hora existe, mas não nesse dia
                break
    return spans


def mentions_unlisted_time(text: str, allowed: list[datetime], tz, now: datetime | None = None) -> bool:
    """True se o texto cita um horário (14h, 15:30) que não está entre os horários reais (com o dia, quando citado)."""
    return bool(_unlisted_spans(text, allowed, tz, now))


_DANGLING_RE = re.compile(r"\b(?:das|dos|às|as|até|ate|de|do|da|a|ao|entre|e|ou|por|em|para|pra)\s*(?=[.,;:!?)]|$)",
                          re.IGNORECASE)
# conectivo que acompanha o horário removido ("até as 18h", "a partir das 9h"): sai junto
_TIME_CONNECTOR_RE = re.compile(r"\s*\b(?:at[ée]|a partir d[aeo]s?|depois d[aeo]s?|antes d[aeo]s?|das|dos)\s+(?:as\s+|às\s+)?$",
                                re.IGNORECASE)
NEUTRAL_AFTER_STRIP = "Posso te passar os horários disponíveis para a nossa conversa, se você quiser."


def strip_unlisted_times(text: str, allowed: list[datetime], tz, now: datetime | None = None) -> str:
    """Remove do texto as menções de horário que não existem nos slots. Se a remoção esvaziar o sentido (sobrou
    pouco texto ou uma preposição solta), devolve um texto neutro em vez da frase quebrada."""
    spans = _unlisted_spans(text, allowed, tz, now)
    if not spans:
        return text
    out = text
    for start, end in reversed(spans):
        lead = _TIME_CONNECTOR_RE.search(out[:start])
        out = out[:lead.start() if lead else start] + out[end:]
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\s+([.,;:!?])", r"\1", out).strip()
    words = re.findall(r"\w+", out)
    if len(words) < 3 or _DANGLING_RE.search(out):
        return NEUTRAL_AFTER_STRIP
    return out


# frase que OFERECE ou PROMETE agendamento: só nela um horário fora da agenda é "inventado". Horário informativo
# ("atendemos das 9h às 18h", "a call dura 1h") não é oferta.
_OFFER_VERB_RE = re.compile(
    r"\b(?:tenho|temos|tem|posso|podemos|consigo|conseguimos|vaga|vagas|dispon[ií]ve(?:l|is)|livre|livres|"
    r"agend\w*|marc(?:ar|o|amos|aremos)|reserv\w*|encaix\w*|fica|ficou|ficamos|confirmad[oa]s?|confirmo|"
    r"chamo|ligo|retorno|volto|falo|perfeito|combinado|fechado|marcado)\b", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def _split_sentences(text: str) -> list[str]:
    return [p for p in _SENTENCE_SPLIT_RE.split(text or "") if p and p.strip()]


def _offer_sentences_with_unlisted(text: str, allowed: list[datetime], tz, now: datetime | None = None) -> list[str]:
    return [sent for sent in _split_sentences(text)
            if _OFFER_VERB_RE.search(sent) and _unlisted_spans(sent, allowed, tz, now)]


def offers_unlisted_time(text: str, allowed: list[datetime], tz, now: datetime | None = None) -> bool:
    """True se alguma frase que oferece/promete agendamento cita horário fora da agenda."""
    return bool(_offer_sentences_with_unlisted(text, allowed, tz, now))


def strip_unlisted_offers(text: str, allowed: list[datetime], tz, now: datetime | None = None) -> str:
    """Tira só a oferta inventada: frases informativas passam intactas; na frase de oferta o horário sai e, se a frase
    ficar pendurada ou vazia, a FRASE inteira sai. Só cai na resposta neutra se não sobrar nada."""
    bad = set(_offer_sentences_with_unlisted(text, allowed, tz, now))
    if not bad:
        return text
    kept = []
    for sent in _split_sentences(text):
        if sent in bad:
            cleaned = strip_unlisted_times(sent, allowed, tz, now)
            if cleaned == NEUTRAL_AFTER_STRIP:
                continue
            kept.append(cleaned)
        else:
            kept.append(sent)
    out = " ".join(k.strip() for k in kept).strip()
    return out or NEUTRAL_AFTER_STRIP


# r27: frase que AFIRMA o estado do agendamento do lead ("sua call está marcada…", "seu horário é…")
_STATE_STATEMENT_RE = re.compile(
    r"\b(?:marcad[oa]s?|agendad[oa]s?|confirmad[oa]s?|(?:sua|a sua) (?:call|reuniao|conversa|ligacao)|"
    r"(?:seu|o seu) horario)\b")
_RESCHEDULE_TALK_RE = re.compile(r"\b(?:remarc\w*|reagend\w*|outro horario|novo horario|trocar|mudar)\b")


def _statement_is_wrong(sent: str, start: datetime, tz, now: datetime | None) -> bool:
    local = start.astimezone(tz)
    today = (now or datetime.now(tz)).astimezone(tz).date()
    times, refs = [], []
    for match in TIME_RE.finditer(sent):
        parsed = _match_times(match, sent)
        if parsed is None:
            continue
        times.extend((h, parsed[1]) for h in parsed[0])
        ref = _day_ref_near(sent, match.start(), match.end())
        if ref is not None:
            refs.append(ref)
    if not times:
        found = _DAY_REF_RE.search(sent)
        refs = [found] if found else []
    if times and (local.hour, local.minute) not in times:
        return True
    return any(not _day_matches(ref, local, today) for ref in refs)


def fix_appointment_statement(text: str, starts_at: datetime, tz, now: datetime | None = None) -> str:
    """r27: com agendamento ativo, a frase que afirma o estado dele (marcada/agendada/confirmada/sua call/seu horário)
    e cita dia ou hora DIFERENTE do agendamento real é trocada por uma frase com o dia e a hora reais (mesma formatação
    da confirmação). Frase com o horário certo, oferta de remarcação e frase sem dia/hora passam intactas."""
    if not text or starts_at is None:
        return text
    real = f"Sua conversa está marcada para {fmt_date(starts_at, tz)} às {fmt_time(starts_at, tz)}."
    pieces = re.split(r"((?<=[.!?])\s+|\n+)", text)
    result, replaced = [], False
    for piece in pieces:
        plain = _plain(piece)
        if (piece.strip() and _STATE_STATEMENT_RE.search(plain) and not _RESCHEDULE_TALK_RE.search(plain)
                and _statement_is_wrong(piece, starts_at, tz, now)):
            if not replaced:
                result.append(real)   # várias frases erradas viram uma só
            replaced = True
            continue
        result.append(piece)
    if not replaced:
        return text
    return re.sub(r"[ \t]{2,}", " ", "".join(result)).strip()


# promessa de ação com outros verbos: "já te coloquei na agenda", "deixei agendado", "te encaixei"
CLAIM_ACTION_RE = re.compile(
    r"\b(?:(?:coloquei|deixei|pus|botei|inclu[ií])\b.{0,30}\bna agenda|te (?:coloquei|reservei|encaixei|marquei))\b",
    re.IGNORECASE | re.DOTALL)
# só é promessa a forma de ação/estado do horário DO LEAD ("agendei", "ficou marcado", "sua call está confirmada",
# "agendado para você"); o adjetivo descritivo ("turmas marcadas para segunda") não é.
_ANY_BOOKED_RE = re.compile(
    r"\b(?:agendei|marquei|reservei|confirmei|"
    r"(?:est[aá]|ficou|fica|foi|ser[aá]|j[aá] est[aá])\s+(?:j[aá]\s+)?(?:agendad|marcad|reservad|confirmad)[oa]\b|"
    r"(?:seu hor[aá]rio|sua (?:call|reuni[aã]o|conversa|sess[aã]o|consulta)|seu (?:atendimento|encontro)|"
    r"nossa (?:call|reuni[aã]o|conversa))\b[^.!?\n]{0,40}?(?:agendad|marcad|confirmad|reservad)[oa]\b|"
    r"(?:agendad|marcad|reservad)[oa]s?\s+(?:para|pra)\s+(?:voc[eê]|vc|o senhor|a senhora))",
    re.IGNORECASE)
# a frase cita dia ou hora (a palavra de estado sozinha, sem dia/hora, não é promessa de horário)
_DAY_OR_HOUR_RE = re.compile(
    r"\b(?:hoje|amanh[ãa]|depois de amanh[ãa]|segunda|ter[çc]a|quarta|quinta|sexta|s[áa]bado|domingo|dia \d{1,2}|"
    r"\d{1,2}/\d{1,2})\b", re.IGNORECASE)


# "combinado", "fechado", "te espero", "nos vemos": fecham o combinado quando a resposta cita dia ou hora
_PROMISE_WORD_RE = re.compile(
    r"\b(?:combinad[oa]s?|garantid[oa]s?|fechad[oa]s?|te espero|te aguardo|nos vemos)\b")
# "estamos fechados aos domingos" é horário de funcionamento, não combinado
_CLOSED_BUSINESS_RE = re.compile(r"\b(?:esta|estamos|ficamos|fica|ficam|estao|permanece\w*)\s+fechad[oa]s?\b")
# a frase oferece/consulta (não promete): "tenho quinta às 15h livre", "qual horário serve?"
_AVAILABILITY_RE = re.compile(
    r"\b(?:tenho|temos|tem|posso|podemos|consigo|conseguimos|vaga|vagas|dispon[ií]ve(?:l|is)|livre|livres|op[cç][ãa]o|"
    r"op[cç][õo]es|serve|servem|que tal|prefere|qual|quais|se quiser|caso|pode ser|funciona)\b", re.IGNORECASE)


def _plain(text: str) -> str:
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()


def _promises_free_slot(text: str, slots, tz, now) -> bool:
    """A resposta crava dia+hora de um horário LIVRE (sem perguntar nem oferecer): é uma promessa de agendamento."""
    locals_ = [s.astimezone(tz) for s in slots]
    today = (now or datetime.now(tz)).astimezone(tz).date()
    for sent in _split_sentences(text):
        if "?" in sent or _AVAILABILITY_RE.search(sent):
            continue
        for match in TIME_RE.finditer(sent):
            parsed = _match_times(match, sent)
            ref = _day_ref_near(sent, match.start(), match.end()) if parsed else None
            if parsed is None or ref is None:
                continue
            hours, minute = parsed
            if any((l.hour, l.minute) == (h, minute) and _day_matches(ref, l, today) for l in locals_ for h in hours):
                return True
    return False


def claims_booking(text: str, has_appointment: bool = False, slots=None, tz=None, now=None, extended: bool = True) -> bool:
    """True se a resposta afirma um agendamento. Com agendamento ativo, "está marcado" é verdade e não conta.
    Com `slots` e `tz`, também conta cravar dia+hora de um horário livre sem oferecer nem perguntar.
    `extended=False` (ação "retomar": "combinado, falo amanhã às 10h" é retorno, não reunião) liga só as regras antigas."""
    text = text or ""
    if CLAIM_VERB_RE.search(text) or CLAIM_ACTION_RE.search(text):
        return True
    if has_appointment:
        return False
    if BOOKING_CLAIM_RE.search(text):
        return True
    if extended and (_DAY_OR_HOUR_RE.search(text) or TIME_RE.search(text)) and any(
            "?" not in sent and _PROMISE_WORD_RE.search(_plain(_CLOSED_BUSINESS_RE.sub("", _plain(sent))))
            for sent in _split_sentences(text)):
        return True
    if extended and slots and tz is not None and _promises_free_slot(text, slots, tz, now):
        return True
    # agendad/marcad/reservad/confirmad em QUALQUER posição, quando a frase cita dia ou hora
    return bool(_ANY_BOOKED_RE.search(text) and (_DAY_OR_HOUR_RE.search(text) or TIME_RE.search(text)))


def confirmation_text(ctx, lead: dict, agent: dict, start: datetime, closer_name: str = "") -> str:
    tz = tz_of(ctx)
    template = agent.get("confirm_template") or DEFAULT_CONFIRM
    text = render(template, nome=first_name(lead.get("name")), data=fmt_date(start, tz),
                  hora=fmt_time(start, tz), closer=closer_name or "")
    return re.sub(r"\s+([!,.?])", r"\1", text).strip()


_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_]\w*)\}")


def safe_confirmation_text(ctx, lead: dict, agent: dict, start: datetime, closer_name: str = "",
                           alert: bool = True) -> str:
    """r24: o modelo de confirmação do dono nunca derruba o que vem depois da reserva já gravada. Exceção do render
    ou placeholder desconhecido (ex.: «{turma}») = texto padrão ao lead + UM alerta ao dono (por modelo)."""
    template = agent.get("confirm_template") or DEFAULT_CONFIRM
    problem = ""
    try:
        text = confirmation_text(ctx, lead, agent, start, closer_name)
        unknown = sorted(set(_PLACEHOLDER_RE.findall(text)))
        if unknown:
            problem = "campo(s) desconhecido(s): " + ", ".join("{" + name + "}" for name in unknown)
    except Exception as exc:  # noqa: BLE001 - qualquer erro do render cai no texto padrão
        ctx.log.error("modelo de confirmação com erro (%s): %s", type(exc).__name__, exc)
        problem = f"erro ao montar o texto ({type(exc).__name__}: {str(exc)[:80]})"
    if not problem:
        return text
    fallback = dict(agent, confirm_template=DEFAULT_CONFIRM)
    text = confirmation_text(ctx, lead, fallback, start, closer_name)
    if alert:
        done = ctx.state.setdefault("confirm_template_alerted", set())
        if template not in done:
            try:
                if queue_owner_alert(ctx, f"⚠️ O modelo de confirmação do agendamento tem erro ({problem}). O lead "
                                          "recebeu a confirmação padrão. Corrija o modelo no painel.") is not False:
                    done.add(template)   # só depois de enfileirar: se falhar, o próximo agendamento tenta de novo
            except Exception as exc:  # noqa: BLE001
                ctx.log.error("alerta de modelo de confirmação com erro não enfileirado: %s", exc)
    return text


def _confirm_direct(ctx, lead: dict, text: str) -> None:
    """A reserva deu certo mas a confirmação não entrou na outbox: envia direto e avisa o dono."""
    from .conversa import deliver   # import tardio: conversa importa este módulo
    try:
        deliver(ctx, lead, text)
    except Exception as exc:  # noqa: BLE001
        ctx.log.error("confirmação direta de %s falhou: %s", lead.get("phone"), exc)
    try:
        notify_owner(ctx, f"⚠️ {lead.get('name') or lead.get('phone')} ({lead.get('phone')}) foi agendado(a), mas a "
                          "confirmação não pôde ser registrada na fila. Tentei enviar direto: confira se chegou.")
    except Exception as exc:  # noqa: BLE001
        ctx.log.error("alerta de confirmação direta não enviado: %s", exc)


def _confirmation_in_outbox(ctx, client_key):
    """A linha da confirmação (client_key determinística) já está na outbox? {} se sim, None se não (ou se não deu
    para saber: nesse caso o envio direto segue, com aviso ao dono para conferir)."""
    if not client_key:
        return None
    try:
        row = ctx.db.outbox_by_client_key(client_key)
    except Exception as exc:  # noqa: BLE001
        ctx.log.error("não consegui consultar a outbox por %s: %s", client_key, exc)
        return None
    return row if row else None


def _confirmation_covers_request(existing: dict, lead: dict) -> bool:
    """A linha já existente cobre o pedido atual? Linha 'enviado' criada ANTES da última mensagem do lead é de um
    pedido anterior (mesmo horário confirmado de novo): não cobre."""
    if existing.get("status") != "enviado":
        return True
    created, asked = parse_ts(existing.get("created_at")), parse_ts(lead.get("last_inbound_at"))
    return not (created and asked and created < asked)


def _book_failure_text() -> str:
    return "Não consegui confirmar esse horário agora. Já volto com você, só um instante."


def _booking_already_done(ctx, lead: dict, starts_at: datetime, now: datetime) -> dict | None:
    """Depois de erro de rede: o agendamento pedido já existe? Devolve um resultado no formato da RPC, ou None."""
    try:
        current = ctx.db.active_appointment(lead["id"], now)
        if not current or parse_ts(current.get("starts_at")) != starts_at:
            return None
        closer = ctx.db.get_closer(current["closer_id"]) if current.get("closer_id") else None
        return {"ok": True, "appointment_id": current.get("id"), "starts_at": current.get("starts_at"),
                "ends_at": current.get("ends_at"), "closer_id": current.get("closer_id"),
                "closer_name": (closer or {}).get("name") or ""}
    except Exception as exc:  # noqa: BLE001 - sem confirmar o estado, segue como falha
        ctx.log.error("não consegui conferir o agendamento de %s: %s", lead.get("phone"), exc)
        return None


def book_for_lead(ctx, lead: dict, starts_at: datetime, agent: dict, notes: str | None = None,
                  queue_confirmation: bool = False):
    """Reserva (ou remarca) pela RPC book_slot / reschedule_slot (esta é atômica no banco).

    Devolve (ok, texto_para_o_lead). Se o horário foi tomado, o texto já traz os 2 próximos horários.
    queue_confirmation=True: a confirmação é gravada na outbox ANTES de qualquer passo que possa falhar depois
    da reserva (e enviada em seguida); quem chama NÃO deve enviar o texto de novo.
    """
    if getattr(ctx.messenger, "simulated", False):
        # simulação nunca grava agendamento nem chama a ponte do Google Agenda
        start = starts_at
        return False, ("(simulação: nada foi agendado) " + safe_confirmation_text(ctx, lead, agent, start, "", alert=False))
    now = ctx.now()
    previous = ctx.db.active_appointment(lead["id"], now)
    try:
        if previous is not None:
            result = ctx.db.reschedule_slot(previous["id"], starts_at)
            if result.get("error") in ("agendamento_inexistente", "agendamento_inativo"):
                previous = None   # mudou entre a leitura e a troca: segue como reserva nova
                result = ctx.db.book_slot(lead["id"], starts_at, source="agente", notes=notes)
        else:
            result = ctx.db.book_slot(lead["id"], starts_at, source="agente", notes=notes)
    except Exception as exc:  # noqa: BLE001 - rede/HTTP: a RPC pode ter gravado antes de a resposta se perder
        ctx.log.error("reserva falhou para %s: %s", lead.get("phone"), exc)
        result = _booking_already_done(ctx, lead, starts_at, now)
        if result is None:
            notify_owner(ctx, f"⚠️ Falha ao reservar horário para {lead.get('name') or lead.get('phone')} "
                              f"({lead.get('phone')}): {str(exc)[:150]}. Confira o agendamento deste lead.")
            return False, _book_failure_text()
        notify_owner(ctx, f"⚠️ A resposta da reserva de {lead.get('name') or lead.get('phone')} ({lead.get('phone')}) "
                          "se perdeu na rede, mas o agendamento existe no banco. Mandei a confirmação; confira.")
    if result.get("ok"):
        start = parse_ts(result.get("starts_at")) or starts_at
        text = safe_confirmation_text(ctx, lead, agent, start, result.get("closer_name") or "")
        row, queued = None, not queue_confirmation
        handled = not queue_confirmation   # True: uma confirmação foi enfileirada/enviada NESTA chamada
        ctx.state["confirmation_handled"] = False
        if queue_confirmation:
            # o horário E o pedido (carimbo da última mensagem do lead) entram na chave: remarcar de volta para um
            # horário já confirmado antes é outro pedido e enfileira a sua própria confirmação
            ckey = (f"booking:{result['appointment_id']}:{iso(start)}:{lead.get('last_inbound_at') or ''}:confirm"
                    if result.get("appointment_id") else None)
            try:
                row = ctx.db.enqueue_outbox(lead["phone"], text, lead_id=lead["id"], created_by="motor",
                                            client_key=ckey)
                queued = bool(row)
                handled = queued
                if not row and ckey:
                    # vazio com client_key: duplicata ignorada (a linha já existe e sai pelo process_outbox) ou falha
                    existing = _confirmation_in_outbox(ctx, ckey)
                    queued = existing is not None
                    handled = queued and _confirmation_covers_request(existing, lead)
            except Exception as exc:  # noqa: BLE001 - a reserva já existe: nunca relançar daqui
                ctx.log.error("confirmação de %s não entrou na outbox: %s", lead.get("phone"), exc)
                existing = _confirmation_in_outbox(ctx, ckey)   # a resposta pode ter se perdido depois de gravar
                if existing is not None:
                    queued = True
                    handled = _confirmation_covers_request(existing, lead)
                    row = existing if existing.get("status") == "pendente" else None
        booked_info = {**result, "starts_at": result.get("starts_at") or iso(starts_at)}
        if previous is not None:
            _post_booking_step(ctx, lead, booked_info, "atualizar o estágio do lead para reagendado",
                               lambda: ctx.db.update_lead(lead["id"], {"stage": "reagendado"}))
            _post_booking_step(ctx, lead, booked_info, "remover o evento antigo do Google Agenda",
                               lambda: _gcal_cancel(ctx, previous))
        _post_booking_step(ctx, lead, booked_info, "criar o evento no Google Agenda",
                           lambda: _gcal_create(ctx, lead, result, agent,
                                                previous_event_id=(previous or {}).get("google_event_id")))
        if row:
            try:
                send_row(ctx, row)
            except Exception as exc:  # noqa: BLE001 - a linha segue na outbox para o próximo ciclo
                ctx.log.error("confirmação de %s não enviada agora: %s", lead.get("phone"), exc)
        elif not queued:
            _confirm_direct(ctx, lead, text)
            handled = True
        ctx.state["confirmation_handled"] = handled   # apply_decision envia o texto quando nada foi enfileirado
        return True, text
    error = result.get("error")
    if error in ("slot_indisponivel", "fora_da_agenda"):
        try:
            others = pick_offers(load_slots(ctx, exclude=starts_at), tz_of(ctx))
        except Exception as exc:  # noqa: BLE001
            ctx.log.error("não consegui ler a agenda: %s", exc)
            _alert_booking_problem(ctx, lead, starts_at, f"agenda ilegível ao oferecer outros horários: {str(exc)[:120]}")
            return False, _book_failure_text()
        if not others:
            alert_no_slots(ctx, lead)
            return False, no_slots_message()
        if previous is None:   # quem já tem horário marcado segue com ele até a troca dar certo
            ctx.db.update_lead(lead["id"], {"stage": "horarios_ofertados"})
        return False, offer_message(ctx, "Esse horário acabou de ser ocupado. Tenho estes outros:", others)
    if error == "lead_ja_agendado":
        current = ctx.db.active_appointment(lead["id"], now)
        if current:
            start = parse_ts(current.get("starts_at"))
            tz = tz_of(ctx)
            return False, f"Você já tem uma conversa marcada para {fmt_date(start, tz)} às {fmt_time(start, tz)}."
    ctx.log.error("reserva recusada para %s: %s", lead.get("phone"), result)
    _alert_booking_problem(ctx, lead, starts_at, f"reserva recusada: {str(error or result)[:120]}")
    return False, _book_failure_text()


def _alert_booking_problem(ctx, lead: dict, starts_at: datetime, detail: str) -> None:
    """Reserva que falhou sem exceção: o lead recebe o texto neutro, mas o dono precisa saber (nome, telefone,
    horário pedido e o erro) para atender à mão."""
    tz = tz_of(ctx)
    try:
        queue_owner_alert(ctx, f"⚠️ Não consegui reservar o horário pedido por {lead.get('name') or lead.get('phone')} "
                               f"({lead.get('phone')}): {fmt_date(starts_at, tz)} às {fmt_time(starts_at, tz)}. "
                               f"{detail}. O lead recebeu um aviso neutro: atenda-o você.")
    except Exception as exc:  # noqa: BLE001
        ctx.log.error("alerta de reserva não concluída não enfileirado: %s", exc)


def _post_booking_step(ctx, lead: dict, booked: dict, what: str, step) -> None:
    """Passo acessório depois da reserva: cada um no próprio try; falha avisa dono e closer (dia, hora, lead)."""
    try:
        step()
    except Exception as exc:  # noqa: BLE001 - o agendamento já existe; o resto é acessório
        ctx.log.error("pós-reserva (%s) falhou para %s: %s", what, lead.get("phone"), exc)
        detail, closer = _when_closer(ctx, lead, booked)
        text = (f"⚠️ Agendamento feito ({detail}), mas não consegui {what}: {str(exc)[:100]}. "
                "Confira a agenda e o lead.")
        try:
            queue_owner_alert(ctx, text)
        except Exception as alert_exc:  # noqa: BLE001
            ctx.log.error("alerta de pós-reserva não enfileirado: %s", alert_exc)
        dest = to_e164((closer or {}).get("whatsapp"))
        if dest:
            try:
                alert_closer(ctx, dest, text)
            except Exception as closer_exc:  # noqa: BLE001
                ctx.log.error("aviso de pós-reserva ao closer não enviado: %s", closer_exc)


# ---- ponte opcional com o Google Agenda (Apps Script) ----
class GoogleCalendar:
    def __init__(self, url: str, secret: str, log):
        self.url, self.secret, self.log = url, secret, log
        self.last_uncertain = False   # a última chamada deu timeout/erro de rede: a ponte pode ter processado

    def _post(self, payload: dict) -> dict:
        self.last_uncertain = False
        try:
            result = http_json("POST", self.url, body={**payload, "secret": self.secret}, timeout=GCAL_TIMEOUT)
        except (HttpError, NetworkError) as exc:
            self.last_uncertain = isinstance(exc, NetworkError)
            self.log.error("Google Agenda: falha na ponte: %s", exc)
            return {}
        return result if isinstance(result, dict) else {}

    def create(self, title, description, start: datetime, end: datetime, guest_email: str = "",
               appointment_id=None) -> str:
        """appointment_id vai no payload como chave de idempotência (a ponte pode reconhecer duplicata). O motor
        envia "<id do agendamento>:<início ISO>": remarcar muda a chave."""
        payload = {"action": "create", "title": title, "description": description,
                   "start": iso(start), "end": iso(end), "guest_email": guest_email}
        if appointment_id:
            payload["appointment_id"] = str(appointment_id)
        result = self._post(payload)
        if not result.get("ok"):
            self.log.error("Google Agenda: evento não criado: %s", result.get("error"))
            return ""
        return str(result.get("id") or "")

    def cancel(self, event_id: str) -> bool:
        return bool(self._post({"action": "cancel", "id": event_id}).get("ok"))

    def ping(self) -> bool:
        return bool(self._post({"action": "ping"}).get("ok"))


def _gcal_create(ctx, lead: dict, booked: dict, agent: dict, previous_event_id=None) -> None:
    """Cria o evento no Google Agenda. Falha nunca fica só no log: avisa dono (e closer) com os dados para inserir
    à mão. Não há fila de retentativa nesta versão."""
    if not ctx.gcal:
        return
    try:
        start, end = parse_ts(booked.get("starts_at")), parse_ts(booked.get("ends_at"))
        name = lead.get("name") or lead.get("phone")
        description = f"Lead: {name}\nWhatsApp: {lead.get('phone')}\nAgendado pelo agente {agent.get('agent_name', '')}."
        # chave de idempotência = agendamento + horário: a remarcação (mesmo appointment_id, outro starts_at) nunca
        # recebe de volta o evento antigo, já cancelado, que a ponte guardou com a chave anterior
        key = f"{booked['appointment_id']}:{iso(start)}" if booked.get("appointment_id") and start else None
        event_id = ctx.gcal.create(f"Conversa com {name}", description, start, end, lead.get("email") or "",
                                   appointment_id=key)
        if event_id and previous_event_id and event_id == previous_event_id:
            ctx.log.error("Google Agenda: a ponte devolveu o id do evento antigo (%s) na remarcação", event_id)
            event_id = ""   # evento cancelado: não conta como criado, o dono insere o novo à mão
    except Exception as exc:  # noqa: BLE001 - a ponte é opcional e nunca derruba o agendamento
        ctx.log.error("Google Agenda: %s", exc)
        event_id = ""
    if not event_id:
        # timeout/erro de rede: a ponte pode ter criado o evento. Sem retentativa automática (duplicaria)
        _alert_gcal_create_failed(ctx, lead, booked, uncertain=bool(getattr(ctx.gcal, "last_uncertain", False)))
        return
    if booked.get("appointment_id"):
        try:
            ctx.db.update_appointment(booked["appointment_id"], {"google_event_id": event_id})
        except Exception as exc:  # noqa: BLE001
            ctx.log.error("Google Agenda: evento %s criado, mas o id não foi gravado: %s", event_id, exc)
            try:
                queue_owner_alert(ctx, "⚠️ O evento da conversa de " + _when_closer(ctx, lead, booked)[0] +
                                  " foi criado no Google Agenda, mas não consegui gravar o id dele. O cancelamento "
                                  "automático desse evento NÃO vai funcionar: se ele for cancelado ou remarcado, "
                                  "remova o evento da agenda do Google manualmente.")
            except Exception as alert_exc:  # noqa: BLE001
                ctx.log.error("alerta de id do evento não gravado não enfileirado: %s", alert_exc)


def _when_closer(ctx, lead: dict, booked: dict):
    """('dia às hora com Closer (lead Nome)', closer dict ou None) para os avisos do Google Agenda."""
    start = parse_ts(booked.get("starts_at"))
    tz = tz_of(ctx)
    when = f"{fmt_date(start, tz)} às {fmt_time(start, tz)}" if start else "horário desconhecido"
    closer = None
    try:
        closer = ctx.db.get_closer(booked["closer_id"]) if booked.get("closer_id") else None
    except Exception as exc:  # noqa: BLE001
        ctx.log.error("não consegui ler o closer do agendamento: %s", exc)
    who = lead.get("name") or lead.get("phone")
    return f"{when}, closer {(closer or {}).get('name') or 'não identificado'}, lead {who}", closer


def _alert_gcal_create_failed(ctx, lead: dict, booked: dict, uncertain: bool = False) -> None:
    detail, closer = _when_closer(ctx, lead, booked)
    if uncertain:
        text = (f"⚠️ A ponte do Google Agenda não respondeu a tempo ao criar o evento: {detail}. O evento pode ter "
                "sido criado: confira a agenda antes de criar de novo. A conversa está marcada no sistema.")
    else:
        text = (f"⚠️ Não consegui criar o evento no Google Agenda: {detail}. A conversa está marcada no sistema; "
                "insira o evento na agenda manualmente.")
    try:
        queue_owner_alert(ctx, text)
    except Exception as exc:  # noqa: BLE001
        ctx.log.error("alerta de evento não criado não enfileirado: %s", exc)
    dest = to_e164((closer or {}).get("whatsapp"))
    if dest:
        try:
            alert_closer(ctx, dest, text)
        except Exception as exc:  # noqa: BLE001
            ctx.log.error("aviso do evento não criado ao closer não enviado: %s", exc)


def _gcal_cancel(ctx, appointment: dict) -> None:
    """Cancela o evento no Google Agenda. Falha (retorno False ou exceção) nunca passa em silêncio: log e aviso
    ao dono (e ao closer) com dia/hora/closer do evento a remover manualmente."""
    if not (ctx.gcal and appointment.get("google_event_id")):
        return
    detail = ""
    try:
        if ctx.gcal.cancel(appointment["google_event_id"]):
            return
        detail = "a ponte respondeu que não cancelou"
    except Exception as exc:  # noqa: BLE001
        detail = str(exc)[:120]
    ctx.log.error("Google Agenda: evento %s não cancelado: %s", appointment.get("google_event_id"), detail)
    _alert_gcal_cancel_failed(ctx, appointment)


def _alert_gcal_cancel_failed(ctx, appointment: dict) -> None:
    start = parse_ts(appointment.get("starts_at"))
    tz = tz_of(ctx)
    when = f"{fmt_date(start, tz)} às {fmt_time(start, tz)}" if start else "horário desconhecido"
    closer = None
    try:
        closer = ctx.db.get_closer(appointment["closer_id"]) if appointment.get("closer_id") else None
    except Exception as exc:  # noqa: BLE001
        ctx.log.error("não consegui ler o closer do evento não cancelado: %s", exc)
    closer_name = (closer or {}).get("name") or "closer não identificado"
    text = (f"⚠️ Não consegui remover da agenda do Google o evento de {when} ({closer_name}). "
            "Remova-o manualmente para não ficar reunião duplicada ou cancelada na agenda.")
    try:
        queue_owner_alert(ctx, text)
    except Exception as exc:  # noqa: BLE001
        ctx.log.error("alerta de evento não removido não enfileirado: %s", exc)
    dest = to_e164((closer or {}).get("whatsapp"))
    if dest:
        try:
            alert_closer(ctx, dest, text)
        except Exception as exc:  # noqa: BLE001
            ctx.log.error("aviso do evento não removido ao closer não enviado: %s", exc)
