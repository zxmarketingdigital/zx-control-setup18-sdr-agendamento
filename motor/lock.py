"""Trava de instância única (data/motor.lock): dois motores respondendo o mesmo lead duplicariam mensagens."""

from __future__ import annotations

import os
from pathlib import Path


class LockError(Exception):
    pass


class InstanceLock:
    """Trava de arquivo do sistema operacional; some sozinha se o processo morrer (sem PID velho para limpar)."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")
        try:
            if os.name == "nt":  # pragma: no cover - testado só no Windows
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise LockError("Já existe um motor rodando neste computador (data/motor.lock). "
                            "Feche o outro antes de iniciar este.") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        if not self._handle:
            return
        try:
            if os.name == "nt":  # pragma: no cover
                import msvcrt
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._handle.close()
        self._handle = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
