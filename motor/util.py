"""Funções pequenas compartilhadas: datas, fuso, telefone e templates."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo

try:  # zoneinfo faz parte da stdlib; no Windows precisa do pacote tzdata
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

PHONE_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")
_FRACTION_RE = re.compile(r"\.(\d+)")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value) -> datetime | None:
    """Lê timestamptz do PostgREST (ISO 8601, com Z ou offset, fração variável)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00").replace(" ", "T", 1)
    text = _FRACTION_RE.sub(lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def get_tz(name: str | None) -> tzinfo:
    """Fuso do negócio. Sem base de fusos (Windows sem tzdata), cai no fuso de Brasília fixo."""
    name = name or "America/Sao_Paulo"
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 - base de fusos ausente ou nome inválido
            pass
    if name == "America/Sao_Paulo":
        return timezone(timedelta(hours=-3))
    return timezone.utc


def digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def to_e164(value: str | None) -> str:
    """Só dígitos (formato da Evolution) -> E.164 com '+'. Vazio se não parecer telefone."""
    number = digits(value)
    candidate = "+" + number if number else ""
    return candidate if PHONE_RE.match(candidate) else ""


def first_name(name: str | None) -> str:
    parts = (name or "").strip().split()
    return parts[0].capitalize() if parts else ""


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render(template: str, **values) -> str:
    """Preenche {chave} sem quebrar quando a chave não existe nem quando há chaves soltas."""
    try:
        return template.format_map(_SafeDict(**values))
    except (ValueError, IndexError, KeyError):
        out = template
        for key, val in values.items():
            out = out.replace("{" + key + "}", str(val))
        return out


def fmt_date(moment: datetime, tz: tzinfo) -> str:
    return moment.astimezone(tz).strftime("%d/%m/%Y")


def fmt_time(moment: datetime, tz: tzinfo) -> str:
    return moment.astimezone(tz).strftime("%H:%M")


WEEKDAYS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


def slot_label(moment: datetime, tz: tzinfo, now: datetime | None = None) -> str:
    """Rótulo humano de um horário, ex.: 'quinta, 20/11, às 14:00'."""
    local = moment.astimezone(tz)
    today = (now or utcnow()).astimezone(tz).date()
    if local.date() == today:
        day = "hoje"
    elif local.date() == today + timedelta(days=1):
        day = "amanhã"
    else:
        day = WEEKDAYS[local.weekday()]
    return f"{day}, {local.strftime('%d/%m')}, às {local.strftime('%H:%M')}"
