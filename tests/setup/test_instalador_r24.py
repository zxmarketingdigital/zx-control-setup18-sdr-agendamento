"""Rodada 24 (instalador): start/restart aplicam a guarda do install (motor de outra pasta recusa; motor próprio avulso é
encerrado) e stop persiste no macOS e no Linux como no Windows. Tudo mockado: nenhum launchctl/systemctl/schtasks real."""
import sys
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "setup"))

import servico  # noqa: E402

REPO = Path("/srv/setup18")
ALVO = "gui/%s/%s" % (servico._uid(), servico.LABEL_MAC)


def setUpModule():
    from tests.setup import _isolamento
    _isolamento.ligar()


def tearDownModule():
    from tests.setup import _isolamento
    _isolamento.desligar()


class Base(unittest.TestCase):
    so = "Darwin"

    def setUp(self):
        self.cmds = []
        self.eventos = []
        for p in (mock.patch.object(servico, "sistema", return_value=self.so),
                  mock.patch.object(servico.Executor, "cmd", lambda ex, c, tolerar=False: self._cmd(c)),
                  mock.patch.object(servico, "garantir_motor_parado", side_effect=lambda ex, **k: self.eventos.append("<garantir>") or 0),
                  mock.patch.object(servico, "erro"), mock.patch.object(servico, "ok"),
                  mock.patch.object(servico, "info"), mock.patch.object(servico, "aviso"),
                  mock.patch.object(servico, "motor_subiu", return_value=True),
                  mock.patch.object(servico, "registrar_identidade_motor", return_value=True),
                  mock.patch.object(servico, "encerrar_dono_da_trava", side_effect=lambda ex: self.eventos.append("<dono>")),
                  mock.patch.object(servico, "caminho_plist", return_value=Path("/x/p.plist"))):
            p.start()
            self.addCleanup(p.stop)
        self.codigos = {}

    def _cmd(self, c):
        self.cmds.append(list(c))
        self.eventos.append(" ".join(c[1:3]) if c[0] == "systemctl" else " ".join(c[:2]))
        return self.codigos.get(" ".join(c[:3]), 0), ""

    def varredura(self, proprios=(), incertos=(), outros=(), feito=True):
        return mock.patch.object(servico, "_varrer_completo", return_value=(list(proprios), list(incertos), list(outros), feito))


    def com_avulso(self, pid):
        """Varreduras na ordem real: guarda (vê o avulso próprio), encerramento (vê de novo) e conferência final (sumiu)."""
        proprio = ([pid], [], [], True)
        return mock.patch.object(servico, "_varrer_completo", side_effect=[proprio, proprio, ([], [], [], True)])


class GuardaStartRestart(Base):
    def _recusa(self, acao, **v):
        with self.varredura(**v), mock.patch.object(servico, "_encerrar_pid") as matar:
            codigo = servico.controlar(servico.Executor(False), acao)
        self.assertEqual(codigo, 1)
        matar.assert_not_called()
        self.assertFalse([c for c in self.cmds if c[:2] in (["launchctl", "bootstrap"], ["launchctl", "kickstart"], ["schtasks", "/Run"])
                          or c[:3] == ["systemctl", "--user", "enable"]])
        msg = servico.erro.call_args[0][0]
        return msg

    def test_outra_pasta_recusa_com_pid_e_pasta(self):
        msg = self._recusa("start", outros=[(777, "/copia/setup18")])
        self.assertIn("777", msg)
        self.assertIn("/copia/setup18", msg)
        self.assertIn("servico.py stop", msg)

    def test_identidade_desconhecida_recusa(self):
        self.assertIn("555", self._recusa("restart", incertos=[555]))

    def test_varredura_que_falha_recusa(self):
        self._recusa("start", feito=False)

    def test_motor_proprio_avulso_e_encerrado_antes_de_subir(self):
        with self.com_avulso(321), mock.patch.object(servico, "_encerrar_pid", side_effect=lambda *a: self.eventos.append("<kill321>")):
            self.assertEqual(servico.controlar(servico.Executor(False), "start"), 0)
        self.assertLess(self.eventos.index("<kill321>"), self.eventos.index("launchctl bootstrap"))

    def test_guarda_roda_antes_de_qualquer_comando(self):
        with self.varredura(outros=[(1, "/o")]):
            servico.controlar(servico.Executor(False), "restart")
        self.assertEqual(self.cmds, [])


class GuardaLinuxWindows(unittest.TestCase):
    def _rodar(self, so, acao):
        cmds = []
        with mock.patch.object(servico, "sistema", return_value=so), \
                mock.patch.object(servico.Executor, "cmd", lambda ex, c, tolerar=False: (cmds.append(list(c)) or (0, ""))), \
                mock.patch.object(servico, "_varrer_completo", return_value=([], [], [(9, "/outra")], True)), \
                mock.patch.object(servico, "erro"):
            return servico.controlar(servico.Executor(False), acao), cmds

    def test_linux_e_windows_recusam_start_e_restart_sem_tocar_em_nada(self):
        for so in ("Linux", "Windows"):
            for acao in ("start", "restart"):
                codigo, cmds = self._rodar(so, acao)
                self.assertEqual((codigo, cmds), (1, []), (so, acao))


class MacPersistente(Base):
    def test_stop_ordem_exata(self):
        self.codigos["launchctl print %s" % ALVO] = 113
        self.assertEqual(servico.controlar(servico.Executor(False), "stop"), 0)
        self.assertEqual(self.cmds, [["launchctl", "bootout", ALVO], ["launchctl", "disable", ALVO], ["launchctl", "print", ALVO]])
        self.assertEqual(self.eventos[-1], "<garantir>")

    def test_stop_com_disable_falhando_e_erro(self):
        self.codigos["launchctl print %s" % ALVO] = 113
        self.codigos["launchctl disable %s" % ALVO] = 1
        self.assertEqual(servico.controlar(servico.Executor(False), "stop"), 1)
        self.assertIn("launchctl disable", servico.erro.call_args[0][0])

    def test_start_e_restart_reabilitam_e_sobem_na_ordem(self):
        for acao in ("start", "restart"):
            self.cmds.clear()
            with self.varredura():
                self.assertEqual(servico.controlar(servico.Executor(False), acao), 0)
            self.assertEqual(self.cmds, [["launchctl", "enable", ALVO], ["launchctl", "bootout", ALVO],
                                         ["launchctl", "bootstrap", "gui/%s" % servico._uid(), "/x/p.plist"],
                                         ["launchctl", "kickstart", "-k", ALVO]], acao)

    def test_status_de_descarregado_com_plist_diz_parado_pelo_stop(self):
        with mock.patch.object(servico, "rodar", return_value=(113, "")), \
                mock.patch.object(Path, "exists", return_value=True):
            self.assertEqual(servico.status(servico.Executor(False)), 1)
        self.assertIn("desativado pelo stop", servico.aviso.call_args[0][0])


class LinuxPersistente(Base):
    so = "Linux"

    def test_stop_usa_disable_now(self):
        self.assertEqual(servico.controlar(servico.Executor(False), "stop"), 0)
        self.assertEqual(self.cmds, [["systemctl", "--user", "disable", "--now", servico.UNIT_LINUX]])
        self.assertEqual(self.eventos[-1], "<garantir>")

    def test_start_e_restart_habilitam_com_now_depois_de_encerrar_o_proprio(self):
        for acao in ("start", "restart"):
            self.cmds.clear()
            self.eventos.clear()
            with self.com_avulso(42), mock.patch.object(servico, "_encerrar_pid", side_effect=lambda *a: self.eventos.append("<kill42>")):
                self.assertEqual(servico.controlar(servico.Executor(False), acao), 0)
            self.assertEqual(self.cmds, [["systemctl", "--user", "stop", servico.UNIT_LINUX],
                                         ["systemctl", "--user", "enable", "--now", servico.UNIT_LINUX]], acao)
            self.assertIn("<kill42>", self.eventos)

    def test_status_de_unit_desabilitada_diz_parado_pelo_stop(self):
        self.codigos["systemctl --user is-enabled"] = 1
        with mock.patch.object(Path, "exists", return_value=True):
            servico.controlar(servico.Executor(False), "status")
        self.assertIn("desativado pelo stop", servico.aviso.call_args[0][0])

    def test_status_de_unit_habilitada_nao_avisa(self):
        with mock.patch.object(Path, "exists", return_value=True):
            servico.controlar(servico.Executor(False), "status")
        servico.aviso.assert_not_called()


class WindowsEncerraProprio(Base):
    so = "Windows"

    def test_start_encerra_tarefa_e_dono_antes_do_run(self):
        with self.varredura(), mock.patch.object(servico, "_subir_windows", return_value=0):
            servico.controlar(servico.Executor(False), "start")
        r = [c[1] for c in self.cmds if c[0] == "schtasks"]
        self.assertEqual(r, ["/Change", "/End", "/Run"])
        self.assertLess(self.eventos.index("<dono>"), self.eventos.index("schtasks /Run"))

    def test_start_com_motor_proprio_avulso_encerra_antes_do_run(self):
        with self.com_avulso(8), mock.patch.object(servico, "_encerrar_pid", side_effect=lambda *a: self.eventos.append("<kill8>")), \
                mock.patch.object(servico, "_subir_windows", return_value=0):
            servico.controlar(servico.Executor(False), "start")
        self.assertLess(self.eventos.index("<kill8>"), self.eventos.index("schtasks /Run"))


if __name__ == "__main__":
    unittest.main()
