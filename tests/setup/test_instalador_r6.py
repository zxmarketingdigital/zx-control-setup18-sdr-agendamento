"""Rodada 6: closers por nome normalizado e sem apagar edição do painel, parada do motor só do PID certo,
migration destrutiva exige confirmação, troca de dono remove o anterior."""
import argparse
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
from tests.setup.test_instalador_r5 import FalsoSO  # noqa: E402


def setUpModule():
    from tests.setup import _isolamento
    _isolamento.ligar()


def tearDownModule():
    from tests.setup import _isolamento
    _isolamento.desligar()


AV = [{"weekday": 1, "start_time": "09:00:00", "end_time": "18:00:00"}]


def _closer(nome, informados, **extra):
    c = {"name": nome, "whatsapp": None, "email": None, "weight": 1, "color": "#D97706", "active": True,
         "availability": AV, "_informados": informados}
    c.update(extra)
    return c


def _aplicar(payload_closers, banco, **kw):
    escritas = []

    def rest(config, metodo, caminho, corpo=None, extra_headers=None, timeout=None):
        escritas.append((metodo, caminho, corpo))
        if metodo == "GET" and caminho.startswith("/closers"):
            return 200, banco
        if metodo == "GET":
            return 200, []
        if metodo == "POST":
            return 201, [{"id": "novo"}]
        return 204, None
    payload = {"agent_config": {}, "schedule_settings": {}, "closers": payload_closers}
    with mock.patch.object(etapa_config, "supabase_rest", side_effect=rest):
        resultado = etapa_config.aplicar_no_banco({}, payload, ["closers"], **kw)
    return resultado, escritas


class ClosersR6(unittest.TestCase):
    def test_nome_com_caixa_ou_espaco_diferente_reaproveita_o_id(self):
        banco = [{"id": "c1", "name": "Ana  Souza", "active": True}]
        ok, escritas = _aplicar([_closer("ana souza", [])], banco)
        self.assertTrue(ok)
        self.assertFalse([e for e in escritas if e[0] == "POST" and e[1] == "/closers"])
        patch = [e for e in escritas if e[0] == "PATCH" and e[1] == "/closers?id=eq.c1"]
        self.assertEqual(len(patch), 1)
        self.assertEqual(patch[0][2]["name"], "ana souza")  # renomeia a grafia
        self.assertFalse([e for e in escritas if e[0] == "PATCH" and e[2] == {"active": False}])

    def test_patch_omite_o_que_a_entrevista_nao_preencheu(self):
        banco = [{"id": "c1", "name": "Ana", "active": True}]
        ok, escritas = _aplicar([_closer("Ana", ["weight"], weight=3)], banco)
        self.assertTrue(ok)
        corpo = [e[2] for e in escritas if e[0] == "PATCH" and e[1] == "/closers?id=eq.c1"][0]
        self.assertEqual(corpo, {"name": "Ana", "weight": 3})
        for campo in ("whatsapp", "email", "color", "active"):
            self.assertNotIn(campo, corpo)

    def test_closer_novo_recebe_todos_os_campos(self):
        ok, escritas = _aplicar([_closer("Bia", [])], [])
        corpo = [e[2] for e in escritas if e[0] == "POST" and e[1] == "/closers"][0]
        self.assertIn("color", corpo)
        self.assertNotIn("_informados", corpo)

    def test_montar_marca_so_o_que_veio_da_entrevista(self):
        d = etapa_config.entrevista_modelo()
        d["closers"] = [{"nome": "Ana", "horarios": [{"dias": ["seg"], "inicio": "09:00", "fim": "18:00"}]}]
        c = etapa_config.montar(d)["closers"][0]
        self.assertEqual(c["_informados"], [])


class PararMotorR6(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock = Path(self.tmp.name) / "motor.lock"
        self.lock.write_text("4321")
        for p in (mock.patch.object(servico, "LOCK_FILE", self.lock), mock.patch.object(servico, "sistema", return_value="Windows"),
                  mock.patch.object(servico, "_dono_do_processo", return_value=True)):  # r10: foco aqui não é o dono do processo
            p.start()
            self.addCleanup(p.stop)

    def _parar(self, falso):
        with mock.patch.object(servico, "rodar", falso), mock.patch.object(servico.time, "sleep", lambda s: None):
            return servico.garantir_motor_parado(servico.Executor(False), matar_arvore=True, espera=0)

    def test_python_alheio_nao_leva_taskkill_e_a_trava_some(self):
        falso = FalsoSO()
        base = falso.__call__
        alheio = lambda c, **kw: (0, "C:\\Python\\python.exe planilha.py") if c[0] == "powershell" else base(c, **kw)  # noqa: E731
        self.assertEqual(self._parar(alheio), 0)
        self.assertFalse(falso.usou("taskkill"))
        self.assertFalse(self.lock.exists())

    def test_motor_de_verdade_leva_taskkill(self):
        falso = FalsoSO()
        self.assertEqual(self._parar(falso), 0)
        self.assertTrue(falso.usou("taskkill", "/T", "/F", "4321"))
        self.assertTrue(falso.usou("powershell"))

    def test_mac_exige_motor_e_run_na_linha_de_comando(self):
        chamadas = []

        def so_mac(c, **kw):
            chamadas.append(c)
            if c[0] == "ps" and "pid=" in c:
                return 0, "4321"
            return 0, "/usr/bin/python3 outro_script.py"
        with mock.patch.object(servico, "sistema", return_value="Darwin"), mock.patch.object(servico, "rodar", so_mac), \
                mock.patch.object(servico.time, "sleep", lambda s: None):
            self.assertEqual(servico.garantir_motor_parado(servico.Executor(False), espera=0), 0)
        self.assertFalse(self.lock.exists())
        self.assertFalse(any(c[0] == "kill" for c in chamadas))


class MigrarDestrutivo(unittest.TestCase):
    def _migrar(self, sql, confirmar=False):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [f.unlink() for f in d.glob("*")] and d.rmdir())
        (d / "20270101000001_x.sql").write_text(sql)
        cfg = {"supabase": {"url": "https://abcdefghijklmnopqrst.supabase.co", "service_role_key": "x", "project_ref": "abcdefghijklmnopqrst"}}
        chamadas = []

        def rodar(cmd, **kw):
            chamadas.append(cmd)
            return 0, "Would push these migrations:\n • 20270101000001_x.sql"
        args = argparse.Namespace(dry_run=False, confirmar_destrutivo=confirmar)
        with mock.patch.object(etapa_banco, "MIGRATIONS_DIR", d), mock.patch.object(etapa_banco, "load_config", return_value=cfg), \
                mock.patch.object(etapa_banco, "achar_cli", return_value=["supabase"]), \
                mock.patch.object(etapa_banco, "entrada_externa", return_value="senha"), \
                mock.patch.object(etapa_banco, "rodar", rodar), mock.patch.object(etapa_banco, "supabase_rest", lambda c, m, p, *a, **k: (200, {"paths": {"/rpc/" + r: {} for r in etapa_banco.RPCS}} if p == "/" else [])), \
                mock.patch.object(etapa_banco, "erro"):
            codigo = etapa_banco.cmd_migrar(args)
        return codigo, chamadas

    def test_drop_sem_flag_nao_aplica(self):
        for sql in ("alter table t drop column x;", "DROP TABLE t;", "truncate t;", "delete from t;"):
            codigo, chamadas = self._migrar(sql)
            self.assertEqual(codigo, 1, sql)
            self.assertNotIn(["supabase", "db", "push"], chamadas, sql)

    def test_drop_com_flag_aplica(self):
        codigo, chamadas = self._migrar("drop table t;", confirmar=True)
        self.assertEqual(codigo, 0)
        self.assertIn(["supabase", "db", "push"], chamadas)

    def test_migration_comum_segue_sem_flag(self):
        codigo, chamadas = self._migrar("create table t(id int);\ndelete from t where id = 1; -- remover linha")
        self.assertEqual(codigo, 0)
        self.assertIn(["supabase", "db", "push"], chamadas)

    def test_migration_do_setup_nao_e_destrutiva(self):
        for a in etapa_banco.migracoes():
            self.assertEqual(etapa_banco.sql_destrutivo(a.read_text()), [], a.name)


class TrocaDeDono(unittest.TestCase):
    CFG = {"supabase": {"url": "https://abcdefghijklmnopqrst.supabase.co", "service_role_key": "x"}}

    def _rodar(self, novo_id="u-novo", delete=(200, [{"user_id": "u-velho", "email": "velho@exemplo.com"}]), restantes=(200, [])):
        def rest(config, metodo, caminho, *a, **k):
            if metodo == "DELETE":
                return delete
            return restantes if metodo == "GET" else (201, None)
        mock_rest = mock.Mock(side_effect=rest)
        with mock.patch.object(etapa_banco, "load_config", return_value=self.CFG), \
                mock.patch.object(etapa_banco, "http_json", side_effect=lambda m, u, **kw: (200, {"users": []}) if m == "GET" else (201, {"id": novo_id})), \
                mock.patch.object(etapa_banco, "supabase_rest", mock_rest), mock.patch.object(etapa_banco, "_senha_dono", return_value="senha-longa-123"):
            codigo = etapa_banco.cmd_dono(mock.Mock(email="novo@exemplo.com", dry_run=False))
        return codigo, mock_rest

    def test_dono_anterior_sai_de_panel_users_e_o_novo_fica(self):
        _comum.salvar_estado({"dono_email": "velho@exemplo.com", "dono_user_id": "u-velho"})
        codigo, rest = self._rodar()
        self.assertEqual(codigo, 0)
        deletes = [c.args for c in rest.call_args_list if c.args[1] == "DELETE"]
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0][2], "/panel_users?user_id=neq.u-novo")
        self.assertEqual(_comum.load_estado()["dono_user_id"], "u-novo")


if __name__ == "__main__":
    unittest.main()
