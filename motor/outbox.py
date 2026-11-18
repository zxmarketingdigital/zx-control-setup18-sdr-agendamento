"""Envio de mensagens: Messenger (único caminho de saída), fila `outbox` do painel e alertas ao dono."""

from __future__ import annotations

from datetime import timedelta

from .evolution import UNCERTAIN_STATUSES, EvolutionError, EvolutionNoKeyId, EvolutionTimeout
from .util import iso, parse_ts, to_e164

# Tentativas de envio de uma linha da outbox antes de marcá-la como 'erro'.
MAX_ATTEMPTS = 5
# Início do erro de uma linha que falhou de vez (4xx permanente ou tentativas esgotadas).
FINAL_FAILURE_PREFIX = "falha_definitiva"
# Linha 'erro' que marca resposta direta ENVIADA cuja gravação no banco falhou (o turno foi respondido).
SENT_UNRECORDED_ERROR = "enviado; gravação falhou"
# Espera entre tentativas de uma linha (falha transitória): 30s, 60s, 120s, 240s... pela contagem de tentativas.
# A outbox não tem coluna de "próxima tentativa": o horário fica na memória do processo, por id de linha.
BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 3600
# Lote de uma passada da outbox e quantas páginas (de 20 em 20) se busca para juntar linhas fora do backoff.
OUTBOX_BATCH = 20
OUTBOX_MAX_PAGES = 5


def backoff_seconds(attempts: int) -> int:
    return min(BACKOFF_BASE_SECONDS * (2 ** max(int(attempts) - 1, 0)), BACKOFF_MAX_SECONDS)


def _in_backoff(ctx, row: dict) -> bool:
    until = (ctx.state.get("outbox_next_at") or {}).get(row.get("id"))
    return bool(until and ctx.now() < until)


def normalize_text(text) -> str:
    """Texto comparável: strip e espaços/quebras colapsados."""
    return " ".join(str(text or "").split())


def phone_set(values) -> set:
    return {to_e164(v) or v for v in (values or []) if v}


def test_destinations(agent: dict) -> set:
    """Destinos liberados em modo teste: o dono e os números de teste."""
    return phone_set(agent.get("test_phones")) | phone_set([agent.get("owner_phone")])


def alert_closer(ctx, dest: str, text: str) -> bool:
    """Aviso a um closer (created_by='alerta'). Modo teste: closer fora de test_phones não recebe; o DONO recebe o
    mesmo texto com "[TESTE] ". Ao vivo (test_only False) e closer em test_phones: vai ao closer normalmente."""
    agent = ctx.state.get("agent") or {}
    dest = to_e164(dest)
    if not dest:
        return False
    if agent.get("test_only") is not False and dest not in test_destinations(agent):
        return queue_owner_alert(ctx, "[TESTE] " + text)
    row = ctx.db.enqueue_outbox(dest, text, created_by="alerta")
    if row:
        send_row(ctx, row)
    return True


def outbound_moment(db, now, lead_id):
    """Carimbo de last_outbound_at: o relógio local, nunca antes de last_inbound_at + 1 s. last_inbound_at vem do
    carimbo do WhatsApp; com o relógio do computador atrasado a resposta ficaria "anterior" à pergunta e a
    cadência de follow-up (que exige last_out >= last_in) pararia em silêncio. Falha de leitura = relógio local."""
    try:
        last_in = parse_ts((db.get_lead(lead_id) or {}).get("last_inbound_at"))
    except Exception:  # noqa: BLE001
        return now
    if last_in and now <= last_in:
        return last_in + timedelta(seconds=1)
    return now


class Messenger:
    """Envia pela Evolution respeitando modo teste e limitador. Só devolve sucesso com key.id."""

    def __init__(self, evo, limiter, db, log, now_fn, sleep_fn, agent_getter):
        self.evo = evo
        self.limiter = limiter
        self.db = db
        self.log = log
        self.now_fn = now_fn
        self.sleep_fn = sleep_fn
        self.agent_getter = agent_getter
        self.last_failure = ""   # 'gate' | 'rate' | 'evolution' | 'timeout_incerto' | 'sem_key_id' | ''
        self.last_error = ""
        self.last_status = None   # HTTP da última falha da Evolution (None se não houve resposta)
        self.evolution_called = False
        self.sent_ids: set = set()   # key.id do que ESTE motor enviou (o ingest não confunde com o dono no celular)
        # envios de resultado incerto (timeout, 5xx, sem key.id): o eco fromMe deles não é o dono no celular
        self.uncertain_sends: list = []   # {"phone", "text", "at"}
        # respostas diretas que SAÍRAM mas cuja gravação falhou: {"lead_id", "phone", "text", "key_id", "at",
        # "marker": bool (linha de erro na outbox gravada), "alerted": bool}. O engine drena (marcador + aviso 1x) e
        # o recover_unanswered consulta `unrecorded_turns` (lead_id -> instante) enquanto o processo vive.
        self.unrecorded_sends: list = []
        self.unrecorded_turns: dict = {}

    def _remember_uncertain(self, phone: str, text: str) -> None:
        self.uncertain_sends.append({"phone": phone, "text": normalize_text(text), "at": self.now_fn()})
        del self.uncertain_sends[:-200]

    def allowed(self, phone: str, internal: bool = False) -> bool:
        agent = self.agent_getter() or {}
        # fail-closed: só sai do modo teste quem tem test_only EXATAMENTE false (NULL/ausente = teste ligado)
        if agent.get("test_only") is False:
            return True
        # modo teste: `internal` NÃO é liberação genérica. Só o dono e os test_phones recebem; aviso destinado a
        # closer fora de test_phones vai para o dono com "[TESTE] " (ver alert_closer)
        return phone in test_destinations(agent)

    def send(self, phone, text, *, lead_id=None, author="agente", proactive=False, internal=False, on_sent=None):
        """internal=True: aviso para dono/closer (não é lead, não passa pelo modo teste).

        on_sent(key_id): chamado ASSIM que a Evolution confirma, antes de qualquer outro registro. Depois de
        key_id obtido nenhuma exceção escapa daqui: a mensagem saiu e quem chama nunca deve reenviar."""
        self.last_failure = self.last_error = ""
        self.last_status = None
        self.evolution_called = False   # True só depois de a chamada à Evolution começar (exceção antes = nada saiu)
        text = (text or "").strip()
        if not text:
            return None
        if not self.allowed(phone, internal):
            self.last_failure = "gate"
            self.log.info("modo teste: envio para %s ignorado (número fora de test_phones)", phone)
            return None
        if not self.limiter.acquire(phone, proactive, self.sleep_fn):
            self.last_failure = "rate"
            self.last_error = "limite de envio atingido; tentando no próximo ciclo"
            self.log.warning("limitador: sem vaga de envio para %s agora", phone)
            return None
        delay = min(800 + 25 * len(text), 6000)  # a Evolution mostra "digitando..." durante o delay
        self.evolution_called = True
        try:
            key_id = self.evo.send_text(phone, text, delay)
        except EvolutionTimeout as exc:
            # a Evolution pode ter enviado: resultado incerto, quem chama NÃO reenvia sozinho
            self.last_failure = "timeout_incerto"
            self.last_error = "timeout_incerto"
            self._remember_uncertain(phone, text)
            self.log.error("envio para %s com timeout (pode ter saído): %s", phone, exc)
            return None
        except EvolutionNoKeyId as exc:
            self._remember_uncertain(phone, text)
            self._no_key(phone, str(exc))
            return None
        except EvolutionError as exc:
            if getattr(exc, "status", None) in UNCERTAIN_STATUSES:
                # 502/503/504: o proxy pode ter falhado depois de a Evolution entregar. Incerto, sem reenvio
                self.last_failure = "timeout_incerto"
                self.last_error = "timeout_incerto"
                self._remember_uncertain(phone, text)
                self.log.error("envio para %s com HTTP %s (pode ter saído): %s", phone, exc.status, exc)
                return None
            self.last_failure = "evolution"
            self.last_error = str(exc)
            self.last_status = getattr(exc, "status", None)
            self.log.error("envio para %s falhou: %s", phone, exc)
            return None
        except Exception as exc:  # noqa: BLE001
            # r23: erro não mapeado DEPOIS de a chamada à Evolution começar = envio incerto (pode ter saído).
            # Mesmo tratamento do timeout: nunca reenviar sozinho; quem chama registra e avisa o dono.
            self.last_failure = "timeout_incerto"
            self.last_error = "timeout_incerto"
            self._remember_uncertain(phone, text)
            self.log.error("envio para %s com erro inesperado (pode ter saído): %s", phone, exc)
            return None
        if not key_id:
            # 2xx sem key.id (ou None) nunca é sucesso: a mensagem pode ter saído, então tampouco é reenviada
            self._remember_uncertain(phone, text)
            self._no_key(phone, "A Evolution respondeu sem key.id: envio não confirmado")
            return None
        if len(self.sent_ids) > 5000:
            self.sent_ids.clear()
        self.sent_ids.add(key_id)
        if on_sent:
            try:
                on_sent(key_id)
            except Exception as exc:  # noqa: BLE001 - a mensagem saiu: o resto é registro
                self.log.error("mensagem enviada (%s) mas o pós-envio falhou: %s", key_id, exc)
        try:
            self.limiter.record(phone)
        except Exception as exc:  # noqa: BLE001 - ex.: ratelimit.db travado: a mensagem JÁ saiu
            self.log.error("mensagem enviada (%s) mas o limitador não registrou: %s", key_id, exc)
        if not lead_id and not internal:
            # envio sem lead_id (painel, SQL): resolve o lead pelo telefone para gravar a mensagem com o key.id
            # (o eco fromMe depois de um reinício não vira "dono no celular")
            try:
                found = self.db.get_lead_by_phone(to_e164(phone) or phone)
                lead_id = (found or {}).get("id")
            except Exception as exc:  # noqa: BLE001 - a mensagem saiu: o resto é registro
                self.log.error("mensagem enviada (%s) mas não consegui resolver o lead pelo telefone: %s", key_id, exc)
        recorded = True
        if lead_id:
            # mensagem e last_outbound_at em passos SEPARADOS, cada um com retry próprio: a mensagem já saiu e a
            # falha de um não pode deixar o outro sem tentativa (o recover_unanswered também confere a conversa)
            message_ok = False
            for attempt in (1, 2):
                try:
                    self.db.add_message(lead_id, "out", author, text, wa_id=key_id)
                    message_ok = True
                    break
                except Exception as exc:  # noqa: BLE001
                    self.log.error("mensagem enviada (%s) mas não registrada no banco (tentativa %d): %s",
                                   key_id, attempt, exc)
            lead_ok = False
            for attempt in (1, 2):
                try:
                    self.db.update_lead(lead_id, {"last_outbound_at": iso(self._outbound_moment(lead_id))})
                    lead_ok = True
                    break
                except Exception as exc:  # noqa: BLE001
                    self.log.error("mensagem enviada (%s) mas last_outbound_at não gravado (tentativa %d): %s",
                                   key_id, attempt, exc)
            recorded = message_ok or lead_ok   # uma das duas gravações já prova o turno respondido
            if not recorded:
                self._note_unrecorded(lead_id, phone, text, key_id)
        return key_id

    def _note_unrecorded(self, lead_id, phone: str, text: str, key_id: str) -> None:
        """A resposta saiu e NADA ficou gravado: marcador durável (linha 'erro' na outbox com o key.id) para o
        _answered_in_outbox reconhecer o turno; se até ele falhar, memória do processo. O aviso ao dono sai pelo
        engine (1x), que também retenta o marcador."""
        entry = {"lead_id": lead_id, "phone": phone, "text": text, "key_id": key_id, "at": self.now_fn(),
                 "marker": False, "alerted": False}
        self.unrecorded_turns[lead_id] = entry["at"]   # vale enquanto o processo vive, mesmo sem marcador
        self.unrecorded_sends.append(entry)
        del self.unrecorded_sends[:-200]
        self.write_unrecorded_marker(entry)

    def write_unrecorded_marker(self, entry: dict) -> bool:
        if entry.get("marker"):
            return True
        try:
            row = self.db.enqueue_outbox(entry["phone"], entry["text"], lead_id=entry["lead_id"], created_by="motor",
                                         status="erro", last_error=SENT_UNRECORDED_ERROR)
            if row and row.get("id") is not None:
                self.db.finish_outbox(row["id"], status="erro", wa_key_id=entry["key_id"], error=SENT_UNRECORDED_ERROR,
                                      attempts=int(row.get("attempts") or 0))
            entry["marker"] = True
        except Exception as exc:  # noqa: BLE001 - fica só na memória; o engine tenta de novo
            self.log.error("não consegui gravar o marcador da resposta enviada (%s): %s", entry.get("key_id"), exc)
        return entry["marker"]


    def _outbound_moment(self, lead_id):
        return outbound_moment(self.db, self.now_fn(), lead_id)

    def _no_key(self, phone: str, detail: str) -> None:
        self.last_failure = "sem_key_id"
        self.last_error = "sem_key_id"
        self.log.error("envio para %s sem key.id (pode ter saído; não será reenviado): %s", phone, detail)


def record_uncertain_send(ctx, phone: str, text: str, lead_id, reason: str, created_by: str = "motor") -> None:
    """Envio DIRETO ao lead com resultado incerto (timeout_incerto / sem_key_id): deixa um marcador durável do
    turno respondido, SEM reenvio. A linha nasce na outbox já em 'erro' (send_row/process_outbox nunca pegam
    linha de erro), com o motivo igual ao last_failure, e leads.last_outbound_at é atualizado. Assim o
    recover_unanswered não chama a IA de novo e o eco fromMe consegue casar com esta linha."""
    try:
        # a linha NASCE em 'erro' (sem passar por 'pendente'): nenhuma falha posterior a deixa elegível ao envio
        ctx.db.enqueue_outbox(phone, text, lead_id=lead_id, created_by=created_by, status="erro", last_error=reason)
    except Exception as exc:  # noqa: BLE001 - o último recurso abaixo ainda grava last_outbound_at
        ctx.log.error("não consegui registrar o envio incerto para %s na outbox: %s", phone, exc)
    if lead_id:
        try:
            ctx.db.update_lead(lead_id, {"last_outbound_at": iso(outbound_moment(ctx.db, ctx.now(), lead_id))})
        except Exception as exc:  # noqa: BLE001
            ctx.log.error("não consegui atualizar last_outbound_at do lead %s: %s", lead_id, exc)
        _alert_uncertain_lead_send(ctx, phone, text, lead_id)


def _alert_uncertain_lead_send(ctx, phone: str, text: str, lead_id, dedupe=None) -> None:
    """UM alerta ao dono por lead e por turno (lead_id + last_inbound_at) quando o envio AO LEAD ficou incerto:
    a mensagem pode não ter chegado e o motor não reenvia. Nome, telefone e o início do texto."""
    lead = None
    try:
        lead = ctx.db.get_lead(lead_id)
    except Exception as exc:  # noqa: BLE001 - o alerta sai mesmo sem o nome
        ctx.log.warning("alerta de envio incerto: não li o lead %s: %s", lead_id, exc)
    turn = (lead or {}).get("last_inbound_at")
    done = ctx.state.setdefault("uncertain_lead_alerted", set())
    mark = (lead_id, turn) if dedupe is None else dedupe   # linha sem lead: dedupe pela própria linha
    if mark in done:
        return
    who = f"{lead['name']} ({phone})" if (lead or {}).get("name") else phone
    try:
        if queue_owner_alert(ctx, f"⚠️ Não tenho certeza de que a mensagem para {who} chegou: \"{normalize_text(text)[:80]}\". "
                                  "Não reenviei para não duplicar: confira no WhatsApp se ela chegou e responda por lá se precisar."):
            done.add(mark)   # só depois de enfileirar: se falhar, o próximo ciclo tenta de novo
    except Exception as exc:  # noqa: BLE001
        ctx.log.error("alerta de envio incerto ao lead não enfileirado: %s", exc)


def takeover_active(lead: dict, now) -> bool:
    until = parse_ts((lead or {}).get("takeover_until"))
    return bool(until and until > now)


def _cancel_motor_row(ctx, row: dict, lead, phone: str):
    """Linha do MOTOR para um lead: relê o lead e devolve o motivo de cancelamento ('' se pode enviar).
    Takeover ativo, estágio final ou mensagem do dono depois da criação da linha cancelam. Falha de leitura levanta
    (quem chama deixa a linha pendente para o próximo ciclo)."""
    from .conversa import FINAL_STAGES   # import tardio: conversa importa este módulo
    if takeover_active(lead, ctx.now()):
        return "cancelado: takeover"
    if lead.get("stage") in FINAL_STAGES:
        return "cancelado: estágio final"
    created = parse_ts(row.get("created_at"))
    if created:
        for m in ctx.db.recent_messages(lead["id"], 20):
            if m.get("direction") == "out" and m.get("author") == "dono" \
                    and (parse_ts(m.get("created_at")) or created) > created:
                return "cancelado: takeover"
    return ""


def _alert_cancelled_takeover(ctx, lead: dict, phone: str) -> None:
    done = ctx.state.setdefault("takeover_cancel_alerted", set())
    if lead.get("id") in done:
        return
    done.add(lead.get("id"))
    who = lead.get("name") or phone
    try:
        queue_owner_alert(ctx, f"ℹ️ A resposta automática para {who} ({phone}) foi cancelada porque você assumiu a "
                               "conversa (ou ela já foi encerrada). Não reenviei: siga por aí.")
    except Exception as exc:  # noqa: BLE001
        ctx.log.error("aviso de resposta cancelada não enfileirado: %s", exc)


def _internal_phones(ctx) -> set:
    phones = {to_e164((ctx.state.get("agent") or {}).get("owner_phone"))}
    try:
        phones |= {to_e164(c.get("whatsapp")) for c in ctx.db.list_closers(active_only=False)}
    except Exception as exc:  # noqa: BLE001
        ctx.log.warning("não consegui listar closers para a outbox: %s", exc)
    return {p for p in phones if p}


# created_by que sobrevive ao opt-out: avisos ao dono e a confirmação do próprio pedido de saída.
# A isenção só vale para destino INTERNO (dono/closer); a única mensagem que ainda pode ir ao próprio lead que
# pediu para sair é o texto fixo da confirmação (reenfileirada quando a Evolution falhou).
OPT_OUT_EXEMPT = ("alerta", "optout")
OPT_OUT_CONFIRMATION = "Tudo bem, não vou mais te enviar mensagens. Se mudar de ideia, é só chamar por aqui."
AUTHORS = {"painel": "dono", "motor": "agente", "optout": "agente"}


def send_row(ctx, row: dict) -> bool:
    """Envia UMA linha da outbox (claim, envio, prova). True só se saiu com key.id."""
    phone = to_e164(row.get("phone"))
    if not phone:
        # nunca usa o valor cru como destino: telefone fora do E.164 não é enviado
        ctx.db.finish_outbox(row["id"], status="erro", error="telefone_invalido",
                             attempts=int(row.get("attempts") or 0))
        ctx.log.warning("outbox %s: telefone fora do E.164, marcada como erro", row.get("id"))
        return False
    internal_phones = _internal_phones(ctx)
    is_internal = phone in internal_phones
    lead = None
    # aviso ao dono: não passa pelo modo teste, mas só se o destino for mesmo o dono ou um closer
    # (o rótulo created_by sozinho não basta: o painel poderia gravá-lo para um número de lead)
    internal = row.get("created_by") == "alerta" and is_internal
    exempt = row.get("created_by") in OPT_OUT_EXEMPT and (
        is_internal or (row.get("created_by") == "optout" and (row.get("body") or "").strip() == OPT_OUT_CONFIRMATION))
    if not exempt:
        # linha sem lead_id (painel, SQL, página pública): resolve o lead pelo telefone E.164
        try:
            lead = ctx.db.get_lead(row["lead_id"]) if row.get("lead_id") else ctx.db.get_lead_by_phone(phone)
        except Exception as exc:  # noqa: BLE001 - sem reler o lead não envia: fica pendente para o próximo ciclo
            ctx.log.error("outbox %s: não consegui reler o lead (%s); fica pendente", row.get("id"), exc)
            return False
        if lead and lead.get("opt_out"):
            ctx.db.finish_outbox(row["id"], status="erro", error="cancelado: o lead pediu para não receber mensagens",
                                 attempts=int(row.get("attempts") or 0))
            return False
        if lead and row.get("created_by") == "motor" and not is_internal:
            try:
                reason = _cancel_motor_row(ctx, row, lead, phone)
            except Exception as exc:  # noqa: BLE001
                ctx.log.error("outbox %s: não consegui conferir takeover (%s); fica pendente", row.get("id"), exc)
                return False
            if reason:
                ctx.db.finish_outbox(row["id"], status="erro", error=reason, attempts=int(row.get("attempts") or 0))
                _alert_cancelled_takeover(ctx, lead, phone)
                return False
    if not ctx.messenger.allowed(phone, internal):
        return False  # modo teste: fica pendente até o aluno liberar
    if not ctx.db.claim_outbox(row):
        return False  # outro processo pegou
    created_by = row.get("created_by")
    attempts = int(row.get("attempts") or 0) + 1
    marked = {"ok": False}

    def mark_sent(key_id):
        ctx.db.finish_outbox(row["id"], status="enviado", wa_key_id=key_id, sent_at=ctx.now(), attempts=attempts)
        marked["ok"] = True

    # marca 'enviado' PRIMEIRO (on_sent), antes do limitador e do registro da conversa
    try:
        key = ctx.messenger.send(
            phone, row.get("body", ""), lead_id=row.get("lead_id"),
            author=AUTHORS.get(created_by, "sistema"), proactive=created_by != "painel", internal=internal,
            on_sent=mark_sent,
        )
    except Exception as exc:  # noqa: BLE001
        if getattr(ctx.messenger, "evolution_called", True) or marked["ok"]:
            raise   # depois de chamar a Evolution vale a regra de incerteza (a recuperação marca erro, não reenvia)
        # erro interno ANTES da Evolution (ex.: ratelimit.db travado): nada saiu, a linha não fica presa em 'enviando'
        ctx.log.warning("outbox %s: erro antes do envio (%s); volta a pendente", row.get("id"), exc)
        ctx.db.finish_outbox(row["id"], status="pendente", error=f"erro interno antes do envio: {str(exc)[:200]}",
                             attempts=attempts - 1)
        return False
    if not key and (ctx.messenger.last_failure == "sem_key_id" or marked["ok"]):
        # a linha NUNCA volta a 'pendente' (nem fica 'enviado' sem prova): erro visível e alerta único ao dono
        ctx.db.finish_outbox(row["id"], status="erro", error="resposta_sem_key_id", attempts=attempts)
        _alert_no_key_once(ctx, phone, created_by, row.get("id"))
        return False
    if key:
        if not marked["ok"]:   # o callback já tentou; uma segunda tentativa, e a linha NUNCA volta a pendente
            try:
                mark_sent(key)
            except Exception as exc:  # noqa: BLE001 - fica 'enviando' (a recuperação marca erro, não reenvia)
                ctx.log.error("outbox %s: enviada (%s) mas não marcada como enviada: %s", row["id"], key, exc)
        return True
    if ctx.messenger.last_failure == "rate":
        ctx.db.finish_outbox(row["id"], status="pendente", error=ctx.messenger.last_error, attempts=attempts - 1)
    elif ctx.messenger.last_failure == "timeout_incerto":
        # pode ter saído: nunca reenvia sozinho; o dono vê o erro no painel e decide
        ctx.db.finish_outbox(row["id"], status="erro", error="timeout_incerto", attempts=attempts)
        if created_by != "alerta" and not is_internal:   # linha ao LEAD: o dono precisa saber; interna não alerta (laço)
            lead_id = row.get("lead_id") or (lead or {}).get("id")
            _alert_uncertain_lead_send(ctx, phone, row.get("body", ""), lead_id,
                                       dedupe=None if lead_id else ("linha", row.get("id")))
    else:
        detail = ctx.messenger.last_error or "falha no envio"
        permanent = _permanent_http(ctx) and not _instance_not_open(ctx)   # r24: instância fechada é transitória
        if created_by in REPLY_AUTHORS and not is_internal:
            # r24: resposta ao lead não vira erro por contagem de tentativas: fica pendente com o backoff até o
            # limite de validade de resposta (catchup_minutes), o mesmo que impede responder mensagem velha
            exhausted = _reply_expired(ctx, row, attempts)
            exhausted_text = f"validade da resposta esgotada (catchup de {ctx.cfg.catchup_minutes} min) ({detail})"
        else:
            exhausted = attempts >= MAX_ATTEMPTS
            exhausted_text = f"{attempts} tentativas esgotadas ({detail})"
        if permanent or exhausted:
            # falha definitiva: o marcador no início do erro faz o recover_unanswered tratar o turno como já
            # tentado (não chama a IA de novo) e o dono recebe UM aviso
            reason = f"{FINAL_FAILURE_PREFIX}: HTTP {ctx.messenger.last_status}: {detail}" if permanent else \
                f"{FINAL_FAILURE_PREFIX}: {exhausted_text}"
            ctx.db.finish_outbox(row["id"], status="erro", error=reason, attempts=attempts)
            _alert_final_failure(ctx, row, phone, reason)
        else:
            ctx.db.finish_outbox(row["id"], status="pendente", error=detail, attempts=attempts)
            wait = backoff_seconds(attempts)
            ctx.state.setdefault("outbox_next_at", {})[row["id"]] = ctx.now() + timedelta(seconds=wait)
            ctx.log.info("outbox %s: tentativa %d falhou; próxima só em %ds", row["id"], attempts, wait)
    return False


# r24: created_by de respostas ao lead (a falha de envio vale pelo tempo, não pela contagem de tentativas)
REPLY_AUTHORS = ("motor", "optout", "pagina")


def _reply_expired(ctx, row: dict, attempts: int) -> bool:
    """A resposta já passou do limite de validade (cfg.catchup_minutes, o mesmo do catch-up do engine)? Sem
    created_at legível, volta à contagem de tentativas."""
    created = parse_ts(row.get("created_at"))
    if not created:
        return attempts >= MAX_ATTEMPTS
    return ctx.now() - created > timedelta(minutes=ctx.cfg.catchup_minutes)


def _instance_not_open(ctx) -> bool:
    """Instância fora de 'open' (ou estado ilegível): um 4xx nesse cenário é transitório, não rejeição do número."""
    try:
        return str(ctx.evo.connection_state()).strip().lower() != "open"
    except Exception as exc:  # noqa: BLE001 - sem confirmar o estado, não declara rejeição definitiva
        ctx.log.warning("não consegui ler o estado da instância ao classificar o 4xx: %s", exc)
        return True


def _permanent_http(ctx) -> bool:
    """HTTP 4xx (exceto 408/429) da Evolution: não se resolve repetindo."""
    status = getattr(ctx.messenger, "last_status", None)
    return (ctx.messenger.last_failure == "evolution" and isinstance(status, int)
            and 400 <= status < 500 and status not in (408, 429))


def _alert_final_failure(ctx, row: dict, phone: str, reason: str) -> None:
    """UM alerta ao dono por linha que virou 'erro' definitivo. Nunca para linhas de alerta (evita laço)."""
    if row.get("created_by") == "alerta":
        return
    done = ctx.state.setdefault("final_failure_alerted", set())
    if row.get("id") in done:
        return
    done.add(row.get("id"))
    who = phone
    try:
        lead = ctx.db.get_lead(row["lead_id"]) if row.get("lead_id") else ctx.db.get_lead_by_phone(phone)
        if lead and lead.get("name"):
            who = f"{lead['name']} ({phone})"
    except Exception as exc:  # noqa: BLE001 - o alerta sai mesmo sem o nome
        ctx.log.warning("alerta de falha definitiva: não li o lead: %s", exc)
    snippet = normalize_text(row.get("body"))[:80]
    collector = ctx.state.get("_final_failures")
    if collector is not None:
        collector.append((who, snippet, reason))   # dentro de process_outbox: sai UM aviso consolidado no fim do ciclo
        return
    _send_final_failure_alert(ctx, [(who, snippet, reason)])


def _flush_final_failures(ctx, failures: list) -> None:
    if failures:
        _send_final_failure_alert(ctx, failures)


def _send_final_failure_alert(ctx, failures: list) -> None:
    if len(failures) == 1:
        who, snippet, reason = failures[0]
        text = (f"⚠️ Não consegui enviar uma mensagem para {who}: \"{snippet}\". Motivo: "
                f"{reason[:150]}. Não vou tentar de novo sozinho: confira o número e responda por "
                "outro caminho.")
    else:
        shown = "; ".join(f"{who} (\"{snippet[:40]}\")" for who, snippet, _ in failures[:10])
        extra = f" e mais {len(failures) - 10}" if len(failures) > 10 else ""
        text = (f"⚠️ Não consegui enviar {len(failures)} mensagens (para: {shown}{extra}). Último motivo: "
                f"{failures[-1][2][:150]}. Não vou tentar de novo sozinho: confira os números e responda por "
                "outro caminho.")
    try:
        queue_owner_alert(ctx, text)
    except Exception as exc:  # noqa: BLE001
        ctx.log.error("alerta de falha definitiva da outbox não enfileirado: %s", exc)


def _alert_no_key_once(ctx, phone: str, created_by, row_id=None) -> None:
    """Resposta sem key.id: avisa o dono UMA vez POR LINHA da outbox (a mesma linha nunca alerta duas vezes; uma
    linha nova alerta de novo). Não vale para as linhas de alerta, que não alertam a si mesmas."""
    if created_by == "alerta":
        return
    done = ctx.state.setdefault("sem_key_alerted_rows", set())
    mark = row_id if row_id is not None else ("fone", phone)
    if mark in done:
        return
    try:
        if queue_owner_alert(ctx, f"⚠️ A Evolution respondeu sem confirmação (key.id) ao enviar para {phone}. A mensagem "
                                  "pode ter saído e NÃO será reenviada sozinha. Confira no WhatsApp e veja a fila no painel."):
            done.add(mark)   # só depois de enfileirar
    except Exception as exc:  # noqa: BLE001
        ctx.log.error("alerta de envio sem key.id não enviado: %s", exc)


def process_outbox(ctx) -> int:
    """Envia o que o painel (ou a página pública) enfileirou. Devolve quantas foram enviadas."""
    agent = ctx.state.get("agent") or {}
    phones = None
    if agent.get("test_only") is not False:
        # modo teste: só busca destinos liberados (test_phones + internos); o resto fica pendente sem travar a fila
        phones = sorted(test_destinations(agent))
        if not phones:
            return 0
    # linhas em espera de backoff não ocupam vaga do lote: busca em páginas crescentes (20, 40, ... até 5 páginas)
    # e fica com as elegíveis; para quando junta o lote ou quando a busca já devolveu tudo o que existe
    eligible: list = []
    for page in range(1, OUTBOX_MAX_PAGES + 1):
        limit = OUTBOX_BATCH * page
        rows = ctx.db.pending_outbox(limit, phones) if phones is not None else ctx.db.pending_outbox(limit)
        eligible = [r for r in rows if not _in_backoff(ctx, r)]
        if len(eligible) >= OUTBOX_BATCH or len(rows) < limit:
            break
    final_failures: list = []
    ctx.state["_final_failures"] = final_failures
    sent = 0
    try:
        for row in eligible[:OUTBOX_BATCH]:
            if send_row(ctx, row):
                sent += 1
    finally:
        ctx.state.pop("_final_failures", None)
        _flush_final_failures(ctx, final_failures)
    return sent


def recover_stuck_outbox(ctx) -> int:
    """Linhas 'enviando' que sobraram de uma queda: o envio pode ter saído, então NÃO reenvia sozinho."""
    stuck = ctx.db.stuck_outbox()
    for row in stuck:
        ctx.db.finish_outbox(row["id"], status="erro",
                             error="interrompido durante o envio: confira no WhatsApp se foi entregue antes de reenviar")
    if stuck:
        ctx.log.warning("%d mensagem(ns) da outbox ficaram 'enviando' numa queda e foram marcadas como erro", len(stuck))
        _alert_interrupted(ctx, stuck)
    return len(stuck)


def _alert_interrupted(ctx, stuck: list) -> None:
    """UM alerta consolidado ao dono com os destinos cujo envio foi interrompido (sem reenvio automático)."""
    phones = sorted({str(r.get("phone") or "?") for r in stuck})
    shown = ", ".join(phones[:10]) + (f" e mais {len(phones) - 10}" if len(phones) > 10 else "")
    try:
        queue_owner_alert(ctx, f"⚠️ O motor caiu durante o envio de {len(stuck)} mensagem(ns) (para: {shown}). "
                               "Não reenviei para não duplicar: confira no WhatsApp se chegaram e responda quem ficou sem resposta.")
    except Exception as exc:  # noqa: BLE001
        ctx.log.error("alerta de envio interrompido não enfileirado: %s", exc)


def notify_owner(ctx, text: str) -> bool:
    """Avisa o dono no WhatsApp. Se o envio falhar, deixa na outbox para sair quando a conexão voltar."""
    agent = ctx.state.get("agent") or {}
    owner = to_e164(agent.get("owner_phone"))
    if not owner:
        ctx.log.warning("alerta sem owner_phone configurado: %s", text)
        return False
    if ctx.messenger.send(owner, text, internal=True):
        return True
    if ctx.messenger.last_failure in ("timeout_incerto", "sem_key_id"):
        ctx.log.warning("alerta ao dono sem confirmação: não reenfileirado (pode ter saído)")
        return False
    ctx.db.enqueue_outbox(owner, text, created_by="alerta")
    return False


def queue_owner_alert(ctx, text: str) -> bool:
    """Alerta ao dono SEMPRE pela outbox (created_by='alerta', para owner_phone): fica registrado e sai com prova."""
    owner = to_e164((ctx.state.get("agent") or {}).get("owner_phone"))
    if not owner:
        ctx.log.warning("alerta sem owner_phone configurado: %s", text)
        return False
    row = ctx.db.enqueue_outbox(owner, text, created_by="alerta")
    if row:
        try:
            send_row(ctx, row)
        except Exception as exc:  # noqa: BLE001 - a linha já está na outbox: sai no próximo ciclo
            ctx.log.error("alerta ao dono enfileirado, mas não enviado agora: %s", exc)
    return True
