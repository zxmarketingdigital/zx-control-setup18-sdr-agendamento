"""Rodada 22 (instalador): varredura de processos que falha (feito=False) = recusa (exit 1) antes de qualquer alteração,
nos 3 sistemas. Tudo mockado: nenhum processo real é listado, sinalizado nem serviço tocado."""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "setup"))

import servico  # noqa: E402

MSG = "Não consegui conferir se já existe outro motor rodando neste computador."


def setUpModule():
    from tests.setup import _isolamento
    _isolamento.ligar()


def tearDownModule():
    from tests.setup import _isolamento
    _isolamento.desligar()


class VarreduraFalhaRecusa(unittest.TestCase):
    def _instalar(self, so, primeira_ok=False):
        d = tempfile.mkdtemp()
        varreduras = [([], [], [], False)] if not primeira_ok else [([], [], [], True), ([], [], [], False)]
        comandos, arquivos = [], []
        ex = servico.Executor(False)
        ex.cmd = lambda c, tolerar=False, **k: (comandos.append(list(c)) or (0, ""))
        ex.arquivo = lambda caminho, conteudo: arquivos.append(str(caminho))
        with mock.patch.object(servico, "sistema", return_value=so), \
                mock.patch.object(servico, "preparar_instalacao", return_value=True), \
                mock.patch.object(servico, "python_cmd", return_value="python3"), \
                mock.patch.object(servico, "LOG_DIR", Path(d)), \
                mock.patch.object(servico, "_varrer_completo", side_effect=varreduras + [([], [], [], False)] * 3), \
                mock.patch.object(servico, "encerrar_dono_da_trava"), \
                mock.patch.object(servico, "_encerrar_pid") as matar, \
                mock.patch.object(servico, "_tarefa_win", create=True), \
                mock.patch.object(servico, "motor_subiu", return_value=True), \
                mock.patch.object(servico, "salvar_estado"), \
                redirect_stdout(io.StringIO()) as saida:
            codigo = servico.instalar(ex)
        matar.assert_not_called()
        return codigo, comandos, arquivos, saida.getvalue(), Path(d)

    def test_instalar_recusa_nos_tres_sos_sem_gravar_nem_subir(self):
        for so in ("Darwin", "Linux", "Windows"):
            codigo, comandos, arquivos, saida, d = self._instalar(so)
            self.assertEqual(codigo, 1, so)
            self.assertEqual(comandos, [], so)
            self.assertEqual(arquivos, [], so)
            self.assertEqual(list(d.iterdir()), [], so)

    def test_mensagem_exata(self):
        self.assertIn(MSG, servico.MSG_VARREDURA_FALHOU)
        self.assertIn("python setup/servico.py stop na pasta antiga", servico.MSG_VARREDURA_FALHOU)
        with mock.patch.object(servico, "_varrer_completo", return_value=([], [], [], False)), \
                mock.patch.object(servico, "erro") as erro:
            self.assertEqual(servico._checar_motor_alheio(servico.Executor(False)), 1)
            self.assertEqual(servico._encerrar_motores_proprios(servico.Executor(False)), 1)
        self.assertEqual(erro.call_count, 2)
        for c in erro.call_args_list:
            self.assertEqual(c.args[0], servico.MSG_VARREDURA_FALHOU)

    def test_segunda_varredura_falhando_recusa_e_nao_sobe_o_servico(self):
        for so in ("Darwin", "Linux", "Windows"):
            codigo, comandos, _, _, _ = self._instalar(so, primeira_ok=True)
            self.assertEqual(codigo, 1, so)
            nomes = [" ".join(c) for c in comandos]
            self.assertFalse(any("bootstrap" in n or "enable --now" in n or "/Run" in n for n in nomes), (so, nomes))

    def test_dry_run_segue_so_com_aviso(self):
        ex = servico.Executor(True)
        with mock.patch.object(servico, "_varrer_completo", side_effect=AssertionError("não varre em simulação")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(servico._checar_motor_alheio(ex), 0)
            self.assertEqual(servico._encerrar_motores_proprios(ex), 0)


if __name__ == "__main__":
    unittest.main()
