"""Rodada 15 (instalador): teste_ok invalidado ANTES da primeira escrita (banco/IA), troca de projeto Supabase zera
teste_ok/banco_migrado e achar_cli nunca resolve a CLI pelo cwd/repositório (subprocess e HTTP mockados)."""
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

SUPA = {"url": "https://abcdefghijklmnopqrst.supabase.co", "service_role_key": "x"}


def setUpModule():
    from tests.setup import _isolamento
    _isolamento.ligar()


def tearDownModule():
    from tests.setup import _isolamento
    _isolamento.desligar()


class InvalidaAntesDaEscrita(unittest.TestCase):
    def setUp(self):
        _comum.salvar_estado({"teste_ok": True})

    def test_banco_falha_no_meio_deixa_false(self):
        _comum.CONFIG_PATH.write_text(json.dumps({"supabase": SUPA}))
        with mock.patch.object(etapa_config, "carregar_entrevista", return_value={"x": 1}), \
             mock.patch.object(etapa_config, "validar_entrevista", return_value=[]), \
             mock.patch.object(etapa_config, "montar", return_value={}), \
             mock.patch.object(etapa_config, "supabase_rest", return_value=(200, [])), \
             mock.patch.object(etapa_config, "aplicar_no_banco", return_value=False):
            codigo = etapa_config.cmd_banco(argparse.Namespace(
                partes=None, dry_run=False, sobrescrever=False, desativar_fora=False))
        self.assertEqual(codigo, 1)
        self.assertFalse(_comum.load_estado().get("teste_ok"))

    def test_banco_dry_run_nao_invalida(self):
        with mock.patch.object(etapa_config, "carregar_entrevista", return_value={"x": 1}), \
             mock.patch.object(etapa_config, "validar_entrevista", return_value=[]), \
             mock.patch.object(etapa_config, "montar", return_value={"agent_config": {}, "schedule_settings": {}, "closers": []}):
            etapa_config.cmd_banco(argparse.Namespace(partes=None, dry_run=True, sobrescrever=False, desativar_fora=False))
        self.assertTrue(_comum.load_estado().get("teste_ok"))

    def test_ia_patch_que_falha_deixa_false(self):
        _comum.CONFIG_PATH.write_text(json.dumps({"supabase": SUPA}))
        os.environ["SDR_AI_API_KEY"] = "chave-nova"
        self.addCleanup(os.environ.pop, "SDR_AI_API_KEY", None)
        with mock.patch.object(etapa_config, "supabase_rest", return_value=(500, "x")):
            codigo = etapa_config.cmd_ia(argparse.Namespace(provider="openai", model=None))
        self.assertNotEqual(codigo, 0)
        self.assertFalse(_comum.load_estado().get("teste_ok"))


class CredenciaisTrocaProjeto(unittest.TestCase):
    def _rodar(self, url, trocar=True):
        os.environ["SDR_SUPABASE_SERVICE_ROLE_KEY"] = "sb_secret_novo"
        self.addCleanup(os.environ.pop, "SDR_SUPABASE_SERVICE_ROLE_KEY", None)
        args = argparse.Namespace(url=url, trocar_chaves=trocar, anon_key="sb_publishable_novo", dry_run=False)
        with mock.patch.object(etapa_banco, "supabase_rest", return_value=(200, {})), \
                mock.patch.object(etapa_banco, "http_json", return_value=(200, {})):
            return etapa_banco.cmd_credenciais(args)

    def test_troca_de_projeto_zera_marcas(self):
        _comum.CONFIG_PATH.write_text(json.dumps({"supabase": {
            "url": SUPA["url"], "service_role_key": "sb_secret_velho", "anon_key": "sb_publishable_velho"}}))
        _comum.salvar_estado({"teste_ok": True, "banco_migrado": True})
        self.assertEqual(self._rodar("https://zzzzzzzzzzzzzzzzzzzz.supabase.co"), 0)
        est = _comum.load_estado()
        self.assertFalse(est.get("teste_ok"))
        self.assertFalse(est.get("banco_migrado"))

    def test_mesmas_credenciais_preservam_marcas(self):
        _comum.CONFIG_PATH.write_text(json.dumps({"supabase": {
            "url": SUPA["url"], "service_role_key": "sb_secret_novo", "anon_key": "sb_publishable_novo"}}))
        _comum.salvar_estado({"teste_ok": True, "banco_migrado": True})
        self.assertEqual(self._rodar(SUPA["url"]), 0)
        est = _comum.load_estado()
        self.assertTrue(est.get("teste_ok"))
        self.assertTrue(est.get("banco_migrado"))


class AcharCliSeguro(unittest.TestCase):
    def _fake(self, d, nome):
        f = Path(d) / nome
        f.write_text("x")
        f.chmod(0o755)
        return f

    def test_ignora_cmd_no_cwd_e_dentro_do_repo(self):
        with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as bom:
            self._fake(cwd, "supabase.cmd")
            antigo = os.getcwd()
            os.chdir(cwd)
            self.addCleanup(os.chdir, antigo)
            # PATH com entrada vazia e '.', como o Windows trata o cwd: não pode achar o supabase.cmd local
            with mock.patch.dict(os.environ, {"PATH": os.pathsep.join(["", ".", bom]), "PATHEXT": ".CMD;.EXE"}):
                self.assertIsNone(_comum.achar_cli("supabase"))

    def test_recusa_caminho_dentro_do_cwd_mesmo_no_path(self):
        with tempfile.TemporaryDirectory() as cwd:
            self._fake(cwd, "supabase")
            antigo = os.getcwd()
            os.chdir(cwd)
            self.addCleanup(os.chdir, antigo)
            with mock.patch.dict(os.environ, {"PATH": cwd}):
                self.assertIsNone(_comum.achar_cli("supabase"))

    def test_recusa_dentro_do_repo(self):
        with mock.patch.dict(os.environ, {"PATH": str(RAIZ / "setup")}):
            self.assertIsNone(_comum.achar_cli("estado.py"))

    def test_aceita_pasta_do_path_fora_do_repo(self):
        with tempfile.TemporaryDirectory() as bom, tempfile.TemporaryDirectory() as cwd:
            exe = self._fake(bom, "supabase")
            antigo = os.getcwd()
            os.chdir(cwd)
            self.addCleanup(os.chdir, antigo)
            with mock.patch.dict(os.environ, {"PATH": bom}):
                self.assertEqual(_comum.achar_cli("supabase"), [str(exe)])


if __name__ == "__main__":
    unittest.main()
