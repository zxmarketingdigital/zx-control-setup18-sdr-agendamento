"""Limitador de envio (anti-ban) em SQLite local: por minuto, por hora, por dia e intervalo por contato."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .util import utcnow

# Espera máxima por uma vaga de envio antes de desistir (a mensagem fica para o próximo ciclo).
MAX_WAIT_SECONDS = 75


class RateLimiter:
    def __init__(self, db_path: Path | str, max_per_minute: int = 8, max_per_hour: int = 120,
                 max_per_day: int = 600, min_interval_seconds: int = 60, now_fn=utcnow):
        self.db_path = Path(db_path)
        self.max_per_minute = max_per_minute
        self.max_per_hour = max_per_hour
        self.max_per_day = max_per_day
        self.min_interval_seconds = min_interval_seconds
        self.now_fn = now_fn
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS sends (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                         "recipient TEXT NOT NULL, sent_at TEXT NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sends_at ON sends(sent_at)")

    def _connect(self):
        return sqlite3.connect(str(self.db_path))

    def _count_since(self, since: datetime) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM sends WHERE sent_at >= ?", (since.isoformat(),)).fetchone()[0]

    def _last_send(self, phone: str) -> datetime | None:
        with self._connect() as conn:
            row = conn.execute("SELECT sent_at FROM sends WHERE recipient = ? ORDER BY sent_at DESC LIMIT 1",
                               (phone,)).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def wait_seconds(self, phone: str, proactive: bool = False) -> float:
        """0 se pode enviar agora; senão quantos segundos faltam para a próxima vaga."""
        now = self.now_fn()
        waits = [0.0]
        if self.max_per_minute and self._count_since(now - timedelta(minutes=1)) >= self.max_per_minute:
            waits.append(61.0)
        if self._count_since(now - timedelta(hours=1)) >= self.max_per_hour:
            waits.append(3600.0)
        if self._count_since(now - timedelta(days=1)) >= self.max_per_day:
            waits.append(86400.0)
        if proactive:
            last = self._last_send(phone)
            if last:
                gap = self.min_interval_seconds - (now - last).total_seconds()
                if gap > 0:
                    waits.append(gap)
        return max(waits)

    def record(self, phone: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO sends (recipient, sent_at) VALUES (?, ?)", (phone, self.now_fn().isoformat()))
            conn.execute("DELETE FROM sends WHERE sent_at < ?", ((self.now_fn() - timedelta(days=2)).isoformat(),))

    def acquire(self, phone: str, proactive: bool, sleep_fn) -> bool:
        """Espera (até MAX_WAIT_SECONDS) por uma vaga. False = sem vaga agora."""
        waited = 0.0
        while True:
            wait = self.wait_seconds(phone, proactive)
            if wait <= 0:
                return True
            if waited + wait > MAX_WAIT_SECONDS:
                return False
            sleep_fn(wait)
            waited += wait
