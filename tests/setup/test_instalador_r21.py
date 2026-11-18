"""Rodada 21 (instalador): install varre o 'motor run' avulso/de outra pasta, confere o PID do serviço (launchd/systemd),
https no web/config.js e identidade do motor por argv. Tudo mockado: nenhum processo real é listado nem sinalizado."""
import sys
import unittest
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "setup"))

import etapa_config  # noqa: E402
import servico  # noqa: E402

REPO = Path("/srv/setup18")


def setUpModule():
    from tests.setup import _isolamento
    _isolamento.ligar()


def tearDownModule():
    from tests.setup import _isolamento
    _isolamento.desligar()


class IdentidadePorArgv(unittest.TestCase):
    def test_luna_review_que_so_menciona_motor_run_nao_e_motor(self):
        linha = ("/usr/bin/python3 /Users/x/bin/luna-review --file motor/cli.py --contexto corrigi o motor run no "
                 "instalador e o motor/cli.py run")
        self.assertFalse(servico._e_linha_do_motor(linha))
        self.assertFalse(servico._e_linha_do_motor('python3 /x/luna-review --file motor/x.py --contexto "o motor run sobe"'))

    def test_varredura_ignora_luna_review_com_cwd_na_raiz_e_nada_e_encerrado(self):
        ps = '4242 python3 /Users/x/bin/luna-review --file motor/x.py --contexto "o motor run sobe"\n'
        with mock.patch.object(servico, "sistema", return_value="Darwin"), \
                mock.patch.object(servico, "REPO_ROOT", REPO), \
                mock.patch.object(servico, "rodar", return_value=(0, ps)), \
                mock.patch.object(servico, "_cwd_do_processo", return_value=str(REPO)), \
                mock.patch.object(servico, "_encerrar_pid") as matar:
            self.assertEqual(servico._varrer_completo(), ([], [], [], True))
            self.assertEqual(servico._encerrar_motores_proprios(servico.Executor(False)), 0)
        matar.assert_not_called()

    def test_entrypoints_reais_do_motor_continuam_valendo(self):
        for linha in ("/usr/bin/python3 -m motor run", "/opt/py/bin/python3.12 -u -m motor.cli run --x",
                      "python3 /srv/setup18/motor/cli.py run", "C:\\Program Files\\Python\\python.exe -m motor run",
                      '"C:\\Users\\Ana\\py\\python.exe" "C:\\setup18\\motor\\__main__.py" run'):
            self.assertTrue(servico._e_linha_do_motor(linha), linha)
        for linha in ("python3 -m motor check", "python3 -m http.server run", "python3 app.py motor run", "node motor run",
                      "cmd.exe /c python3 -m motor run >> log"):
            self.assertFalse(servico._e_linha_do_motor(linha), linha)


class Guarda(unittest.TestCase):
    def test_suite_nao_consegue_matar_processo_real(self):
        import os
        with self.assertRaises(AssertionError):
            os.kill(os.getpid(), 0)
        import subprocess
        with self.assertRaises(AssertionError):
            subprocess.run(["taskkill", "/PID", "1"])


def _instalar(so, varreduras, extra=()):
    """Roda instalar() com a listagem de processos mockada (`varreduras` = retornos sucessivos de _varrer_completo)."""
    ex = servico.Executor(False)
    comandos = []
    estados = []
    patches = [mock.patch.object(servico, "sistema", return_value=so),
               mock.patch.object(servico, "preparar_instalacao", return_value=True),
               mock.patch.object(servico, "python_cmd", return_value="python3"),
               mock.patch.object(servico, "REPO_ROOT", REPO),
               mock.patch.object(servico, "LOG_DIR", Path("/nonexistent-log")),
               mock.patch.object(servico, "_varrer_completo", side_effect=list(varreduras)),
               mock.patch.object(servico.Executor, "arquivo", side_effect=lambda *a: comandos.append(["<arquivo>"])),
               mock.patch.object(servico.Executor, "cmd", side_effect=lambda c, tolerar=False: comandos.append(list(c)) or (0, "")),
               mock.patch.object(servico, "motor_subiu", return_value=True),
               mock.patch.object(servico, "_registrar_apos_subir", return_value=(True, True)),
               mock.patch.object(servico, "servico_assumiu_o_motor", return_value=True),
               mock.patch.object(servico, "salvar_estado", side_effect=estados.append),
               mock.patch.object(servico, "erro"), mock.patch.object(servico, "ok"), mock.patch.object(servico, "info"),
               mock.patch.object(servico, "aviso"),
               mock.patch("pathlib.Path.write_text"), mock.patch("pathlib.Path.mkdir")]
    pids = mock.Mock()
    patches.append(mock.patch.object(servico, "_encerrar_pid", pids))
    iniciados = []
    try:
        for p in patches:
            p.start()
            iniciados.append(p)
        codigo = servico.instalar(ex)
    finally:
        for p in reversed(iniciados):
            p.stop()
    return codigo, comandos, estados, pids


class InstalarVarreMotores(unittest.TestCase):
    def test_motor_de_outra_pasta_recusa_sem_alterar_nada(self):
        for so in ("Darwin", "Linux", "Windows"):
            codigo, comandos, estados, pids = _instalar(so, [([], [], [(777, "/antiga/setup18")], True)])
            self.assertEqual(codigo, 1, so)
            self.assertEqual(comandos, [], so)
            self.assertEqual(estados, [], so)
            pids.assert_not_called()

    def test_mensagem_lista_pid_e_pasta_e_a_instrucao(self):
        with mock.patch.object(servico, "erro") as erro:
            ex = servico.Executor(False)
            with mock.patch.object(servico, "_varrer_completo", return_value=([], [], [(777, "/antiga/setup18")], True)):
                self.assertEqual(servico._checar_motor_alheio(ex), 1)
        msg = erro.call_args[0][0]
        self.assertIn("777", msg)
        self.assertIn("/antiga/setup18", msg)
        self.assertIn("servico.py stop", msg)

    def test_identidade_desconhecida_tambem_recusa(self):
        for so in ("Darwin", "Linux", "Windows"):
            codigo, comandos, estados, _ = _instalar(so, [([], [888], [], True)])
            self.assertEqual(codigo, 1, so)
            self.assertEqual(comandos, [], so)

    def test_motor_avulso_desta_instalacao_e_encerrado_e_confirmado_antes_de_subir(self):
        for so, marca in (("Darwin", ["launchctl", "kickstart"]), ("Linux", ["systemctl", "--user", "daemon-reload"]),
                          ("Windows", ["schtasks", "/Run"])):
            codigo, comandos, _, pids = _instalar(so, [([], [], [], True), ([555], [], [], True), ([], [], [], True)])
            self.assertEqual(codigo, 0, so)
            self.assertEqual(pids.call_args[0][1], 555, so)
            self.assertTrue(pids.call_args[0][2], so)  # árvore
            self.assertTrue(any(c[:len(marca)] == marca for c in comandos), so)

    def test_motor_que_nao_morre_impede_subir_o_servico(self):
        codigo, comandos, estados, _ = _instalar("Darwin", [([], [], [], True), ([555], [], [], True), ([555], [], [], True)])
        self.assertEqual(codigo, 1)
        self.assertFalse(any(c[:2] == ["launchctl", "bootstrap"] for c in comandos))
        self.assertNotIn({"servico_instalado": True}, estados)

    def test_dry_run_so_mostra(self):
        ex = servico.Executor(True)
        with mock.patch.object(servico, "_varrer_completo") as v:
            self.assertEqual(servico._checar_motor_alheio(ex), 0)
            self.assertEqual(servico._encerrar_motores_proprios(ex), 0)
        v.assert_not_called()


class PidDoServico(unittest.TestCase):
    def _rodar(self, so, codigo, saida):
        return mock.patch.object(servico, "sistema", return_value=so), mock.patch.object(servico, "rodar", return_value=(codigo, saida))

    def test_le_o_pid_no_launchctl_e_no_systemctl(self):
        a, b = self._rodar("Darwin", 0, "x = {\n\tstate = running\n\tpid = 321\n}\n")
        with a, b:
            self.assertEqual(servico.pid_do_servico(), (321, True))
        a, b = self._rodar("Darwin", 0, "state = waiting\n")
        with a, b:
            self.assertEqual(servico.pid_do_servico(), (None, True))
        a, b = self._rodar("Linux", 0, "MainPID=654\n")
        with a, b:
            self.assertEqual(servico.pid_do_servico(), (654, True))
        a, b = self._rodar("Linux", 0, "MainPID=0\n")
        with a, b:
            self.assertEqual(servico.pid_do_servico(), (None, True))
        a, b = self._rodar("Linux", 1, "")
        with a, b:
            self.assertEqual(servico.pid_do_servico(), (None, False))

    def _assumiu(self, servico_pid, legivel, trava):
        with mock.patch.object(servico, "pid_do_servico", return_value=(servico_pid, legivel)), \
                mock.patch.object(servico, "ler_pid_motor", return_value=trava), \
                mock.patch.object(servico, "erro") as erro, mock.patch.object(servico, "aviso") as aviso:
            r = servico.servico_assumiu_o_motor(dormir=lambda s: None, espera=0)
        return r, erro, aviso

    def test_pid_igual_ok(self):
        r, erro, _ = self._assumiu(100, True, 100)
        self.assertTrue(r)
        erro.assert_not_called()

    def test_pid_diferente_erro_claro(self):
        r, erro, _ = self._assumiu(100, True, 200)
        self.assertFalse(r)
        msg = erro.call_args[0][0]
        self.assertIn("o serviço não assumiu o motor", msg.lower())
        self.assertIn("200", msg)

    def test_servico_sem_pid_e_erro(self):
        r, erro, _ = self._assumiu(None, True, 200)
        self.assertFalse(r)

    def test_leitura_falha_cai_na_trava_com_aviso(self):
        r, erro, aviso = self._assumiu(None, False, 200)
        self.assertTrue(r)
        erro.assert_not_called()
        aviso.assert_called_once()

    def test_instalar_nao_marca_instalado_quando_o_servico_nao_assumiu(self):
        for so in ("Darwin", "Linux"):
            ex = servico.Executor(False)
            estados = []
            with mock.patch.object(servico, "sistema", return_value=so), \
                    mock.patch.object(servico, "preparar_instalacao", return_value=True), \
                    mock.patch.object(servico, "_checar_motor_alheio", return_value=0), \
                    mock.patch.object(servico, "_encerrar_motores_proprios", return_value=0), \
                    mock.patch.object(servico.Executor, "arquivo"), \
                    mock.patch.object(servico.Executor, "cmd", return_value=(0, "")), \
                    mock.patch.object(servico, "motor_subiu", return_value=True), \
                    mock.patch.object(servico, "servico_assumiu_o_motor", return_value=False), \
                    mock.patch.object(servico, "salvar_estado", side_effect=estados.append):
                self.assertEqual(servico.instalar(ex), 1, so)
            self.assertNotIn({"servico_instalado": True}, estados, so)


class HttpsNoConfigJs(unittest.TestCase):
    def test_validador(self):
        for url in ("https://abc.supabase.co", "http://localhost:54321", "http://127.0.0.1:54321", "http://[::1]:54321"):
            self.assertEqual(etapa_config.url_supabase_segura(url), "", url)
        for url in ("http://abc.supabase.co", "abc.supabase.co", "ftp://x", "", "https://", "http://localhost.evil.com"):
            self.assertIn("https://", etapa_config.url_supabase_segura(url), url)

    def test_web_js_recusa_http_remoto_e_nao_grava(self):
        import base64
        import json

        def jwt(role):
            b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
            return "%s.%s.sig" % (b({"alg": "none"}), b({"role": role}))
        cfgx = {"supabase": {"url": "http://abc.supabase.co", "anon_key": jwt("anon"), "service_role_key": jwt("service_role")}}
        gravar = mock.Mock()
        with mock.patch.object(etapa_config, "load_config", return_value=cfgx), \
                mock.patch.object(etapa_config, "gravar_atomico", gravar), mock.patch.object(etapa_config, "erro") as erro:
            self.assertEqual(etapa_config.cmd_web_js(mock.Mock(demo=False, cf_beacon_token="")), 1)
        gravar.assert_not_called()
        self.assertIn("https", erro.call_args[0][0])
        cfgx["supabase"]["url"] = "http://localhost:54321"
        with mock.patch.object(etapa_config, "load_config", return_value=cfgx), \
                mock.patch.object(etapa_config, "gravar_atomico", gravar):
            self.assertEqual(etapa_config.cmd_web_js(mock.Mock(demo=False, cf_beacon_token="")), 0)


if __name__ == "__main__":
    unittest.main()
