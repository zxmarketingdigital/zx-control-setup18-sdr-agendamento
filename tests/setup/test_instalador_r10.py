"""Rodada 10: troca de IA grava o banco primeiro, o motor só morre se for o desta instalação, a CLI do Supabase vem com
versão fixa e o guard destrutivo vê WHERE trivial como DELETE sem filtro (subprocess e HTTP mockados)."""
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


# ------------------------------------------------------------------ 7: cmd_ia

class IaBancoPrimeiro(unittest.TestCase):
    def setUp(self):
        _comum.CONFIG_PATH.write_text(json.dumps({"ai": {"provider": "gemini", "api_key": "k-velha", "model": ""}, "supabase": SUPA}))
        os.environ["SDR_AI_API_KEY"] = "chave-nova"
        self.addCleanup(os.environ.pop, "SDR_AI_API_KEY", None)
        self.ordem = []

    def _rodar(self, patch, leitura=(200, [{"ai_provider": "openai"}]), com_banco=True):
        if not com_banco:
            _comum.CONFIG_PATH.write_text(json.dumps({"ai": {"provider": "gemini", "api_key": "k-velha", "model": ""}}))

        def rest(config, metodo, caminho, *a, **k):
            self.ordem.append(metodo)
            # a chave só pode estar gravada no config depois do PATCH confirmado
            self.chave_no_config = _comum.load_config().get("ai", {}).get("api_key")
            return patch if metodo == "PATCH" else leitura
        with mock.patch.object(etapa_config, "supabase_rest", rest), mock.patch.object(etapa_config, "aviso") as aviso:
            codigo = etapa_config.cmd_ia(argparse.Namespace(provider="openai", model=None))
        return codigo, aviso

    def test_patch_falhou_deixa_o_config_intacto_e_sai_diferente_de_zero(self):
        codigo, _ = self._rodar((503, "erro"))
        self.assertNotEqual(codigo, 0)
        ai = _comum.load_config()["ai"]
        self.assertEqual((ai["provider"], ai["api_key"]), ("gemini", "k-velha"))

    def test_releitura_divergente_deixa_o_config_intacto(self):
        codigo, _ = self._rodar((200, [{"ai_provider": "openai"}]), leitura=(200, [{"ai_provider": "gemini"}]))
        self.assertNotEqual(codigo, 0)
        self.assertEqual(_comum.load_config()["ai"]["api_key"], "k-velha")

    def test_sucesso_grava_o_config_antes_do_banco(self):  # r19: ordem invertida
        codigo, _ = self._rodar((200, [{"id": 1}]))
        self.assertEqual(codigo, 0)
        self.assertEqual(self.ordem, ["GET", "PATCH", "GET"])  # r12: lê o provedor anterior antes do PATCH
        self.assertEqual(self.chave_no_config, "chave-nova")  # r19: durante o PATCH o par novo já está no config
        self.assertEqual(_comum.load_config()["ai"]["api_key"], "chave-nova")

    def test_sem_supabase_grava_no_config_e_avisa_o_comando(self):
        codigo, aviso = self._rodar((200, []), com_banco=False)
        self.assertEqual(codigo, 0)
        self.assertEqual(self.ordem, [])
        self.assertEqual(_comum.load_config()["ai"]["provider"], "openai")
        texto = aviso.call_args[0][0]
        self.assertIn("banco", texto)
        self.assertIn("etapa_config.py ia --provider openai", texto)

    def test_ia_null_na_entrevista_nao_quebra(self):
        with mock.patch.object(etapa_config, "carregar_entrevista", return_value={"ia": None}), \
                mock.patch.object(etapa_config, "supabase_rest", return_value=(200, [{"ai_provider": "gemini", "id": 1}])):
            codigo = etapa_config.cmd_ia(argparse.Namespace(provider=None, model=None))
        self.assertEqual(codigo, 0)


class ValidarTipos(unittest.TestCase):
    def test_numero_em_campo_de_texto_vira_erro_de_validacao_sem_traceback(self):
        d = etapa_config.entrevista_modelo()
        d["negocio"]["empresa"] = 123
        d["agente"]["nome"] = 7
        d["closers"][0]["nome"] = 5
        erros = etapa_config.validar_entrevista(d)
        self.assertTrue(any("negocio.empresa" in e for e in erros))
        self.assertTrue(any("agente.nome" in e for e in erros))
        self.assertTrue(any("closers[0].nome" in e for e in erros))


# ------------------------------------------------------------------ 8: identidade do motor

REPO = servico.REPO_ROOT


class SoMac:
    """Mac/Linux falso: `ps` + `lsof` com a linha de comando e o diretório de trabalho do PID 4321."""

    def __init__(self, linha, cwd=None, lsof_ok=True):
        self.linha, self.cwd, self.lsof_ok = linha, cwd, lsof_ok
        self.chamadas = []

    def __call__(self, c, **kw):
        self.chamadas.append(list(c))
        if c[0] == "ps" and "pid=" in c and "-p" in c:
            return 0, "4321"
        if c[0] == "ps" and "command=" in c:
            return 0, self.linha
        if c[0] == "ps":  # varredura
            return 0, "  4321 %s\n  9 /bin/zsh" % self.linha
        if c[0] == "lsof":
            return (0, "p4321\nn%s" % self.cwd) if self.lsof_ok and self.cwd else (1, "")
        return 0, ""


class IdentidadeDoMotor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock = Path(self.tmp.name) / "motor.lock"
        for p in (mock.patch.object(servico, "LOCK_FILE", self.lock), mock.patch.object(servico, "sistema", return_value="Darwin"),
                  mock.patch.object(servico, "erro"), mock.patch.object(servico, "aviso"), mock.patch.object(servico, "info"),
                  mock.patch.object(servico.time, "sleep", lambda s: None)):
            p.start()
            self.addCleanup(p.stop)

    def _parar(self, so, com_trava=True):
        if com_trava:
            self.lock.write_text("4321")
        with mock.patch.object(servico, "rodar", so), mock.patch.object(servico.os, "kill") as kill:
            codigo = servico.garantir_motor_parado(servico.Executor(False), espera=0, dormir=lambda s: None)
        return codigo, kill

    def test_python_de_outro_projeto_com_a_palavra_run_nao_e_o_motor(self):
        linha = "python.exe C:/projetos/motor-financeiro/app.py run"
        self.assertFalse(servico._e_linha_do_motor(linha))
        self.assertFalse(servico._e_linha_do_motor("/usr/bin/python3 motor_x.py run"))

    def test_windows_motor_financeiro_nao_leva_taskkill(self):
        chamadas = []

        def rodar(c, **kw):
            chamadas.append(list(c))
            if c[0] == "tasklist":
                return 0, '"python.exe","4321","Console","1","20,000 K"'
            return 0, "python.exe C:/projetos/motor-financeiro/app.py run"
        self.lock.write_text("4321")
        with mock.patch.object(servico, "sistema", return_value="Windows"), mock.patch.object(servico, "rodar", rodar):
            codigo = servico.garantir_motor_parado(servico.Executor(False), matar_arvore=True, espera=0, dormir=lambda s: None)
        self.assertFalse(any(c[0] == "taskkill" for c in chamadas))
        self.assertFalse(self.lock.exists())  # trava obsoleta removida; nada encerrado

    def test_motor_de_outra_pasta_nao_e_encerrado(self):
        so = SoMac("/usr/bin/python3 -m motor run", cwd="/outra/pasta/sdr")
        codigo, kill = self._parar(so)
        kill.assert_not_called()
        self.assertEqual(codigo, 0)
        self.assertFalse(self.lock.exists())

    def test_sem_como_provar_a_pasta_fail_closed_nao_mata_e_avisa(self):
        so = SoMac("/usr/bin/python3 -m motor run", cwd=None)
        codigo, kill = self._parar(so)
        kill.assert_not_called()
        self.assertEqual(codigo, 1)
        self.assertTrue(self.lock.exists())  # trava preservada

    def test_cwd_igual_ao_repo_e_o_motor(self):
        so = SoMac("/usr/bin/python3 -m motor run", cwd=str(REPO))
        with mock.patch.object(servico, "rodar", so):
            self.assertEqual(servico.identidade_do_pid(4321), "motor")

    def test_repo_na_linha_de_comando_basta(self):
        so = SoMac("%s/venv/bin/python -m motor run" % REPO, cwd=None)
        with mock.patch.object(servico, "rodar", so):
            self.assertEqual(servico.identidade_do_pid(4321), "motor")

    def test_windows_pai_com_o_repo_prova_o_dono(self):
        def rodar(c, **kw):
            if c[0] == "tasklist":
                return 0, '"python.exe","4321","Console","1","20,000 K"'
            if "ParentProcessId" in c[-1]:
                return 0, 'cmd.exe /c ""python.exe" -m motor run >> "%s\\logs\\motor.log" 2>&1"' % str(REPO).replace("/", "\\")
            return 0, "python.exe -m motor run"
        with mock.patch.object(servico, "sistema", return_value="Windows"), mock.patch.object(servico, "rodar", rodar):
            self.assertEqual(servico.identidade_do_pid(4321), "motor")

    def test_varredura_sem_trava_so_mata_o_proprio_e_trata_incerto_como_erro(self):
        incerto = SoMac("/usr/bin/python3 -m motor run", cwd=None)
        codigo, kill = self._parar(incerto, com_trava=False)
        kill.assert_not_called()
        self.assertEqual(codigo, 1)
        alheio = SoMac("/usr/bin/python3 -m motor run", cwd="/outra/pasta")
        codigo, kill = self._parar(alheio, com_trava=False)
        kill.assert_not_called()
        self.assertEqual(codigo, 0)

    def test_varredura_sem_trava_mata_o_motor_desta_instalacao(self):
        so = SoMac("/usr/bin/python3 -m motor run", cwd=str(REPO))
        estado = {"vivo": True}
        base = so.__call__

        def rodar(c, **kw):
            if c[0] == "ps" and "-ax" in c:
                return (0, "  4321 %s" % so.linha) if estado["vivo"] else (0, "  9 /bin/zsh")
            return base(c, **kw)
        with mock.patch.object(servico, "rodar", rodar), mock.patch.object(servico.os, "kill", side_effect=lambda p, s: estado.update(vivo=False)) as kill:
            codigo = servico.garantir_motor_parado(servico.Executor(False), espera=0, dormir=lambda s: None)
        self.assertEqual(codigo, 0)
        self.assertEqual(kill.call_args_list[0][0][0], 4321)


# ------------------------------------------------------------------ 9: CLI com versão fixa

class CliComVersaoFixa(unittest.TestCase):
    def test_supabase_via_npx_usa_a_versao_testada(self):
        def falso(nome, path=None):
            return {"npx": "/usr/bin/npx"}.get(nome)
        with mock.patch("shutil.which", side_effect=falso):
            cmd = _comum.achar_cli("supabase")
        self.assertEqual(cmd, ["/usr/bin/npx", "--yes", "supabase@2.90.0"])
        self.assertNotIn("latest", " ".join(cmd))

    def test_cli_instalada_tem_prioridade(self):
        with mock.patch("shutil.which", side_effect=lambda n, path=None: "/opt/supabase" if n == "supabase" else "/usr/bin/npx"):
            self.assertEqual(_comum.achar_cli("supabase"), ["/opt/supabase"])

    def test_migrar_usa_o_comando_fixo(self):
        with mock.patch("shutil.which", side_effect=lambda n, path=None: "/usr/bin/npx" if n == "npx" else None), \
                mock.patch.object(etapa_banco, "load_config", return_value={"supabase": dict(SUPA, project_ref="abcdefghijklmnopqrst")}), \
                mock.patch("builtins.print") as imprimir:
            etapa_banco.cmd_migrar(argparse.Namespace(dry_run=True, confirmar_destrutivo=False))
        saida = " ".join(str(c[0][0]) for c in imprimir.call_args_list)
        self.assertIn("supabase@2.90.0", saida)


# ------------------------------------------------------------------ 10: WHERE trivial

class WhereTrivial(unittest.TestCase):
    def test_where_trivial_conta_como_delete_sem_filtro(self):
        for sql in ("delete from messages where true;", "DELETE FROM messages WHERE 1=1;", "delete from t where 1 = 1",
                    "delete from t where 'a'='a'", "delete from t where id = id", "delete from public.t where t.id = t.id",
                    "delete from t where not false", "delete from t where (true)", "delete from t where id = 3 or true",
                    "delete from t where true and 2=2", "delete from t where true returning id",
                    "with d as (delete from t where true returning *) select 1"):
            self.assertIn("DELETE FROM sem WHERE", etapa_banco.sql_destrutivo(sql), sql)

    def test_where_com_filtro_de_verdade_nao_conta(self):
        for sql in ("delete from t where id = 3;", "delete from t where a = b and c = 1", "delete from t where true and id = 3",
                    "delete from t where id in (select id from u where 1=1)", "delete from t where created_at < now() - interval '1 day'",
                    "delete from t where status = 'x' or id = 4"):
            self.assertEqual(etapa_banco.sql_destrutivo(sql), [], sql)


if __name__ == "__main__":
    unittest.main()
