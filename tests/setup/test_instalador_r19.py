"""Rodada 19 (instalador): Windows stop desfeito pelo RestartOnFailure (DISABLE/ENABLE da tarefa), nicho que não é texto e
ordem segura do par provedor/chave em `ia` (tudo mockado)."""
import argparse
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "setup"))

import _comum  # noqa: E402
import etapa_config  # noqa: E402
import servico  # noqa: E402

SUPA = {"url": "https://abc.supabase.co", "service_role_key": "k"}


def setUpModule():
    from tests.setup import _isolamento
    _isolamento.ligar()


def tearDownModule():
    from tests.setup import _isolamento
    _isolamento.desligar()


class WindowsDisable(unittest.TestCase):
    def setUp(self):
        self.cmds = []
        self.disable_codigo = 0
        for p in (mock.patch.object(servico, "sistema", return_value="Windows"),
                  mock.patch.object(servico, "encerrar_dono_da_trava", side_effect=lambda ex: self.cmds.append(["<kill-dono>"])),
                  mock.patch.object(servico, "garantir_motor_parado", side_effect=self._garantir),
                  mock.patch.object(servico, "_subir_windows", return_value=0),
                  mock.patch.object(servico, "preparar_instalacao", return_value=True),
                  mock.patch.object(servico, "_registrar_apos_subir", return_value=(True, True)),
                  mock.patch.object(servico, "_checar_motor_alheio", return_value=0),
                  mock.patch.object(servico, "_encerrar_motores_proprios", return_value=0),
                  mock.patch.object(servico, "motor_subiu", return_value=True),
                  mock.patch.object(servico, "salvar_estado"),
                  mock.patch.object(servico, "LOG_DIR", Path(os.devnull).parent),
                  mock.patch.object(servico.Executor, "cmd", lambda ex, comando, tolerar=False: self._cmd(comando)),
                  mock.patch.object(servico, "erro"), mock.patch.object(servico, "ok"), mock.patch.object(servico, "info"),
                  mock.patch.object(servico, "aviso")):
            p.start()
            self.addCleanup(p.stop)

    def _garantir(self, ex, **kw):
        self.cmds.append(["<garantir-parado>"])
        return 0

    def _cmd(self, comando):
        self.cmds.append(list(comando))
        if "/DISABLE" in comando:
            return self.disable_codigo, ""
        if comando[:2] == ["schtasks", "/Query"]:
            return 0, getattr(self, "saida_query", "")
        return 0, ""

    def _t(self, *args):
        return ["schtasks", *args, ]

    def _rotulos(self):
        r = []
        for c in self.cmds:
            if c[0] == "schtasks":
                r.append(c[1] + (":" + c[4] if c[1] == "/Change" else ""))
            else:
                r.append(c[0])
        return r

    def test_stop_ordem_disable_end_kill_garantir(self):
        self.assertEqual(servico.controlar(servico.Executor(False), "stop"), 0)
        self.assertEqual(self._rotulos(), ["/Change:/DISABLE", "/End", "<kill-dono>", "<garantir-parado>"])

    def test_stop_com_disable_falhando_devolve_1_e_avisa_mas_ainda_derruba(self):
        self.disable_codigo = 1
        self.assertEqual(servico.controlar(servico.Executor(False), "stop"), 1)
        self.assertIn("<kill-dono>", self._rotulos())
        msg = servico.erro.call_args[0][0]
        self.assertIn("DISABLE", msg)
        self.assertIn("religar", msg)

    def test_start_habilita_antes_do_run(self):
        servico.controlar(servico.Executor(False), "start")
        r = self._rotulos()
        self.assertEqual(r[:4], ["/Change:/ENABLE", "/End", "<kill-dono>", "/Run"])  # r24: start encerra o motor próprio antes do /Run

    def test_restart_ordem_completa(self):
        servico.controlar(servico.Executor(False), "restart")
        self.assertEqual(self._rotulos(), ["/Change:/DISABLE", "/End", "<kill-dono>", "<garantir-parado>", "/Change:/ENABLE", "/Run"])

    def test_remover_ordem(self):
        with mock.patch.object(servico, "_conferir_removido", return_value=[]):
            self.assertEqual(servico.remover(servico.Executor(False)), 0)
        self.assertEqual(self._rotulos(), ["/Change:/DISABLE", "/End", "<kill-dono>", "<garantir-parado>", "/Delete"])

    def test_install_habilita_e_encerra_a_tarefa_antes_de_matar_o_dono(self):
        with mock.patch.object(Path, "write_text"), mock.patch.object(Path, "mkdir"):
            servico.instalar(servico.Executor(False))
        r = self._rotulos()
        self.assertEqual(r[:5], ["/Create", "/Change:/ENABLE", "/End", "<kill-dono>", "/Run"])

    def test_status_de_tarefa_desabilitada_diz_parado_pelo_stop(self):
        self.saida_query = "Nome da tarefa: x\nEstado da tarefa agendada: Desabilitado\n"
        servico.controlar(servico.Executor(False), "status")
        self.assertIn("desativado pelo stop", servico.aviso.call_args[0][0])

    def test_status_de_tarefa_habilitada_nao_avisa(self):
        self.saida_query = "Scheduled Task State: Enabled\n"
        servico.controlar(servico.Executor(False), "status")
        servico.aviso.assert_not_called()

    def test_xml_mantem_restart_on_failure(self):
        self.assertIn("RestartOnFailure", servico.xml_tarefa_windows("python", RAIZ, RAIZ / "log"))


class NichoNaoTexto(unittest.TestCase):
    def test_lista_vira_erro_sem_traceback(self):
        d = etapa_config.entrevista_modelo()
        d["nicho"] = ["servicos-consultoria"]
        erros = etapa_config.validar_entrevista(d)
        self.assertIn("nicho deve ser texto", erros)

    def test_objeto_e_nulo_tambem(self):
        for v in ({"a": 1}, None, 5):
            d = etapa_config.entrevista_modelo()
            d["nicho"] = v
            self.assertIn("nicho deve ser texto", etapa_config.validar_entrevista(d))


class IaParConsistente(unittest.TestCase):
    def setUp(self):
        self.original = json.dumps({"ai": {"provider": "gemini", "api_key": "k-velha", "model": ""}, "supabase": SUPA}, indent=3)
        _comum.CONFIG_PATH.write_text(self.original, encoding="utf-8")
        os.environ["SDR_AI_API_KEY"] = "chave-nova"
        self.addCleanup(os.environ.pop, "SDR_AI_API_KEY", None)

    def _rodar(self, patch_status):
        vistos = []

        def rest(config, metodo, caminho, corpo=None, *a, **k):
            if metodo == "PATCH":
                vistos.append(_comum.CONFIG_PATH.read_text(encoding="utf-8"))
                return patch_status, ([{"id": 1}] if patch_status in (200, 204) else "erro")
            return 200, [{"ai_provider": "openai" if (patch_status in (200, 204)) else "gemini"}]
        with mock.patch.object(etapa_config, "supabase_rest", rest), mock.patch.object(etapa_config, "erro"):
            codigo = etapa_config.cmd_ia(argparse.Namespace(provider="openai", model=None))
        return codigo, vistos

    def test_patch_falha_restaura_o_config_byte_a_byte(self):
        codigo, vistos = self._rodar(503)
        self.assertEqual(codigo, 1)
        self.assertIn("chave-nova", vistos[0])  # o par novo já estava no config durante o PATCH
        self.assertEqual(_comum.CONFIG_PATH.read_bytes(), self.original.encode("utf-8"))

    def test_patch_ok_deixa_o_par_novo(self):
        codigo, _ = self._rodar(204)
        self.assertEqual(codigo, 0)
        ai = _comum.load_config()["ai"]
        self.assertEqual((ai["provider"], ai["api_key"]), ("openai", "chave-nova"))


if __name__ == "__main__":
    unittest.main()
