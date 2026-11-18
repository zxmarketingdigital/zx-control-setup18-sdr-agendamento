"""Rodada 11: verificar sem OpenAPI não mexe no estado, dono do motor com fronteira de diretório, 'ia' sem --provider
mantém o provedor atual, rollback de faixas e QR code 0600 (subprocess e HTTP mockados)."""
import argparse
import base64
import json
import os
import stat
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

CFG = {"supabase": {"url": "https://abcdefghijklmnopqrst.supabase.co", "service_role_key": "x"}}


def setUpModule():
    from tests.setup import _isolamento
    _isolamento.ligar()


def tearDownModule():
    from tests.setup import _isolamento
    _isolamento.desligar()


# ------------------------------------------------------------------ 9: verificar

class VerificarOpenApi(unittest.TestCase):
    def _verificar(self, openapi, migracoes=None):
        def rest(config, metodo, caminho, *a, **k):
            return openapi if caminho == "/" else (200, [])
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [f.unlink() for f in d.glob("*")] and d.rmdir())
        for nome, sql in (migracoes or {}).items():
            (d / nome).write_text(sql)
        with mock.patch.object(etapa_banco, "load_config", return_value=CFG), mock.patch.object(etapa_banco, "supabase_rest", rest), \
                mock.patch.object(etapa_banco, "MIGRATIONS_DIR", d), \
                mock.patch.object(etapa_banco, "salvar_estado") as salvar, mock.patch.object(etapa_banco, "erro"), \
                mock.patch.object(etapa_banco, "aviso"), mock.patch.object(etapa_banco, "info"), \
                mock.patch.object(etapa_banco, "instrucoes_sql_editor") as plano:
            codigo = etapa_banco.cmd_verificar(mock.Mock(confirmar_destrutivo=False))
        return codigo, salvar, plano, d

    def test_openapi_503_sai_diferente_de_zero_sem_mexer_no_estado_nem_plano_b(self):
        codigo, salvar, plano, _ = self._verificar((503, "indisponivel"))
        self.assertNotEqual(codigo, 0)
        salvar.assert_not_called()
        plano.assert_not_called()

    def test_openapi_sem_paths_tambem_nao_conclui_nada(self):
        codigo, salvar, plano, _ = self._verificar((200, "texto"))
        self.assertNotEqual(codigo, 0)
        salvar.assert_not_called()
        plano.assert_not_called()

    def test_rpc_ausente_lista_so_a_migration_que_cria_a_funcao(self):
        migr = {"20260101000001_antiga.sql": "create table a(id int);",
                "20261118000001_sdr_agendamento.sql": "create or replace function public.book_slot(x int) returns int as $$ select 1 $$ language sql;"}
        paths = {"/rpc/" + r: {} for r in etapa_banco.RPCS if r != "book_slot"}
        codigo, salvar, plano, d = self._verificar((200, {"paths": paths}), migr)
        self.assertEqual(codigo, 1)
        pendentes = plano.call_args[0][0]
        self.assertEqual([p.name for p in pendentes], ["20261118000001_sdr_agendamento.sql"])
        salvar.assert_called_once_with({"banco_migrado": False})

    def test_rpc_ausente_sem_migration_conhecida_nunca_manda_colar_todas(self):
        paths = {"/rpc/" + r: {} for r in etapa_banco.RPCS if r != "book_slot"}
        codigo, salvar, plano, _ = self._verificar((200, {"paths": paths}), {"20260101000001_antiga.sql": "create table a(id int);"})
        self.assertEqual(codigo, 1)
        plano.assert_not_called()


# ------------------------------------------------------------------ 10: dono do processo

class DonoComFronteira(unittest.TestCase):
    def _dono(self, raiz, linha, cwd=None, pai=None, so="Windows"):
        with mock.patch.object(servico, "REPO_ROOT", raiz), mock.patch.object(servico, "sistema", return_value=so), \
                mock.patch.object(servico, "_cwd_do_processo", return_value=cwd), \
                mock.patch.object(servico, "_linha_do_pai_windows", return_value=pai):
            return servico._dono_do_processo(1, linha)

    def test_pasta_irma_com_prefixo_igual_nao_e_da_instalacao_na_linha_de_comando(self):
        linha = r"python.exe -m motor run --log c:\users\ana\setup18-2\logs\motor.log"
        self.assertIsNot(self._dono(r"C:\Users\Ana\setup18", linha), True)

    def test_pasta_irma_no_processo_pai(self):
        self.assertIsNot(self._dono(r"C:\Users\Ana\setup18", "python.exe -m motor run",
                                    pai=r'cmd.exe /c "c:\users\ana\setup18-2\logs\motor.log"'), True)

    def test_caminho_com_a_raiz_no_meio_de_outro_caminho_nao_conta(self):
        self.assertIsNot(self._dono("/home/ana/setup18", "/usr/bin/python3 -m motor run --log /srv/home/ana/setup18/logs/m.log", so="Linux"), True)

    def test_logs_da_propria_instalacao_continuam_sendo_dela(self):
        linha = r"python.exe -m motor run --log c:\users\ana\setup18\logs\motor.log"
        self.assertIs(self._dono(r"C:\Users\Ana\setup18", linha), True)
        self.assertIs(self._dono(r"C:\Users\Ana\setup18", "python.exe -m motor run", pai=r'cmd.exe /c "c:\users\ana\setup18\logs\m.log" 2>&1'), True)
        self.assertIs(self._dono("/home/ana/setup18", "/home/ana/setup18/.venv/bin/python -m motor run", so="Linux"), True)

    def test_raiz_com_espaco_no_caminho(self):
        self.assertIs(self._dono("/home/ana maria/setup18", "/home/ana maria/setup18/.venv/bin/python -m motor run", so="Linux"), True)
        self.assertIsNot(self._dono("/home/ana maria/setup18", "/home/ana maria/setup18-2/.venv/bin/python -m motor run", so="Linux"), True)


# ------------------------------------------------------------------ 11: ia sem --provider

class IaProvedorPadrao(unittest.TestCase):
    def setUp(self):
        os.environ["SDR_AI_API_KEY"] = "chave-nova"
        self.addCleanup(os.environ.pop, "SDR_AI_API_KEY", None)

    def _ia(self, config, banco, entrevista):
        _comum.CONFIG_PATH.write_text(json.dumps(config))
        patches = []

        def rest(cfg_, metodo, caminho, corpo=None, *a, **k):
            if metodo == "PATCH":
                patches.append(corpo)
                return 200, [{"id": 1}]
            return banco
        with mock.patch.object(etapa_config, "supabase_rest", rest), \
                mock.patch.object(etapa_config, "carregar_entrevista", return_value=entrevista):
            codigo = etapa_config.cmd_ia(argparse.Namespace(provider=None, model=None))
        return codigo, patches

    def test_banco_manda_sobre_config_e_entrevista(self):
        codigo, patches = self._ia({"ai": {"provider": "gemini", "api_key": "k"}, "supabase": CFG["supabase"]},
                                   (200, [{"ai_provider": "openai"}]), {"ia": {"provider": "gemini"}})
        self.assertEqual(codigo, 0)
        self.assertEqual(patches[0]["ai_provider"], "openai")

    def test_sem_banco_o_config_manda_sobre_a_entrevista(self):
        codigo, _ = self._ia({"ai": {"provider": "openai", "api_key": "k"}}, (200, []), {"ia": {"provider": "gemini"}})
        self.assertEqual(codigo, 0)
        self.assertEqual(_comum.load_config()["ai"]["provider"], "openai")

    def test_entrevista_so_na_primeira_configuracao(self):
        codigo, _ = self._ia({}, (200, []), {"ia": {"provider": "openai"}})
        self.assertEqual(codigo, 0)
        self.assertEqual(_comum.load_config()["ai"]["provider"], "openai")


# ------------------------------------------------------------------ 12: rollback das faixas

class SubstituirRegrasRollback(unittest.TestCase):
    def test_delete_das_antigas_falha_apaga_as_novas(self):
        chamadas = []

        def rest(config, metodo, caminho, corpo=None, **k):
            chamadas.append((metodo, caminho))
            if metodo == "GET":
                return 200, [{"id": "velha-1"}]
            if metodo == "POST":
                return 201, [{"id": "nova-1"}, {"id": "nova-2"}]
            if metodo == "DELETE" and "velha-1" in caminho:
                return 503, "timeout"
            return 204, ""
        regras = [{"weekday": 1}, {"weekday": 2}]
        with mock.patch.object(etapa_config, "supabase_rest", rest), mock.patch.object(etapa_config, "erro"):
            self.assertFalse(etapa_config._substituir_regras(CFG, "c1", "Ana", regras))
        deletes = [c for m, c in chamadas if m == "DELETE"]
        self.assertEqual(len(deletes), 2)
        self.assertIn("nova-1", deletes[1])
        self.assertIn("nova-2", deletes[1])
        self.assertNotIn("velha-1", deletes[1])

    def test_delete_ok_nao_apaga_as_novas(self):
        chamadas = []

        def rest(config, metodo, caminho, corpo=None, **k):
            chamadas.append((metodo, caminho))
            return (200, [{"id": "velha-1"}]) if metodo == "GET" else ((201, [{"id": "n1"}]) if metodo == "POST" else (204, ""))
        with mock.patch.object(etapa_config, "supabase_rest", rest):
            self.assertTrue(etapa_config._substituir_regras(CFG, "c1", "Ana", [{"weekday": 1}]))
        self.assertEqual(len([1 for m, _ in chamadas if m == "DELETE"]), 1)


# ------------------------------------------------------------------ 13: QR code

class QrCode(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [f.unlink() for f in self.tmp.glob("*")] and self.tmp.rmdir())

    @unittest.skipIf(os.name == "nt", "modo POSIX")
    def test_qr_gravado_com_modo_0600_mesmo_se_havia_arquivo_antigo_aberto(self):
        alvo = self.tmp / "qrcode-whatsapp.png"
        alvo.write_bytes(b"velho")
        os.chmod(alvo, 0o644)
        etapa_config._gravar_qr(alvo, b"PNG")
        self.assertEqual(stat.S_IMODE(alvo.stat().st_mode), 0o600)
        self.assertEqual(alvo.read_bytes(), b"PNG")

    def _evolution(self, estados):
        seq = iter(estados)

        def http(metodo, url, **k):
            if "/instance/connect/" in url:
                return 200, {"base64": "data:image/png;base64," + base64.b64encode(b"PNG").decode()}
            return 200, {"instance": {"state": next(seq)}}
        args = argparse.Namespace(url="http://localhost:8080", instance="sdr", criar=False, trocar_chave=False)
        with mock.patch.object(etapa_config, "DATA_DIR", self.tmp), mock.patch.object(etapa_config, "http_json", http), \
                mock.patch.object(etapa_config, "segredo", return_value="k"), mock.patch.object(etapa_config, "atualizar_config"), \
                mock.patch.object(etapa_config.time, "sleep", lambda s: None):
            return etapa_config.cmd_evolution(args)

    def test_qr_some_quando_a_conexao_e_confirmada(self):
        self.assertEqual(self._evolution(["close", "connecting", "open"]), 0)
        self.assertFalse((self.tmp / "qrcode-whatsapp.png").exists())

    def test_qr_antigo_some_se_ja_estava_conectado(self):
        (self.tmp / "qrcode-whatsapp.png").write_bytes(b"velho")
        self.assertEqual(self._evolution(["open"]), 0)
        self.assertFalse((self.tmp / "qrcode-whatsapp.png").exists())


if __name__ == "__main__":
    unittest.main()
