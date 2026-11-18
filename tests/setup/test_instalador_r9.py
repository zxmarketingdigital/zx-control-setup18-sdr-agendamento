"""Rodada 9: troca de dono revoga por consulta, guard destrutivo por nível de parênteses e fail-closed, lista parcial de
pendências, verificar exige as RPCs e a desinstalação só vale conferida (subprocess e HTTP mockados)."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "setup"))

import etapa_banco  # noqa: E402
import servico  # noqa: E402

CFG = {"supabase": {"url": "https://abcdefghijklmnopqrst.supabase.co", "service_role_key": "x"}}


def setUpModule():
    from tests.setup import _isolamento
    _isolamento.ligar()


def tearDownModule():
    from tests.setup import _isolamento
    _isolamento.desligar()


class TrocaDeDonoPorConsulta(unittest.TestCase):
    def _rodar(self, delete, restantes=(200, [])):
        chamadas = []

        def rest(config, metodo, caminho, *a, **k):
            chamadas.append((metodo, caminho))
            if metodo == "DELETE":
                return delete
            return restantes if metodo == "GET" else (201, None)
        with mock.patch.object(etapa_banco, "load_config", return_value=CFG), \
                mock.patch.object(etapa_banco, "load_estado", return_value={}), \
                mock.patch.object(etapa_banco, "http_json", side_effect=lambda m, u, **kw: (200, {"users": []}) if m == "GET" else (201, {"id": "u-novo"})), \
                mock.patch.object(etapa_banco, "supabase_rest", rest), mock.patch.object(etapa_banco, "_senha_dono", return_value="senha-longa-123"), \
                mock.patch.object(etapa_banco, "salvar_estado") as salvar, mock.patch.object(etapa_banco, "aviso") as aviso:
            codigo = etapa_banco.cmd_dono(mock.Mock(email="novo@exemplo.com", dry_run=False))
        return codigo, chamadas, salvar, aviso

    def test_estado_ausente_ainda_revoga_os_outros_e_diz_quem_saiu(self):
        codigo, chamadas, salvar, aviso = self._rodar((200, [{"user_id": "u-a", "email": "a@exemplo.com"}]))
        self.assertEqual(codigo, 0)
        self.assertIn(("DELETE", "/panel_users?user_id=neq.u-novo"), chamadas)
        self.assertIn("a@exemplo.com", aviso.call_args[0][0])
        salvar.assert_called_once_with({"dono_email": "novo@exemplo.com", "dono_user_id": "u-novo"})

    def test_delete_500_sai_diferente_de_zero_sem_gravar_o_novo_dono(self):
        codigo, _, salvar, _ = self._rodar((500, "erro"))
        self.assertNotEqual(codigo, 0)
        salvar.assert_not_called()

    def test_sobra_alguem_depois_do_delete_nao_grava(self):
        codigo, _, salvar, _ = self._rodar((204, None), restantes=(200, [{"user_id": "u-a"}]))
        self.assertNotEqual(codigo, 0)
        salvar.assert_not_called()


class GuardDestrutivo(unittest.TestCase):
    def test_delete_sem_where_dentro_de_cte_com_where_fora(self):
        sql = "WITH x AS (DELETE FROM leads) SELECT count(*) FROM x WHERE true;"
        self.assertIn("DELETE FROM sem WHERE", etapa_banco.sql_destrutivo(sql))

    def test_delete_com_where_no_mesmo_nivel_ou_subconsulta_ok(self):
        for sql in ("DELETE FROM leads WHERE id = 1;",
                    "WITH x AS (DELETE FROM leads WHERE id = 1 RETURNING id) SELECT * FROM x;",
                    "DELETE FROM leads WHERE id IN (SELECT id FROM t WHERE a);"):
            self.assertEqual(etapa_banco.sql_destrutivo(sql), [], sql)

    def test_where_so_em_subconsulta_nao_protege_o_delete(self):
        self.assertIn("DELETE FROM sem WHERE", etapa_banco.sql_destrutivo("DELETE FROM leads USING (SELECT 1 WHERE true) s;"))

    def test_arquivo_ilegivel_e_achado(self):
        arq = mock.Mock()
        arq.name = "20270101000001_x.sql"
        arq.read_text.side_effect = PermissionError(13, "Permission denied")
        perigos = etapa_banco.perigos_das([arq])
        self.assertIn(arq.name, perigos)
        self.assertIn("não consegui ler", perigos[arq.name][0])


class ListaParcial(unittest.TestCase):
    def test_citada_uma_analisa_todas_a_partir_dela(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: ([f.unlink() for f in d.glob("*")], d.rmdir()))
        for n in ("20260101000001_antiga.sql", "20260301000001_a.sql", "20260302000001_b.sql"):
            (d / n).write_text("select 1;")
        (d / "20260302000001_b.sql").write_text("ALTER TABLE leads DROP COLUMN x;")
        with mock.patch.object(etapa_banco, "MIGRATIONS_DIR", d):
            pend = etapa_banco.pendentes_ou_none(0, "Would push:\n • 20260301000001_a.sql")
            self.assertEqual([a.name for a in pend], ["20260301000001_a.sql", "20260302000001_b.sql"])
            self.assertIn("20260302000001_b.sql", etapa_banco.perigos_das(pend))


class VerificarExigeRpcs(unittest.TestCase):
    def _verificar(self, paths):
        chamadas = []

        def rest(config, metodo, caminho, *a, **k):
            chamadas.append((metodo, caminho))
            if caminho == "/":
                return 200, {"paths": {p: {} for p in paths}}
            return 200, []
        with mock.patch.object(etapa_banco, "load_config", return_value=CFG), mock.patch.object(etapa_banco, "supabase_rest", rest), \
                mock.patch.object(etapa_banco, "salvar_estado") as salvar, mock.patch.object(etapa_banco, "erro") as erro, \
                mock.patch.object(etapa_banco, "instrucoes_sql_editor"):
            codigo = etapa_banco.cmd_verificar(mock.Mock(confirmar_destrutivo=False))
        return codigo, salvar, erro, chamadas

    def test_tudo_presente_grava_banco_migrado(self):
        codigo, salvar, _, chamadas = self._verificar(["/rpc/" + r for r in etapa_banco.RPCS])
        self.assertEqual(codigo, 0)
        salvar.assert_called_once_with({"banco_migrado": True})
        self.assertFalse(any(m != "GET" or "/rpc/" in c for m, c in chamadas))  # nunca chama RPC para testar

    def test_rpc_faltando_sai_diferente_de_zero_e_lista(self):
        codigo, salvar, erro, _ = self._verificar(["/rpc/book_slot", "/rpc/public_slots"])
        self.assertEqual(codigo, 1)
        for r in ("public_book", "reschedule_slot", "set_appointment_status"):
            self.assertIn("/rpc/" + r, erro.call_args[0][0])
        self.assertNotIn(mock.call({"banco_migrado": True}), salvar.call_args_list)

    def test_openapi_ilegivel_e_fail_closed(self):
        with mock.patch.object(etapa_banco, "supabase_rest", return_value=(200, "texto")):
            self.assertTrue(etapa_banco._rpcs_ausentes(CFG))


class FalsoSO:
    def __init__(self, delete_codigo=0, apaga=True, plist=None, unit=None):
        self.delete_codigo, self.apaga, self.chamadas = delete_codigo, apaga, []
        self.existe = True

    def __call__(self, c, **kw):
        self.chamadas.append(list(c))
        prog = c[0]
        if prog == "schtasks" and c[1] == "/Delete":
            if self.apaga:
                self.existe = False
            return self.delete_codigo, "negado" if self.delete_codigo else "ok"
        if prog == "schtasks" and c[1] == "/Query":
            return (0, "x") if self.existe else (1, "não existe")
        if prog == "launchctl" and c[1] == "list":
            return (0, "x") if self.existe else (113, "")
        if prog == "launchctl" and c[1] == "bootout":
            if self.apaga:
                self.existe = False
            return 0, ""
        if prog == "systemctl" and "is-enabled" in c:
            return (0, "enabled") if self.existe else (1, "disabled")
        if prog == "systemctl" and "disable" in c:
            if self.apaga:
                self.existe = False
            return self.delete_codigo, ""
        return 0, ""


class DesinstalacaoConferida(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.plist, self.unit = d / "x.plist", d / "x.service"
        for p in (mock.patch.object(servico, "LOCK_FILE", d / "motor.lock"), mock.patch.object(servico, "caminho_plist", return_value=self.plist),
                  mock.patch.object(servico, "caminho_unit", return_value=self.unit), mock.patch.object(servico, "salvar_estado"),
                  mock.patch.object(servico, "garantir_motor_parado", return_value=0)):
            p.start()
            self.addCleanup(p.stop)

    def _rodar(self, so, falso):
        with mock.patch.object(servico, "sistema", return_value=so), mock.patch.object(servico, "rodar", falso), \
                mock.patch.object(servico, "erro") as erro, mock.patch.object(servico, "info"):
            return servico.remover(servico.Executor(False)), erro

    def test_windows_delete_negado_e_tarefa_continua_sai_1_sem_gravar(self):
        codigo, erro = self._rodar("Windows", FalsoSO(delete_codigo=1, apaga=False))
        self.assertEqual(codigo, 1)
        self.assertIn("NÃO foi removido", erro.call_args[0][0])
        servico.salvar_estado.assert_not_called()

    def test_windows_removida_e_conferida_grava(self):
        falso = FalsoSO()
        self.assertEqual(self._rodar("Windows", falso)[0], 0)
        self.assertIn(["schtasks", "/Query", "/TN", servico.TAREFA_WIN], falso.chamadas)
        servico.salvar_estado.assert_called_once_with({"servico_instalado": False})

    def test_linux_unit_ainda_habilitada_ou_arquivo_existente_falha(self):
        self.unit.write_text("[Unit]")
        codigo, _ = self._rodar("Linux", FalsoSO(delete_codigo=1, apaga=False))
        self.assertEqual(codigo, 1)
        servico.salvar_estado.assert_not_called()

    def test_linux_ok_quando_some(self):
        self.assertEqual(self._rodar("Linux", FalsoSO())[0], 0)

    def test_mac_ainda_carregado_falha(self):
        codigo, _ = self._rodar("Darwin", FalsoSO(apaga=False))
        self.assertEqual(codigo, 1)
        servico.salvar_estado.assert_not_called()

    def test_mac_plist_que_nao_saiu_falha(self):
        self.plist.write_text("<plist/>")
        with mock.patch.object(servico.Executor, "remover"):  # simula arquivo que não dá para apagar
            codigo, erro = self._rodar("Darwin", FalsoSO())
        self.assertEqual(codigo, 1)
        self.assertIn(str(self.plist), erro.call_args[0][0])

    def test_mac_removido_ok(self):
        self.assertEqual(self._rodar("Darwin", FalsoSO())[0], 0)


if __name__ == "__main__":
    unittest.main()
