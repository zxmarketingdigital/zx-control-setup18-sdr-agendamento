"""Rodada 23 (instalador): validar_entrevista confere o fuso com zoneinfo; base indisponível só avisa."""
import copy
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "setup"))

import etapa_config  # noqa: E402


def setUpModule():
    from tests.setup import _isolamento
    _isolamento.ligar()


def tearDownModule():
    from tests.setup import _isolamento
    _isolamento.desligar()


def _entrevista(fuso):
    d = copy.deepcopy(etapa_config.entrevista_modelo())
    d["agenda"]["fuso"] = fuso
    return d


def _erros_fuso(erros):
    return [e for e in erros if "fuso" in e]


class FusoExiste(unittest.TestCase):
    def test_inexistente_reprova(self):
        erros = _erros_fuso(etapa_config.validar_entrevista(_entrevista("America/Sao_Paolo")))
        self.assertEqual(len(erros), 1)
        self.assertIn("fuso horário 'America/Sao_Paolo' não existe", erros[0])
        self.assertIn("America/Sao_Paulo", erros[0])

    def test_existente_passa(self):
        self.assertEqual(_erros_fuso(etapa_config.validar_entrevista(_entrevista("America/Sao_Paulo"))), [])

    def test_base_indisponivel_so_avisa(self):
        import zoneinfo
        buf = io.StringIO()
        with mock.patch.object(zoneinfo, "ZoneInfo", side_effect=zoneinfo.ZoneInfoNotFoundError("x")), \
                redirect_stdout(buf):
            erros = etapa_config.validar_entrevista(_entrevista("America/Sao_Paolo"))
        self.assertEqual(_erros_fuso(erros), [])
        self.assertIn("não foi possível conferir o fuso neste computador", buf.getvalue())

    def test_formato_invalido_segue_reprovando(self):
        erros = _erros_fuso(etapa_config.validar_entrevista(_entrevista("lixo!!")))
        self.assertEqual(len(erros), 1)
        self.assertIn("agenda.fuso inválido", erros[0])

    def test_banco_usa_mesma_validacao(self):
        d = _entrevista("America/Sao_Paolo")
        buf = io.StringIO()
        with mock.patch.object(etapa_config, "carregar_entrevista", return_value=d), \
                mock.patch.object(etapa_config, "supabase_rest") as rest, redirect_stdout(buf):
            rc = etapa_config.cmd_banco(mock.Mock(partes=None, dry_run=False, sobrescrever=False, desativar_fora=False))
        self.assertEqual(rc, 1)
        rest.assert_not_called()
        self.assertIn("não existe", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
