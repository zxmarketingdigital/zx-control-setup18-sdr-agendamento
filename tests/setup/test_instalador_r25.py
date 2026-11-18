"""Rodada 25 (instalador): o motor é reconhecido mesmo com opções do Python entre o interpretador e o módulo
(-X utf8, -W ignore, -u, -B, python3.12). Tudo mockado: nenhum processo real é consultado ou encerrado."""
import sys
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "setup"))

import servico  # noqa: E402

REPO = Path("/srv/setup18")
LINHAS_MOTOR = (
    "python -X utf8 -m motor run",
    "python -W ignore -m motor run",
    "python -u -m motor run",
    "python3.12 -B -m motor run",
    "/usr/bin/python3 -X utf8 -u /srv/setup18/motor/cli.py run",
    "python3 -W ignore /srv/setup18/motor/cli.py run",
    "C:\\Python\\pythonw.exe -X utf8 -m motor run",
)


def setUpModule():
    from tests.setup import _isolamento
    _isolamento.ligar()


def tearDownModule():
    from tests.setup import _isolamento
    _isolamento.desligar()


class ReconheceOpcoes(unittest.TestCase):
    def test_opcoes_do_python_antes_do_modulo(self):
        for linha in LINHAS_MOTOR:
            self.assertTrue(servico._e_linha_do_motor(linha), linha)

    def test_continua_recusando_o_que_nao_e_o_motor(self):
        for linha in ("python -X utf8 -m motor", "python -X utf8 -m motor check", "python -W ignore -m http.server run",
                      "python -X utf8 app.py motor run", "python -X utf8"):
            self.assertFalse(servico._e_linha_do_motor(linha), linha)

    def test_identidade_e_desta_instalacao_quando_o_cwd_bate(self):
        for linha in ("python -X utf8 -m motor run", "python -W ignore -m motor run", "python -u -m motor run",
                      "python3.12 -B -m motor run"):
            with mock.patch.object(servico, "sistema", return_value="Darwin"), \
                    mock.patch.object(servico, "REPO_ROOT", REPO), \
                    mock.patch.object(servico, "rodar", return_value=(0, "4242\n")), \
                    mock.patch.object(servico, "_linha_de_comando", return_value=linha), \
                    mock.patch.object(servico, "_cwd_do_processo", return_value=str(REPO)):
                self.assertEqual(servico.identidade_do_pid(4242), "motor", linha)

    def test_motor_de_outra_pasta_com_opcoes_continua_outro(self):
        with mock.patch.object(servico, "sistema", return_value="Darwin"), \
                mock.patch.object(servico, "REPO_ROOT", REPO), \
                mock.patch.object(servico, "rodar", return_value=(0, "4242\n")), \
                mock.patch.object(servico, "_linha_de_comando", return_value="python -X utf8 -m motor run"), \
                mock.patch.object(servico, "_cwd_do_processo", return_value="/outra/pasta"):
            self.assertEqual(servico.identidade_do_pid(4242), "outro")


class StopComMotorComOpcoes(unittest.TestCase):
    def test_stop_encerra_motor_com_x_utf8_e_so_devolve_0_depois(self):
        vivo = {"v": True}
        eventos = []

        def identidade_real_ps(args, timeout=None):
            return (0, "4242\n") if vivo["v"] else (1, "")

        def matar(ex, pid, matar_arvore, dormir):
            eventos.append(("matar", pid))
            vivo["v"] = False

        ps = "4242 python -X utf8 -m motor run\n"

        def rodar(args, timeout=None):
            if args[:2] == ["ps", "-ax"]:
                return (0, ps if vivo["v"] else "")
            return identidade_real_ps(args, timeout)

        with mock.patch.object(servico, "sistema", return_value="Darwin"), \
                mock.patch.object(servico, "REPO_ROOT", REPO), \
                mock.patch.object(servico, "ler_pid_motor", return_value=None), \
                mock.patch.object(servico, "rodar", side_effect=rodar), \
                mock.patch.object(servico, "_cwd_do_processo", return_value=str(REPO)), \
                mock.patch.object(servico, "_linha_de_comando", return_value="python -x utf8 -m motor run"), \
                mock.patch.object(servico, "_encerrar_pid", side_effect=matar), \
                mock.patch.object(servico, "info"), mock.patch.object(servico, "erro"), mock.patch.object(servico, "aviso"):
            codigo = servico.garantir_motor_parado(servico.Executor(False), dormir=lambda s: None)
        self.assertEqual(eventos, [("matar", 4242)])
        self.assertEqual(codigo, 0)


if __name__ == "__main__":
    unittest.main()
