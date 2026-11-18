"""Contexto compartilhado entre os módulos (injeção de dependências, facilita os testes)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .util import utcnow


@dataclass
class Ctx:
    cfg: Any                    # MotorConfig
    db: Any                     # Database (PostgREST) ou falso nos testes
    evo: Any                    # EvolutionClient ou falso
    ai: Any                     # AIClient ou falso
    messenger: Any              # Messenger
    log: logging.Logger
    now_fn: Callable[[], datetime] = utcnow
    sleep_fn: Callable[[float], None] = time.sleep
    gcal: Any = None            # ponte Google Calendar (opcional)
    # Estado em memória da execução (não é regra de negócio)
    state: dict = field(default_factory=dict)

    def now(self) -> datetime:
        return self.now_fn()
