"""Leitura e validação de config/config.json (gitignored) e constantes do motor."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config" / "config.json"
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"

DEFAULT_MODELS = {
    "gemini": "gemini-flash-lite-latest",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5",
}
PROVIDERS = tuple(DEFAULT_MODELS)

# Dono assumiu a conversa pelo celular: o agente cala por este tempo (igual ao botão "Assumir" do painel).
TAKEOVER_HOURS = 24


def env_positive(name: str, default: float) -> float:
    """Número positivo vindo de variável de ambiente. Inválido, zero ou negativo cai no padrão."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


class ConfigError(Exception):
    pass


@dataclass
class MotorConfig:
    supabase_url: str = ""
    service_role_key: str = ""
    evolution_url: str = "http://localhost:8080"
    evolution_key: str = ""
    instance: str = ""
    ai_provider: str = "gemini"
    ai_key: str = ""
    ai_model: str = ""
    gcal_url: str = ""
    gcal_secret: str = ""
    poll_seconds: float = 15
    jobs_every_seconds: float = 60
    max_msgs_per_minute: int = 8
    batch_window_seconds: float = 9
    catchup_minutes: int = 360
    data_dir: Path = DATA_DIR
    log_dir: Path = LOG_DIR

    @property
    def model(self) -> str:
        return self.ai_model or DEFAULT_MODELS.get(self.ai_provider, "")

    @property
    def gcal_enabled(self) -> bool:
        return bool(self.gcal_url and self.gcal_secret)

    def problems(self, need_evolution: bool = True) -> list[str]:
        """Lista o que falta para o motor rodar de verdade (texto para o aluno)."""
        out = []
        if not self.supabase_url.startswith("http"):
            out.append("supabase.url vazio ou inválido")
        if not self.service_role_key:
            out.append("supabase.service_role_key vazio")
        if need_evolution:   # `simulate` roda sem Evolution
            if not self.evolution_url.startswith("http"):
                out.append("evolution.url vazio ou inválido")
            if not self.evolution_key:
                out.append("evolution.api_key vazio")
            if not self.instance:
                out.append("evolution.instance vazio")
        if self.ai_provider not in PROVIDERS:
            out.append(f"ai.provider deve ser um de: {', '.join(PROVIDERS)}")
        if not self.ai_key:
            out.append("ai.api_key vazio")
        return out


def load_config(path: str | os.PathLike | None = None) -> MotorConfig:
    config_path = Path(path or os.environ.get("ZX_SDR_CONFIG") or DEFAULT_CONFIG_PATH)
    if not config_path.exists():
        raise ConfigError(
            f"Arquivo de configuração não encontrado: {config_path}. "
            "Copie config/config.exemplo.json para config/config.json e preencha."
        )
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ConfigError(f"config.json inválido (JSON quebrado): {exc}") from exc
    return from_dict(raw)


def from_dict(raw: dict) -> MotorConfig:
    sup = raw.get("supabase") or {}
    evo = raw.get("evolution") or {}
    ai = raw.get("ai") or {}
    gcal = raw.get("google_calendar") or {}
    motor = raw.get("motor") or {}
    cfg = MotorConfig(
        supabase_url=str(sup.get("url", "")).strip().rstrip("/"),
        service_role_key=str(sup.get("service_role_key", "")).strip(),
        evolution_url=(str(evo.get("url", "")).strip() or "http://localhost:8080").rstrip("/"),
        evolution_key=str(evo.get("api_key", "")).strip(),
        instance=str(evo.get("instance", "")).strip(),
        ai_provider=(str(ai.get("provider", "")).strip().lower() or "gemini"),
        ai_key=str(ai.get("api_key", "")).strip(),
        ai_model=str(ai.get("model", "")).strip(),
        gcal_url=str(gcal.get("apps_script_url", "")).strip(),
        gcal_secret=str(gcal.get("secret", "")).strip(),
    )
    for field, key in (
        ("poll_seconds", "poll_seconds"),
        ("jobs_every_seconds", "jobs_every_seconds"),
        ("max_msgs_per_minute", "max_msgs_per_minute"),
        ("batch_window_seconds", "batch_window_seconds"),
        ("catchup_minutes", "catchup_minutes"),
    ):
        if key in motor:
            try:
                value = float(motor[key])
            except (TypeError, ValueError):
                continue
            if value >= 0 and (value > 0 or key == "batch_window_seconds"):
                setattr(cfg, field, int(value) if field in ("max_msgs_per_minute", "catchup_minutes") else value)
    return cfg
