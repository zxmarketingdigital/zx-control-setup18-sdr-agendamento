"""Rodada 18 (instalador): closers repetidos por nome (casefold) no banco e URL nova da Evolution sem chave nova (tudo mockado)."""
import argparse
import os
import sys
import unittest
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


PAYLOAD = {"closers": [{"name": "Ana", "availability": [], "weight": 1}]}


def _rest(linhas, chamadas):
    def rest(config, metodo, caminho, corpo=None, extra_headers=None):
        chamadas.append((metodo, caminho, corpo))
        if metodo == "GET":
            return 200, linhas
        if metodo == "POST":
            return 201, [{"id": "novo"}]
        return 204, None
    return rest


class ClosersRepetidos(unittest.TestCase):
    LINHAS = [
        {"id": "id2", "name": "ana", "active": True, "created_at": "2026-01-02"},
        {"id": "id1", "name": "Ana", "active": True, "created_at": "2026-01-03"},
    ]

    def _rodar(self, desativar):
        chamadas = []
        with mock.patch.object(etapa_config, "supabase_rest", _rest(self.LINHAS, chamadas)), \
                mock.patch.object(etapa_config, "_substituir_regras", return_value=True):
            ok = etapa_config.aplicar_no_banco({}, PAYLOAD, ["closers"], desativar_fora=desativar)
        return ok, chamadas

    def test_dois_ativos_com_mesma_chave_recusa_e_nao_grava(self):
        ok, chamadas = self._rodar(False)
        self.assertFalse(ok)
        self.assertEqual([c for c in chamadas if c[0] != "GET"], [])

    def test_desativar_fora_mantem_nome_exato_e_desativa_a_outra(self):
        ok, chamadas = self._rodar(True)
        self.assertTrue(ok)
        escritas = [c for c in chamadas if c[0] != "GET"]
        self.assertIn(("PATCH", "/closers?id=eq.id2", {"active": False}), escritas)
        patch_ana = [c for c in escritas if c[1] == "/closers?id=eq.id1"]
        self.assertEqual(len(patch_ana), 1)
        self.assertNotIn("active", patch_ana[0][2])

    def test_desativar_fora_sem_nome_exato_mantem_a_mais_antiga(self):
        linhas = [{"id": "b", "name": "ANA", "active": True, "created_at": "2026-02-01"},
                  {"id": "a", "name": "aNa", "active": True, "created_at": "2026-01-01"}]
        chamadas = []
        with mock.patch.object(etapa_config, "supabase_rest", _rest(linhas, chamadas)), \
                mock.patch.object(etapa_config, "_substituir_regras", return_value=True):
            self.assertTrue(etapa_config.aplicar_no_banco({}, PAYLOAD, ["closers"], desativar_fora=True))
        self.assertIn(("PATCH", "/closers?id=eq.b", {"active": False}), chamadas)


class EvolutionUrlNova(unittest.TestCase):
    def _args(self, url):
        return argparse.Namespace(url=url, instance="sdr", trocar_chave=False, criar=False, permitir_http=False)

    def _rodar(self, url, env=None):
        config = {"evolution": {"url": "https://evo.exemplo.com", "api_key": "CHAVE-ANTIGA", "instance": "sdr"}}
        estado = mock.Mock(return_value=(200, "open", {}))
        with mock.patch.object(etapa_config, "load_config", return_value=config), \
                mock.patch.object(etapa_config, "_estado_instancia", estado), \
                mock.patch.object(etapa_config, "atualizar_config"), \
                mock.patch.object(etapa_config, "_invalidar_teste"), \
                mock.patch.object(etapa_config, "_apagar_qr"), \
                mock.patch.dict(os.environ, env or {}):
            codigo = etapa_config.cmd_evolution(self._args(url))
        return codigo, estado

    def test_url_nova_sem_chave_nao_envia_nada(self):
        codigo, estado = self._rodar("https://outro-host.exemplo")
        self.assertNotEqual(codigo, 0)
        estado.assert_not_called()

    def test_url_igual_reaproveita_chave(self):
        codigo, estado = self._rodar("HTTPS://EVO.exemplo.com:443/")
        self.assertEqual(codigo, 0)
        self.assertEqual(estado.call_args[0][1], "CHAVE-ANTIGA")

    def test_url_nova_com_chave_nova_usa_a_nova(self):
        codigo, estado = self._rodar("https://outro-host.exemplo", {"SDR_EVOLUTION_API_KEY": "NOVA"})
        self.assertEqual(codigo, 0)
        self.assertEqual(estado.call_args[0][1], "NOVA")


if __name__ == "__main__":
    unittest.main()
