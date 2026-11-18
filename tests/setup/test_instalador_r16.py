"""Rodada 16 (instalador): config.json inválido não deixa o banco divergir (cmd_ia), migração invalida o estado antes do push,
start/restart no Windows exigem o motor de pé, resolver_exe nunca usa o diretório atual, http remoto da Evolution exige
--permitir-http, validação de blocos com tipo errado e a função whatsapp_status na verificação (tudo mockado)."""
import argparse
import json
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

SUPA = {"url": "https://abcdefghijklmnopqrst.supabase.co", "service_role_key": "x"}


def setUpModule():
    from tests.setup import _isolamento
    _isolamento.ligar()


def tearDownModule():
    from tests.setup import _isolamento
    _isolamento.desligar()


class IaConfigInvalido(unittest.TestCase):
    def setUp(self):
        os.environ["SDR_AI_API_KEY"] = "chave-nova"
        self.addCleanup(os.environ.pop, "SDR_AI_API_KEY", None)

    def test_json_invalido_nao_toca_no_banco(self):
        _comum.CONFIG_PATH.write_text('{"supabase": {"url": "x",}}')  # vírgula sobrando
        chamadas = []

        def rest(config, metodo, caminho, *a, **k):
            chamadas.append(metodo)
            return 200, []
        with mock.patch.object(etapa_config, "supabase_rest", rest), mock.patch.object(etapa_config, "load_config",
                                                                                       return_value={"supabase": SUPA}):
            codigo = etapa_config.cmd_ia(argparse.Namespace(provider="openai", model=None))
        self.assertEqual(codigo, 1)
        self.assertNotIn("PATCH", chamadas)

    def test_systemexit_no_config_nao_toca_no_banco_e_relanca(self):
        _comum.CONFIG_PATH.write_text(json.dumps({"supabase": SUPA, "ai": {"provider": "gemini"}}))
        patches = []

        def rest(config, metodo, caminho, corpo=None, *a, **k):
            if metodo == "PATCH":
                patches.append(corpo["ai_provider"])
                return 204, [{"ok": 1}]
            return 200, [{"ai_provider": patches[-1] if patches else "gemini"}]
        with mock.patch.object(etapa_config, "supabase_rest", rest), \
                mock.patch.object(etapa_config, "atualizar_config", side_effect=SystemExit("config quebrou")), \
                mock.patch.object(etapa_config, "erro") as erro:
            with self.assertRaises(SystemExit):
                etapa_config.cmd_ia(argparse.Namespace(provider="openai", model=None))
        self.assertEqual(patches, [])  # r19: o config falhou antes do PATCH



class MigrarInvalidaAntes(unittest.TestCase):
    def test_estado_zerado_antes_do_push_e_falha_mantem_false(self):
        _comum.salvar_estado({"teste_ok": True, "banco_migrado": True})
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [f.unlink() for f in d.glob("*")] and d.rmdir())
        (d / "20270101000001_x.sql").write_text("create table t(id int);")
        cfg = {"supabase": {**SUPA, "project_ref": "abcdefghijklmnopqrst"}}
        vistos = []

        def rodar(cmd, **kw):
            if cmd[-1] == "push":
                vistos.append(dict(_comum.load_estado()))
                return 1, "falhou no meio"
            return 0, "Would push these migrations:\n • 20270101000001_x.sql"
        with mock.patch.object(etapa_banco, "MIGRATIONS_DIR", d), mock.patch.object(etapa_banco, "load_config", return_value=cfg), \
                mock.patch.object(etapa_banco, "achar_cli", return_value=["supabase"]), \
                mock.patch.object(etapa_banco, "entrada_externa", return_value="senha"), \
                mock.patch.object(etapa_banco, "rodar", rodar), mock.patch.object(etapa_banco, "erro"), \
                mock.patch.object(etapa_banco, "instrucoes_sql_editor"):
            codigo = etapa_banco.cmd_migrar(argparse.Namespace(dry_run=False, confirmar_destrutivo=False))
        self.assertEqual(codigo, 1)
        self.assertFalse(vistos[0]["teste_ok"])
        self.assertFalse(vistos[0]["banco_migrado"])
        self.assertFalse(_comum.load_estado()["banco_migrado"])
        self.assertFalse(_comum.load_estado()["teste_ok"])


class ServicoWindowsExigeMotor(unittest.TestCase):
    def _controlar(self, acao, subiu):
        ex = servico.Executor(False)
        with mock.patch.object(servico, "sistema", return_value="Windows"), \
                mock.patch.object(servico, "rodar", return_value=(0, "")), \
                mock.patch.object(servico, "encerrar_dono_da_trava"), \
                mock.patch.object(servico, "garantir_motor_parado", return_value=0), \
                mock.patch.object(servico, "motor_subiu", return_value=subiu), \
                mock.patch.object(servico, "registrar_identidade_motor", return_value=True), \
                mock.patch.object(servico, "erro") as erro:
            return servico.controlar(ex, acao), erro

    def test_start_e_restart_sem_motor_retornam_1(self):
        for acao in ("start", "restart"):
            codigo, erro = self._controlar(acao, False)
            self.assertEqual(codigo, 1, acao)
            self.assertIn("logs/motor.log", erro.call_args[0][0])

    def test_start_com_motor_retorna_0(self):
        self.assertEqual(self._controlar("start", True)[0], 0)

    def test_falha_so_de_identidade_tem_mensagem_propria_e_nao_falha(self):
        ex = servico.Executor(False)
        with mock.patch.object(servico, "sistema", return_value="Windows"), mock.patch.object(servico, "rodar", return_value=(0, "")), \
                mock.patch.object(servico, "motor_subiu", return_value=True), \
                mock.patch.object(servico, "registrar_identidade_motor", return_value=False), \
                mock.patch.object(servico, "aviso") as aviso, mock.patch.object(servico, "erro") as erro:
            self.assertEqual(servico.controlar(ex, "start"), 0)
        self.assertIn("identidade", aviso.call_args[0][0])
        erro.assert_not_called()


class ResolverExeSemCwd(unittest.TestCase):
    def test_windows_usa_system32_mesmo_com_taskkill_bat_no_cwd(self):
        d = tempfile.mkdtemp()
        (Path(d) / "taskkill.bat").write_text("@echo plantado")
        (Path(d) / "powershell.exe").write_text("x")
        antigo = os.getcwd()
        os.chdir(d)
        self.addCleanup(os.chdir, antigo)
        env = {"SystemRoot": "C:\\Windows", "PATH": d}
        with mock.patch.object(_comum, "_eh_windows", return_value=True), mock.patch.dict(os.environ, env):
            for nome, fim in (("taskkill", "System32\\taskkill.exe"), ("tasklist", "System32\\tasklist.exe"),
                              ("schtasks", "System32\\schtasks.exe"),
                              ("powershell", "System32\\WindowsPowerShell\\v1.0\\powershell.exe")):
                r = _comum.resolver_exe(nome)
                self.assertTrue(r.startswith("C:\\Windows\\"), r)
                self.assertTrue(r.endswith(fim), r)
            # nome que não é do sistema e só existe no cwd/PATH relativo: não resolve para o diretório atual
            self.assertNotIn(d, _comum.resolver_exe("git"))

    def test_posix_nao_resolve_pelo_cwd(self):
        d = tempfile.mkdtemp()
        exe = Path(d) / "ferramenta-plantada"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        antigo = os.getcwd()
        os.chdir(d)
        self.addCleanup(os.chdir, antigo)
        with mock.patch.dict(os.environ, {"PATH": "." + os.pathsep + d}):
            self.assertEqual(_comum.resolver_exe("ferramenta-plantada"), "ferramenta-plantada")

    def test_caminho_explicito_passa_direto(self):
        self.assertEqual(_comum.resolver_exe(sys.executable), sys.executable)


class EvolutionHttpRemoto(unittest.TestCase):
    def _rodar(self, url, **extra):
        args = argparse.Namespace(url=url, instance="sdr", criar=False, trocar_chave=False, **extra)
        with mock.patch.object(etapa_config, "segredo", return_value="k"), \
                mock.patch.object(etapa_config, "_estado_instancia", return_value=(200, "open", {})), \
                mock.patch.object(etapa_config, "atualizar_config"), mock.patch.object(etapa_config, "erro") as erro:
            return etapa_config.cmd_evolution(args), erro

    def test_http_remoto_recusado(self):
        codigo, erro = self._rodar("http://203.0.113.5:8080")
        self.assertEqual(codigo, 2)
        self.assertIn("sem criptografia", erro.call_args[0][0])

    def test_http_remoto_com_flag_passa(self):
        self.assertEqual(self._rodar("http://203.0.113.5:8080", permitir_http=True)[0], 0)

    def test_https_e_localhost_passam(self):
        for u in ("https://evo.exemplo.com", "http://localhost:8080", "http://127.0.0.1:8080", "http://[::1]:8080"):
            self.assertEqual(self._rodar(u)[0], 0, u)


class EntrevistaTipoErrado(unittest.TestCase):
    def _base(self):
        return {"nicho": "clinica", "negocio": {"empresa": "A", "oferta": "B"}, "agente": {"nome": "Bia"},
                "dono": {"whatsapp": "+5511999990000"}, "closers": []}

    def test_blocos_com_tipo_errado_listam_erro_sem_quebrar(self):
        for chave in ("ia", "agenda", "qualificacao"):
            d = self._base()
            d[chave] = "texto"
            erros = etapa_config.validar_entrevista(d)
            self.assertTrue(any(e.startswith(chave + " deve ser um objeto") for e in erros), (chave, erros))
        d = self._base()
        d["ia"] = ["gemini"]
        self.assertTrue(any("ia deve ser um objeto" in e for e in etapa_config.validar_entrevista(d)))

    def test_provedor_padrao_com_entrevista_torta(self):
        for lixo in (["a"], {"ia": "gemini"}, {"ia": ["x"]}, None):
            with mock.patch.object(etapa_config, "carregar_entrevista", return_value=lixo):
                self.assertEqual(etapa_config._provedor_padrao({}), "gemini")
        with mock.patch.object(etapa_config, "carregar_entrevista", return_value={"ia": {"provider": "openai"}}):
            self.assertEqual(etapa_config._provedor_padrao({}), "openai")


class VerificarWhatsappStatus(unittest.TestCase):
    def test_whatsapp_status_esta_nas_rpcs_esperadas(self):
        self.assertIn("whatsapp_status", etapa_banco.RPCS)
        self.assertEqual(len(etapa_banco.RPCS), 7)


if __name__ == "__main__":
    unittest.main()
