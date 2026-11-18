"""Testes da passada 2 do instalador (decisoes: Auth admin, reinstalacao segura, stop, ia, Windows)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "setup"))

import _comum  # noqa: E402
import etapa_banco  # noqa: E402
import etapa_config  # noqa: E402
import servico  # noqa: E402

CONFIG = {"supabase": {"url": "https://abcdefghijklmnopqrst.supabase.co", "service_role_key": "svc"}}


def setUpModule():
    from tests.setup import _isolamento
    _isolamento.ligar()


def tearDownModule():
    from tests.setup import _isolamento
    _isolamento.desligar()


def _rest_dono(config, metodo, caminho, *a, **k):
    """PostgREST falso do fluxo de dono: POST/DELETE ok e nenhuma outra linha em panel_users."""
    return (200, []) if metodo == "GET" or metodo == "DELETE" else (201, None)


def _posts(rest):
    return [c for c in rest.call_args_list if c.args[1] == "POST"]


class Dono(unittest.TestCase):
    def _args(self):
        return mock.Mock(email="dono@exemplo.com", dry_run=False, redefinir_senha=False)

    def test_cria_pela_auth_admin_e_grava_panel_users_pelo_id(self):
        chamadas = []

        def http(metodo, url, **kw):
            chamadas.append((metodo, url, kw.get("corpo")))
            if metodo == "GET":
                return 200, {"users": []}
            return 200, {"id": "uuid-novo"}
        rest = mock.Mock(side_effect=_rest_dono)
        with mock.patch.object(etapa_banco, "load_config", return_value=CONFIG), \
                mock.patch.object(etapa_banco, "http_json", side_effect=http), \
                mock.patch.object(etapa_banco, "supabase_rest", rest), \
                mock.patch.object(etapa_banco, "_senha_dono", return_value="senha-longa-123"), \
                mock.patch.object(etapa_banco, "salvar_estado"):
            self.assertEqual(etapa_banco.cmd_dono(self._args()), 0)
        post = [c for c in chamadas if c[0] == "POST"][0]
        self.assertTrue(post[1].endswith("/auth/v1/admin/users"))
        self.assertIs(post[2]["email_confirm"], True)
        self.assertEqual(_posts(rest)[0].args[3], [{"user_id": "uuid-novo", "email": "dono@exemplo.com"}])
        self.assertIn("on_conflict=user_id", _posts(rest)[0].args[2])

    def test_conta_existente_exige_senha_e_redefine_pela_admin_api(self):
        chamadas = []

        def http(metodo, url, **kw):
            chamadas.append((metodo, url, kw.get("corpo")))
            if metodo == "GET":
                return 200, {"users": [{"id": "uuid-velho", "email": "Dono@Exemplo.com"}]}
            return 200, {}
        rest = mock.Mock(side_effect=_rest_dono)
        with mock.patch.object(etapa_banco, "load_config", return_value=CONFIG), \
                mock.patch.object(etapa_banco, "http_json", side_effect=http), \
                mock.patch.object(etapa_banco, "supabase_rest", rest), \
                mock.patch.object(etapa_banco, "_senha_dono", return_value="senha-longa-123"), \
                mock.patch.object(etapa_banco, "salvar_estado"):
            self.assertEqual(etapa_banco.cmd_dono(self._args()), 0)
        put = [c for c in chamadas if c[0] == "PUT"]
        self.assertEqual(len(put), 1)
        self.assertTrue(put[0][1].endswith("/auth/v1/admin/users/uuid-velho"))
        self.assertEqual(put[0][2], {"password": "senha-longa-123", "email_confirm": True})
        self.assertEqual(_posts(rest)[0].args[3][0]["user_id"], "uuid-velho")

    def test_conta_existente_sem_senha_nao_libera_painel(self):
        def http(metodo, url, **kw):
            self.assertEqual(metodo, "GET")
            return 200, {"users": [{"id": "uuid-velho", "email": "dono@exemplo.com"}]}
        rest = mock.Mock(return_value=(201, None))
        with mock.patch.object(etapa_banco, "load_config", return_value=CONFIG), \
                mock.patch.object(etapa_banco, "http_json", side_effect=http), \
                mock.patch.object(etapa_banco, "supabase_rest", rest), \
                mock.patch.object(etapa_banco, "_senha_dono", return_value=None), \
                mock.patch.object(etapa_banco, "salvar_estado"):
            self.assertNotEqual(etapa_banco.cmd_dono(self._args()), 0)
        rest.assert_not_called()

    def test_conta_existente_sem_terminal_nem_variavel_sai_diferente_de_zero(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(_comum, "CONFIG_PATH", Path(d) / "config.json"), \
                mock.patch.dict("os.environ", {}, clear=False), \
                mock.patch.object(_comum, "sem_terminal", return_value=True), \
                mock.patch.object(etapa_banco, "load_config", return_value=CONFIG), \
                mock.patch.object(etapa_banco, "http_json", return_value=(200, {"users": [{"id": "u", "email": "dono@exemplo.com"}]})), \
                mock.patch.object(etapa_banco, "supabase_rest") as rest:
            import os
            os.environ.pop("SDR_OWNER_PASSWORD", None)
            with self.assertRaises(SystemExit) as cm:
                etapa_banco.cmd_dono(self._args())
            self.assertNotEqual(cm.exception.code, 0)
            rest.assert_not_called()


class ConfigInvalido(unittest.TestCase):
    def test_atualizar_config_nao_sobrescreve_json_invalido(self):
        with tempfile.TemporaryDirectory() as d:
            caminho = Path(d) / "config.json"
            caminho.write_text('{"supabase": {"url": "x",}}', encoding="utf-8")
            with mock.patch.object(_comum, "CONFIG_PATH", caminho):
                with self.assertRaises(SystemExit):
                    _comum.atualizar_config({"ai": {"provider": "gemini"}})
            self.assertEqual(caminho.read_text(encoding="utf-8"), '{"supabase": {"url": "x",}}')

    def test_estado_invalido_vira_copia_e_recomeca(self):
        with tempfile.TemporaryDirectory() as d:
            caminho = Path(d) / "estado.json"
            caminho.write_text("{quebrado", encoding="utf-8")
            with mock.patch.object(_comum, "ESTADO_PATH", caminho):
                _comum.salvar_estado({"a": 1})
            self.assertEqual(json.loads(caminho.read_text(encoding="utf-8")), {"a": 1})
            self.assertTrue(Path(str(caminho) + ".invalido").exists())

    def test_http_json_nao_lanca_em_httpexception(self):
        import http.client
        with mock.patch.object(_comum._OPENER, "open", side_effect=http.client.IncompleteRead(b"x")):
            st, msg = _comum.http_json("GET", "https://x.invalid/")
        self.assertEqual(st, 0)


class StopMac(unittest.TestCase):
    def _stop(self, retornos):
        ex = servico.Executor(False)
        with mock.patch.object(servico, "sistema", return_value="Darwin"), \
                mock.patch.object(servico.Executor, "cmd", side_effect=retornos):
            return servico.controlar(ex, "stop")

    def test_stop_falha_se_servico_continua_carregado(self):
        self.assertEqual(self._stop([(5, "erro de io"), (0, ""), (0, "state = running")]), 1)

    def test_stop_ok_quando_bootout_funciona(self):
        self.assertEqual(self._stop([(0, ""), (0, ""), (113, "not found")]), 0)

    def test_stop_ok_se_ja_estava_parado(self):
        self.assertEqual(self._stop([(3, "no such process"), (0, ""), (113, "not found")]), 0)


class Windows(unittest.TestCase):
    def test_install_usa_xml_e_nao_linha_tr_longa(self):
        ex = servico.Executor(False)
        comandos = []
        longo = "C:\\Users\\Nome Sobrenome Muito Longo\\OneDrive\\Documentos\\" + "pasta\\" * 20
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(servico, "sistema", return_value="Windows"), \
                mock.patch.object(servico, "preparar_instalacao", return_value=True), \
                mock.patch.object(servico, "python_cmd", return_value=longo + "python.exe"), \
                mock.patch.object(servico, "REPO_ROOT", Path(longo)), \
                mock.patch.object(servico, "LOG_DIR", Path(d)), \
                mock.patch.object(servico.Executor, "cmd", side_effect=lambda c, tolerar=False: comandos.append(c) or (0, "")), \
                mock.patch.object(servico, "_varrer_completo", return_value=([], [], [], True)), \
                mock.patch.object(servico, "motor_subiu", return_value=True), \
                mock.patch.object(servico, "salvar_estado"):
            self.assertEqual(servico.instalar(ex), 0)
        criar = comandos[0]
        self.assertNotIn("/TR", criar)
        self.assertIn("/XML", criar)
        self.assertTrue(all(len(str(x)) <= 261 for x in criar))


class AplicarNoBanco(unittest.TestCase):
    def _payload(self):
        return {"agent_config": {"agent_name": "A", "niche": "n"},
                "schedule_settings": {"call_minutes": 30, "timezone": "America/Sao_Paulo", "public_enabled": True},
                "closers": [{"name": "Ana", "active": True, "weight": 1,
                             "availability": [{"weekday": 1, "start_time": "09:00:00", "end_time": "18:00:00"}]}]}

    def _rodar(self, respostas):
        chamadas = []

        def rest(config, metodo, caminho, corpo=None, extra_headers=None, timeout=None):
            chamadas.append((metodo, caminho, corpo))
            return respostas(metodo, caminho)
        with mock.patch.object(etapa_config, "supabase_rest", side_effect=rest):
            resultado = etapa_config.aplicar_no_banco({}, self._payload(), ("agenda", "closers"))
        return resultado, chamadas

    def _existente(self, metodo, caminho):
        if metodo == "GET" and caminho.startswith("/schedule_settings"):
            return 200, [{"id": 1}]
        if metodo == "GET" and caminho.startswith("/closers"):
            return 200, [{"id": "c1", "name": "Ana", "active": False}]
        if metodo == "GET" and caminho.startswith("/availability_rules"):
            return 200, [{"id": "velha"}]
        if metodo == "POST" and caminho.startswith("/availability_rules"):
            return 201, [{"id": "nova"}]
        return 204, None

    def test_reinstalar_nao_reativa_closer_nem_pagina_publica(self):
        resultado, chamadas = self._rodar(self._existente)
        self.assertTrue(resultado)
        upsert = [c for c in chamadas if c[0] == "POST" and c[1].startswith("/schedule_settings")][0]
        self.assertNotIn("public_enabled", upsert[2][0])
        patch = [c for c in chamadas if c[0] == "PATCH"][0]
        self.assertNotIn("active", patch[2])

    def test_agenda_nova_envia_public_enabled(self):
        def novo(metodo, caminho):
            if metodo == "GET" and caminho.startswith("/schedule_settings"):
                return 200, []
            return self._existente(metodo, caminho)
        _, chamadas = self._rodar(novo)
        upsert = [c for c in chamadas if c[0] == "POST" and c[1].startswith("/schedule_settings")][0]
        self.assertIn("public_enabled", upsert[2][0])

    def test_regras_novas_entram_antes_de_apagar_as_antigas(self):
        _, chamadas = self._rodar(self._existente)
        ordem = [(m, c.split("?")[0]) for m, c, _ in chamadas if c.startswith("/availability_rules")]
        self.assertLess(ordem.index(("POST", "/availability_rules")), ordem.index(("DELETE", "/availability_rules")))
        apagar = [c for m, c, _ in chamadas if m == "DELETE"][0]
        self.assertIn("velha", apagar)
        self.assertNotIn("nova", apagar)

    def test_falha_ao_gravar_novas_nao_apaga_antigas(self):
        def falha(metodo, caminho):
            if metodo == "POST" and caminho.startswith("/availability_rules"):
                return 500, "erro"
            return self._existente(metodo, caminho)
        resultado, chamadas = self._rodar(falha)
        self.assertFalse(resultado)
        self.assertFalse([c for c in chamadas if c[0] == "DELETE"])


class IA(unittest.TestCase):
    def _cmd(self, patch_resp, leitura=None):
        args = mock.Mock(provider="openai", model=None)
        salvo = {"supabase": CONFIG["supabase"], "ai": {"provider": "openai", "api_key": "k-123456"}}

        def rest(config, metodo, caminho, corpo=None, extra_headers=None, timeout=None):
            return patch_resp if metodo == "PATCH" else (leitura or (200, [{"ai_provider": "openai"}]))
        with mock.patch.object(etapa_config, "load_config", side_effect=[CONFIG, salvo]), \
                mock.patch.object(etapa_config, "carregar_entrevista", return_value=None), \
                mock.patch.object(etapa_config, "segredo", return_value="k-123456"), \
                mock.patch.object(etapa_config, "atualizar_config"), \
                mock.patch.object(etapa_config, "supabase_rest", side_effect=rest):
            return etapa_config.cmd_ia(args)

    def test_falha_no_banco_nao_reporta_sucesso(self):
        self.assertEqual(self._cmd((500, "erro")), 1)

    def test_linha_inexistente_nao_reporta_sucesso(self):
        self.assertEqual(self._cmd((200, [])), 1)

    def test_sucesso_so_depois_de_reler(self):
        self.assertEqual(self._cmd((200, [{"ai_provider": "openai"}])), 0)
        self.assertEqual(self._cmd((200, [{"ai_provider": "openai"}]), leitura=(200, [{"ai_provider": "gemini"}])), 1)


class Validar(unittest.TestCase):
    def test_horario_que_nao_e_objeto_vira_erro_sem_traceback(self):
        d = etapa_config.entrevista_modelo()
        d["closers"][0]["horarios"] = ["seg 09:00-18:00"]
        erros = etapa_config.validar_entrevista(d)
        self.assertTrue(any("horarios[0]" in e for e in erros))


if __name__ == "__main__":
    unittest.main()
