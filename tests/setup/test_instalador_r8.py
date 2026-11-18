"""Rodada 8: guard destrutivo em qualquer formato de identificador e sobre o SQL bruto, banco em dia não bloqueia
`migrar`, e parada do serviço sem trava legível procura o motor pela linha de comando (subprocess mockado)."""
import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "setup"))

import etapa_banco  # noqa: E402
import servico  # noqa: E402


def setUpModule():
    from tests.setup import _isolamento
    _isolamento.ligar()


def tearDownModule():
    from tests.setup import _isolamento
    _isolamento.desligar()


class GuardDropColumn(unittest.TestCase):
    def test_formatos_de_identificador(self):
        for sql in ('ALTER TABLE "public"."leads" DROP COLUMN telefone;',
                    'alter table public."leads" drop column x;',
                    "ALTER TABLE ONLY public.leads DROP COLUMN IF EXISTS x;"):
            self.assertIn("DROP COLUMN", etapa_banco.sql_destrutivo(sql), sql)

    def test_drop_constraint_nao_e_destrutivo(self):
        self.assertEqual(etapa_banco.sql_destrutivo("ALTER TABLE leads DROP CONSTRAINT c;"), [])
        self.assertEqual(etapa_banco.sql_destrutivo('ALTER TABLE "public"."leads" DROP CONSTRAINT c;'), [])

    def test_sql_bruto_com_traco_dentro_de_string(self):
        self.assertIn("DROP TABLE", etapa_banco.sql_destrutivo("INSERT INTO t(k,v) VALUES ('modo','--'); DROP TABLE leads;"))

    def test_comentario_e_falso_positivo_aceito(self):
        self.assertIn("DROP TABLE", etapa_banco.sql_destrutivo("-- drop table velha\nselect 1;"))


class MigrarEmDia(unittest.TestCase):
    def test_banco_em_dia_com_migration_antiga_destrutiva_nao_bloqueia(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: ([f.unlink() for f in d.glob("*")], d.rmdir()))
        (d / "20260101000000_base.sql").write_text("DROP TABLE IF EXISTS antiga;")
        chamadas = []

        def rodar(c, **kw):
            chamadas.append(list(c))
            if "--dry-run" in c:
                return 0, "Remote database is up to date."
            return 0, "ok"
        cfgs = {"supabase": {"project_ref": "abcdefghijklmnopqrst"}}
        with mock.patch.object(etapa_banco, "MIGRATIONS_DIR", d), mock.patch.object(etapa_banco, "load_config", return_value=cfgs), \
                mock.patch.object(etapa_banco, "achar_cli", return_value=["supabase"]), \
                mock.patch.object(etapa_banco, "entrada_externa", return_value="senha"), \
                mock.patch.object(etapa_banco, "rodar", rodar), mock.patch.object(etapa_banco, "cmd_verificar", return_value=0), \
                mock.patch.object(etapa_banco, "salvar_estado"), mock.patch.object(etapa_banco, "erro") as erro, \
                mock.patch.object(etapa_banco, "info"), mock.patch.object(etapa_banco, "ok"):
            codigo = etapa_banco.cmd_migrar(argparse.Namespace(dry_run=False, confirmar_destrutivo=False))
        self.assertEqual(codigo, 0)
        erro.assert_not_called()
        self.assertFalse(any(c[-1] == "push" for c in chamadas))

    def test_saida_ilegivel_continua_fail_closed(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: ([f.unlink() for f in d.glob("*")], d.rmdir()))
        (d / "20260101000000_base.sql").write_text("DROP TABLE antiga;")

        def rodar(c, **kw):
            return 0, "???"
        cfgs = {"supabase": {"project_ref": "abcdefghijklmnopqrst"}}
        with mock.patch.object(etapa_banco, "MIGRATIONS_DIR", d), mock.patch.object(etapa_banco, "load_config", return_value=cfgs), \
                mock.patch.object(etapa_banco, "achar_cli", return_value=["supabase"]), \
                mock.patch.object(etapa_banco, "entrada_externa", return_value="senha"), \
                mock.patch.object(etapa_banco, "rodar", rodar), mock.patch.object(etapa_banco, "erro"), \
                mock.patch.object(etapa_banco, "info"), mock.patch.object(etapa_banco, "ok"):
            codigo = etapa_banco.cmd_migrar(argparse.Namespace(dry_run=False, confirmar_destrutivo=False))
        self.assertEqual(codigo, 1)


class ParadaSemTrava(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(servico, "LOCK_FILE", Path(self.tmp.name) / "motor.lock")  # inexistente
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(servico, "_dono_do_processo", return_value=True)  # r10: foco aqui não é o dono do processo
        p.start()
        self.addCleanup(p.stop)

    def _parar(self, so, respostas):
        chamadas = []

        def rodar(c, **kw):
            chamadas.append(list(c))
            return respostas(c, chamadas)
        with mock.patch.object(servico, "sistema", return_value=so), mock.patch.object(servico, "rodar", rodar), \
                mock.patch.object(servico, "erro") as erro, mock.patch.object(servico, "info"), \
                mock.patch.object(servico.os, "kill") as kill:
            codigo = servico.garantir_motor_parado(servico.Executor(False), matar_arvore=(so == "Windows"), espera=0,
                                                   dormir=lambda s: None)
        return codigo, chamadas, erro, kill

    def test_windows_acha_python_do_motor_e_mata_a_arvore(self):
        def r(c, ch):
            if c[0] == "powershell":
                vivo = not any(x[0] == "taskkill" for x in ch)
                return 0, ('777 "C:\\Python\\python.exe" -m motor run\n12 C:\\x\\notepad.exe' if vivo else '12 C:\\x\\notepad.exe')
            return 0, ""
        codigo, ch, erro, _ = self._parar("Windows", r)
        self.assertEqual(codigo, 0)
        self.assertIn(["taskkill", "/T", "/F", "/PID", "777"], ch)

    def test_windows_varredura_falhou_sai_com_erro(self):
        codigo, ch, erro, _ = self._parar("Windows", lambda c, ch: (1, "bloqueado"))
        self.assertEqual(codigo, 1)
        self.assertIn("Gerenciador de Tarefas", erro.call_args[0][0])

    def test_posix_sigterm_e_confere(self):
        estado = {"vivo": True}

        def r(c, ch):
            if estado["vivo"]:
                return 0, "  555 /usr/bin/python3 -m motor.cli run\n  9 /bin/zsh"
            return 0, "  9 /bin/zsh"
        codigo, ch, erro, kill = self._parar("Darwin", lambda c, ch: r(c, ch))
        # o processo nunca morre no mock: SIGTERM, SIGKILL e depois erro na reconferência
        self.assertEqual(codigo, 1)
        sinais = [a[0][1] for a in kill.call_args_list]
        import signal
        self.assertEqual(sinais, [signal.SIGTERM, signal.SIGKILL])
        self.assertIn("Monitor de Atividade", erro.call_args[0][0])

    def test_posix_nenhum_motor_vivo_e_ok(self):
        codigo, ch, erro, kill = self._parar("Linux", lambda c, ch: (0, "  9 /bin/zsh\n 10 python3 planilha.py"))
        self.assertEqual(codigo, 0)
        kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
