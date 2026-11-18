"""Falsos usados nos testes do motor: banco em memória, Evolution, IA e relógio. Nada sai para a rede."""

from __future__ import annotations

import json
import logging
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from motor import engine
from motor.ai import AIError
from motor.config import MotorConfig
from motor.context import Ctx
from motor.evolution import EvolutionError, EvolutionTimeout
from motor.outbox import Messenger
from motor.ratelimit import RateLimiter
from motor.util import iso, parse_ts, to_e164

# 18/11/2026 12:00 UTC = 09:00 em São Paulo (sem horário de verão em 2026)
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
LEAD_PHONE = "+5511999990001"
TEST_PHONE = "+5511999990000"
OWNER_PHONE = "+5511999990099"
CLOSER_PHONE = "+5511999990088"


def sp(day: int, hour: int, minute: int = 0) -> datetime:
    """Horário local de São Paulo de novembro/2026 em UTC (UTC-3)."""
    return datetime(2026, 11, day, tzinfo=timezone.utc) + timedelta(hours=hour + 3, minutes=minute)


class Clock:
    def __init__(self, start: datetime = NOW):
        self.current = start

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **kwargs) -> None:
        self.current += timedelta(**kwargs)


def default_agent(**over) -> dict:
    agent = {
        "agent_name": "Ana", "company": "Clínica Exemplo", "tone": "cordial e direto", "niche": "saúde",
        "offer": "avaliação inicial gratuita", "knowledge": "Atendemos de segunda a sexta.",
        "qualification_questions": [{"key": "interesse", "pergunta": "O que você procura?", "obrigatoria": True}],
        "disqualify_rules": [], "objection_playbook": [],
        "followup_messages": [{"apos_horas": 2, "texto": "Oi {nome}, ainda posso te ajudar?"},
                              {"apos_horas": 24, "texto": "{nome}, posso reservar um horário para você?"}],
        "confirm_template": "Perfeito, {nome}! Sua conversa com {closer} ficou para {data} às {hora}.",
        "reminder_24h_template": "Oi {nome}, lembrete: amanhã, {data} às {hora}.",
        "reminder_2h_template": "Oi {nome}, daqui a pouco, às {hora}.",
        "owner_phone": OWNER_PHONE, "test_only": False, "test_phones": [], "ai_provider": "gemini",
        "paused": False, "followup_window_start": "08:00", "followup_window_end": "20:00",
    }
    agent.update(over)
    return agent


class FakeDatabase:
    """Implementa exatamente a superfície de motor.db.Database, em memória."""

    def __init__(self, clock: Clock, agent: dict | None = None):
        self.clock = clock
        self.agent = agent or default_agent()
        self.settings = {"call_minutes": 30, "timezone": "America/Sao_Paulo", "days_ahead": 14}
        self.closers = [{"id": "c1", "name": "Marcos", "whatsapp": CLOSER_PHONE, "active": True}]
        self.leads: dict = {}
        self.messages: list = []
        self.objections: list = []
        self.appointments: list = []
        self.outbox: list = []
        self.slots: list = [sp(19, 14), sp(19, 15), sp(20, 10), sp(21, 16)]
        self.taken: set = set()
        self.force_conflict: set = set()
        self.promote_stage_on_book = True   # a RPC real também promove o estágio; False isola o lado do motor
        self._seq = 0

    def _id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq}"

    # configuração
    def get_agent_config(self):
        return dict(self.agent)

    def get_schedule_settings(self):
        return dict(self.settings)

    def list_closers(self, active_only=True):
        return [c for c in self.closers if c["active"] or not active_only]

    def get_closer(self, closer_id):
        return next((c for c in self.closers if c["id"] == closer_id), None)

    # leads
    def get_lead(self, lead_id):
        lead = self.leads.get(lead_id)
        return dict(lead) if lead else None

    def get_lead_by_phone(self, phone):
        lead = next((l for l in self.leads.values() if l["phone"] == phone), None)
        return dict(lead) if lead else None

    def create_lead(self, phone, name=None, source="whatsapp"):
        existing = self.get_lead_by_phone(phone)
        if existing:
            return existing
        lead = {"id": self._id("L"), "phone": phone, "name": name, "email": None, "source": source,
                "stage": "novo", "profile": None, "qualification": {}, "score": None,
                "disqualify_reason": None, "takeover_until": None, "closer_id": None, "opt_out": False,
                "followup_step": 0, "last_inbound_at": None, "last_outbound_at": None,
                "created_at": iso(self.clock())}
        self.leads[lead["id"]] = lead
        return dict(lead)

    def update_lead(self, lead_id, fields):
        self.leads[lead_id].update(fields)
        return dict(self.leads[lead_id])

    def active_leads(self, limit=1000):
        return [dict(l) for l in self.leads.values()
                if not l["opt_out"] and l["stage"] not in ("venda", "perdido", "desqualificado")]

    # mensagens
    def add_message(self, lead_id, direction, author, body, media_type=None, wa_id=None, created_at=None):
        if wa_id and any(m.get("wa_id") == wa_id for m in self.messages):
            return False
        self.messages.append({"lead_id": lead_id, "direction": direction, "author": author, "body": body,
                              "media_type": media_type, "wa_id": wa_id,
                              "created_at": created_at or self.clock()})
        return True

    def known_wa_ids(self, ids):
        return {m["wa_id"] for m in self.messages if m.get("wa_id") in set(ids)}

    def recent_messages(self, lead_id, limit=20):
        return [m for m in self.messages if m["lead_id"] == lead_id][-limit:]

    def agent_messages_since(self, lead_id, since, limit=50):
        rows = [m for m in self.messages if m["lead_id"] == lead_id and m["direction"] == "out"
                and m["author"] == "agente" and parse_ts(m["created_at"]) >= since]
        return [{"id": i} for i, _ in enumerate(rows[:limit])]

    # objeções
    def add_objection(self, lead_id, category, excerpt):
        self.objections.append({"lead_id": lead_id, "category": category, "excerpt": excerpt})

    def list_objections(self, lead_id):
        return [o for o in self.objections if o["lead_id"] == lead_id]

    # agenda
    def available_slots(self, p_from, p_to, p_closer=None):
        return [{"starts_at": iso(s), "ends_at": iso(s + timedelta(minutes=30)), "closer_id": "c1"}
                for s in self.slots if p_from <= s <= p_to and s not in self.taken]

    def reschedule_slot(self, appointment_id, starts_at, closer_id=None):
        """Espelha a RPC reschedule_slot: troca atômica (se o novo falhar, o antigo continua como estava)."""
        old = next((a for a in self.appointments if a["id"] == appointment_id), None)
        if not old:
            return {"ok": False, "error": "agendamento_inexistente"}
        if old["status"] not in ("agendado", "confirmado"):
            return {"ok": False, "error": "agendamento_inativo"}
        previous_status, old_start = old["status"], parse_ts(old["starts_at"])
        old["status"] = "reagendado"
        result = self.book_slot(old["lead_id"], starts_at, source=old.get("source") or "agente",
                                notes=f"reagendado de {old['starts_at']}")
        if not result.get("ok"):
            old["status"] = previous_status
            return result
        self.taken.discard(old_start)
        # a RPC real zera, na mesma transação, as flags de envio ligadas ao horário (decisão r8-1)
        new = next(a for a in self.appointments if a["id"] == result["appointment_id"])
        new.update(reminder_24h_at=None, reminder_2h_at=None, handoff_sent_at=None, handoff_1h_sent_at=None)
        return {**result, "old_appointment_id": appointment_id}

    def book_slot(self, lead_id, starts_at, source="agente", closer_id=None, notes=None):
        if lead_id not in self.leads:
            return {"ok": False, "error": "lead_inexistente"}
        if starts_at in self.force_conflict:
            self.taken.add(starts_at)
            return {"ok": False, "error": "slot_indisponivel"}
        if starts_at not in self.slots:
            return {"ok": False, "error": "fora_da_agenda"}
        if starts_at in self.taken:
            return {"ok": False, "error": "slot_indisponivel"}
        if self.active_appointment(lead_id, self.clock()):
            return {"ok": False, "error": "lead_ja_agendado"}
        self.taken.add(starts_at)
        appt = {"id": self._id("A"), "lead_id": lead_id, "closer_id": "c1", "starts_at": iso(starts_at),
                "ends_at": iso(starts_at + timedelta(minutes=30)), "status": "agendado", "source": source,
                "notes": notes, "created_at": iso(self.clock()), "reminder_24h_at": None, "reminder_2h_at": None,
                "handoff_sent_at": None, "handoff_1h_sent_at": None, "google_event_id": None}
        self.appointments.append(appt)
        self.leads[lead_id]["closer_id"] = "c1"
        if self.promote_stage_on_book:
            self.leads[lead_id]["stage"] = "agendado"   # a RPC real faz o mesmo
        return {"ok": True, "appointment_id": appt["id"], "closer_id": "c1", "closer_name": "Marcos",
                "starts_at": appt["starts_at"], "ends_at": appt["ends_at"]}

    def active_appointment(self, lead_id, now):
        rows = [a for a in self.appointments if a["lead_id"] == lead_id and a["status"] in ("agendado", "confirmado")]
        return dict(sorted(rows, key=lambda a: a["starts_at"])[0]) if rows else None

    def upcoming_appointments(self, now):
        return [dict(a) for a in sorted((a for a in self.appointments if a["status"] in ("agendado", "confirmado")
                                         and parse_ts(a["starts_at"]) >= now), key=lambda a: a["starts_at"])]

    def update_appointment(self, appointment_id, fields):
        appt = next(a for a in self.appointments if a["id"] == appointment_id)
        appt.update(fields)
        return appt

    def confirm_appointment_if_scheduled(self, appointment_id):
        appt = next((a for a in self.appointments if a["id"] == appointment_id), None)
        if not appt or appt["status"] != "agendado":
            return False
        appt["status"] = "confirmado"
        return True

    # outbox
    def pending_outbox(self, limit=20, phones=None):
        rows = [dict(r) for r in self.outbox if r["status"] == "pendente"]
        if phones is not None:
            wanted = {to_e164(p) or p for p in phones}
            rows = [r for r in rows if (to_e164(r["phone"]) or r["phone"]) in wanted]
        return rows[:limit]

    def claim_outbox(self, row):
        current = next(r for r in self.outbox if r["id"] == row["id"])
        if current["status"] != "pendente" or getattr(self, "lose_claim", False):
            return False
        current["status"] = "enviando"
        current["attempts"] = int(current.get("attempts") or 0) + 1
        return True

    def finish_outbox(self, outbox_id, *, status, wa_key_id=None, error=None, sent_at=None, attempts=None):
        row = next(r for r in self.outbox if r["id"] == outbox_id)
        row["status"] = status
        if attempts is not None:
            row["attempts"] = attempts
        if wa_key_id:
            row["wa_key_id"] = wa_key_id
        if error is not None:
            row["last_error"] = error
        if sent_at:
            row["sent_at"] = iso(sent_at)

    def outbox_for_lead_since(self, lead_id, since):
        return [dict(r) for r in self.outbox
                if r.get("lead_id") == lead_id and parse_ts(r["created_at"]) >= since]

    def outbox_for_phone_since(self, phone, since):
        return [dict(r) for r in self.outbox
                if (to_e164(r["phone"]) or r["phone"]) == (to_e164(phone) or phone) and parse_ts(r["created_at"]) >= since]

    def stuck_outbox(self):
        return [dict(r) for r in self.outbox if r["status"] == "enviando"]

    def enqueue_outbox(self, phone, body, lead_id=None, created_by="motor", client_key=None, status=None,
                       last_error=None):
        if client_key and any(r.get("client_key") == client_key for r in self.outbox):
            return {}   # duplicata ignorada (on_conflict do PostgREST)
        row = self.add_outbox(phone, body, lead_id=lead_id, created_by=created_by, status=status or "pendente")
        if last_error is not None:
            row["last_error"] = last_error
        row["client_key"] = client_key
        return row

    def outbox_by_wa_key(self, wa_key_id):
        row = next((r for r in self.outbox if r.get("wa_key_id") == wa_key_id), None)
        return dict(row) if row else None

    def outbox_by_client_key(self, client_key):
        row = next((r for r in self.outbox if r.get("client_key") == client_key), None)
        return dict(row) if row else None

    def add_outbox(self, phone, body, lead_id=None, created_by="painel", status="pendente", attempts=0):
        row = {"id": len(self.outbox) + 1, "phone": phone, "body": body, "lead_id": lead_id, "status": status,
               "attempts": attempts, "created_by": created_by, "wa_key_id": None, "last_error": None,
               "created_at": iso(self.clock())}
        self.outbox.append(row)
        return row

    def ping(self):
        return "agente 'Ana'"


class FakeEvolution:
    def __init__(self, clock: Clock):
        self.clock = clock
        self.records: list = []
        self.sent: list = []          # (phone, text)
        self.fail_send = False
        self.fail_status = None       # HTTP da falha de envio (com fail_send)
        self.timeout_send = False
        self.state = "open"
        self.media = None
        self._n = 0

    def add(self, phone, text, wa_id=None, from_me=False, age_seconds=20, jid=None, extra=None):
        self._n += 1
        digits = phone.lstrip("+")
        record = {"key": {"remoteJid": jid or f"{digits}@s.whatsapp.net", "fromMe": from_me,
                          "id": wa_id or f"IN{self._n}"},
                  "message": {"conversation": text}, "pushName": "Joana",
                  "messageTimestamp": int(self.clock().timestamp()) - age_seconds}
        record.update(extra or {})
        self.records.append(record)
        return record

    def recent_messages(self, limit=50, page=1):
        newest_first = list(reversed(self.records))
        return newest_first[(page - 1) * limit:page * limit]

    def chat_messages(self, phone, limit=10):
        """Leitura direcionada do chat de um contato (mais recentes primeiro). `fail_chat_read` simula a falha."""
        if getattr(self, "fail_chat_read", False):
            raise EvolutionError("leitura do chat falhou")
        wanted = phone.lstrip("+")
        rows = [r for r in reversed(self.records) if (r["key"].get("remoteJid") or "").split("@")[0] == wanted]
        return rows[:limit]

    def send_text(self, phone, text, delay_ms=0):
        if self.timeout_send:
            raise EvolutionTimeout("timed out")
        if self.fail_send:
            raise EvolutionError("Evolution fora do ar", status=self.fail_status)
        self.sent.append((phone, text))
        return f"OUT{len(self.sent)}"

    def media_base64(self, record):
        return self.media

    def connection_state(self):
        if self.state == "erro":
            raise EvolutionError("sem resposta")
        return self.state

    def owner_jid(self):
        return ""

    def texts_to(self, phone):
        return [t for p, t in self.sent if p == phone]


class FakeAI:
    """Roteiro de respostas: string (JSON ou texto), dict (vira JSON) ou exceção (levantada)."""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls: list = []

    def push(self, *items):
        self.script.extend(items)

    def complete(self, system, messages):
        self.calls.append({"system": system, "messages": messages})
        item = self.script.pop(0) if self.script else {"reply": "ok", "action": "responder"}
        if isinstance(item, Exception):
            raise item
        return json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else item

    def transcribe(self, data, mime):
        return "quero saber o preço da avaliação"

    def ping(self):
        return "gemini/fake: OK"


def make_ctx(agent: dict | None = None, tmp: Path | None = None):
    clock = Clock()
    tmp = Path(tmp or tempfile.mkdtemp(prefix="sdr-test-"))
    db = FakeDatabase(clock, agent)
    evo, ai = FakeEvolution(clock), FakeAI()
    cfg = MotorConfig(supabase_url="https://x.supabase.co", service_role_key="k", evolution_key="k",
                      instance="inst", ai_key="k", data_dir=tmp / "data", log_dir=tmp / "logs",
                      batch_window_seconds=0)
    log = logging.getLogger("motor.test")
    log.addHandler(logging.NullHandler())
    log.propagate = False
    slept: list = []
    ctx = Ctx(cfg=cfg, db=db, evo=evo, ai=ai, messenger=None, log=log, now_fn=clock,
              sleep_fn=lambda s: slept.append(s))
    limiter = RateLimiter(cfg.data_dir / "ratelimit.db", max_per_minute=100, now_fn=clock, min_interval_seconds=0)
    ctx.messenger = Messenger(evo, limiter, db, log, clock, ctx.sleep_fn, lambda: ctx.state.get("agent"))
    ctx.slept = slept
    ctx.clock = clock
    engine.refresh(ctx)
    return ctx


def inbound(ctx, text="oi", phone=LEAD_PHONE, **kw):
    """Cria o lead, grava a mensagem como o ingest faria e devolve o lead."""
    lead = ctx.db.get_lead_by_phone(phone) or ctx.db.create_lead(phone, name=kw.get("name", "Joana"))
    ctx.db.add_message(lead["id"], "in", "lead", text)
    ctx.db.update_lead(lead["id"], {"last_inbound_at": iso(ctx.now()), "followup_step": 0})
    return lead
