"""Rodada 26 (instalador): install no macOS reabilita o serviço desativado pelo stop (launchctl enable antes do bootstrap)
e as definições do serviço reiniciam o motor se ele sair. Tudo mockado: nenhum launchctl/systemctl/schtasks real."""
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


class InstallMacReabilita(unittest.TestCase):
    def setUp(self):
        self.cmds = []
        self.desativado = False  # estado simulado do launchd
        self.subiu = False
        for p in (mock.patch.object(servico, "sistema", return_value="Darwin"),
                  mock.patch.object(servico.Executor, "cmd", lambda ex, c, tolerar=False: self._cmd(c)),
                  mock.patch.object(servico.Executor, "arquivo", lambda ex, *a, **k: None),
                  mock.patch.object(servico, "preparar_instalacao", return_value=True),
                  mock.patch.object(servico, "_checar_motor_alheio", return_value=False),
                  mock.patch.object(servico, "_encerrar_motores_proprios", return_value=False),
                  mock.patch.object(servico, "garantir_motor_parado", return_value=0),
                  mock.patch.object(servico, "pid_do_servico", return_value=(None, True)),
                  mock.patch.object(servico, "servico_assumiu_o_motor", return_value=True),
                  mock.patch.object(servico, "salvar_estado"),
                  mock.patch.object(servico, "motor_subiu", side_effect=lambda *a, **k: self.subiu),
                  mock.patch.object(servico, "registrar_identidade_motor", return_value=True),
                  mock.patch.object(servico, "caminho_plist", return_value=Path("/x/p.plist")),
                  mock.patch.object(servico, "erro"), mock.patch.object(servico, "ok"),
                  mock.patch.object(servico, "info"), mock.patch.object(servico, "aviso"),
                  mock.patch.object(servico, "titulo")):
            p.start()
            self.addCleanup(p.stop)

    def _cmd(self, c):
        self.cmds.append(list(c))
        if c[:2] == ["launchctl", "disable"]:
            self.desativado = True
        elif c[:2] == ["launchctl", "enable"]:
            self.desativado = False
        elif c[:2] == ["launchctl", "bootstrap"]:
            # launchd real: serviço desativado não é carregado e o bootstrap falha
            if self.desativado:
                return 5, "Bootstrap failed: 5: Input/output error"
        elif c[:2] == ["launchctl", "kickstart"]:
            self.subiu = not self.desativado
        return 0, ""

    def test_enable_vem_antes_do_bootstrap(self):
        servico.instalar(servico.Executor(False))
        nomes = [c[:2] for c in self.cmds]
        self.assertIn(["launchctl", "enable"], nomes)
        self.assertLess(nomes.index(["launchctl", "enable"]), nomes.index(["launchctl", "bootstrap"]))
        self.assertIn(["launchctl", "enable", ALVO], self.cmds)

    def test_stop_seguido_de_install_sobe_o_motor(self):
        self.desativado = True  # o stop gravou launchctl disable
        codigo = servico.instalar(servico.Executor(False))
        self.assertEqual(codigo, 0)
        self.assertFalse(self.desativado)
        self.assertTrue(self.subiu)


class ReinicioAutomatico(unittest.TestCase):
    def test_plist_mac_tem_keepalive_e_throttle(self):
        xml = servico.plist_mac("/usr/bin/python3", REPO, "/tmp/l.log")
        self.assertIn("<key>KeepAlive</key><true/>", xml)
        self.assertIn("<key>ThrottleInterval</key>", xml)

    def test_unit_systemd_reinicia_com_intervalo(self):
        unit = servico.unit_linux("/usr/bin/python3", REPO, "/tmp/l.log")
        self.assertIn("Restart=always", unit)
        self.assertIn("RestartSec=15", unit)

    def test_tarefa_windows_reinicia_em_falha(self):
        self.assertIn("<RestartOnFailure>", servico.xml_tarefa_windows("python", REPO, "C:\\l.log"))


if __name__ == "__main__":
    unittest.main()
