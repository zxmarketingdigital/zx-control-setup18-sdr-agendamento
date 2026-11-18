"""Rodada 28 (instalador): o rollback do config.json grava com mkstemp 0600 e nunca deixa tmp com segredo para trás."""
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "setup"))

import _comum  # noqa: E402
import etapa_config  # noqa: E402


def setUpModule():
    from tests.setup import _isolamento
    _isolamento.ligar()


def tearDownModule():
    from tests.setup import _isolamento
    _isolamento.desligar()


class RestaurarConfigSeguro(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.cfg = Path(self.d.name) / "config.json"
        self.cfg.write_bytes(b'{"novo": 1}')
        p = mock.patch.object(_comum, "CONFIG_PATH", str(self.cfg))
        p.start()
        self.addCleanup(p.stop)

    def _sobras(self):
        return [p.name for p in Path(self.d.name).iterdir() if p.name != "config.json"]

    def test_replace_falha_devolve_false_sem_tmp(self):
        with mock.patch.object(etapa_config.os, "replace", side_effect=PermissionError("em uso")):
            self.assertFalse(etapa_config._restaurar_config_bytes(b'{"antigo": 1}'))
        self.assertEqual(self._sobras(), [])
        self.assertEqual(self.cfg.read_bytes(), b'{"novo": 1}')

    def test_sucesso_restaura_sem_sobra(self):
        self.assertTrue(etapa_config._restaurar_config_bytes(b'{"antigo": 1}'))
        self.assertEqual(self.cfg.read_bytes(), b'{"antigo": 1}')
        self.assertEqual(self._sobras(), [])

    @unittest.skipIf(os.name == "nt", "modo POSIX")
    def test_tmp_nasce_com_0600(self):
        modos = []
        real = os.replace

        def espia(origem, destino):
            modos.append(stat.S_IMODE(os.stat(origem).st_mode))
            return real(origem, destino)

        with mock.patch.object(etapa_config.os, "replace", side_effect=espia):
            self.assertTrue(etapa_config._restaurar_config_bytes(b"{}"))
        self.assertEqual(modos, [0o600])

    def test_gitignore_cobre_tmp(self):
        linhas = (RAIZ / ".gitignore").read_text(encoding="utf-8").split()
        for esperado in ("config/.tmp-*", "config/*.tmp", "config/config.json"):
            self.assertIn(esperado, linhas)


if __name__ == "__main__":
    unittest.main()
