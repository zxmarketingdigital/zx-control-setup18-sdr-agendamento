"""Rodada 4: teste guiado nunca deixa teste_ok=True se interrompido; testes isolados do config local."""
import sys
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "setup"))

import _comum  # noqa: E402
import teste_guiado  # noqa: E402

CONFIG = {"supabase": {"url": "https://abcdefghijklmnopqrst.supabase.co", "service_role_key": "svc"}}


def setUpModule():
    from tests.setup import _isolamento
    _isolamento.ligar()


def tearDownModule():
    from tests.setup import _isolamento
    _isolamento.desligar()


class TesteInterrompido(unittest.TestCase):
    def _rodar(self, efeito):
        _comum.salvar_estado({"teste_ok": True})
        with mock.patch.object(teste_guiado, "load_config", return_value=CONFIG), \
                mock.patch.dict(teste_guiado.FUNCOES, {"modo-teste": mock.Mock(side_effect=efeito)}):
            return teste_guiado.main(["--passos", "modo-teste"])

    def test_ctrl_c_deixa_teste_ok_falso(self):
        self.assertEqual(self._rodar(KeyboardInterrupt()), 130)
        self.assertIs(_comum.carregar_estado().get("teste_ok") if hasattr(_comum, "carregar_estado") else _comum.ler_json(_comum.ESTADO_PATH).get("teste_ok"), False)

    def test_systemexit_no_meio_deixa_teste_ok_falso(self):
        self.assertNotEqual(self._rodar(SystemExit(2)), 0)
        self.assertIs(_comum.ler_json(_comum.ESTADO_PATH).get("teste_ok"), False)

    def test_marcador_antigo_cai_antes_do_primeiro_passo(self):
        vistos = []
        _comum.salvar_estado({"teste_ok": True})

        def passo(config, ctx):
            vistos.append(_comum.ler_json(_comum.ESTADO_PATH).get("teste_ok"))
            return False
        with mock.patch.object(teste_guiado, "load_config", return_value=CONFIG), \
                mock.patch.dict(teste_guiado.FUNCOES, {"modo-teste": passo}):
            teste_guiado.main(["--passos", "modo-teste"])
        self.assertEqual(vistos, [False])


class Isolamento(unittest.TestCase):
    def test_caminho_do_config_e_temporario(self):
        self.assertNotIn("config/config.json", str(_comum.CONFIG_PATH).replace("\\", "/").replace(str(RAIZ).replace("\\", "/"), ""))
        self.assertFalse(_comum.CONFIG_PATH.exists())


if __name__ == "__main__":
    unittest.main()
