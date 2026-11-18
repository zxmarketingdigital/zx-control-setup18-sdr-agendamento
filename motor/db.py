"""Cliente do Supabase do aluno (PostgREST + RPC) com service_role.

Todo estado do agente mora no banco: o motor só lê e grava pelas tabelas e RPCs do contrato.
Os métodos de domínio abaixo são a única superfície usada pelo resto do motor (e a que os
testes substituem por um banco em memória).
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from datetime import datetime

from .http import HttpError, http_json
from .util import iso

ACTIVE_STATUSES = ("agendado", "confirmado")
# Estágios em que o agente NÃO conversa nem faz follow-up. 'compareceu' é com o closer; 'no_show' e 'reagendado'
# seguem ativos (precisam de recuperação). Fonte única: conversa.FINAL_STAGES é o subconjunto que ainda alerta o dono.
CLOSER_STAGES = ("compareceu",)
TERMINAL_STAGES = ("venda", "perdido", "desqualificado") + CLOSER_STAGES
WA_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_log = logging.getLogger("motor.db")


def valid_wa_id(value) -> bool:
    return isinstance(value, str) and bool(WA_ID_RE.match(value))


class PostgREST:
    def __init__(self, base_url: str, service_key: str):
        self.base = base_url.rstrip("/") + "/rest/v1"
        self.key = service_key

    def _headers(self, prefer: str | None = None) -> dict:
        headers = {"apikey": self.key, "Authorization": f"Bearer {self.key}"}
        if prefer:
            headers["Prefer"] = prefer
        return headers

    @staticmethod
    def _query(params: dict | list | None) -> str:
        if not params:
            return ""
        items = params.items() if isinstance(params, dict) else params
        return "?" + urllib.parse.urlencode(list(items), safe="(),.:*")

    def select(self, table: str, params: dict | list | None = None):
        return http_json("GET", f"{self.base}/{table}{self._query(params)}", headers=self._headers())

    def insert(self, table: str, row: dict, *, ignore_duplicates_on: str | None = None):
        prefer = "return=representation"
        query = ""
        if ignore_duplicates_on:
            prefer = "resolution=ignore-duplicates,return=representation"
            query = self._query({"on_conflict": ignore_duplicates_on})
        result = http_json("POST", f"{self.base}/{table}{query}", headers=self._headers(prefer), body=row)
        return result if isinstance(result, list) else []

    def update(self, table: str, filters: dict, fields: dict):
        result = http_json(
            "PATCH",
            f"{self.base}/{table}{self._query(filters)}",
            headers=self._headers("return=representation"),
            body=fields,
        )
        return result if isinstance(result, list) else []

    def rpc(self, name: str, args: dict):
        return http_json("POST", f"{self.base}/rpc/{name}", headers=self._headers(), body=args)


class Database(PostgREST):
    """Operações de domínio do motor sobre o Supabase."""

    # ---- configuração (linhas únicas) ----
    def get_agent_config(self) -> dict:
        rows = self.select("agent_config", {"id": "eq.1", "limit": "1"})
        return rows[0] if rows else {}

    def get_schedule_settings(self) -> dict:
        rows = self.select("schedule_settings", {"id": "eq.1", "limit": "1"})
        return rows[0] if rows else {}

    def list_closers(self, active_only: bool = True) -> list:
        params = {"order": "name.asc"}
        if active_only:
            params["active"] = "eq.true"
        return self.select("closers", params)

    def get_closer(self, closer_id: str) -> dict | None:
        rows = self.select("closers", {"id": f"eq.{closer_id}", "limit": "1"})
        return rows[0] if rows else None

    # ---- leads ----
    def get_lead(self, lead_id: str) -> dict | None:
        rows = self.select("leads", {"id": f"eq.{lead_id}", "limit": "1"})
        return rows[0] if rows else None

    def get_lead_by_phone(self, phone: str) -> dict | None:
        rows = self.select("leads", {"phone": f"eq.{phone}", "limit": "1"})
        return rows[0] if rows else None

    def create_lead(self, phone: str, name: str | None = None, source: str = "whatsapp") -> dict:
        row = {"phone": phone, "source": source}
        if name:
            row["name"] = name
        created = self.insert("leads", row, ignore_duplicates_on="phone")
        if created:
            return created[0]
        return self.get_lead_by_phone(phone) or {}

    def update_lead(self, lead_id: str, fields: dict) -> dict:
        rows = self.update("leads", {"id": f"eq.{lead_id}"}, fields)
        return rows[0] if rows else {}

    def active_leads(self, limit: int = 1000) -> list:
        """Leads em andamento (fora de opt-out e de estágios finais), TODOS: pagina até esgotar.

        `limit` é o tamanho da página (o PostgREST corta em 1000). Ordem por id: estável entre páginas.
        """
        rows: list = []
        seen: set = set()
        last_id = None
        while True:
            params = {
                "opt_out": "eq.false",
                "stage": "not.in.(" + ",".join(TERMINAL_STAGES) + ")",
                "order": "id.asc",
                "limit": str(limit),
            }
            if last_id is not None:
                params["id"] = f"gt.{last_id}"   # keyset: inserção no meio da varredura não duplica nem pula
            page = self.select("leads", params)
            for row in page:
                if row.get("id") not in seen:
                    seen.add(row.get("id"))
                    rows.append(row)
            if len(page) < limit or not page:
                return rows
            last_id = page[-1].get("id")

    # ---- mensagens ----
    def add_message(self, lead_id, direction, author, body, media_type=None, wa_id=None, created_at=None) -> bool:
        """Grava a mensagem. Devolve False quando o wa_id já existia (idempotência)."""
        row = {"lead_id": lead_id, "direction": direction, "author": author, "body": body}
        if media_type:
            row["media_type"] = media_type
        if wa_id:
            row["wa_id"] = wa_id
        if created_at:
            row["created_at"] = iso(created_at) if isinstance(created_at, datetime) else created_at
        return bool(self.insert("messages", row, ignore_duplicates_on="wa_id" if wa_id else None))

    def known_wa_ids(self, ids: list) -> set:
        valid = [i for i in ids if valid_wa_id(i)]
        for bad in (i for i in ids if i and not valid_wa_id(i)):
            _log.warning("wa_id fora do formato (descartado da consulta): %.40r", bad)
        ids = valid
        if not ids:
            return set()
        # só [A-Za-z0-9_-] chega aqui: nada a escapar (aspas, barra e vírgula já foram barradas)
        quoted = ",".join('"' + i + '"' for i in ids)
        rows = self.select("messages", {"wa_id": f"in.({quoted})", "select": "wa_id"})
        return {r["wa_id"] for r in rows}

    def recent_messages(self, lead_id: str, limit: int = 20) -> list:
        rows = self.select(
            "messages",
            {"lead_id": f"eq.{lead_id}", "order": "created_at.desc,id.desc", "limit": str(limit)},
        )
        return list(reversed(rows))

    def agent_messages_since(self, lead_id: str, since: datetime, limit: int = 50) -> list:
        """Mensagens de saída do AGENTE a este lead desde `since` (só ids; consulta barata para o teto por hora)."""
        return self.select("messages", {"lead_id": f"eq.{lead_id}", "direction": "eq.out", "author": "eq.agente",
                                        "created_at": f"gte.{iso(since)}", "select": "id", "limit": str(limit)})

    # ---- objeções ----
    def add_objection(self, lead_id: str, category: str, excerpt: str) -> None:
        self.insert("objections", {"lead_id": lead_id, "category": category, "excerpt": excerpt})

    def list_objections(self, lead_id: str) -> list:
        return self.select("objections", {"lead_id": f"eq.{lead_id}", "order": "created_at.asc"})

    # ---- agenda ----
    def available_slots(self, p_from: datetime, p_to: datetime, p_closer: str | None = None) -> list:
        args = {"p_from": iso(p_from), "p_to": iso(p_to)}
        if p_closer:
            args["p_closer"] = p_closer
        result = self.rpc("available_slots", args)
        return result if isinstance(result, list) else []

    def book_slot(self, lead_id, starts_at: datetime, source="agente", closer_id=None, notes=None) -> dict:
        args = {"p_lead": lead_id, "p_starts_at": iso(starts_at), "p_source": source}
        if closer_id:
            args["p_closer"] = closer_id
        if notes:
            args["p_notes"] = notes
        result = self.rpc("book_slot", args)
        return result if isinstance(result, dict) else {"ok": False, "error": "resposta_invalida"}

    def reschedule_slot(self, appointment_id, starts_at: datetime, closer_id=None) -> dict:
        """Troca ATÔMICA no banco (RPC reschedule_slot): se o novo horário falhar, o antigo continua intacto."""
        args = {"p_appointment": appointment_id, "p_starts_at": iso(starts_at)}
        if closer_id:
            args["p_closer"] = closer_id
        result = self.rpc("reschedule_slot", args)
        return result if isinstance(result, dict) else {"ok": False, "error": "resposta_invalida"}

    def active_appointment(self, lead_id: str, now: datetime | None = None) -> dict | None:
        """Agendamento ativo = status agendado/confirmado, SEM filtro de horário (igual ao índice único do banco):
        uma conversa que já começou e não foi encerrada continua sendo o agendamento do lead."""
        rows = self.select(
            "appointments",
            [
                ("lead_id", f"eq.{lead_id}"),
                ("status", "in.(agendado,confirmado)"),
                ("order", "starts_at.asc"),
                ("limit", "1"),
            ],
        )
        return rows[0] if rows else None

    def upcoming_appointments(self, now: datetime) -> list:
        """Agendamentos ativos que ainda não começaram (base dos lembretes e do repasse)."""
        return self.select(
            "appointments",
            [("status", "in.(agendado,confirmado)"), ("starts_at", f"gte.{iso(now)}"), ("order", "starts_at.asc")],
        )

    def update_appointment(self, appointment_id: str, fields: dict) -> dict:
        rows = self.update("appointments", {"id": f"eq.{appointment_id}"}, fields)
        return rows[0] if rows else {}

    def confirm_appointment_if_scheduled(self, appointment_id: str) -> bool:
        """Confirma SÓ se o agendamento ainda está 'agendado' (PATCH condicional). True = 1 linha afetada."""
        rows = self.update("appointments", {"id": f"eq.{appointment_id}", "status": "eq.agendado"},
                           {"status": "confirmado"})
        return bool(rows)

    # ---- outbox ----
    def pending_outbox(self, limit: int = 20, phones=None) -> list:
        """Pendentes mais antigas primeiro. phones (modo teste): só esses destinos, via filtro PostgREST."""
        params = {"status": "eq.pendente", "order": "id.asc", "limit": str(limit)}
        if phones is not None:
            values = set()
            for p in phones:
                raw = str(p or "").strip()
                if raw:
                    values.add(raw)
                    values.add(raw.lstrip("+"))
            quoted = ",".join('"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"' for v in sorted(values))
            params["phone"] = f"in.({quoted})"
        return self.select("outbox", params)

    def claim_outbox(self, row: dict) -> bool:
        """Marca 'enviando' só se ainda estiver 'pendente'. Lista vazia = outro processo pegou."""
        won = self.update(
            "outbox",
            {"id": f"eq.{row['id']}", "status": "eq.pendente"},
            {"status": "enviando", "attempts": int(row.get("attempts") or 0) + 1},
        )
        return bool(won)

    def finish_outbox(self, outbox_id, *, status: str, wa_key_id: str | None = None, error: str | None = None,
                      sent_at: datetime | None = None, attempts: int | None = None) -> None:
        fields = {"status": status}
        if attempts is not None:
            fields["attempts"] = attempts
        if wa_key_id:
            fields["wa_key_id"] = wa_key_id
        if error is not None:
            fields["last_error"] = error[:500]
        if sent_at:
            fields["sent_at"] = iso(sent_at)
        self.update("outbox", {"id": f"eq.{outbox_id}"}, fields)

    def outbox_for_lead_since(self, lead_id, since) -> list:
        """Linhas da outbox do lead criadas a partir de `since` (qualquer status)."""
        return self.select("outbox", {"lead_id": f"eq.{lead_id}", "created_at": f"gte.{iso(since)}",
                                      "order": "id.asc", "limit": "50"})

    def outbox_for_phone_since(self, phone: str, since) -> list:
        """Linhas da outbox desse telefone (E.164, com ou sem '+') criadas a partir de `since`."""
        raw = str(phone or "").strip()
        values = ",".join(sorted({raw, raw.lstrip("+")} - {""}))
        return self.select("outbox", {"phone": f"in.({values})", "created_at": f"gte.{iso(since)}",
                                      "order": "id.asc", "limit": "50"})

    def stuck_outbox(self) -> list:
        return self.select("outbox", {"status": "eq.enviando"})

    def enqueue_outbox(self, phone: str, body: str, lead_id=None, created_by: str = "motor",
                       client_key: str | None = None, status: str | None = None,
                       last_error: str | None = None) -> dict:
        """Com client_key (índice único da outbox) o insert ignora duplicata: devolve {} se a chave já existia.
        `status` (ex.: 'erro') faz a linha nascer já nesse estado, sem passar por 'pendente'."""
        row = {"phone": phone, "body": body, "created_by": created_by}
        if status:
            row["status"] = status
        if last_error is not None:
            row["last_error"] = last_error
        if lead_id:
            row["lead_id"] = lead_id
        if client_key:
            row["client_key"] = client_key
            created = self.insert("outbox", row, ignore_duplicates_on="client_key")
        else:
            created = self.insert("outbox", row)
        return created[0] if created else {}

    def outbox_by_wa_key(self, wa_key_id: str) -> dict | None:
        """Linha da outbox cujo key.id da Evolution é este (qualquer status); None se não houver."""
        if not valid_wa_id(wa_key_id):
            return None
        rows = self.select("outbox", {"wa_key_id": f"eq.{wa_key_id}", "limit": "1"})
        return rows[0] if rows else None

    def outbox_by_client_key(self, client_key: str) -> dict | None:
        rows = self.select("outbox", {"client_key": f"eq.{client_key}", "limit": "1"})
        return rows[0] if rows else None

    # ---- conexão do WhatsApp (painel pede, motor executa) ----
    def get_whatsapp_connection(self) -> dict | None:
        """Estado pela RPC whatsapp_status: traz o relógio e as idades calculados no BANCO.
        Função ausente (migration não aplicada) = None; o motor avisa e a reconexão pelo painel fica desligada."""
        try:
            result = self.rpc("whatsapp_status", {})
        except HttpError as exc:
            if exc.status == 404:
                return None
            raise
        if isinstance(result, list):
            result = result[0] if result else None
        return result if isinstance(result, dict) and result.get("id") is not None else None

    def update_whatsapp_connection(self, fields: dict, command_id: str | None = None) -> int:
        """PATCH da linha única. Com command_id, só vale se o comando ainda é o mesmo; devolve as linhas afetadas."""
        filters = {"id": "eq.1"}
        if command_id:
            filters["command_id"] = f"eq.{command_id}"
        return len(self.update("whatsapp_connection", filters, fields))

    # ---- diagnóstico ----
    def ping(self) -> str:
        """Confere acesso às tabelas e às RPCs do contrato. Devolve texto de detalhe."""
        cfg = self.get_agent_config()
        if not cfg:
            raise RuntimeError("tabela agent_config sem a linha única (id=1): aplique as migrations")
        now = datetime.now().astimezone()
        self.available_slots(now, now)
        return f"agente '{cfg.get('agent_name', '?')}'"
