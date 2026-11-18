"""Isola os testes do config/estado reais do computador: caminhos temporários e sem variáveis SDR_*."""
import os
import sys
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "setup"))

import _comum  # noqa: E402

_patches = []
_NOMES_DE_KILL = {"kill", "pkill", "killall", "taskkill", "taskkill.exe", "launchctl", "systemctl", "schtasks", "schtasks.exe"}
_run_real = subprocess.run


def _kill_proibido(*a, **k):
    raise AssertionError("tentou matar processo real")


def _run_guardado(comando, *a, **k):
    """Guarda da suíte: nenhum teste sinaliza processo nem mexe em serviço de verdade (taskkill/kill/launchctl/...)."""
    argv = comando if isinstance(comando, (list, tuple)) else str(comando).split()
    if argv and os.path.basename(str(argv[0])).lower() in _NOMES_DE_KILL:
        raise AssertionError("tentou matar processo real (ou mexer em serviço real): %s" % " ".join(map(str, argv)))
    return _run_real(comando, *a, **k)

_tmp = None


def ligar():
    global _tmp
    _tmp = tempfile.TemporaryDirectory()
    d = Path(_tmp.name)
    env = {k: v for k, v in os.environ.items() if not k.startswith("SDR_")}
    for p in (mock.patch.object(_comum, "CONFIG_PATH", d / "config.json"),
              mock.patch.object(_comum, "ESTADO_PATH", d / "estado.json"),
              mock.patch.object(os, "kill", _kill_proibido),
              mock.patch.object(os, "killpg", _kill_proibido, create=True),
              mock.patch.object(subprocess, "run", _run_guardado),
              mock.patch.dict(os.environ, env, clear=True)):
        p.start()
        _patches.append(p)


def desligar():
    while _patches:
        _patches.pop().stop()
    if _tmp:
        _tmp.cleanup()
