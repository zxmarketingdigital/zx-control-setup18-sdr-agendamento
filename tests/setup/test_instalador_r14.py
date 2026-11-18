"""Rodada 14 (instalador): teste_ok invalidado ao mudar banco/Evolution/IA, prova de dono do motor no Windows sem depender
do cmd.exe pai e http_json que não segue redirect para outro host (subprocess e HTTP mockados)."""
import argparse
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "setup"))

import _comum  # noqa: E402
import etapa_config  # noqa: E402
import servico  # noqa: E402

SUPA = {"url": "https://abcdefghijklmnopqrst.supabase.co", "service_role_key": "x"}


def setUpModule():
    from tests.setup import _isolamento
    _isolamento.ligar()


def tearDownModule():
    from tests.setup import _isolamento
    _isolamento.desligar()


class TesteOkInvalidado(unittest.TestCase):
    def setUp(self):
        _comum.salvar_estado({"teste_ok": True})

    def _teste_ok(self):
        return _comum.load_estado().get("teste_ok")

    def test_ia_grava_false(self):
        _comum.CONFIG_PATH.write_text(json.dumps({"ai": {"provider": "gemini", "api_key": "velha", "model": ""}}))
        os.environ["SDR_AI_API_KEY"] = "chave-nova"
        self.addCleanup(os.environ.pop, "SDR_AI_API_KEY", None)
        with mock.patch.object(etapa_config, "aviso") as aviso:
            codigo = etapa_config.cmd_ia(argparse.Namespace(provider="anthropic", model=None))
        self.assertEqual(codigo, 0)
        self.assertFalse(self._teste_ok())
        self.assertIn("teste guiado", " ".join(c[0][0] for c in aviso.call_args_list))

    def test_ia_que_falha_no_patch_invalida_mesmo_assim(self):  # r15: a escrita parcial já pode ter ocorrido
        _comum.CONFIG_PATH.write_text(json.dumps({"supabase": SUPA}))
        os.environ["SDR_AI_API_KEY"] = "chave-nova"
        self.addCleanup(os.environ.pop, "SDR_AI_API_KEY", None)
        with mock.patch.object(etapa_config, "supabase_rest", return_value=(500, "x")):
            codigo = etapa_config.cmd_ia(argparse.Namespace(provider="openai", model=None))
        self.assertNotEqual(codigo, 0)
        self.assertFalse(self._teste_ok())

    def test_evolution_grava_false(self):
        _comum.CONFIG_PATH.write_text(json.dumps({"evolution": {"url": "http://localhost:8080", "api_key": "k", "instance": "sdr"}}))
        with mock.patch.object(etapa_config, "_estado_instancia", return_value=(200, "open", {})):
            codigo = etapa_config.cmd_evolution(argparse.Namespace(url=None, instance="sdr2", criar=False, trocar_chave=False))
        self.assertEqual(codigo, 0)
        self.assertFalse(self._teste_ok())

    def test_banco_grava_false_antes_da_escrita(self):
        args = argparse.Namespace(partes="agente", dry_run=False, sobrescrever=True, desativar_fora=False)
        _comum.CONFIG_PATH.write_text(json.dumps({"supabase": SUPA}))
        with mock.patch.object(etapa_config, "carregar_entrevista", return_value={"x": 1}), \
                mock.patch.object(etapa_config, "validar_entrevista", return_value=[]), \
                mock.patch.object(etapa_config, "montar", return_value={}), \
                mock.patch.object(etapa_config, "supabase_rest", return_value=(200, [{"company": "A"}])):
            with mock.patch.object(etapa_config, "aplicar_no_banco", return_value=False):
                self.assertEqual(etapa_config.cmd_banco(args), 1)
                self.assertFalse(self._teste_ok())  # r15: invalida antes da escrita, mesmo que falhe no meio
            _comum.salvar_estado({"teste_ok": True})
            with mock.patch.object(etapa_config, "aplicar_no_banco", return_value=True):
                self.assertEqual(etapa_config.cmd_banco(args), 0)
                self.assertFalse(self._teste_ok())


class MotorWindows(unittest.TestCase):
    PID = 4242
    CRIACAO = "2026-11-17T10:00:00.0000000Z"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.lock, self.lock_id = d / "motor.lock", d / "motor.lock.json"
        self.lock.write_text(str(self.PID))
        self.repo = d / "repo"
        for p in (mock.patch.object(servico, "LOCK_FILE", self.lock), mock.patch.object(servico, "LOCK_ID_FILE", self.lock_id),
                  mock.patch.object(servico, "REPO_ROOT", self.repo), mock.patch.object(servico, "sistema", lambda: "Windows"),
                  mock.patch.object(servico, "erro"), mock.patch.object(servico, "aviso"), mock.patch.object(servico, "info"),
                  mock.patch.object(servico, "ok")):
            p.start()
            self.addCleanup(p.stop)
        self.vivo = True
        self.cmds = []

    def _rodar(self, comando, timeout=None, **kw):
        self.cmds.append(list(comando))
        prog = comando[0]
        if prog == "tasklist":
            return 0, ('"python.exe","%d","Console"' % self.PID) if self.vivo else "INFO: nenhuma tarefa"
        if prog == "taskkill":
            self.vivo = False
            return 0, ""
        if prog == "powershell":
            texto = comando[-1]
            if "ParentProcessId" in texto:
                return 0, ""  # o cmd.exe pai já morreu
            if "CreationDate" in texto:
                return 0, self.criacao_atual
            return 0, "python -m motor run"  # linha sem o caminho do repo
        return 0, ""

    criacao_atual = CRIACAO

    def _parar(self):
        ex = servico.Executor(False)
        with mock.patch.object(servico, "rodar", self._rodar), mock.patch.object(servico.time, "sleep"):
            return servico.controlar(ex, "stop")

    def _ordem(self, prefixo):
        return next(i for i, c in enumerate(self.cmds) if c[:len(prefixo)] == prefixo)

    def test_stop_com_pai_morto_encerra_a_tarefa_e_depois_mata_o_dono(self):  # r19: /End antes do taskkill
        self.lock_id.write_text(json.dumps({"pid": self.PID, "criacao": self.CRIACAO, "repo": str(self.repo)}))
        self.assertEqual(self._parar(), 0)
        self.assertLess(self._ordem(["schtasks", "/End"]), self._ordem(["taskkill", "/T", "/F", "/PID", str(self.PID)]))
        self.assertFalse(self.vivo)

    def test_sem_identidade_gravada_continua_fail_closed(self):
        self.assertEqual(self._parar(), 1)
        self.assertFalse([c for c in self.cmds if c[0] == "taskkill"])
        self.assertTrue(self.vivo)

    def test_hora_de_criacao_diferente_nao_mata_pid_reaproveitado(self):
        self.lock_id.write_text(json.dumps({"pid": self.PID, "criacao": "2026-01-01T00:00:00.0000000Z", "repo": str(self.repo)}))
        self.assertEqual(servico.identidade_do_pid(self.PID) if self._com_rodar() else None, "outro")
        self.assertFalse([c for c in self.cmds if c[0] == "taskkill"])

    def _com_rodar(self):
        p = mock.patch.object(servico, "rodar", self._rodar)
        p.start()
        self.addCleanup(p.stop)
        return True

    def test_registro_de_outra_pasta_nao_prova_dono(self):
        self.lock_id.write_text(json.dumps({"pid": self.PID, "criacao": self.CRIACAO, "repo": str(Path(self.tmp.name) / "outro")}))
        self._com_rodar()
        self.assertEqual(servico.identidade_do_pid(self.PID), "outro")

    def test_registrar_identidade_grava_pid_criacao_e_repo(self):
        self._com_rodar()
        self.assertTrue(servico.registrar_identidade_motor())
        dados = json.loads(self.lock_id.read_text())
        self.assertEqual(dados, {"pid": self.PID, "criacao": self.CRIACAO, "repo": str(self.repo)})
        self.assertEqual(servico.identidade_do_pid(self.PID), "motor")


class _Redirecionador(BaseHTTPRequestHandler):
    destino_chamado = False

    def do_GET(self):
        if self.path == "/destino":
            type(self).destino_chamado = True
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok": true}')
            return
        self.send_response(302)
        self.send_header("Location", "http://localhost:%d/destino" % self.server.server_port)
        self.end_headers()

    def log_message(self, *a):
        pass


class SemRedirectOutroHost(unittest.TestCase):
    def test_redirect_para_outro_host_vira_status_3xx_sem_enviar_a_chave(self):
        _Redirecionador.destino_chamado = False
        srv = HTTPServer(("127.0.0.1", 0), _Redirecionador)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        # 127.0.0.1 -> localhost: outro host, mesma máquina.
        st, _ = _comum.http_json("GET", "http://127.0.0.1:%d/origem" % srv.server_port, headers={"apikey": "segredo"})
        self.assertEqual(st, 302)
        self.assertFalse(_Redirecionador.destino_chamado)

    def test_redirect_no_mesmo_host_continua_seguindo(self):
        h = _comum._SemRedirectOutroHost()
        req = urllib.request.Request("https://api.exemplo.com/a", headers={"apikey": "k"})
        novo = h.redirect_request(req, None, 302, "Found", {}, "/b")
        self.assertIsNotNone(novo)
        self.assertEqual(novo.full_url, "https://api.exemplo.com/b")
        self.assertIsNone(h.redirect_request(req, None, 302, "Found", {}, "https://outro.exemplo.com/b"))
        self.assertIsNone(h.redirect_request(req, None, 302, "Found", {}, "http://api.exemplo.com/b"))


if __name__ == "__main__":
    unittest.main()
