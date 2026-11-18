"""Motor: lê a Evolution, responde conversas, roda a outbox e as rotinas periódicas."""

from __future__ import annotations

import logging
import logging.handlers
import time
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from . import jobs, whatsapp
from .agenda import GoogleCalendar
from .ai import AIClient
from .config import TAKEOVER_HOURS, MotorConfig
from .context import Ctx
from .config import env_positive
from .conversa import (NAME_MAX, PAUSE_INBOUND_KEY, _clear_pause_mark, handle_turn, sanitize_lead_text, last_message_is_agent_reply, is_opt_out,
                       warn_opt_out_with_appointment)
from .db import CLOSER_STAGES, TERMINAL_STAGES, Database
from .evolution import (EvolutionClient, EvolutionError, EvolutionFormatoDesconhecido, is_silent_record, record_media, record_phone, record_text,
                        record_timestamp, record_timestamp_or_none)
from .outbox import (FINAL_FAILURE_PREFIX, SENT_UNRECORDED_ERROR, Messenger, normalize_text, notify_owner, phone_set, process_outbox, queue_owner_alert,
                     recover_stuck_outbox)
from .http import HttpError, NetworkError
from .ratelimit import RateLimiter
from .util import iso, parse_ts, to_e164

POLL_LIMIT = 50   # tamanho de cada página lida da Evolution
# Teto de páginas por ciclo (override: ZX_SDR_MAX_POLL_PAGES). Bateu o teto sem chegar a página já vista = alerta ao dono.
MAX_POLL_PAGES = max(1, int(env_positive("ZX_SDR_MAX_POLL_PAGES", 10)))
# De quanto em quanto tempo o ciclo reconfere leads com mensagem sem resposta (override: ZX_SDR_RECOVER_EVERY_SECONDS).
RECOVER_EVERY_SECONDS = env_positive("ZX_SDR_RECOVER_EVERY_SECONDS", 300)
KNOWN_CHUNK = 100   # wa_ids por consulta (URL do PostgREST tem limite de tamanho)
# Quantas vezes um lead cujo turno deu exceção é reprocessado nos ciclos seguintes antes de alertar o dono.
MAX_TURN_ATTEMPTS = 3


def setup_logging(cfg: MotorConfig, console: bool = True) -> logging.Logger:
    log = logging.getLogger("motor")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(cfg.log_dir / "motor.log", maxBytes=1_000_000,
                                                        backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        log.addHandler(stream)
    return log


def build_ctx(cfg: MotorConfig, log: logging.Logger) -> Ctx:
    db = Database(cfg.supabase_url, cfg.service_role_key)
    evo = EvolutionClient(cfg.evolution_url, cfg.evolution_key, cfg.instance)
    ai = AIClient(cfg.ai_provider, cfg.ai_key, cfg.model, log)
    limiter = RateLimiter(cfg.data_dir / "ratelimit.db", max_per_minute=cfg.max_msgs_per_minute)
    ctx = Ctx(cfg=cfg, db=db, evo=evo, ai=ai, messenger=None, log=log)
    ctx.messenger = Messenger(evo, limiter, db, log, ctx.now_fn, ctx.sleep_fn, lambda: ctx.state.get("agent"))
    if cfg.gcal_enabled:
        ctx.gcal = GoogleCalendar(cfg.gcal_url, cfg.gcal_secret, log)
    return ctx


def refresh(ctx: Ctx) -> None:
    """Recarrega do banco o que o dono edita pelo painel (vale já na próxima conversa)."""
    ctx.state["agent"] = ctx.db.get_agent_config()
    ctx.state["settings"] = ctx.db.get_schedule_settings()
    ctx.state["ignored"] = _ignored_phones(ctx)


def _ignored_phones(ctx: Ctx):
    """Quem NÃO é lead: o dono e os closers (respondem ao repasse) e o próprio número da instância.

    Falha ao ler os closers mantém o ÚLTIMO conjunto válido; sem nenhum ainda devolve None (o ingest do ciclo
    aborta: melhor não ler do que tratar um closer como lead)."""
    agent = ctx.state.get("agent") or {}
    test = phone_set(agent.get("test_phones"))
    ignored = {to_e164(agent.get("owner_phone"))}
    try:
        closers = {to_e164(c.get("whatsapp")) for c in ctx.db.list_closers(active_only=False)}
        ctx.state["closer_phones"] = closers
    except Exception as exc:  # noqa: BLE001
        closers = ctx.state.get("closer_phones")
        if closers is None:
            ctx.log.error("não consegui listar closers e não há lista anterior: ingestão pausada neste ciclo (%s)", exc)
            return None
        ctx.log.warning("não consegui listar closers, mantendo a última lista válida: %s", exc)
    ignored |= closers
    ignored.add(ctx.state.get("own_phone", ""))
    result = {p for p in ignored if p}
    if agent.get("test_only") is not False:
        result -= test   # modo teste: número de teste do dono continua valendo como lead
    return result   # ao vivo: dono e closers seguem ignorados, mesmo que ainda estejam em test_phones


def _media_body(ctx: Ctx, record: dict, kind: str, caption: str) -> str:
    return _media_body_heard(ctx, record, kind, caption)[0]


def _media_body_heard(ctx: Ctx, record: dict, kind: str, caption: str) -> tuple[str, bool]:
    """Corpo gravado da mídia e se o agente a OUVIU/VIU: True quando há transcrição, ou legenda de texto do lead.
    Áudio/imagem sem transcrição (provedor sem suporte, ou falha depois dos retries) devolve False: o motor não sabe
    o que o lead disse (pode ser um pedido de saída)."""
    label = {"audio": "áudio", "image": "imagem", "video": "vídeo", "document": "documento", "sticker": "figurinha"}[kind]
    if kind in ("audio", "image") and ctx.cfg.ai_provider == "gemini":
        try:
            media = ctx.evo.media_base64(record)
            if media:
                transcribed = str(ctx.ai.transcribe(*media) or "").strip()
                if transcribed:
                    return f"[{label}] {transcribed}".strip() + (f" (legenda: {caption})" if caption else ""), True
        except Exception as exc:  # noqa: BLE001 - mídia sem transcrição vira placeholder
            ctx.log.warning("mídia não transcrita: %s", exc)
    heard = kind not in ("audio", "image") or bool(caption)
    return f"[{label}]" + (f" {caption}" if caption else ""), heard


def _known_ids(ctx: Ctx, records: list) -> set:
    ids = [(r.get("key") or {}).get("id") for r in records]
    known: set = set()
    for start in range(0, len(ids), KNOWN_CHUNK):
        known |= ctx.db.known_wa_ids(ids[start:start + KNOWN_CHUNK])
    return known


SEEN_LIMIT = 20000


def _seen_ids(ctx: Ctx) -> set:
    """wa_ids já vistos neste processo, inclusive os descartados (reação, grupo, modo teste): nunca são gravados
    no banco, então só a memória sabe que já foram examinados."""
    seen = ctx.state.setdefault("seen_wa_ids", set())
    if len(seen) > SEEN_LIMIT:
        seen.clear()
    return seen


def _is_unexamined_lead_record(record: dict, known: set, seen: set) -> bool:
    """Conversa individual (não grupo) com texto ou mídia que ainda não foi examinada nem está no banco."""
    key = record.get("key") or {}
    wa_id = key.get("id")
    if not wa_id or wa_id in known or wa_id in seen:
        return False
    jid = key.get("remoteJid") or ""
    if not (record_phone(key) or jid.endswith("@lid")):
        return False   # grupo, status ou broadcast
    if is_silent_record(record):
        return False   # reação, protocolo
    return bool(record_text(record) or record_media(record))


def _read_records(ctx: Ctx) -> tuple[list, set]:
    """Lê a Evolution (ver _read_pages) e aposenta os wa_ids com falha de gravação que a janela de catch-up ou o
    número de ciclos sem reaparecer esgotou: sem isso um wa_id que a Evolution não devolve mais mantém a
    leitura de todas as páginas em todo ciclo."""
    failed = ctx.state.setdefault("failed_wa_ids", set())
    _expire_failed(ctx, failed)
    waiting = set(failed)   # wa_ids cuja gravação falhou num poll anterior e ainda não reapareceram nesta leitura
    result = _read_pages(ctx, waiting)   # erro de leitura sobe: não conta como "ausente"
    _expire_failed(ctx, failed, waiting)
    return result


def _read_pages(ctx: Ctx, waiting: set) -> tuple[list, set]:
    """Pagina a Evolution (mais recentes primeiro) até esvaziar ou até uma página inteira já conhecida
    (no banco ou já examinada por este processo).

    Bateu o teto de páginas sem chegar a uma página já vista: alerta o dono (pode haver mensagem fora)."""
    records: list = []
    known: set = set()
    seen = _seen_ids(ctx)
    failed = ctx.state.setdefault("failed_wa_ids", set())
    last_chunk: list = []
    last_known: set = set()
    for page in range(1, MAX_POLL_PAGES + 1):
        chunk = ctx.evo.recent_messages(POLL_LIMIT, page)
        records.extend(chunk)
        chunk_known = _known_ids(ctx, chunk)
        last_chunk, last_known = chunk, chunk_known
        known |= chunk_known
        waiting -= {(r.get("key") or {}).get("id") for r in chunk}
        if len(chunk) < POLL_LIMIT:
            failed -= waiting   # a Evolution não tem mais essas mensagens: não há o que reler
            return records, known
        if not waiting and all(((r.get("key") or {}).get("id") in chunk_known or (r.get("key") or {}).get("id") in seen
                or not (r.get("key") or {}).get("id")) for r in chunk):
            return records, known   # daqui para trás já está tudo examinado
    ctx.log.error("leitura da Evolution parou no teto de %d páginas: pode haver mensagens fora do ciclo", MAX_POLL_PAGES)
    # alerta SEMPRE (1x por dia): o teto corta as mensagens mais antigas de qualquer tipo, então o conteúdo da
    # última página não prova que nenhum lead ficou de fora
    _alert_once(ctx, "poll_cap", f"⚠️ Chegaram mais mensagens do que o motor consegue ler de uma vez "
                                 f"({MAX_POLL_PAGES * POLL_LIMIT}): a leitura atingiu o limite de {MAX_POLL_PAGES} "
                                 "páginas e mensagens mais antigas podem não ter sido lidas. Pode haver conversa "
                                 "sem resposta: confira o WhatsApp.")
    return records, known


_NAME_URL_RE = re.compile(r"(?:https?://|ftp://|www\.)\S+|\b[\w-]+(?:\.[\w-]+)+/\S*", re.IGNORECASE)
SNIPPET_MAX = 120   # trecho de mensagem do lead citado nos alertas ao dono


def clean_lead_name(value) -> str:
    """Nome do contato (pushName) tratado como DADO: sem quebras de linha nem caracteres de controle, espaços
    colapsados, sem URLs/links e no máximo NAME_MAX caracteres. Fonte única para a ingestão e para os alertas."""
    text = "".join(" " if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") else c for c in str(value or ""))
    text = _NAME_URL_RE.sub(" ", text)
    return sanitize_lead_text(text, NAME_MAX)


def _who(lead: dict, phone: str) -> str:
    """Como o lead aparece nos alertas ao dono: nome saneado entre «», ou só o telefone."""
    name = clean_lead_name(lead.get("name"))
    return f"«{name}»" if name else phone


def _snippet(text) -> str:
    return sanitize_lead_text(text, SNIPPET_MAX)


def _alert_once(ctx: Ctx, key: str, text: str) -> None:
    """Alerta ao dono no máximo uma vez por dia por chave (não inunda o WhatsApp do dono)."""
    today = ctx.now().date().isoformat()
    alerted = ctx.state.setdefault("alerted_day", {})
    if alerted.get(key) == today:
        return
    try:
        notify_owner(ctx, text)
        alerted[key] = today   # só depois de enfileirar: falha não gasta o alerta do dia
    except Exception as exc:  # noqa: BLE001
        ctx.log.error("alerta ao dono não enviado: %s", exc)


def ingest(ctx: Ctx) -> set:
    """Lê as mensagens recentes da Evolution e grava as novas (idempotente por wa_id).

    Exceção em UMA mensagem não derruba o lote: é logada, o dono é avisado e o resto segue (a mensagem sem
    gravação volta no próximo poll; a já gravada não perde a resposta). Devolve os leads que precisam de resposta."""
    agent = ctx.state.get("agent") or {}
    ignored = ctx.state.get("ignored", set())
    if ignored is None:
        return set()   # fail-closed: sem a lista de closers não dá para separar lead de closer
    records, known = _read_records(ctx)
    return _process_records(ctx, records, known, agent, ignored)


MAX_WA_ATTEMPTS = 3   # tentativas de ingestão por wa_id antes de virar falha permanente (alerta próprio)
CHAT_READ_LIMIT = 10  # mensagens lidas na leitura direcionada do chat de um lead


def _process_records(ctx: Ctx, records: list, known: set, agent: dict, ignored: set) -> set:
    records = sorted(records, key=record_timestamp)
    _flush_failed_alerts(ctx)   # avisos de "mensagem ilegível" que não puderam ser enfileirados antes
    seen = _seen_ids(ctx)
    failed = ctx.state.setdefault("failed_wa_ids", set())
    attempts = ctx.state.setdefault("failed_wa_attempts", {})
    permanent = ctx.state.setdefault("failed_wa_permanent", set())
    fresh: set = set()
    failures = 0
    for record in records:
        wa_id = (record.get("key") or {}).get("id")
        if wa_id and wa_id in permanent:
            continue   # falha permanente já registrada e avisada: não tenta de novo a cada ciclo
        if not wa_id or wa_id in known:
            if wa_id:
                if wa_id in _pending_effects(ctx):
                    # mensagem gravada, mas o PATCH do lead (last_inbound_at/opt-out) falhou antes: reaplica o efeito
                    # (idempotente, nunca reenvia resposta). Falhou de novo: continua na releitura.
                    try:
                        _reapply_inbound_effects(ctx, wa_id)
                    except Exception as exc:  # noqa: BLE001
                        ctx.log.warning("efeito da mensagem %s ainda não aplicado (tento no próximo poll): %s", wa_id, exc)
                        failed.add(wa_id)
                        _note_failed(ctx, wa_id, record)
                        continue
                failed.discard(wa_id)   # já está no banco
                attempts.pop(wa_id, None)
            continue
        stored: list = []   # lead cuja mensagem já foi gravada: mesmo com erro depois, ele ainda precisa de resposta
        try:
            lead_id = _ingest_record(ctx, record, agent, ignored, stored)
            seen.add(wa_id)   # examinada (gravada ou descartada de propósito): não precisa ler de novo
            failed.discard(wa_id)
            attempts.pop(wa_id, None)
            if lead_id:
                fresh.add(lead_id)
        except EcoIndeterminado as exc:
            # "não sei" (outbox ilegível): nada foi gravado nem decidido; volta no próximo poll sem contar tentativa
            ctx.log.warning("eco %s ainda indeterminado, releio no próximo poll: %s", wa_id, exc)
            failed.add(wa_id)
            _note_failed(ctx, wa_id, record)
            continue
        except Exception as exc:  # noqa: BLE001 - uma mensagem ruim não derruba as outras
            failures += 1
            ctx.log.exception("erro ao ingerir a mensagem %s: %s", wa_id, exc)
            fresh.update(stored)
            if _transient_error(exc) and _within_catchup(ctx, record):
                failed.add(wa_id)   # rede/5xx/408/429: continua na releitura enquanto dentro da janela, sem contar
                _note_failed(ctx, wa_id, record)
                continue
            attempts[wa_id] = attempts.get(wa_id, 0) + 1
            if attempts[wa_id] >= MAX_WA_ATTEMPTS:
                _fail_permanently(ctx, record, wa_id, attempts[wa_id])
            else:
                failed.add(wa_id)   # o próximo poll não para antes de relê-la
                _note_failed(ctx, wa_id, record)
    if failures:
        _alert_once(ctx, "ingest_falhou", f"⚠️ {failures} mensagem(ns) deram erro ao entrar no sistema neste ciclo. "
                                          "As que não foram gravadas serão lidas de novo; confira se algum lead ficou sem resposta.")
    return fresh


class EcoIndeterminado(Exception):
    """Não foi possível dizer se um eco fromMe é do agente (leitura da outbox falhou). Nem takeover nem gravação."""


def _pending_effects(ctx: Ctx) -> dict:
    return ctx.state.setdefault("pending_inbound_effects", {})


MIDIA_NAO_OUVIDA_KEY = jobs.MIDIA_NAO_OUVIDA_KEY   # qualification: carimbo do áudio/imagem que o agente não ouviu


def _apply_media_marker(ctx: Ctx, lead: dict, moment, heard) -> None:
    """Marcador `_midia_nao_ouvida` (merge de UMA chave, relendo o lead): áudio/imagem sem transcrição o põe; texto
    (ou mídia transcrita) MAIS NOVO que o carimbo o tira. heard=None: mensagem sem efeito sobre o marcador.
    Monotônico: reler uma mensagem antiga nunca recua nem apaga um marcador mais novo."""
    if heard is None:
        return
    current = ctx.db.get_lead(lead["id"]) or lead
    qualification = current.get("qualification") if isinstance(current.get("qualification"), dict) else {}
    stamp = parse_ts(qualification.get(MIDIA_NAO_OUVIDA_KEY))
    if heard:
        if MIDIA_NAO_OUVIDA_KEY in qualification and (not stamp or moment > stamp):
            jobs._set_qualification_key(ctx, current, MIDIA_NAO_OUVIDA_KEY, jobs._REMOVE)
        return
    if stamp and stamp >= moment:
        return
    jobs._set_qualification_key(ctx, current, MIDIA_NAO_OUVIDA_KEY, iso(moment))


def _alert_midia(ctx: Ctx, lead: dict, phone: str) -> None:
    who = _who(lead, phone)
    _alert_once(ctx, f"midia_nao_ouvida:{lead.get('id')}",
                f"⚠️ {who} ({phone}) mandou um áudio ou uma imagem que não consegui ouvir ou ver. Confira no "
                "WhatsApp se ele pediu algo (inclusive para parar). Até ele escrever, não mando follow-up nem retomada.")


def _apply_inbound_effects(ctx: Ctx, lead: dict, moment, body: str, *, always: bool, heard=None) -> None:
    """Efeitos de uma mensagem do lead. Monotônico: last_inbound_at só avança e a cadência (followup_step) só
    zera quando a mensagem é mais nova que a última registrada; reler uma mensagem antiga nunca recua o instante."""
    last_in = parse_ts(lead.get("last_inbound_at"))
    if not last_in or moment > last_in:
        ctx.db.update_lead(lead["id"], {"last_inbound_at": iso(moment), "followup_step": 0})
    _mark_opt_out(ctx, lead, body)
    _apply_media_marker(ctx, lead, moment, heard)
    if heard is False:
        _alert_midia(ctx, lead, lead.get("phone") or "")


def _reapply_inbound_effects(ctx: Ctx, wa_id: str) -> None:
    """Reaplica last_inbound_at/followup_step/opt-out de uma mensagem já gravada (a 1ª aplicação falhou)."""
    entry = _pending_effects(ctx).get(wa_id)
    if not entry:
        return
    lead = ctx.db.get_lead(entry["lead_id"])
    if lead:
        _apply_inbound_effects(ctx, lead, entry["moment"], entry["body"], always=False, heard=entry.get("heard"))
        if entry.get("stale"):
            _alert_stale(ctx, lead, entry["phone"])
    _pending_effects(ctx).pop(wa_id, None)


def _transient_error(exc: BaseException) -> bool:
    """Falha que passa sozinha (rede, tempo esgotado, 5xx, 408, 429): nunca conta para a falha permanente.
    Erro de dado (4xx, validação, parse) conta."""
    if isinstance(exc, NetworkError):
        return True
    status = getattr(exc, "status", None)
    if isinstance(exc, (HttpError, EvolutionError)):
        return status is None or status >= 500 or status in (408, 429)
    return False


def _within_catchup(ctx: Ctx, record: dict) -> bool:
    stamp = record_timestamp(record)
    if not stamp:
        return True
    moment = datetime.fromtimestamp(stamp, tz=timezone.utc)
    return ctx.now() - moment <= timedelta(minutes=ctx.cfg.catchup_minutes)


FAILED_MAX_CICLOS_AUSENTE = 30   # ciclos seguidos sem a Evolution devolver o wa_id antes de desistir dele


def _fail_permanently(ctx: Ctx, record: dict, wa_id: str, tries: int) -> None:
    """Depois de MAX_WA_ATTEMPTS falhas a mensagem sai da releitura forçada (que empurraria a paginação até o teto
    todo ciclo) e o dono recebe UM aviso específico, sem passar pelo alerta de excesso de mensagens."""
    phone = record_phone(record.get("key") or {}) or "um contato"
    ctx.log.error("mensagem %s de %s falhou %d vezes: falha permanente, não será relida", wa_id, phone, tries)
    _give_up(ctx, wa_id, phone, tries)


def _give_up(ctx: Ctx, wa_id: str, phone: str, tries: int) -> None:
    """Tira o wa_id da releitura forçada e agenda o aviso ao dono. O aviso fica PENDENTE até ser enfileirado: se o
    banco falhar agora, o próximo ciclo tenta de novo (a mensagem já não volta ao fluxo, então não há outra chance)."""
    ctx.state.setdefault("failed_wa_ids", set()).discard(wa_id)
    ctx.state.setdefault("failed_wa_info", {}).pop(wa_id, None)
    ctx.state.setdefault("failed_wa_permanent", set()).add(wa_id)
    _seen_ids(ctx).add(wa_id)
    detail = f"falhou {tries} vezes" if tries else "não consegui gravá-la a tempo"
    ctx.state.setdefault("failed_wa_alert_pending", {})[wa_id] = (
        f"⚠️ Uma mensagem de {phone} não pôde ser lida pelo sistema ({detail}) e "
        "não será tentada de novo. Confira a conversa no WhatsApp e responda se for preciso.")
    _flush_failed_alerts(ctx)


def _flush_failed_alerts(ctx: Ctx) -> None:
    pending = ctx.state.get("failed_wa_alert_pending") or {}
    for wa_id, text in list(pending.items()):
        try:
            queue_owner_alert(ctx, text)   # False = sem número do dono configurado: já está no log, não insiste
            pending.pop(wa_id, None)
        except Exception as exc:  # noqa: BLE001 - a chave fica livre: o próximo ciclo tenta de novo
            ctx.log.error("alerta de mensagem ilegível não enfileirado (tento no próximo ciclo): %s", exc)


def _note_failed(ctx: Ctx, wa_id: str, record: dict) -> None:
    """Guarda o instante da 1ª falha e o telefone (para o aviso se o wa_id nunca mais aparecer)."""
    info = ctx.state.setdefault("failed_wa_info", {})
    info.setdefault(wa_id, {"since": ctx.now(), "phone": record_phone(record.get("key") or {}) or "um contato",
                            "ausente": 0})


def _expire_failed(ctx: Ctx, failed: set, waiting: set | None = None) -> None:
    """wa_id em `failed` que passou da janela de catch-up (desde a 1ª falha) ou ficou
    FAILED_MAX_CICLOS_AUSENTE ciclos sem reaparecer: desiste (log + um aviso ao dono)."""
    info = ctx.state.setdefault("failed_wa_info", {})
    window = timedelta(minutes=ctx.cfg.catchup_minutes)
    attempts = ctx.state.setdefault("failed_wa_attempts", {})
    now = ctx.now()
    for wa_id in list(failed):
        entry = info.setdefault(wa_id, {"since": now, "phone": "um contato", "ausente": 0})
        if waiting is not None and wa_id in waiting:
            entry["ausente"] += 1
        elif waiting is not None:
            entry["ausente"] = 0
        if now - entry["since"] > window or entry["ausente"] >= FAILED_MAX_CICLOS_AUSENTE:
            ctx.log.error("mensagem %s de %s segue sem gravar e saiu da releitura (janela/ciclos esgotados)",
                          wa_id, entry["phone"])
            _give_up(ctx, wa_id, entry["phone"], attempts.get(wa_id, 0))
    for wa_id in list(info):
        if wa_id not in failed:
            info.pop(wa_id, None)   # já saiu da releitura (gravada, perdida na paginação ou desistida)


class IngestaoIndisponivel(Exception):
    """A lista de closers/dono nunca foi carregada: sem ela a leitura pré-envio não pode separar lead de closer.
    Quem vai ENVIAR a um lead não deve seguir (fail-closed); o turno fica para o próximo ciclo."""


def lead_sends_blocked(ctx: Ctx) -> bool:
    """True enquanto a lista de ignorados não existe (envio ao lead fica bloqueado; interno ao dono/closer não)."""
    return ctx.state.get("ignored", set()) is None


def ingest_chat(ctx: Ctx, phone: str) -> set:
    """Leitura DIRECIONADA do chat de um lead (últimas mensagens do remoteJid): ingere o que ainda não está no banco
    (inclui pedido de saída e takeover). Chamada logo antes de cada envio ao lead, porque a IA pode ter demorado."""
    agent = ctx.state.get("agent") or {}
    ignored = ctx.state.get("ignored", set())
    if ignored is None:
        raise IngestaoIndisponivel("lista de closers ainda não carregada: leitura pré-envio impossível")
    records = list(ctx.evo.chat_messages(phone, CHAT_READ_LIMIT) or [])
    lid = (ctx.state.get("lid_by_phone") or {}).get(phone)
    if lid:
        try:
            extra = ctx.evo.chat_messages(phone, CHAT_READ_LIMIT, jid=lid) or []
        except Exception as exc:  # noqa: BLE001 - a leitura pelo número já valeu; o @lid é reforço
            ctx.log.warning("leitura direcionada pelo @lid de %s falhou (sigo só pelo número): %s", phone, exc)
            extra = []
        have = {(r.get("key") or {}).get("id") for r in records}
        added = [r for r in extra if (r.get("key") or {}).get("id") not in have]
        if added:
            ctx.log.info("leitura direcionada: %d mensagem(ns) a mais pelo @lid de %s", len(added), phone)
            records = sorted(records + added, key=lambda r: record_timestamp(r) or 0, reverse=True)
    known = _known_ids(ctx, records)
    return _process_records(ctx, records, known, agent, ignored)


def _warn_unresolved_lid(ctx: Ctx, record: dict, key: dict, moment, now, window, test_only: bool) -> None:
    """Contato @lid cujo número o WhatsApp não informou: aviso no log e UM alerta ao dono por LID. Grupos e status
    não chegam aqui (só @lid). Sem conteúdo, mensagem antiga, enviada pelo dono ou modo teste: só o log."""
    jid = key.get("remoteJid") or ""
    ctx.log.warning("mensagem de um contato @lid sem número resolvível (%s): não vira lead", jid)
    if test_only or key.get("fromMe") or now - moment > window or not (record_text(record) or record_media(record)):
        return
    done = ctx.state.setdefault("lid_alerted", set())
    if jid in done:
        return
    try:
        if queue_owner_alert(ctx, "⚠️ Chegou uma mensagem de um contato cujo número o WhatsApp não informou (aparece "
                                  "como identificador oculto). O agente não consegue responder nem criar o lead. Abra o "
                                  "WhatsApp do aparelho, veja a conversa mais recente e responda por lá, ou peça para a "
                                  "pessoa escrever de novo."):
            done.add(jid)   # só depois de enfileirar
    except Exception as exc:  # noqa: BLE001
        ctx.log.error("alerta de contato @lid não enfileirado: %s", exc)


def _ingest_record(ctx: Ctx, record: dict, agent: dict, ignored: set, stored: list):
    now = ctx.now()
    window = timedelta(minutes=ctx.cfg.catchup_minutes)
    test_only = agent.get("test_only") is not False   # fail-closed: NULL/ausente = modo teste
    test = phone_set(agent.get("test_phones"))
    sent_ids = getattr(ctx.messenger, "sent_ids", set())
    key = record.get("key") or {}
    wa_id = key.get("id")
    phone = record_phone(key)
    stamp = record_timestamp_or_none(record)   # None = sem horário confiável: nunca vira "agora" para decidir
    moment = datetime.fromtimestamp(stamp, tz=timezone.utc) if stamp else now
    if phone and (key.get("remoteJid") or "").endswith("@lid"):
        # contato @lid com número conhecido: guarda o @lid (memória do processo) para a leitura direcionada olhar
        # também por ele; sem isso um "pare" escrito durante a chamada da IA ficaria invisível
        ctx.state.setdefault("lid_by_phone", {})[phone] = key.get("remoteJid")
    if not phone and (key.get("remoteJid") or "").endswith("@lid"):
        _warn_unresolved_lid(ctx, record, key, moment, now, window, test_only)
        return None   # sem número não há lead: nada é gravado
    if not phone or phone in ignored:
        return None  # grupo, status ou dono/closer
    stale = now - moment > window
    if test_only and phone not in test:
        return None  # modo teste: nem cria lead para quem não é número de teste
    text, kind = record_text(record), record_media(record)
    unsupported = False
    if not text and not kind:
        if is_silent_record(record) or not record.get("message") or stale:
            return None  # reação, protocolo etc. (ou mensagem antiga sem conteúdo legível)
        if not key.get("fromMe"):
            unsupported = True   # conteúdo de lead que o agente não entende: grava, não responde às cegas e avisa o dono
    if stamp is None:
        return _ingest_untimed(ctx, record, key, phone, text, kind, unsupported)
    if key.get("fromMe"):
        lead = ctx.db.get_lead_by_phone(phone)
        if not lead:
            return None
        out_body = text or (f"[{kind}]" if kind else OWNER_NO_TEXT_BODY)   # contato/localização/enquete: placeholder
        if wa_id in sent_ids:
            # enviada por este motor, mas a gravação falhou na hora: regrava e NÃO conta como o dono no celular
            ctx.db.add_message(lead["id"], "out", "agente", out_body, kind, wa_id, moment)
            return None
        if _is_own_outbox_echo(ctx, wa_id):   # levanta EcoIndeterminado se a outbox não respondeu
            # eco de um envio da outbox deste motor (id conhecido pela outbox, ex.: reinício antes de gravar a
            # mensagem): é do agente, não do dono no celular
            ctx.db.add_message(lead["id"], "out", "agente", out_body, kind, wa_id, moment)
            return None
        if text and _match_ambiguous_send(ctx, lead, phone, text, wa_id, moment, now):
            # eco de um envio do próprio motor cujo resultado ficou incerto: não é o dono, não ativa takeover
            ctx.db.add_message(lead["id"], "out", "agente", text, kind, wa_id, moment)
            ctx.db.update_lead(lead["id"], {"last_outbound_at": iso(moment)})   # o turno foi respondido
            return None
        # mensagem enviada por fora do motor = o dono assumiu pelo celular
        # takeover ANTES de gravar a mensagem: se o update falhar nada é gravado e o wa_id volta no próximo poll
        # (failed_wa_ids); gravar antes deixaria o wa_id "conhecido" e o takeover nunca seria ativado
        if stale and _has_later_reply(ctx, lead, moment):
            # mensagem antiga do dono, e o agente/dono já falou depois dela: só entra no histórico (sem takeover)
            ctx.db.add_message(lead["id"], "out", "dono", out_body, kind, wa_id, moment)
            return None
        created_lead = parse_ts(lead.get("created_at"))
        if stale and created_lead and created_lead > moment:
            # eco relido (ex.: reinício) de uma mensagem ANTERIOR à existência do lead: não é o dono assumindo a
            # conversa dele. Só entra no histórico, sem takeover e sem alerta
            ctx.db.add_message(lead["id"], "out", "dono", out_body, kind, wa_id, moment)
            return None
        previous = parse_ts(lead.get("takeover_until"))
        ctx.db.update_lead(lead["id"], {"takeover_until": iso(now + timedelta(hours=TAKEOVER_HOURS)),
                                        "last_outbound_at": iso(moment)})
        ctx.db.add_message(lead["id"], "out", "dono", out_body, kind, wa_id, moment)
        ctx.log.info("dono respondeu %s pelo celular: agente em silêncio por %dh", phone, TAKEOVER_HOURS)
        if not (previous and previous > now):   # nova ativação (não é prorrogação): avisa o dono uma vez
            _alert_takeover(ctx, lead, phone)
        return None
    heard = None if unsupported else True   # texto do lead: ouvido (e tira o marcador de mídia não ouvida)
    if unsupported:
        body = UNSUPPORTED_BODY
    elif not kind:
        body = text
    else:
        body, heard = _media_body_heard(ctx, record, kind, text)
    lead = ctx.db.get_lead_by_phone(phone) or ctx.db.create_lead(phone, name=(clean_lead_name(record.get("pushName")) or None))
    if not lead:
        return None
    if stale:
        # fora da janela de catch-up: grava (nada se perde) mas NÃO responde, e avisa o dono uma vez por lead
        if ctx.db.add_message(lead["id"], "in", "lead", body, kind, wa_id, moment):
            # o lead escreveu DEPOIS da nossa última saída: a cadência não pode cobrar quem acabou de falar
            _pending_effects(ctx)[wa_id] = {"lead_id": lead["id"], "moment": moment, "body": body, "stale": True,
                                            "phone": phone, "heard": heard}
            _apply_inbound_effects(ctx, lead, moment, body, always=False, heard=heard)
            _pending_effects(ctx).pop(wa_id, None)
            _alert_stale(ctx, lead, phone)
        return None
    # entrega atrasada: carimbo do WhatsApp anterior à última saída do agente => grava com o instante da leitura, para
    # a mensagem ficar DEPOIS da resposta (senão o turno vira "já respondido" e a pergunta nunca é respondida)
    created = now if _delivered_late(ctx, lead, moment) else moment
    if created is not moment:
        ctx.log.info("mensagem %s de %s chegou com atraso (carimbo anterior à última resposta do agente): "
                     "gravada com o horário da leitura", wa_id, phone)
    if ctx.db.add_message(lead["id"], "in", "lead", body, kind, wa_id, created):
        stored.append(lead["id"])
        _pending_effects(ctx)[wa_id] = {"lead_id": lead["id"], "moment": moment, "body": body, "stale": False,
                                        "phone": phone, "heard": heard}
        _apply_inbound_effects(ctx, lead, moment, body, always=True, heard=heard)
        _pending_effects(ctx).pop(wa_id, None)
        if unsupported:
            _alert_unsupported(ctx, lead, phone)
        return lead["id"]
    return None


def _last_agent_out_at(ctx: Ctx, lead: dict):
    """Instante da última saída (agente ou dono) conhecida do lead: o maior entre last_outbound_at e a última
    mensagem 'out' gravada. Sem nada legível, None."""
    best = parse_ts(lead.get("last_outbound_at"))
    try:
        for m in ctx.db.recent_messages(lead["id"], 50):
            if m.get("direction") != "out":
                continue
            created = m.get("created_at")
            created = parse_ts(created) if isinstance(created, str) else created
            if created and (best is None or created > best):
                best = created
    except Exception as exc:  # noqa: BLE001 - sem o histórico vale só o last_outbound_at
        ctx.log.warning("não consegui ler o histórico de %s para checar entrega atrasada: %s", lead.get("phone"), exc)
    return best


def _delivered_late(ctx: Ctx, lead: dict, moment) -> bool:
    last_out = _last_agent_out_at(ctx, lead)
    return bool(last_out and moment < last_out)


def _ingest_untimed(ctx: Ctx, record: dict, key: dict, phone: str, text: str, kind, unsupported: bool):
    """Mensagem de conversa individual cujo messageTimestamp sumiu ou não dá para ler. Não há como saber se é nova:
    nunca dispara turno nem vale como inbound novo (last_inbound_at/cadência intactos). Mensagem do lead entra no
    histórico (nada se perde) e só o pedido de saída é honrado, porque é a direção segura. Log e alerta 1x por dia."""
    wa_id = key.get("id")
    ctx.log.warning("mensagem %s de %s sem horário confiável (messageTimestamp ilegível): sem resposta automática",
                    wa_id, phone)
    _alert_once(ctx, "timestamp_ilegivel",
                "⚠️ O WhatsApp entregou mensagem(ns) sem horário legível. Por segurança o agente não respondeu "
                "a elas (poderiam ser antigas). Abra o WhatsApp, confira as conversas recentes e responda se for "
                "preciso.")
    if key.get("fromMe"):
        # sem horário não dá para saber se é eco recente: passa pela MESMA triagem de eco do caminho com horário
        # (sent_ids, outbox, envio incerto) ANTES de qualquer takeover. Sem casamento NÃO ativa takeover (o eco do
        # próprio agente nunca pode pausar a IA por 24h): avisa o dono 1x por lead por dia e o agente segue ativo
        lead = ctx.db.get_lead_by_phone(phone)
        if not lead:
            return None
        now = ctx.now()
        out_body = text or (f"[{kind}]" if kind else OWNER_NO_TEXT_BODY)
        if wa_id in getattr(ctx.messenger, "sent_ids", set()) or _is_own_outbox_echo(ctx, wa_id):
            ctx.db.add_message(lead["id"], "out", "agente", out_body, kind, wa_id, now)
            return None
        if text and _match_ambiguous_send(ctx, lead, phone, text, wa_id, now, now):
            ctx.db.add_message(lead["id"], "out", "agente", text, kind, wa_id, now)
            return None
        ctx.log.warning("fromMe %s em %s sem horário legível e sem casar com envio do agente: sem takeover", wa_id, phone)
        _alert_once(ctx, f"fromme_sem_horario:{lead.get('id')}",
                    f"⚠️ Chegou uma mensagem enviada pelo número do negócio para {_who(lead, phone)} ({phone}) sem "
                    f"horário legível: «{_snippet(text or out_body)}». Não sei se foi você; o agente seguiu ativo "
                    "nessa conversa. Se foi você quem assumiu, ative a pausa da IA no painel.")
        return None
    lead = ctx.db.get_lead_by_phone(phone) or ctx.db.create_lead(phone, name=(clean_lead_name(record.get("pushName")) or None))
    if not lead:
        return None
    body = UNSUPPORTED_BODY if unsupported else (text or (f"[{kind}]" if kind else UNSUPPORTED_BODY))
    if ctx.db.add_message(lead["id"], "in", "lead", body, kind, wa_id, ctx.now()):
        _mark_opt_out(ctx, lead, body)
        if kind in ("audio", "image") and not text:
            _apply_media_marker(ctx, lead, ctx.now(), False)   # sem horário e sem transcrição: direção segura
            _alert_midia(ctx, lead, phone)
    return None


def _has_later_reply(ctx: Ctx, lead: dict, moment: datetime) -> bool:
    """True quando já existe mensagem do agente/dono gravada DEPOIS de `moment` (a conversa seguiu sem este eco)."""
    for m in ctx.db.recent_messages(lead["id"], 50):
        if m.get("direction") != "out" or m.get("author") not in ("agente", "dono"):
            continue
        created = m.get("created_at")
        created = parse_ts(created) if isinstance(created, str) else created
        if created and created > moment:
            return True
    return False


UNSUPPORTED_BODY = "[mensagem não suportada pelo agente]"
OWNER_NO_TEXT_BODY = "[mensagem do dono sem texto]"


def _alert_unsupported(ctx: Ctx, lead: dict, phone: str) -> None:
    """Conteúdo do lead que o agente não lê (tipo desconhecido): UM aviso ao dono por lead, no processo."""
    done = ctx.state.setdefault("unsupported_alerted", set())
    if lead.get("id") in done:
        return
    who = _who(lead, phone)
    try:
        if queue_owner_alert(ctx, f"⚠️ {who} ({phone}) mandou um tipo de mensagem que o agente não consegue ler. Abra a "
                                  "conversa no WhatsApp e veja o que foi enviado; responda por lá se for preciso."):
            done.add(lead.get("id"))   # só depois de enfileirar
    except Exception as exc:  # noqa: BLE001
        ctx.log.error("alerta de mensagem não suportada não enfileirado: %s", exc)


def _is_own_outbox_echo(ctx: Ctx, wa_id) -> bool:
    """O key.id do eco fromMe consta na outbox (wa_key_id, qualquer status)? Falha de leitura = "não sei":
    levanta EcoIndeterminado (sem takeover, sem gravar como dono; o wa_id volta no próximo poll)."""
    if not wa_id:
        return False
    try:
        return bool(ctx.db.outbox_by_wa_key(wa_id))
    except Exception as exc:  # noqa: BLE001
        raise EcoIndeterminado(f"outbox ilegível ao conferir o id {wa_id}: {exc}") from exc


AMBIGUOUS_WINDOW = timedelta(minutes=15)
UNCERTAIN_OUTBOX_ERRORS = ("timeout_incerto", "sem_key_id", "resposta_sem_key_id", "interrompido",
                           SENT_UNRECORDED_ERROR)
# erro definitivo (4xx permanente / tentativas esgotadas): o turno já foi tentado, a IA não roda de novo
ANSWERED_OUTBOX_ERRORS = UNCERTAIN_OUTBOX_ERRORS + (FINAL_FAILURE_PREFIX,)


def _match_ambiguous_send(ctx: Ctx, lead: dict, phone: str, text: str, wa_id, moment, now) -> bool:
    """O eco fromMe de id desconhecido é de um envio incerto do próprio motor (15 min ao redor do INSTANTE DO ECO,
    mesmo telefone, mesmo texto normalizado)? Procura (a) na memória do processo e (b) na outbox. Casou: consome o registro
    em memória e preenche o wa_key_id da linha da outbox."""
    wanted = normalize_text(text)
    matched = False
    pool = getattr(ctx.messenger, "uncertain_sends", None)
    for item in list(pool or []):
        if item.get("phone") == phone and item.get("text") == wanted and abs(moment - item["at"]) <= AMBIGUOUS_WINDOW:
            pool.remove(item)
            matched = True
            break
    try:
        rows = ctx.db.outbox_for_phone_since(phone, moment - AMBIGUOUS_WINDOW)
    except Exception as exc:  # noqa: BLE001
        if matched:
            ctx.log.warning("não consegui ler a outbox para casar o eco de %s: %s", phone, exc)
            return True   # a memória já casou
        raise EcoIndeterminado(f"outbox ilegível ao casar o eco de {phone}: {exc}") from exc
    for row in rows:
        created = parse_ts(row.get("created_at"))
        if created and created > moment + AMBIGUOUS_WINDOW:
            continue   # janela simétrica ao redor do eco
        key = row.get("wa_key_id")
        if normalize_text(row.get("body")) != wanted or (key and key != wa_id):
            continue   # linha já enviada com OUTRO key.id é outra mensagem; a do mesmo id é este eco
        if key == wa_id:
            return True
        status, error = row.get("status"), str(row.get("last_error") or "")
        uncertain = (status == "erro" and error.startswith(UNCERTAIN_OUTBOX_ERRORS)) or status == "enviado"
        if not uncertain:
            continue
        try:
            ctx.db.finish_outbox(row["id"], status="enviado", wa_key_id=wa_id, sent_at=moment)
        except Exception as exc:  # noqa: BLE001 - a mensagem é do agente de qualquer forma
            ctx.log.error("não consegui preencher o wa_key_id da outbox %s: %s", row.get("id"), exc)
        return True
    return matched


def _alert_takeover(ctx: Ctx, lead: dict, phone: str) -> None:
    try:
        queue_owner_alert(ctx, f"ℹ️ Você respondeu {_who(lead, phone)} ({phone}) pelo celular: a IA ficou "
                               f"pausada nesse lead por {TAKEOVER_HOURS}h. Para retomar antes, desligue a pausa da "
                               "IA na conversa desse lead no painel.")
    except Exception as exc:  # noqa: BLE001
        ctx.log.error("alerta de pausa da IA não enfileirado: %s", exc)


def _mark_opt_out(ctx: Ctx, lead: dict, body: str) -> None:
    """Pedido de saída vale já na ingestão (inclusive catch-up/stale), antes de qualquer rotina: a cadência,
    os lembretes e a retomada nunca enviam ao lead que pediu para parar. Só o flag: estágio e resposta
    ficam com o turno."""
    if not is_opt_out(body) or lead.get("opt_out"):
        return
    ctx.db.update_lead(lead["id"], {"opt_out": True, "followup_step": 0})
    warn_opt_out_with_appointment(ctx, lead)


def _alert_stale(ctx: Ctx, lead: dict, phone: str) -> None:
    alerted = ctx.state.setdefault("stale_alerted", set())
    ctx.log.warning("mensagem de %s fora da janela de %d min: gravada, sem resposta automática", phone,
                    ctx.cfg.catchup_minutes)
    if lead["id"] in alerted:
        return
    try:
        if queue_owner_alert(ctx, f"⚠️ {_who(lead, phone)} ({phone}) escreveu há mais de "
                                  f"{ctx.cfg.catchup_minutes} minutos, com o agente parado. A mensagem foi gravada, "
                                  "mas não respondi sozinho: responda você ou peça para o agente retomar."):
            alerted.add(lead["id"])   # só depois de enfileirar: se falhar, a próxima mensagem tenta de novo
    except Exception as exc:  # noqa: BLE001
        ctx.log.error("alerta de mensagem antiga não enviado: %s", exc)


def recover_unanswered(ctx: Ctx) -> set:
    """Catch-up na partida: leads cuja última mensagem é do lead e ficou sem resposta.

    Lead que escreveu com a IA pausada (`qualification._inbound_em_pausa`) entra independentemente da janela de
    catch-up, depois que a pausa acaba (respeitando opt-out, takeover e estágio terminal: active_leads já os exclui)."""
    now = ctx.now()
    cutoff = now - timedelta(minutes=ctx.cfg.catchup_minutes)
    agent = ctx.state.get("agent") or {}
    paused = bool(agent.get("paused"))
    test_only = agent.get("test_only") is not False
    test = phone_set(agent.get("test_phones"))
    unrecorded = getattr(ctx.messenger, "unrecorded_turns", None) or {}
    pending = set()
    for lead in ctx.db.active_leads():
        if lead.get("stage") in TERMINAL_STAGES:
            continue   # defesa extra: nunca depende só do filtro do banco
        if test_only and not _is_test_lead(lead, test):
            continue   # modo teste: lead real fica isolado (nada de IA, estágio ou qualificação)
        last_in, last_out = parse_ts(lead.get("last_inbound_at")), parse_ts(lead.get("last_outbound_at"))
        unanswered = bool(last_in and (not last_out or last_out < last_in))
        qualification = lead.get("qualification") if isinstance(lead.get("qualification"), dict) else {}
        marked = bool(qualification.get(PAUSE_INBOUND_KEY))
        if marked and not unanswered:
            _clear_pause_mark(ctx, lead)   # alguém respondeu nesse meio tempo
            continue
        if not unanswered:
            continue
        if marked:
            takeover = parse_ts(lead.get("takeover_until"))
            if paused or (takeover and takeover > now):
                continue
        elif last_in < cutoff:
            continue
        if _answered_in_outbox(ctx, lead["id"], last_in) or _answered_in_messages(ctx, lead["id"]):
            continue
        if lead["id"] in unrecorded and unrecorded[lead["id"]] >= last_in:
            continue   # resposta enviada cuja gravação falhou (nem o marcador gravou): só a memória sabe
        pending.add(lead["id"])
    return pending


def _is_test_lead(lead: dict, test: set) -> bool:
    phone = to_e164(lead.get("phone")) or lead.get("phone")
    return phone in test


def _flush_unrecorded(ctx: Ctx) -> None:
    """Respostas diretas que saíram mas não foram gravadas: retenta o marcador durável e avisa o dono UMA vez."""
    entries = getattr(ctx.messenger, "unrecorded_sends", None)
    if not entries:
        return
    for entry in list(entries):
        if not entry.get("marker"):
            ctx.messenger.write_unrecorded_marker(entry)
        if entry.get("alerted"):
            continue
        phone = entry.get("phone")
        try:
            if queue_owner_alert(ctx, f"⚠️ Respondi {phone}, mas o banco falhou na hora de gravar a mensagem. Ela "
                                      "chegou ao lead; não vou responder de novo. Confira a conversa no WhatsApp e "
                                      "o painel."):
                entry["alerted"] = True
        except Exception as exc:  # noqa: BLE001
            ctx.log.error("aviso de resposta não gravada não enfileirado: %s", exc)
    entries[:] = [e for e in entries if not (e.get("marker") and e.get("alerted"))]


def _answered_in_messages(ctx: Ctx, lead_id: str) -> bool:
    """A última mensagem gravada é uma saída do agente: o turno foi respondido mesmo com last_outbound_at
    desatualizado (o PATCH do lead pode ter falhado depois do envio). Falha de leitura = não reprocessa."""
    try:
        return last_message_is_agent_reply(ctx.db.recent_messages(lead_id, 5))
    except Exception as exc:  # noqa: BLE001
        ctx.log.warning("não consegui ler a conversa do lead %s: %s", lead_id, exc)
        return True


def _answered_in_outbox(ctx: Ctx, lead_id: str, since) -> bool:
    """Já existe resposta na outbox (pendente, enviando, enviada ou com timeout incerto) criada depois da mensagem
    do lead: reprocessar o turno chamaria a IA de novo e enfileiraria OUTRA resposta (Evolution fora = rajada).
    Falha de leitura = não reprocessa (melhor esperar o próximo ciclo do que duplicar)."""
    try:
        rows = ctx.db.outbox_for_lead_since(lead_id, since)
    except Exception as exc:  # noqa: BLE001
        ctx.log.warning("não consegui ler a outbox do lead %s: %s", lead_id, exc)
        return True
    for row in rows:
        status, error = row.get("status"), str(row.get("last_error") or "")
        if status in ("pendente", "enviando", "enviado"):
            return True
        if status == "erro" and error.startswith(ANSWERED_OUTBOX_ERRORS):
            return True
    return False


def startup(ctx: Ctx) -> None:
    refresh(ctx)
    recover_stuck_outbox(ctx)
    try:
        own = ctx.evo.owner_jid()
        ctx.state["own_phone"] = to_e164(own.split("@")[0]) if own else ""
    except Exception:  # noqa: BLE001
        ctx.state["own_phone"] = ""
    ctx.state["pending"] = recover_unanswered(ctx)
    ctx.state["last_recover"] = ctx.now()
    refresh(ctx)


def _periodic_recover(ctx: Ctx) -> set:
    """Reconfere de tempos em tempos quem ficou sem resposta (falha parcial de um ciclo anterior)."""
    now = ctx.now()
    last = ctx.state.get("last_recover")
    if last is not None and (now - last).total_seconds() < RECOVER_EVERY_SECONDS:
        return set()
    ctx.state["last_recover"] = now
    try:
        return recover_unanswered(ctx)
    except Exception as exc:  # noqa: BLE001
        ctx.log.warning("não consegui reconferir leads sem resposta: %s", exc)
        return set()


def cycle(ctx: Ctx) -> dict:
    """Um ciclo de polling: ingere, espera a janela de agrupamento, responde e envia a outbox."""
    try:
        whatsapp.tick(ctx, ctx.now())   # reconexão pedida pelo painel; nunca derruba o ciclo
    except Exception as exc:  # noqa: BLE001
        ctx.log.warning("conexão do WhatsApp: erro no passo do motor (%s)", type(exc).__name__)
    refresh(ctx)
    _flush_unrecorded(ctx)
    summary = {"respostas": 0, "outbox": 0}
    pending = set(ctx.state.pop("pending", set()))
    pending |= _periodic_recover(ctx)
    try:
        new = ingest(ctx)
        if new and ctx.cfg.batch_window_seconds > 0:
            ctx.sleep_fn(ctx.cfg.batch_window_seconds)   # agrupa mensagens em sequência do mesmo lead
            new |= ingest(ctx)
        pending |= new
    except EvolutionFormatoDesconhecido as exc:
        ctx.log.error("Evolution indisponível neste ciclo: %s", exc)
        _alert_once(ctx, "evolution_formato", f"⚠️ O motor não consegue entender a resposta da Evolution ({str(exc)[:160]}). "
                                              "Pode ter mudado a versão da Evolution; o agente não está lendo mensagens novas.")
    except EvolutionError as exc:
        ctx.log.error("Evolution indisponível neste ciclo: %s", exc)
    except Exception as exc:  # noqa: BLE001 - ex.: Supabase fora ao conferir wa_id: o resto do ciclo segue
        ctx.log.exception("erro na leitura de mensagens neste ciclo: %s", exc)
        _alert_once(ctx, "ingest_ciclo", f"⚠️ O motor não conseguiu ler as mensagens neste ciclo ({str(exc)[:120]}). "
                                         "Tentarei de novo no próximo.")
    retry = ctx.state.setdefault("retry", {})
    if lead_sends_blocked(ctx):
        # fail-closed: sem a lista de closers a releitura pré-envio não funciona, então NADA sai para lead neste
        # ciclo; o turno fica guardado (não é marcado como respondido) e roda quando a lista carregar
        ctx.log.warning("lista de closers não carregada: respostas a leads adiadas para o próximo ciclo (%d)", len(pending))
        ctx.state.setdefault("pending", set()).update(pending)
        pending = set()
    else:
        pending |= set(retry)   # leads cujo turno deu exceção no ciclo anterior
    for lead_id in pending:
        if _blocked_by_test_mode(ctx, lead_id):
            ctx.log.info("modo teste: lead %s fora de test_phones, sem turno da IA", lead_id)
            continue
        if _closer_stage(ctx, lead_id):
            ctx.log.info("lead %s já compareceu: sem resposta da IA (é com o closer)", lead_id)
            _alert_closer_stage(ctx, lead_id)
            continue
        try:
            result = handle_turn(ctx, lead_id)
            retry.pop(lead_id, None)
            summary["respostas"] += 1 if result.get("status") in ("ok", "ia_falhou", "opt_out_confirmado") else 0
        except Exception as exc:  # noqa: BLE001 - um lead com problema não trava os outros
            ctx.log.exception("erro ao responder o lead %s: %s", lead_id, exc)
            _register_turn_failure(ctx, retry, lead_id, exc)
    try:
        summary["outbox"] = process_outbox(ctx)
    except Exception as exc:  # noqa: BLE001
        ctx.log.exception("erro ao processar a outbox: %s", exc)
    return summary


def _blocked_by_test_mode(ctx: Ctx, lead_id: str) -> bool:
    """Modo teste: lead fora de test_phones não leva turno (nem IA nem escrita de estágio). Falha ao ler o lead com
    o modo teste ligado = bloqueia (fail-closed); ao vivo nunca bloqueia."""
    agent = ctx.state.get("agent") or {}
    if agent.get("test_only") is False:
        return False
    try:
        lead = ctx.db.get_lead(lead_id)
    except Exception:  # noqa: BLE001
        return True
    return bool(lead) and not _is_test_lead(lead, phone_set(agent.get("test_phones")))


def _closer_stage(ctx: Ctx, lead_id: str) -> bool:
    """Lead em estágio entregue ao closer (compareceu): a IA não responde. Falha de leitura = segue o fluxo normal."""
    try:
        lead = ctx.db.get_lead(lead_id)
    except Exception:  # noqa: BLE001
        return False
    return bool(lead and lead.get("stage") in CLOSER_STAGES)


def _alert_closer_stage(ctx: Ctx, lead_id: str) -> None:
    """Lead em 'compareceu' escreveu e a IA não responde: o dono é avisado (1x por lead por dia) com nome, telefone
    e um trecho da mensagem, para a conversa não ficar sem resposta em silêncio."""
    try:
        lead = ctx.db.get_lead(lead_id) or {}
        last = ""
        for m in reversed(ctx.db.recent_messages(lead_id, 20)):
            if m.get("direction") == "in":
                last = m.get("body") or ""
                break
        phone = lead.get("phone") or "?"
        _alert_once(ctx, f"compareceu_escreveu:{lead_id}",
                    f"⚠️ {_who(lead, phone)} ({phone}) já compareceu e escreveu: «{_snippet(last)}». A IA não "
                    "responde nesse estágio (é com o closer): responda você ou o closer.")
    except Exception as exc:  # noqa: BLE001 - o aviso nunca derruba o ciclo
        ctx.log.error("aviso de lead que compareceu não enviado: %s", exc)


def _register_turn_failure(ctx: Ctx, retry: dict, lead_id: str, exc: Exception) -> None:
    attempts = retry.get(lead_id, 0) + 1
    if attempts < MAX_TURN_ATTEMPTS:
        retry[lead_id] = attempts
        return
    retry.pop(lead_id, None)
    try:
        lead = ctx.db.get_lead(lead_id) or {}
        who = f"{_who(lead, lead.get('phone') or str(lead_id))} ({lead.get('phone', '?')})"
        notify_owner(ctx, f"⚠️ Não consegui responder {who} depois de {attempts} tentativas. "
                          f"A conversa aguarda você. Erro: {str(exc)[:150]}")
    except Exception as alert_exc:  # noqa: BLE001
        ctx.log.error("alerta de falha repetida não enviado: %s", alert_exc)


def _try_startup(ctx: Ctx) -> bool:
    """startup que nunca derruba o motor: falha (transitória ou não) é logada e repetida no próximo ciclo. Erro que
    não é transitório também avisa o dono (no máximo 1x por dia)."""
    try:
        startup(ctx)
        return True
    except Exception as exc:  # noqa: BLE001 - o motor não sai por falha de partida
        if _transient_error(exc):
            ctx.log.error("partida do motor falhou (erro passageiro), tento de novo no próximo ciclo: %s", exc)
        else:
            ctx.log.exception("partida do motor falhou, tento de novo no próximo ciclo: %s", exc)
            _alert_once(ctx, "startup_falhou", f"⚠️ O motor não conseguiu terminar a partida ({str(exc)[:120]}). "
                                               "Vou tentar de novo a cada ciclo; o agente não responde até isso passar.")
        return False


def run_forever(ctx: Ctx) -> None:
    ready = _try_startup(ctx)
    if ready:
        ctx.log.info("motor iniciado (instância %s, IA %s/%s)", ctx.cfg.instance, ctx.cfg.ai_provider, ctx.cfg.model)
    last_jobs = 0.0
    while True:
        started = time.monotonic()
        try:
            if not ready:
                ready = _try_startup(ctx)
                if ready:
                    ctx.log.info("motor iniciado (instância %s, IA %s/%s)", ctx.cfg.instance, ctx.cfg.ai_provider,
                                 ctx.cfg.model)
                else:
                    time.sleep(max(1.0, ctx.cfg.poll_seconds - (time.monotonic() - started)))
                    continue
            cycle(ctx)
            if started - last_jobs >= ctx.cfg.jobs_every_seconds:
                last_jobs = started
                _run_jobs(ctx)
        except Exception as exc:  # noqa: BLE001 - o laço nunca morre por um erro de ciclo
            ctx.log.exception("erro no ciclo: %s", exc)
        time.sleep(max(1.0, ctx.cfg.poll_seconds - (time.monotonic() - started)))


def run_once(ctx: Ctx) -> dict:
    startup(ctx)
    result = cycle(ctx)
    result["rotinas"] = _run_jobs(ctx)
    return result


def _run_jobs(ctx: Ctx) -> dict:
    """Ingere de novo ANTES das rotinas (um 'pare' que chegou agora vale já) e roda os jobs."""
    try:
        ctx.state.setdefault("pending", set()).update(ingest(ctx))   # o próximo ciclo responde
    except Exception as exc:  # noqa: BLE001 - sem ingestão as rotinas ainda rodam (cada envio relê o lead)
        ctx.log.warning("ingestão antes das rotinas falhou: %s", exc)
    return jobs.run_all(ctx)


# ---------------------------------------------------------------- simulação (sem Evolution)
class ReadOnlyDB:
    """Banco da simulação: LÊ do banco real e NUNCA escreve. Toda escrita vira um registro em memória
    (`writes`) e uma sobreposição local, para a conversa simulada enxergar o que ela mesma 'gravou'."""

    READS = {"get_agent_config", "get_schedule_settings", "list_closers", "get_closer", "active_leads",
             "available_slots", "known_wa_ids", "list_objections", "upcoming_appointments", "pending_outbox",
             "stuck_outbox", "outbox_for_phone_since", "ping", "get_whatsapp_connection"}

    def __init__(self, real, now_fn=None):
        self._real = real
        self._now = now_fn
        self.writes: list = []
        self._virtual: dict = {}     # leads criados só na simulação
        self._patch: dict = {}       # id -> campos 'gravados' em lead real
        self._appt_patch: dict = {}
        self._messages: list = []
        self._seq = 0

    def _record(self, name: str, *args) -> None:
        self.writes.append((name, args))

    def __getattr__(self, name):
        if name.startswith("_") or name in ("writes",):
            raise AttributeError(name)
        if name in self.READS:
            return getattr(self._real, name)

        def blocked(*args, **kwargs):
            self._record(name, *args, *kwargs.values())
            return {}
        return blocked

    def _merge(self, lead):
        return {**lead, **self._patch.get(lead.get("id"), {})} if lead else None

    def get_lead(self, lead_id):
        if lead_id in self._virtual:
            return dict(self._virtual[lead_id])
        return self._merge(self._real.get_lead(lead_id))

    def get_lead_by_phone(self, phone):
        virtual = next((l for l in self._virtual.values() if l["phone"] == phone), None)
        return dict(virtual) if virtual else self._merge(self._real.get_lead_by_phone(phone))

    def create_lead(self, phone, name=None, source="whatsapp"):
        self._record("create_lead", phone, name, source)
        self._seq += 1
        lead = {"id": f"sim-{self._seq}", "phone": phone, "name": name, "email": None, "source": source,
                "stage": "novo", "profile": None, "qualification": {}, "score": None, "opt_out": False,
                "takeover_until": None, "followup_step": 0, "last_inbound_at": None, "last_outbound_at": None}
        self._virtual[lead["id"]] = lead
        return dict(lead)

    def update_lead(self, lead_id, fields):
        self._record("update_lead", lead_id, fields)
        if lead_id in self._virtual:
            self._virtual[lead_id].update(fields)
        else:
            self._patch.setdefault(lead_id, {}).update(fields)
        return self.get_lead(lead_id) or {}

    def add_message(self, lead_id, direction, author, body, media_type=None, wa_id=None, created_at=None):
        self._record("add_message", lead_id, direction, author, body)
        self._messages.append({"lead_id": lead_id, "direction": direction, "author": author, "body": body,
                               "media_type": media_type, "wa_id": wa_id, "created_at": created_at})
        return True

    def recent_messages(self, lead_id, limit=20):
        real = [] if lead_id in self._virtual else list(self._real.recent_messages(lead_id, limit))
        return (real + [m for m in self._messages if m["lead_id"] == lead_id])[-limit:]

    def add_objection(self, lead_id, category, excerpt):
        self._record("add_objection", lead_id, category, excerpt)

    def active_appointment(self, lead_id, now):
        if lead_id in self._virtual:
            return None
        appt = self._real.active_appointment(lead_id, now)
        if not appt:
            return None
        appt = {**appt, **self._appt_patch.get(appt.get("id"), {})}
        return appt if appt.get("status") in ("agendado", "confirmado") else None

    def update_appointment(self, appointment_id, fields):
        self._record("update_appointment", appointment_id, fields)
        self._appt_patch.setdefault(appointment_id, {}).update(fields)
        return {"id": appointment_id, **fields}

    def confirm_appointment_if_scheduled(self, appointment_id):
        self._record("confirm_appointment_if_scheduled", appointment_id)
        return True

    def book_slot(self, *args, **kwargs):
        self._record("book_slot", *args)
        return {"ok": False, "error": "simulacao"}

    reschedule_slot = book_slot

    def enqueue_outbox(self, phone, body, lead_id=None, created_by="motor", client_key=None):
        self._record("enqueue_outbox", phone, body)
        return {}


class SimMessenger:
    """Substitui o Messenger: não envia nada ao WhatsApp, só imprime e registra a resposta (em memória)."""

    def __init__(self, db, now_fn, out=print):
        self.db, self.now_fn, self.out = db, now_fn, out
        self.last_failure = self.last_error = ""
        self.last_status = None
        self.simulated = True   # book_for_lead não grava agendamento real nem chama o Google Agenda
        self.sent: list = []

    def allowed(self, phone, internal=False):
        return True

    def send(self, phone, text, *, lead_id=None, author="agente", proactive=False, internal=False, on_sent=None):
        self.sent.append((phone, text))
        self.out(f"[{'aviso interno' if internal else 'resposta'} para {phone}]\n{text}\n")
        if lead_id:
            self.db.add_message(lead_id, "out", author, text)
            self.db.update_lead(lead_id, {"last_outbound_at": iso(self.now_fn())})
        return "simulado"


def simulate(ctx: Ctx, phone: str, text: str, out=print) -> dict:
    """Roda UM turno da conversa como se `text` tivesse chegado de `phone`. Não toca a Evolution NEM escreve no
    banco: as escritas que o turno faria aparecem no relatório no fim."""
    number = to_e164(phone)
    if not number:
        raise ValueError("telefone inválido: use o formato internacional, ex.: +5511999990000")
    real_db, real_messenger, real_gcal = ctx.db, ctx.messenger, ctx.gcal
    sim_db = ReadOnlyDB(real_db, ctx.now_fn)
    ctx.db = sim_db
    ctx.messenger = SimMessenger(sim_db, ctx.now_fn, out)
    ctx.gcal = None   # simulação nunca cria nem cancela evento (e convite) de verdade no Google Agenda
    try:
        refresh(ctx)
        lead = sim_db.get_lead_by_phone(number) or sim_db.create_lead(number, name=None, source="simulacao")
        sim_db.add_message(lead["id"], "in", "lead", text)
        sim_db.update_lead(lead["id"], {"last_inbound_at": iso(ctx.now()), "followup_step": 0})
        result = handle_turn(ctx, lead["id"])
    finally:
        ctx.db, ctx.messenger, ctx.gcal = real_db, real_messenger, real_gcal
    out("[simulação] nada foi gravado no banco. Escritas que o turno faria: " +
        (", ".join(name for name, _ in sim_db.writes) or "nenhuma"))
    return {**result, "escritas_simuladas": [(name, args) for name, args in sim_db.writes]}
