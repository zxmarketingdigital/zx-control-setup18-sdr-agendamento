"""Conexão do WhatsApp pelo painel: o painel PEDE (tabela whatsapp_connection) e o motor EXECUTA.

A apikey da Evolution só existe no computador do aluno, então só o motor fala com ela. A cada ciclo o motor
confere o estado da instância (heartbeat que o painel usa para saber se o motor está vivo) e atende o comando
pedido ('connect' = ler o QR Code, 'logout' = trocar de número). O QR fica no banco só enquanto vale e nunca
vai para log nem para arquivo.
"""

from __future__ import annotations

import base64
import binascii
import re
from datetime import timedelta

from .evolution import EvolutionError
from .util import digits, iso, parse_ts

CHECK_INTERVAL_SECONDS = 30   # conferência do estado / heartbeat do motor
QR_TTL_SECONDS = 40           # validade de cada QR (a Evolution renova perto disso)
QR_WINDOW_SECONDS = 300       # tempo total que um pedido de conexão fica renovando o QR
COMMAND_TTL_SECONDS = 120     # pedido pendente mais velho que isso (relógio do banco) nunca é executado

ACTIVE = ("pendente", "em_andamento", "aguardando_leitura")
STATES = ("open", "close", "connecting")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
QR_PREFIX = "data:image/png;base64,"
MAX_QR_CHARS = 400_000
_B64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")

MSG_QR_INVALID = "a Evolution não devolveu um QR válido"
MSG_EVOLUTION = "Não consegui falar com a Evolution. Confira se ela está ligada e tente de novo."
MSG_TABLE_HINT = ("tabela whatsapp_connection indisponível: rode `python3 setup/etapa_banco.py migrar` "
                  "(a reconexão pelo painel fica desligada até lá)")


def normalize_qr(raw) -> str | None:
    """Devolve `data:image/png;base64,<b64>` ou None se não for base64 de um PNG. Aceita com ou sem prefixo."""
    if not isinstance(raw, str):
        return None
    text = "".join(raw.split())
    if text.lower().startswith("data:"):
        if not text.lower().startswith(QR_PREFIX):
            return None
        text = text[len(QR_PREFIX):]
    if not text or len(text) > MAX_QR_CHARS or not _B64_RE.match(text):
        return None
    try:
        decoded = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not decoded.startswith(PNG_SIGNATURE):
        return None
    return QR_PREFIX + text


def _qr_from(result) -> tuple[str | None, str | None]:
    """(qr normalizado, código de pareamento) de uma resposta do connect()."""
    if not isinstance(result, dict):
        return None, None
    raw = result.get("base64")
    if raw is None and isinstance(result.get("qrcode"), dict):
        raw = result["qrcode"].get("base64")
    pairing = result.get("pairingCode")
    if not isinstance(pairing, str) or not re.match(r"^[A-Za-z0-9-]{4,20}$", pairing):
        pairing = None
    return normalize_qr(raw), pairing


def _evolution_message(exc: EvolutionError) -> str:
    # texto fixo: a mensagem original pode trazer endereço interno; nunca vai segredo para a tabela
    if exc.status:
        return f"A Evolution respondeu com erro (HTTP {exc.status}). Confira se ela está ligada e tente de novo."
    return MSG_EVOLUTION


def _is_not_found(exc: EvolutionError) -> bool:
    return exc.status == 404


def _is_not_connected(exc: EvolutionError) -> bool:
    return exc.status == 404 or "not connected" in str(exc).lower()


def _simulated(ctx) -> bool:
    if ctx.evo is None or getattr(ctx.messenger, "simulated", False):
        return True
    return type(ctx.db).__name__ == "ReadOnlyDB"


def _fetch_qr(ctx):
    """Pede o QR; 404 cria a instância e pede de novo. Devolve (qr, pairing) ou levanta EvolutionError."""
    try:
        result = ctx.evo.connect()
    except EvolutionError as exc:
        if not _is_not_found(exc):
            raise
        ctx.evo.create_instance()
        result = ctx.evo.connect()
    return _qr_from(result)


def _read_row(ctx):
    mem = ctx.state.setdefault("wa", {})
    try:
        row = ctx.db.get_whatsapp_connection()
    except Exception as exc:  # noqa: BLE001 - tabela ausente (migration não aplicada) ou banco fora
        if not mem.get("missing_logged"):
            mem["missing_logged"] = True
            ctx.log.warning("%s (%s)", MSG_TABLE_HINT, type(exc).__name__)
        return None
    if row is None:
        if not mem.get("missing_logged"):
            mem["missing_logged"] = True
            ctx.log.warning("%s: linha única (id=1) ausente", MSG_TABLE_HINT)
        return None
    mem["missing_logged"] = False
    return row


def _read_state(ctx) -> tuple[str, str]:
    """(estado, número só com dígitos). Falha de leitura vira 'desconhecido'; instância inexistente (404) = close."""
    try:
        state = ctx.evo.connection_state()
    except EvolutionError as exc:
        return ("close" if _is_not_found(exc) else "desconhecido"), ""
    state = state if state in STATES else "desconhecido"
    number = ""
    if state == "open":
        try:
            number = digits(str(ctx.evo.owner_jid() or "").split("@")[0].split(":")[0])
        except Exception:  # noqa: BLE001
            number = ""
    return state, number


class _Superseded(Exception):
    """O comando foi trocado no meio do passo (command_id mudou): este passo para, sem erro."""


def _save(ctx, row: dict, fields: dict) -> None:
    """Escrita que mexe em campo de comando: só vale para o command_id lido. 0 linhas = comando trocado."""
    command_id = row.get("command_id")
    changed = ctx.db.update_whatsapp_connection(fields, command_id=command_id) if command_id \
        else ctx.db.update_whatsapp_connection(fields)
    if isinstance(changed, int) and not isinstance(changed, bool) and changed == 0:
        raise _Superseded()
    row.update(fields)


def _clear_qr() -> dict:
    return {"qr_base64": None, "pairing_code": None, "qr_expires_at": None}


def _num(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _age(row: dict, age_key: str, ts_key: str, snow) -> float | None:
    """Idade em segundos pelo relógio do BANCO (campo calculado no servidor); sem ele, cai no horário gravado."""
    age = _num(row.get(age_key))
    if age is not None:
        return age
    moment = parse_ts(row.get(ts_key))
    return None if moment is None else (snow - moment).total_seconds()


def _fail(ctx, row, message: str) -> None:
    _save(ctx, row, {"command_status": "erro", "command_error": message[:200], **_clear_qr()})
    ctx.log.warning("conexão do WhatsApp: pedido terminou com erro (%s)", message[:120])


def _publish_qr(ctx, row, snow, *, first: bool) -> None:
    try:
        qr, pairing = _fetch_qr(ctx)
    except EvolutionError as exc:
        _fail(ctx, row, _evolution_message(exc))
        return
    if not qr:
        _fail(ctx, row, MSG_QR_INVALID)
        return
    _save(ctx, row, {"qr_base64": qr, "pairing_code": pairing,
                     "qr_expires_at": iso(snow + timedelta(seconds=QR_TTL_SECONDS)),
                     "command_status": "aguardando_leitura", "command_error": None})
    ctx.log.info("conexão do WhatsApp: QR %s (aguardando leitura)", "gerado" if first else "renovado")


def _connected(ctx, row, number: str) -> None:
    fields = {"command_status": "concluido", "command_error": None, **_clear_qr()}
    if number:   # número ilegível: não apaga o já gravado
        fields["owner_number"] = number
    _save(ctx, row, fields)
    ctx.log.info("conexão do WhatsApp: concluída")


def _start(ctx, row, snow, state: str, number: str) -> None:
    """Executa um comando recém-pedido (pendente, ou em_andamento deixado por um motor que caiu no meio)."""
    command = row.get("command")
    _save(ctx, row, {"command_status": "em_andamento", "command_error": None})
    if command == "logout":
        try:
            ctx.evo.logout()
        except EvolutionError as exc:
            if not _is_not_connected(exc):
                _fail(ctx, row, _evolution_message(exc))
                return
        # logout concluído: o comando termina aqui, SEM publicar QR. O dono clica em Reconectar para gerar o QR
        # do número novo (um comando por vez; a RPC recusa pedido enquanto outro está em andamento)
        _save(ctx, row, {"owner_number": None, "state": "close", "command_status": "concluido",
                         "command_error": None, **_clear_qr()})
        ctx.log.info("conexão do WhatsApp: logout concluído (sem QR; use Reconectar)")
        return
    elif state == "open":
        _connected(ctx, row, number)
        return
    _publish_qr(ctx, row, snow, first=True)


def _wait_reading(ctx, row, snow, state: str, number: str) -> None:
    if state == "open":
        _connected(ctx, row, number)
        return
    age = _age(row, "command_age_seconds", "command_requested_at", snow)
    in_window = age is not None and age < QR_WINDOW_SECONDS
    valid = row.get("qr_valid")
    if not isinstance(valid, bool):
        expires = parse_ts(row.get("qr_expires_at"))
        valid = expires is not None and snow < expires
    if valid:
        return   # QR ainda vale
    if not in_window:
        _save(ctx, row, {"command_status": "expirado", **_clear_qr()})
        ctx.log.info("conexão do WhatsApp: QR expirou sem leitura")
        return
    _publish_qr(ctx, row, snow, first=False)


def _server_now(row: dict, now):
    """Relógio do tick = o do banco (server_now), nunca o da máquina: devolve `now` deslocado pelo offset."""
    server = parse_ts(row.get("server_now"))
    return now if server is None else now + (server - now)


def _tick(ctx, row: dict, now) -> None:
    snow = _server_now(row, now)
    mem = ctx.state.setdefault("wa", {})
    status = row.get("command_status")
    active = bool(row.get("command")) and status in ACTIVE
    last = mem.get("last_check")
    if not active and last is not None and (now - last).total_seconds() < CHECK_INTERVAL_SECONDS:
        return
    state, number = _read_state(ctx)
    mem["last_check"] = now
    fields = {"state": state, "checked_at": iso(snow)}
    if state == "open":
        if number:   # owner_jid ilegível: omite o campo e mantém o número já gravado
            fields["owner_number"] = number
    elif state == "close":
        fields["owner_number"] = None
    ctx.db.update_whatsapp_connection(fields)   # heartbeat: incondicional por id=1 (o banco carimba checked_at)
    row.update(fields)
    if not active:
        if state == "open" and (row.get("qr_base64") or row.get("pairing_code") or row.get("qr_expires_at")):
            _save(ctx, row, _clear_qr())   # conectou sem comando ativo: QR residual não fica no banco
        return
    age = _age(row, "command_age_seconds", "command_requested_at", snow)
    if status == "pendente" and age is not None and age >= COMMAND_TTL_SECONDS:
        _save(ctx, row, {"command_status": "expirado", **_clear_qr()})   # velho demais: nunca executa
        ctx.log.info("conexão do WhatsApp: pedido antigo expirou sem ser executado")
        return
    if status in ("pendente", "em_andamento"):
        if status == "em_andamento" and age is not None and age >= QR_WINDOW_SECONDS:
            _save(ctx, row, {"command_status": "expirado", **_clear_qr()})
            return
        _start(ctx, row, snow, state, number)
    else:
        _wait_reading(ctx, row, snow, state, number)


def tick(ctx, now) -> None:
    """Um passo do motor sobre a conexão do WhatsApp. Chamado no início de cada ciclo."""
    if _simulated(ctx):
        return
    row = _read_row(ctx)
    if row is None:
        return
    try:
        _tick(ctx, row, now)
    except _Superseded:
        ctx.log.info("conexão do WhatsApp: o pedido foi trocado no meio; este passo parou")
