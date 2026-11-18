"""Rodada 5: parada confirmada do motor, troca de provedor de IA, banco_migrado, closers e credenciais."""
import argparse
import os
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


def setUpModule():
    from tests.setup import _isolamento
    _isolamento.ligar()


def tearDownModule():
    from tests.setup import _isolamento
    _isolamento.desligar()


def _jwt(role):
    import base64
    import json
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return "%s.%s.assinatura" % (b({"alg": "HS256"}), b({"role": role}))


class FalsoSO:
    """Simula tasklist/taskkill/schtasks: o motor (PID 4321) morre só se o taskkill rodar (ou se `morre_sozinho`)."""

    def __init__(self, vivo=True, taskkill_mata=True, morre_sozinho=False):
        self.vivo = vivo
        self.taskkill_mata = taskkill_mata
        self.morre_sozinho = morre_sozinho
        self.chamadas = []
        self.tarefa = True

    def __call__(self, comando, **kw):
        self.chamadas.append(list(comando))
        if comando[0] == "tasklist":
            return (0, '"python.exe","4321","Console","1","20,000 K"') if self.vivo else (0, "INFO: No tasks are running")
        if comando[0] == "powershell":
            return 0, "C:\\Python\\python.exe -m motor run"
        if comando[0] == "taskkill":
            if self.taskkill_mata:
                self.vivo = False
            return 0, "SUCCESS"
        if comando[:2] == ["schtasks", "/Delete"]:
            self.tarefa = False
        if comando[:2] == ["schtasks", "/Query"]:
            return (0, "tarefa") if self.tarefa else (1, "ERRO: o sistema não pode encontrar o arquivo")
        if comando[0] == "schtasks" and self.morre_sozinho:
            self.vivo = False
        return 0, ""

    def usou(self, programa, *args):
        return any(c[0] == programa and all(a in c for a in args) for c in self.chamadas)


class ParadaDoMotor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock = Path(self.tmp.name) / "motor.lock"
        self.lock.write_text("4321")
        self.addCleanup(self.tmp.cleanup)
        for p in (mock.patch.object(servico, "LOCK_FILE", self.lock),
                  mock.patch.object(servico, "sistema", return_value="Windows"),
                  mock.patch.object(servico, "salvar_estado"),
                  mock.patch.object(servico, "_dono_do_processo", return_value=True)):  # r10: foco aqui não é o dono do processo
            p.start()
            self.addCleanup(p.stop)

    def _rodar(self, falso, acao):
        with mock.patch.object(servico, "rodar", falso), mock.patch.object(servico.time, "sleep", lambda s: None):
            ex = servico.Executor(False)
            return servico.remover(ex) if acao == "uninstall" else servico.controlar(ex, acao)

    def test_stop_windows_mata_a_arvore_do_motor(self):
        falso = FalsoSO()
        self.assertEqual(self._rodar(falso, "stop"), 0)
        self.assertTrue(falso.usou("taskkill", "/T", "/F", "4321"))
        self.assertFalse(falso.vivo)

    def test_stop_windows_motor_que_nao_morre_e_erro(self):
        falso = FalsoSO(taskkill_mata=False)
        with mock.patch.object(servico, "garantir_motor_parado", wraps=servico.garantir_motor_parado) as g:
            codigo = self._rodar(falso, "stop")
        self.assertEqual(codigo, 1)
        self.assertTrue(g.called)

    def test_uninstall_windows_encerra_motor_antes_de_apagar_a_tarefa(self):
        falso = FalsoSO()
        self.assertEqual(self._rodar(falso, "uninstall"), 0)
        i_kill = next(i for i, c in enumerate(falso.chamadas) if c[0] == "taskkill")
        i_del = next(i for i, c in enumerate(falso.chamadas) if c[:2] == ["schtasks", "/Delete"])
        self.assertLess(i_kill, i_del)

    def test_uninstall_windows_com_motor_vivo_falha_e_nao_diz_sucesso(self):
        falso = FalsoSO(taskkill_mata=False)
        self.assertEqual(self._rodar(falso, "uninstall"), 1)
        self.assertFalse(falso.usou("schtasks", "/Delete"))
        servico.salvar_estado.assert_not_called()

    def test_pid_de_outro_programa_nao_leva_taskkill(self):
        falso = FalsoSO()
        outro = lambda c, **kw: (0, '"chrome.exe","4321","Console","1","1 K"') if c[0] == "tasklist" else falso(c, **kw)  # noqa: E731
        self.assertEqual(self._rodar(outro, "stop"), 0)
        self.assertFalse(falso.usou("taskkill"))

    def test_stop_mac_com_motor_vivo_apos_bootout_e_erro(self):
        def so_mac(c, **kw):
            if c[0] == "launchctl" and c[1] == "print":
                return 113, "not found"
            if c[0] == "ps":
                return 0, ("4321" if "pid=" in c else "python -m motor run")
            return 0, ""
        with mock.patch.object(servico, "sistema", return_value="Darwin"), \
                mock.patch.object(servico, "rodar", so_mac), mock.patch.object(servico.time, "sleep", lambda s: None), \
                mock.patch.object(servico, "ESPERA_SUBIR", 0):
            codigo = servico.garantir_motor_parado(servico.Executor(False), espera=0)
        self.assertEqual(codigo, 1)


class TrocaDeProvedor(unittest.TestCase):
    def setUp(self):
        _comum.atualizar_config({"ai": {"provider": "gemini", "api_key": "k", "model": "gemini-2.5-flash"}})
        os.environ["SDR_AI_API_KEY"] = "chave-nova"
        self.addCleanup(os.environ.pop, "SDR_AI_API_KEY", None)

    def _ia(self, **kw):
        return etapa_config.cmd_ia(argparse.Namespace(provider=kw.get("provider"), model=kw.get("model")))

    def test_trocar_provedor_sem_model_limpa_o_modelo(self):
        self.assertEqual(self._ia(provider="openai"), 0)
        ai = _comum.load_config()["ai"]
        self.assertEqual((ai["provider"], ai["model"]), ("openai", ""))

    def test_mesmo_provedor_sem_model_mantem_o_modelo(self):
        self.assertEqual(self._ia(provider="gemini"), 0)
        self.assertEqual(_comum.load_config()["ai"]["model"], "gemini-2.5-flash")

    def test_modelo_de_outro_provedor_e_recusado(self):
        self.assertEqual(self._ia(provider="openai", model="gemini-2.5-flash"), 2)
        self.assertEqual(_comum.load_config()["ai"]["provider"], "gemini")


class BancoMigrado(unittest.TestCase):
    def test_verificacao_falha_nao_deixa_banco_migrado_true(self):
        _comum.salvar_estado({"banco_migrado": True})
        cfg = {"supabase": {"url": "https://abcdefghijklmnopqrst.supabase.co", "service_role_key": "x", "project_ref": "abcdefghijklmnopqrst"}}
        args = argparse.Namespace(dry_run=False)
        with mock.patch.object(etapa_banco, "load_config", return_value=cfg), \
                mock.patch.object(etapa_banco, "achar_cli", return_value=["supabase"]), \
                mock.patch.object(etapa_banco, "entrada_externa", return_value="senha"), \
                mock.patch.object(etapa_banco, "rodar", return_value=(0, "ok")), \
                mock.patch.object(etapa_banco, "supabase_rest", return_value=(404, "nao existe")):
            self.assertEqual(etapa_banco.cmd_migrar(args), 1)
        self.assertIs(_comum.ler_json(_comum.ESTADO_PATH).get("banco_migrado"), False)


class ClosersDuplicados(unittest.TestCase):
    def test_nome_repetido_com_espaco_ou_caixa_diferente(self):
        d = etapa_config.entrevista_modelo()
        base = d["closers"][0]
        d["closers"] = [dict(base, nome="Ana"), dict(base, nome="Ana "), dict(base, nome="ANA")]
        erros = [e for e in etapa_config.validar_entrevista(d) if "repetido" in e]
        self.assertEqual(len(erros), 2)


class CredenciaisInvalidas(unittest.TestCase):
    def test_401_nao_grava_a_chave_e_orienta_trocar_chaves(self):
        url = "https://abcdefghijklmnopqrst.supabase.co"
        os.environ["SDR_SUPABASE_SERVICE_KEY"] = _jwt("service_role")
        self.addCleanup(os.environ.pop, "SDR_SUPABASE_SERVICE_KEY", None)
        args = argparse.Namespace(url=url, trocar_chaves=False, anon_key=_jwt("anon"), dry_run=False)
        saida = []
        with mock.patch.object(etapa_banco, "supabase_rest", return_value=(401, "no")), \
                mock.patch.object(etapa_banco, "erro", side_effect=saida.append):
            self.assertEqual(etapa_banco.cmd_credenciais(args), 1)
        self.assertFalse(_comum.cfg(_comum.load_config(), "supabase", "service_role_key"))
        self.assertIn("--trocar-chaves", saida[-1])


if __name__ == "__main__":
    unittest.main()
