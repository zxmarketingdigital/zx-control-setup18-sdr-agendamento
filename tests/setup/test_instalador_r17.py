"""Rodada 17 (instalador): motor já rodando não dá falso erro na instalação (identidade do PID, restart no Linux, kill da
árvore no Windows), lista vazia de qualificação é vazio explícito, entrevista com tipo errado vira erro de validação,
_which_seguro não suspeita de ancestrais do cwd e a anon é testada antes de gravar (tudo mockado)."""
import argparse
import json
import os
import sys
import tempfile
import time
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


class MotorJaRodando(unittest.TestCase):
    def test_motor_subiu_aceita_pid_do_motor_com_trava_antiga(self):
        with tempfile.TemporaryDirectory() as d:
            lock = Path(d) / "motor.lock"
            lock.write_text("4321", encoding="utf-8")
            os.utime(lock, (time.time() - 86400, time.time() - 86400))  # trava de ontem: o motor não regravou
            with mock.patch.object(servico, "identidade_do_pid", return_value="motor"):
                self.assertTrue(servico.motor_subiu(time.time(), lock, espera=0))
            for quem in ("outro", "morto"):
                with mock.patch.object(servico, "identidade_do_pid", return_value=quem):
                    self.assertFalse(servico.motor_subiu(time.time() - 100000, lock, espera=0), quem)

    def _instalar_linux(self, ativa):
        comandos = []

        def cmd(self_, comando, tolerar=False):
            comandos.append(list(comando))
            if comando[:3] == ["systemctl", "--user", "is-active"]:
                return (0 if ativa else 3), ""
            return 0, ""
        estados = []
        with mock.patch.object(servico, "sistema", return_value="Linux"), \
                mock.patch.object(servico, "preparar_instalacao", return_value=True), \
                mock.patch.object(servico.Executor, "arquivo"), mock.patch.object(servico.Executor, "cmd", cmd), \
                mock.patch.object(servico, "identidade_do_pid", return_value="motor"), \
                mock.patch.object(servico, "_checar_motor_alheio", return_value=0), \
                mock.patch.object(servico, "_encerrar_motores_proprios", return_value=0), \
                mock.patch.object(servico, "pid_do_servico", return_value=(4321, True)), \
                mock.patch.object(servico, "LOCK_FILE", Path(tempfile.mkdtemp()) / "motor.lock"), \
                mock.patch.object(servico, "salvar_estado", side_effect=estados.append):
            servico.LOCK_FILE.write_text("4321")
            os.utime(servico.LOCK_FILE, (time.time() - 86400, time.time() - 86400))
            codigo = servico.instalar(servico.Executor(False))
        return codigo, comandos, estados

    def test_linux_com_unit_ativa_reinicia_e_conclui_com_sucesso(self):
        codigo, comandos, estados = self._instalar_linux(True)
        self.assertEqual(codigo, 0)
        self.assertIn(["systemctl", "--user", "restart", servico.UNIT_LINUX], comandos)
        self.assertEqual(estados[-1], {"servico_instalado": True})

    def test_linux_com_unit_parada_nao_reinicia(self):
        codigo, comandos, _ = self._instalar_linux(False)
        self.assertEqual(codigo, 0)
        self.assertNotIn(["systemctl", "--user", "restart", servico.UNIT_LINUX], comandos)

    def test_windows_encerra_a_arvore_do_motor_vivo_antes_do_run(self):
        comandos = []

        def cmd(self_, comando, tolerar=False):
            comandos.append([str(c) for c in comando])
            return 0, ""
        estados = []
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(servico, "sistema", return_value="Windows"), \
                mock.patch.object(servico, "preparar_instalacao", return_value=True), \
                mock.patch.object(servico, "LOG_DIR", Path(d)), \
                mock.patch.object(servico.Executor, "cmd", cmd), \
                mock.patch.object(servico, "ler_pid_motor", return_value=4321), \
                mock.patch.object(servico, "identidade_do_pid", return_value="motor"), \
                mock.patch.object(servico, "_varrer_completo", return_value=([], [], [], True)), \
                mock.patch.object(servico, "motor_subiu", return_value=True), \
                mock.patch.object(servico, "registrar_identidade_motor", return_value=True), \
                mock.patch.object(servico, "salvar_estado", side_effect=estados.append):
            codigo = servico.instalar(servico.Executor(False))
        self.assertEqual(codigo, 0)
        kill = [i for i, c in enumerate(comandos) if c[:4] == ["taskkill", "/T", "/F", "/PID"] and c[4] == "4321"]
        run = [i for i, c in enumerate(comandos) if c[:2] == ["schtasks", "/Run"]]
        self.assertTrue(kill and run and kill[0] < run[0], comandos)
        self.assertEqual(estados[-1], {"servico_instalado": True})


def _entrevista(**mudar):
    d = etapa_config.entrevista_modelo()
    d["negocio"] = {"empresa": "Clínica Sol", "oferta": "Consulta de avaliação"}
    d["dono"] = {"whatsapp": "+5511988887777", "email": "dona@clinicasol.com.br"}
    d["closers"][0].update({"nome": "Marina", "whatsapp": "+5511977776666"})
    d["telefones_teste"] = ["+5511966665555"]
    d.update(mudar)
    return d


class QualificacaoVazia(unittest.TestCase):
    def test_lista_vazia_e_gravada_como_vazia(self):
        d = _entrevista(qualificacao={"perguntas": [], "desqualificar": []})
        self.assertEqual(etapa_config.validar_entrevista(d), [])
        ac = etapa_config.montar(d)["agent_config"]
        self.assertEqual(ac["qualification_questions"], [])
        self.assertEqual(ac["disqualify_rules"], [])

    def test_null_ou_ausente_usa_o_padrao_do_nicho(self):
        for qual in ({"perguntas": None, "desqualificar": None}, {}):
            ac = etapa_config.montar(_entrevista(qualificacao=qual))["agent_config"]
            self.assertTrue(ac["qualification_questions"])
            self.assertTrue(ac["disqualify_rules"])


class TipoErradoNaEntrevista(unittest.TestCase):
    def test_dono_texto_e_telefones_numero_viram_erro_sem_traceback(self):
        for campo, valor in (("dono", "+5511999990000"), ("negocio", "Clínica"), ("telefones_teste", 5)):
            d = _entrevista()
            d[campo] = valor
            erros = etapa_config.validar_entrevista(d)  # não pode levantar
            self.assertTrue(erros, campo)

    def test_telefones_teste_numero_tem_mensagem_propria(self):
        erros = etapa_config.validar_entrevista(_entrevista(telefones_teste=5))
        self.assertTrue(any("telefones_teste deve ser uma lista" in e for e in erros), erros)


class WhichSeguro(unittest.TestCase):
    def _fake(self, d, nome):
        f = Path(d) / nome
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
        f.chmod(0o755)
        return f

    def test_rodar_do_home_aceita_cli_em_pasta_abaixo_dele(self):
        with tempfile.TemporaryDirectory() as home:
            exe = self._fake(Path(home) / ".nvm" / "bin", "supabase")
            antigo = os.getcwd()
            os.chdir(home)
            self.addCleanup(os.chdir, antigo)
            with mock.patch.dict(os.environ, {"PATH": str(exe.parent)}):
                self.assertEqual(_comum.achar_cli("supabase"), [str(exe)])

    def test_binario_dentro_do_repo_continua_recusado(self):
        with mock.patch.dict(os.environ, {"PATH": str(RAIZ / "setup")}):
            self.assertIsNone(_comum.achar_cli("estado.py"))


class AnonTestadaAntesDeGravar(unittest.TestCase):
    URL = "https://abcdefghijklmnopqrst.supabase.co"

    def _rodar(self, status_anon):
        os.environ["SDR_SUPABASE_SERVICE_ROLE_KEY"] = "sb_secret_abc"
        self.addCleanup(os.environ.pop, "SDR_SUPABASE_SERVICE_ROLE_KEY", None)
        args = argparse.Namespace(url=self.URL, anon_key="sb_publishable_outro", trocar_chaves=True, dry_run=False)
        chamadas = []

        def http(metodo, url, headers=None, corpo=None, timeout=None):
            chamadas.append((metodo, url, headers))
            return status_anon, "x"
        with mock.patch.object(etapa_banco, "supabase_rest", return_value=(200, {})), mock.patch.object(etapa_banco, "http_json", http), \
                mock.patch.object(etapa_banco, "atualizar_config") as grava, mock.patch.object(etapa_banco, "erro") as erro, \
                mock.patch.object(etapa_banco, "ok"):
            codigo = etapa_banco.cmd_credenciais(args)
        return codigo, grava, erro, chamadas

    def test_anon_de_outro_projeto_nao_grava(self):
        for st in (401, 403):
            codigo, grava, erro, _ = self._rodar(st)
            self.assertEqual(codigo, 1, st)
            grava.assert_not_called()
            self.assertIn("anon não é deste projeto", erro.call_args[0][0])

    def test_anon_valida_grava_e_usa_a_propria_chave(self):
        codigo, grava, _, chamadas = self._rodar(200)
        self.assertEqual(codigo, 0)
        grava.assert_called_once()
        metodo, url, headers = chamadas[0]
        self.assertEqual((metodo, url), ("GET", self.URL + "/auth/v1/settings"))
        self.assertEqual(headers["apikey"], "sb_publishable_outro")
        self.assertNotIn("Authorization", headers)


if __name__ == "__main__":
    unittest.main()
