"""Testes da rodada 3 do instalador (decisao 6: papel das chaves, ai_provider, closers fora, systemd)."""
import base64
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "setup"))

import _comum  # noqa: E402
import etapa_banco  # noqa: E402
import etapa_config  # noqa: E402
import servico  # noqa: E402
import teste_guiado  # noqa: E402

CONFIG = {"supabase": {"url": "https://abcdefghijklmnopqrst.supabase.co", "service_role_key": "svc"}}


def jwt(role):
    def b(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return "%s.%s.assinatura" % (b({"alg": "HS256"}), b({"role": role, "iss": "supabase"}))


def setUpModule():
    from tests.setup import _isolamento
    _isolamento.ligar()


def tearDownModule():
    from tests.setup import _isolamento
    _isolamento.desligar()


class PapelDasChaves(unittest.TestCase):
    def test_jwt_e_formato_novo(self):
        self.assertEqual(_comum.papel_da_chave(jwt("anon")), "anon")
        self.assertEqual(_comum.papel_da_chave(jwt("service_role")), "service_role")
        self.assertEqual(_comum.papel_da_chave("sb_publishable_abc"), "anon")
        self.assertEqual(_comum.papel_da_chave("sb_secret_abc"), "service_role")
        self.assertIsNone(_comum.papel_da_chave("svc"))
        self.assertIsNone(_comum.papel_da_chave(jwt("authenticated")))

    def test_credenciais_recusa_chaves_trocadas_sem_gravar(self):
        args = mock.Mock(url="https://abcdefghijklmnopqrst.supabase.co", trocar_chaves=True, anon_key=jwt("service_role"), dry_run=False)
        atualizar = mock.Mock()
        with mock.patch.object(etapa_banco, "load_config", return_value={}), \
                mock.patch.object(etapa_banco, "entrada_externa", return_value=jwt("anon")), \
                mock.patch.object(etapa_banco, "atualizar_config", atualizar), \
                mock.patch.object(etapa_banco, "supabase_rest", return_value=(200, [])):
            self.assertEqual(etapa_banco.cmd_credenciais(args), 1)
        atualizar.assert_not_called()

    def test_web_js_recusa_service_role_no_lugar_da_anon(self):
        cfgx = {"supabase": {"url": "u", "anon_key": jwt("service_role"), "service_role_key": jwt("anon")}}
        gravar = mock.Mock()
        args = mock.Mock(demo=False, cf_beacon_token="")
        with mock.patch.object(etapa_config, "load_config", return_value=cfgx), \
                mock.patch.object(etapa_config, "gravar_atomico", gravar):
            self.assertEqual(etapa_config.cmd_web_js(args), 1)
        gravar.assert_not_called()

    def test_web_js_aceita_anon(self):
        cfgx = {"supabase": {"url": "https://abc.supabase.co", "anon_key": jwt("anon"), "service_role_key": jwt("service_role")}}
        gravar = mock.Mock()
        with mock.patch.object(etapa_config, "load_config", return_value=cfgx), \
                mock.patch.object(etapa_config, "gravar_atomico", gravar):
            self.assertEqual(etapa_config.cmd_web_js(mock.Mock(demo=False, cf_beacon_token="")), 0)
        gravar.assert_called_once()


class BancoPreserva(unittest.TestCase):
    def _payload(self):
        d = etapa_config.entrevista_modelo()
        return etapa_config.montar(d)

    def test_sobrescrever_preserva_ai_provider_do_banco(self):
        gravados = []

        def rest(config, metodo, caminho, corpo=None, extra_headers=None, timeout=None):
            if metodo == "GET":
                return 200, [{"ai_provider": "openai"}]
            gravados.append(corpo)
            return 201, None
        with mock.patch.object(etapa_config, "supabase_rest", side_effect=rest):
            self.assertTrue(etapa_config.aplicar_no_banco(CONFIG, self._payload(), ["agente"]))
        self.assertEqual(gravados[0][0]["ai_provider"], "openai")

    def test_closer_ativo_fora_da_entrevista_bloqueia_sem_flag(self):
        escritas = []

        def rest(config, metodo, caminho, corpo=None, extra_headers=None, timeout=None):
            if metodo == "GET":
                return 200, [{"id": "c-velho", "name": "Antiga", "active": True}]
            escritas.append((metodo, caminho))
            return 200, [{"id": "x"}]
        with mock.patch.object(etapa_config, "supabase_rest", side_effect=rest):
            self.assertFalse(etapa_config.aplicar_no_banco(CONFIG, self._payload(), ["closers"]))
        self.assertEqual(escritas, [])

    def test_desativar_fora_desativa_e_segue(self):
        escritas = []

        def rest(config, metodo, caminho, corpo=None, extra_headers=None, timeout=None):
            if metodo == "GET":
                return 200, ([{"id": "c-velho", "name": "Antiga", "active": True}] if "closers" in caminho else [])
            escritas.append((metodo, caminho, corpo))
            return 200, [{"id": "n%d" % i} for i in range(len(corpo) if isinstance(corpo, list) else 1)]
        with mock.patch.object(etapa_config, "supabase_rest", side_effect=rest):
            self.assertTrue(etapa_config.aplicar_no_banco(CONFIG, self._payload(), ["closers"], desativar_fora=True))
        self.assertIn(("PATCH", "/closers?id=eq.c-velho", {"active": False}), [(m, c, b) for m, c, b in escritas if m == "PATCH"])


class Baixos(unittest.TestCase):
    def test_test_only_nulo_e_modo_teste(self):
        ac = {"test_only": None, "paused": False, "test_phones": [], "company": "C", "agent_name": "A"}
        with mock.patch.object(teste_guiado, "supabase_rest", return_value=(200, [ac])), \
                mock.patch.object(teste_guiado, "aviso") as av, mock.patch.object(teste_guiado, "erro"):
            self.assertFalse(teste_guiado.passo_modo_teste({}, {}))
        av.assert_not_called()

    def test_execstart_com_caminho_com_espaco_entre_aspas(self):
        unit = servico.unit_linux("/home/ana/Meus Projetos/.venv/bin/python", "/r", "/l")
        self.assertIn('ExecStart="/home/ana/Meus Projetos/.venv/bin/python" -m motor run', unit)


if __name__ == "__main__":
    unittest.main()
