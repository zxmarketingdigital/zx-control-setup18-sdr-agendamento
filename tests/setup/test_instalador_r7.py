"""Rodada 7: PID desconhecido nunca vira 'parado', Plano B do SQL Editor respeita o guard destrutivo, sql_destrutivo
com DROP sem COLUMN/DROP SCHEMA, validar recusa os valores do modelo e credenciais só aceita HTTP 200."""
import argparse
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "setup"))

import etapa_banco  # noqa: E402
import etapa_config  # noqa: E402
import servico  # noqa: E402


def setUpModule():
    from tests.setup import _isolamento
    _isolamento.ligar()


def tearDownModule():
    from tests.setup import _isolamento
    _isolamento.desligar()


# ------------------------------------------------------------------ 14: PID desconhecido

class PidDesconhecido(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock = Path(self.tmp.name) / "motor.lock"
        self.lock.write_text("4321")
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(servico, "LOCK_FILE", self.lock)
        p.start()
        self.addCleanup(p.stop)
        self.chamadas = []

    def _parar(self, so, respostas, matar_arvore=False):
        def rodar(c, **kw):
            self.chamadas.append(list(c))
            return respostas(c)
        with mock.patch.object(servico, "sistema", return_value=so), mock.patch.object(servico, "rodar", rodar), \
                mock.patch.object(servico, "erro") as erro, mock.patch.object(servico.time, "sleep", lambda s: None):
            codigo = servico.garantir_motor_parado(servico.Executor(False), matar_arvore=matar_arvore, espera=0)
        return codigo, erro

    def test_windows_powershell_bloqueado_e_desconhecido(self):
        def r(c):
            if c[0] == "tasklist":
                return 0, '"python.exe","4321","Console","1","20,000 K"'
            return 1, "bloqueado por política"
        codigo, erro = self._parar("Windows", r, matar_arvore=True)
        self.assertEqual(codigo, 1)
        self.assertTrue(self.lock.exists())
        self.assertFalse(any(c[0] == "taskkill" for c in self.chamadas))
        self.assertIn("4321", erro.call_args[0][0])

    def test_windows_tasklist_falhou_e_desconhecido(self):
        codigo, _ = self._parar("Windows", lambda c: (-1, "o comando passou do tempo limite"), matar_arvore=True)
        self.assertEqual(codigo, 1)
        self.assertTrue(self.lock.exists())

    def test_mac_probe_com_timeout_e_desconhecido(self):
        codigo, _ = self._parar("Darwin", lambda c: (-1, "o comando passou do tempo limite"))
        self.assertEqual(codigo, 1)
        self.assertTrue(self.lock.exists())

    def test_mac_linha_de_comando_ilegivel_e_desconhecido(self):
        codigo, _ = self._parar("Linux", lambda c: (0, "4321") if "pid=" in c else (0, ""))
        self.assertEqual(codigo, 1)
        self.assertTrue(self.lock.exists())

    def test_mac_pid_comprovadamente_morto_nao_e_erro(self):
        codigo, _ = self._parar("Darwin", lambda c: (1, ""))
        self.assertEqual(codigo, 0)

    def test_pid_do_motor_vivo_trata_desconhecido_como_vivo(self):
        with mock.patch.object(servico, "identidade_do_pid", return_value="desconhecido"):
            self.assertTrue(servico.pid_do_motor_vivo(4321))
        with mock.patch.object(servico, "identidade_do_pid", return_value="outro"):
            self.assertFalse(servico.pid_do_motor_vivo(4321))

    def test_outro_programa_legivel_continua_removendo_a_trava(self):
        def r(c):
            return (0, "4321") if "pid=" in c else (0, "/usr/bin/python3 planilha.py")
        codigo, _ = self._parar("Darwin", r)
        self.assertEqual(codigo, 0)
        self.assertFalse(self.lock.exists())


# ------------------------------------------------------------------ 16: sql_destrutivo

class SqlDestrutivo(unittest.TestCase):
    def test_drop_com_e_sem_column(self):
        for sql in ("alter table leads drop telefone_antigo;",
                    "ALTER TABLE ONLY public.leads DROP COLUMN IF EXISTS x;",
                    'alter table "leads" drop column "x";',
                    "alter table t add column a numeric(10,2), drop b;",
                    "alter table t drop constraint c, drop column b;"):
            self.assertIn("DROP COLUMN", etapa_banco.sql_destrutivo(sql), sql)

    def test_drop_schema(self):
        self.assertIn("DROP SCHEMA", etapa_banco.sql_destrutivo("drop schema if exists s cascade;"))

    def test_drop_de_constraint_default_e_not_null_nao_conta(self):
        for sql in ("alter table t drop constraint c;",
                    "alter table t alter column x drop default;",
                    "alter table t alter column x drop not null;",
                    "alter table t add column a numeric(10,2);"):
            self.assertEqual(etapa_banco.sql_destrutivo(sql), [], sql)


# ------------------------------------------------------------------ 15: Plano B

class PlanoB(unittest.TestCase):
    def _sql_editor(self, arquivos, pendentes=None, confirmar=False, migrado=False):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [f.unlink() for f in d.glob("*")] and d.rmdir())
        for nome, sql in arquivos.items():
            (d / nome).write_text(sql)
        escolhidas = None if pendentes is None else [d / n for n in pendentes]
        saidas, erros = [], []
        with mock.patch.object(etapa_banco, "MIGRATIONS_DIR", d), \
                mock.patch.object(etapa_banco, "load_estado", return_value={"banco_migrado": migrado}), \
                mock.patch.object(etapa_banco, "erro", side_effect=erros.append), mock.patch.object(etapa_banco, "aviso"), \
                mock.patch.object(etapa_banco, "titulo"), mock.patch.object(etapa_banco, "info"), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: saidas.append(" ".join(map(str, a)))):
            ok = etapa_banco.instrucoes_sql_editor(escolhidas, confirmar)
        return ok, "\n".join(saidas), "\n".join(erros)

    ANTIGA = "20260101000001_antiga.sql"
    SETUP = "20261118000001_sdr_agendamento.sql"

    def test_instalacao_inicial_sem_lista_cola_todas_as_migrations_em_ordem(self):
        ok, saida, _ = self._sql_editor({self.ANTIGA: "create table a(id int);", self.SETUP: "create table b(id int);"})
        self.assertTrue(ok)
        self.assertIn(self.SETUP, saida)
        self.assertIn(self.ANTIGA, saida)
        self.assertLess(saida.index(self.ANTIGA), saida.index(self.SETUP))

    def test_lista_conhecida_cola_so_as_pendentes(self):
        ok, saida, _ = self._sql_editor({self.ANTIGA: "create table a(id int);", self.SETUP: "create table b(id int);"},
                                        pendentes=[self.SETUP])
        self.assertTrue(ok)
        self.assertNotIn(self.ANTIGA, saida)

    def test_sem_pendentes_nao_manda_colar_nada(self):
        ok, saida, _ = self._sql_editor({self.SETUP: "create table b(id int);"}, pendentes=[])
        self.assertFalse(ok)
        self.assertNotIn("copie TODO", saida)

    def test_banco_ja_migrado_e_lista_desconhecida_nao_manda_colar(self):
        ok, saida, _ = self._sql_editor({self.SETUP: "create table b(id int);"}, migrado=True)
        self.assertFalse(ok)
        self.assertNotIn("copie TODO", saida)

    def test_arquivo_destrutivo_recusado_sem_flag(self):
        ok, saida, erros = self._sql_editor({self.SETUP: "drop table leads;"})
        self.assertFalse(ok)
        self.assertNotIn("copie TODO", saida)
        self.assertIn("--confirmar-destrutivo", erros)
        self.assertIn(self.SETUP, erros)

    def test_arquivo_destrutivo_com_flag_aparece(self):
        ok, saida, _ = self._sql_editor({self.SETUP: "alter table leads drop x;"}, confirmar=True)
        self.assertTrue(ok)
        self.assertIn(self.SETUP, saida)

    def test_pendentes_ou_none(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [f.unlink() for f in d.glob("*")] and d.rmdir())
        (d / self.SETUP).write_text("select 1;")
        with mock.patch.object(etapa_banco, "MIGRATIONS_DIR", d):
            self.assertIsNone(etapa_banco.pendentes_ou_none(1, "erro"))
            self.assertIsNone(etapa_banco.pendentes_ou_none(0, "saída que não se entende"))
            self.assertEqual(etapa_banco.pendentes_ou_none(0, "Remote database is up to date."), [])
            self.assertEqual([a.name for a in etapa_banco.pendentes_ou_none(0, "Would push:\n • " + self.SETUP)], [self.SETUP])

    def test_falha_do_link_passa_lista_desconhecida_ao_plano_b(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [f.unlink() for f in d.glob("*")] and d.rmdir())
        (d / self.SETUP).write_text("select 1;")
        cfg = {"supabase": {"url": "https://abcdefghijklmnopqrst.supabase.co", "service_role_key": "x", "project_ref": "abcdefghijklmnopqrst"}}
        with mock.patch.object(etapa_banco, "MIGRATIONS_DIR", d), mock.patch.object(etapa_banco, "load_config", return_value=cfg), \
                mock.patch.object(etapa_banco, "achar_cli", return_value=["supabase"]), \
                mock.patch.object(etapa_banco, "entrada_externa", return_value="senha"), \
                mock.patch.object(etapa_banco, "rodar", return_value=(1, "login expirou")), mock.patch.object(etapa_banco, "erro"), \
                mock.patch.object(etapa_banco, "instrucoes_sql_editor") as plano:
            codigo = etapa_banco.cmd_migrar(argparse.Namespace(dry_run=False, confirmar_destrutivo=False))
        self.assertEqual(codigo, 1)
        plano.assert_called_once_with(None, False)


# ------------------------------------------------------------------ 17: valores do modelo

def _entrevista_real():
    d = copy.deepcopy(etapa_config.entrevista_modelo())
    d["negocio"] = {"empresa": "Clínica Sorriso", "oferta": "Clareamento dental", "descricao": ""}
    d["dono"] = {"whatsapp": "+5585988880001", "email": "dona@clinicasorriso.com.br"}
    d["closers"][0].update({"nome": "Marina", "whatsapp": "+5585988880002", "email": ""})
    d["telefones_teste"] = ["+5585988880003"]
    return d


class ValoresDoModelo(unittest.TestCase):
    def test_entrevista_real_e_valida(self):
        self.assertEqual(etapa_config.validar_entrevista(_entrevista_real()), [])

    def test_modelo_intacto_e_recusado_com_o_campo_a_trocar(self):
        texto = " | ".join(etapa_config.validar_entrevista(etapa_config.entrevista_modelo()))
        for campo in ("negocio.empresa", "negocio.oferta", "dono.whatsapp", "dono.email", "closers[0].nome",
                      "closers[0].whatsapp", "telefones_teste"):
            self.assertIn(campo, texto)

    def test_cada_valor_de_exemplo_sozinho_e_recusado(self):
        casos = [("negocio", "empresa", "  nome do seu negócio "), ("dono", "whatsapp", "+55 11 99999-0000"),
                 ("dono", "email", "VOCE@exemplo.com")]
        for grupo, campo, valor in casos:
            d = _entrevista_real()
            d[grupo][campo] = valor
            self.assertTrue(any("%s.%s" % (grupo, campo) in e for e in etapa_config.validar_entrevista(d)), (grupo, campo))
        d = _entrevista_real()
        d["closers"][0]["nome"] = "closer  1"
        self.assertTrue(any("closers[0].nome" in e for e in etapa_config.validar_entrevista(d)))
        d = _entrevista_real()
        d["closers"][0]["whatsapp"] = "+5511999990001"
        self.assertTrue(any("closers[0].whatsapp" in e for e in etapa_config.validar_entrevista(d)))
        d = _entrevista_real()
        d["telefones_teste"] = ["+5511999990001"]
        self.assertTrue(any("telefones_teste" in e for e in etapa_config.validar_entrevista(d)))

    def test_banco_recusa_o_modelo(self):
        with mock.patch.object(etapa_config, "carregar_entrevista", return_value=etapa_config.entrevista_modelo()), \
                mock.patch.object(etapa_config, "erro") as erro, mock.patch.object(etapa_config, "supabase_rest") as rest:
            codigo = etapa_config.cmd_banco(argparse.Namespace(partes="", dry_run=False, sobrescrever=False, desativar_fora=False))
        self.assertEqual(codigo, 1)
        rest.assert_not_called()
        self.assertIn("negocio.empresa", erro.call_args[0][0])


# ------------------------------------------------------------------ 18: credenciais

class CredenciaisStatus(unittest.TestCase):
    def _rodar(self, status):
        args = argparse.Namespace(url="https://abcdefghijklmnopqrst.supabase.co", anon_key="sb_publishable_abc", trocar_chaves=False,
                                  dry_run=False)
        with mock.patch.object(etapa_banco, "entrada_externa", return_value="sb_secret_abc"), \
                mock.patch.object(etapa_banco, "supabase_rest", return_value=(status, "corpo")), \
                mock.patch.object(etapa_banco, "http_json", return_value=(200, {})), \
                mock.patch.object(etapa_banco, "atualizar_config") as grava, mock.patch.object(etapa_banco, "erro") as erro, \
                mock.patch.object(etapa_banco, "ok"):
            codigo = etapa_banco.cmd_credenciais(args)
        return codigo, grava, erro

    def test_so_200_grava(self):
        codigo, grava, _ = self._rodar(200)
        self.assertEqual(codigo, 0)
        grava.assert_called_once()

    def test_outros_status_abortam_sem_gravar_e_mostram_o_codigo(self):
        for st in (204, 404, 500, 503):
            codigo, grava, erro = self._rodar(st)
            self.assertEqual(codigo, 1, st)
            grava.assert_not_called()
            self.assertIn(str(st), erro.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
