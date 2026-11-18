"""Testes do instalador (setup/). Rodar: python3 -m unittest discover -s tests/setup -t . -v"""
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[2]
SETUP = RAIZ / "setup"
sys.path.insert(0, str(SETUP))

import _comum  # noqa: E402
import etapa_banco  # noqa: E402
import etapa_web  # noqa: E402
import liberar_producao  # noqa: E402
import nichos  # noqa: E402
import servico  # noqa: E402
import teste_guiado  # noqa: E402


def setUpModule():
    from tests.setup import _isolamento
    _isolamento.ligar()


def tearDownModule():
    from tests.setup import _isolamento
    _isolamento.desligar()


class EntradaSemTerminal(unittest.TestCase):
    def test_script_sem_stdin_aborta_rapido_e_sem_loop(self):
        t0 = time.monotonic()
        proc = subprocess.run(
            [sys.executable, str(SETUP / "etapa_banco.py"), "credenciais", "--dry-run"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20, cwd=str(RAIZ),
            env={k: v for k, v in os.environ.items() if not k.startswith("SDR_")},
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertLess(time.monotonic() - t0, 15)
        self.assertLess(len(proc.stdout) + len(proc.stderr), 5000)
        self.assertIn("SDR_SUPABASE_URL", proc.stdout + proc.stderr)

    def test_ask_eof_sem_default_levanta_systemexit(self):
        with mock.patch.object(_comum, "sem_terminal", return_value=False), \
                mock.patch("builtins.input", side_effect=EOFError):
            with self.assertRaises(SystemExit):
                _comum.ask("Pergunta", env="SDR_X")

    def test_ask_sem_terminal_usa_default_ou_env(self):
        with mock.patch.object(_comum, "sem_terminal", return_value=True):
            self.assertEqual(_comum.ask("P", "padrao"), "padrao")
            with mock.patch.dict(os.environ, {"SDR_ASK_TESTE": " valor "}):
                self.assertEqual(_comum.ask("P", env="SDR_ASK_TESTE"), "valor")
            with self.assertRaises(SystemExit) as ctx:
                _comum.ask("P", env="SDR_ASK_TESTE_AUSENTE")
            self.assertEqual(ctx.exception.code, 2)

    def test_segredo_sem_terminal_aborta_e_le_env(self):
        with mock.patch.object(_comum, "sem_terminal", return_value=True):
            with self.assertRaises(SystemExit):
                _comum.segredo("Chave", "SDR_SEGREDO_AUSENTE")
            with mock.patch.dict(os.environ, {"SDR_SEGREDO_T": "abc"}):
                self.assertEqual(_comum.segredo("Chave", "SDR_SEGREDO_T"), "abc")

    def test_segredo_eof_nao_repete(self):
        with mock.patch.object(_comum, "sem_terminal", return_value=False), \
                mock.patch("getpass.getpass", side_effect=EOFError):
            with self.assertRaises(SystemExit):
                _comum.segredo("Chave", "SDR_SEGREDO_AUSENTE")

    def test_entrada_vem_do_config_json(self):
        with tempfile.TemporaryDirectory() as d:
            caminho = Path(d) / "config.json"
            caminho.write_text(json.dumps({"entrada": {"SDR_CFG_T": "do-arquivo"}}), encoding="utf-8")
            with mock.patch.object(_comum, "CONFIG_PATH", caminho):
                self.assertEqual(_comum.entrada_externa("SDR_CFG_T"), "do-arquivo")

    def test_confirmar_sem_terminal_nao_confirma(self):
        with mock.patch.object(_comum, "sem_terminal", return_value=True):
            self.assertFalse(_comum.confirmar("ok?", True))


class Windows(unittest.TestCase):
    def test_achar_cli_usa_caminho_completo_do_which(self):
        def falso(nome, path=None):
            return {"supabase": r"C:\tools\supabase.CMD", "npx": r"C:\node\npx.CMD"}.get(nome)
        with mock.patch("shutil.which", side_effect=falso):
            self.assertEqual(_comum.achar_cli("supabase"), [r"C:\tools\supabase.CMD"])
            self.assertEqual(_comum.achar_cli("wrangler"), [r"C:\node\npx.CMD", "--yes", "wrangler"])

    def test_rodar_resolve_exe_e_forca_utf8(self):
        capturado = {}

        def falso_run(cmd, **kw):
            capturado["cmd"], capturado["kw"] = cmd, kw
            return subprocess.CompletedProcess(cmd, 0, "ok", "")
        with mock.patch("shutil.which", return_value=r"C:\x\gh.CMD"), mock.patch("subprocess.run", side_effect=falso_run):
            codigo, _ = _comum.rodar(["gh", "--version"])
        self.assertEqual(codigo, 0)
        self.assertEqual(capturado["cmd"][0], r"C:\x\gh.CMD")
        self.assertEqual(capturado["kw"]["encoding"], "utf-8")
        self.assertEqual(capturado["kw"]["errors"], "replace")

    def test_saida_reconfigurada_para_utf8(self):
        bruto = io.BytesIO()
        fluxo = io.TextIOWrapper(bruto, encoding="cp1252", errors="strict")
        with mock.patch.object(sys, "stdout", fluxo), mock.patch.object(sys, "stderr", fluxo):
            _comum._forcar_utf8()
            _comum.ok("acentuação ✅")  # sem reconfigurar, UnicodeEncodeError em cp1252
            fluxo.flush()
        self.assertIn("✅".encode("utf-8"), bruto.getvalue())

    def test_xml_da_tarefa_sem_limite_e_com_reinicio(self):
        xml = servico.xml_tarefa_windows(r"C:\Python\python.exe", r"C:\proj", r"C:\proj\logs\motor.log")
        self.assertIn("<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>", xml)
        self.assertIn("<RestartOnFailure>", xml)
        self.assertIn("LogonTrigger", xml)
        import xml.dom.minidom as m
        m.parseString(xml.replace('encoding="UTF-16"', 'encoding="UTF-8"'))  # XML bem formado


class Servico(unittest.TestCase):
    def test_motor_subiu_le_lock_com_pid_recente(self):
        with tempfile.TemporaryDirectory() as d:
            lock = Path(d) / "motor.lock"
            self.assertFalse(servico.motor_subiu(time.time(), lock, espera=0))
            lock.write_text("", encoding="utf-8")
            self.assertFalse(servico.motor_subiu(time.time(), lock, espera=0))
            lock.write_text("1234", encoding="utf-8")
            with mock.patch.object(servico, "identidade_do_pid", return_value="motor"):  # r13: PID precisa ser o motor
                self.assertTrue(servico.motor_subiu(time.time() - 5, lock, espera=0))
                # r17: identidade comprovada vale mesmo com a trava mais velha que o início (motor já estava de pé)
                self.assertTrue(servico.motor_subiu(time.time() + 3600, lock, espera=0))
            with mock.patch.object(servico, "identidade_do_pid", return_value="desconhecido"):
                self.assertTrue(servico.motor_subiu(time.time() - 5, lock, espera=0))
                self.assertFalse(servico.motor_subiu(time.time() + 3600, lock, espera=0))

    def _instalar(self, subiu):
        ex = servico.Executor(False)
        estados = []
        with mock.patch.object(servico, "sistema", return_value="Linux"), \
                mock.patch.object(servico, "preparar_instalacao", return_value=True), \
                mock.patch.object(servico.Executor, "arquivo"), \
                mock.patch.object(servico.Executor, "cmd", return_value=(0, "")), \
                mock.patch.object(servico, "motor_subiu", return_value=subiu), \
                mock.patch.object(servico, "_checar_motor_alheio", return_value=0), \
                mock.patch.object(servico, "_encerrar_motores_proprios", return_value=0), \
                mock.patch.object(servico, "servico_assumiu_o_motor", return_value=True), \
                mock.patch.object(servico, "salvar_estado", side_effect=estados.append):
            codigo = servico.instalar(ex)
        return codigo, estados

    def test_nao_marca_instalado_se_motor_nao_subiu(self):
        codigo, estados = self._instalar(False)
        self.assertEqual(codigo, 1)
        self.assertTrue(all(e.get("servico_instalado") is False for e in estados))

    def test_marca_instalado_quando_motor_sobe(self):
        codigo, estados = self._instalar(True)
        self.assertEqual(codigo, 0)
        self.assertEqual(estados, [{"servico_instalado": True}])


class PaginaPublica(unittest.TestCase):
    def test_link_leva_o_slug(self):
        self.assertEqual(etapa_web.link_publico("https://x.pages.dev/", "agendar"), "https://x.pages.dev/agendar/agendar")
        self.assertTrue(etapa_web.link_publico("https://x.pages.dev", "clinica").endswith("/agendar/clinica"))

    def test_escolher_url_prefere_producao(self):
        saida = "Deployment: https://abc123.meu-sdr.pages.dev\nProd: https://meu-sdr.pages.dev"
        self.assertEqual(etapa_web.escolher_url(saida, "meu-sdr"), "https://meu-sdr.pages.dev")
        saida = "Preview https://abc123.meu-sdr.pages.dev e producao https://meu-sdr-x9.pages.dev"
        self.assertEqual(etapa_web.escolher_url(saida, "meu-sdr"), "https://meu-sdr-x9.pages.dev")
        self.assertEqual(etapa_web.escolher_url("", "meu-sdr"), "https://meu-sdr.pages.dev")

    def test_servidor_local_entrega_pagina_para_qualquer_slug(self):
        h = etapa_web._ServidorLocal.__new__(etapa_web._ServidorLocal)
        h.directory = str(RAIZ / "web")
        caminho = h.translate_path("/agendar/clinica-x")
        self.assertTrue(caminho.endswith(os.path.join("agendar", "index.html")))
        # Produção (Cloudflare) aplica o _redirects antes dos arquivos: nada além do index mora em /agendar/.
        self.assertTrue(h.translate_path("/agendar/agendar.js").endswith(os.path.join("agendar", "index.html")))
        self.assertTrue(h.translate_path("/js/agendar.js").endswith(os.path.join("js", "agendar.js")))
        self.assertTrue(h.translate_path("/css/agendar.css").endswith(os.path.join("css", "agendar.css")))

    def test_pagina_publica_nao_tem_arquivo_dentro_de_agendar(self):
        """Regressão: com '/agendar/*' no _redirects, /agendar/agendar.js voltava HTML no Cloudflare."""
        web = RAIZ / "web"
        dentro = sorted(p.name for p in (web / "agendar").iterdir())
        self.assertEqual(dentro, ["index.html"])
        html = (web / "agendar" / "index.html").read_text(encoding="utf-8")
        for ref in re.findall(r'(?:src|href)="(/[^"]+)"', html):
            self.assertFalse(ref.startswith("/agendar/"), ref)
            if ref != "/config.js":  # gerado na instalação (fora do Git)
                self.assertTrue((web / ref.lstrip("/")).exists(), ref)
        regras = [l.split() for l in (web / "_redirects").read_text(encoding="utf-8").splitlines()
                  if l.strip() and not l.startswith("#")]
        self.assertEqual(regras, [["/agendar/:slug", "/agendar/", "200"]])

    def test_slug_vem_do_banco(self):
        with mock.patch.object(etapa_web, "load_config", return_value={"supabase": {"url": "https://a.supabase.co", "service_role_key": "k"}}), \
                mock.patch.object(etapa_web, "supabase_rest", return_value=(200, [{"public_slug": "Clinica-Sol"}])):
            self.assertEqual(etapa_web.slug_publico(), "clinica-sol")

    def test_mensagem_final_do_deploy_mostra_slug(self):
        saida = io.StringIO()
        args = mock.Mock(projeto="meu-sdr", dry_run=False, permitir_demo=False)
        with tempfile.TemporaryDirectory() as d:
            cfgjs = Path(d) / "config.js"
            cfgjs.write_text('window.SDR_CONFIG={"url":"x"}', encoding="utf-8")
            with mock.patch.object(etapa_web, "WEB_CONFIG_JS", cfgjs), \
                    mock.patch.object(etapa_web, "achar_cli", return_value=["wrangler"]), \
                    mock.patch.object(etapa_web, "rodar", return_value=(0, "https://meu-sdr.pages.dev")), \
                    mock.patch.object(etapa_web, "slug_publico", return_value="minha-clinica"), \
                    mock.patch.object(etapa_web, "salvar_estado"), \
                    mock.patch("sys.stdout", saida):
                self.assertEqual(etapa_web.cmd_deploy(args), 0)
        self.assertIn("https://meu-sdr.pages.dev/agendar/minha-clinica", saida.getvalue())


class TesteGuiado(unittest.TestCase):
    def test_resposta_nova_exige_mensagem_e_resposta_depois_do_inicio(self):
        inicio = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        antigo = (inicio - timedelta(hours=1)).isoformat()
        novo_in = (inicio + timedelta(seconds=10)).isoformat()
        novo_out = (inicio + timedelta(seconds=20)).isoformat()
        self.assertFalse(teste_guiado.resposta_nova(None, inicio))
        self.assertFalse(teste_guiado.resposta_nova({"last_inbound_at": antigo, "last_outbound_at": antigo}, inicio))
        self.assertFalse(teste_guiado.resposta_nova({"last_inbound_at": antigo, "last_outbound_at": novo_out}, inicio))
        self.assertFalse(teste_guiado.resposta_nova({"last_inbound_at": novo_in, "last_outbound_at": antigo}, inicio))
        self.assertTrue(teste_guiado.resposta_nova({"last_inbound_at": novo_in, "last_outbound_at": novo_out}, inicio))

    def test_horarios_falha_com_pagina_publica_desativada(self):
        config = {"supabase": {"url": "https://a.supabase.co", "service_role_key": "k", "anon_key": "a"}}
        with mock.patch.object(teste_guiado, "supabase_rest", return_value=(200, [{"public_slug": "agendar", "public_enabled": False}])):
            self.assertFalse(teste_guiado.passo_horarios(config, {}))
            self.assertTrue(teste_guiado.passo_horarios(config, {"sem_pagina_publica": True}))

    def _main(self, argv, passa=True):
        estados = []
        falsas = {n: (lambda c, x: passa) for n in teste_guiado.FUNCOES}
        with mock.patch.object(teste_guiado, "FUNCOES", falsas), \
                mock.patch.object(teste_guiado, "load_config", return_value={"supabase": {"url": "u", "service_role_key": "k"}}), \
                mock.patch.object(teste_guiado, "salvar_estado", side_effect=estados.append):
            codigo = teste_guiado.main(argv)
        return codigo, estados

    def test_teste_parcial_nao_grava_teste_ok(self):
        codigo, estados = self._main(["--passos", "modo-teste"])
        self.assertNotEqual(codigo, 0)
        self.assertNotIn({"teste_ok": True}, estados)

    def test_teste_completo_grava_teste_ok(self):
        codigo, estados = self._main([])
        self.assertEqual(codigo, 0)
        self.assertEqual(estados[-1], {"teste_ok": True})

    def test_passo_que_falha_nao_grava_teste_ok(self):
        codigo, estados = self._main([], passa=False)
        self.assertEqual(codigo, 1)
        self.assertNotIn({"teste_ok": True}, estados)


class LiberarProducao(unittest.TestCase):
    def test_pendencia_so_no_playbook_e_detectada(self):
        ac = {"knowledge": "tudo certo", "objection_playbook": [{"gatilho": "preço", "resposta": "custa [PREENCHER]"}],
              "disqualify_rules": [{"regra": "[PREENCHER]"}], "company": "X"}
        achadas = liberar_producao.pendencias_por_campo(ac)
        self.assertEqual(achadas, {"objection_playbook": 1, "disqualify_rules": 1})
        self.assertEqual(liberar_producao.pendencias_por_campo({"knowledge": "ok"}), {})

    def test_main_bloqueia_com_pendencia_fora_do_knowledge(self):
        ac = {"test_only": True, "paused": False, "knowledge": "ok", "agent_name": "A", "company": "C",
              "objection_playbook": [{"resposta": "[PREENCHER]"}]}
        patches = []
        with mock.patch.object(liberar_producao, "load_config", return_value={"supabase": {"url": "u", "service_role_key": "k"}}), \
                mock.patch.object(liberar_producao, "supabase_rest", return_value=(200, [ac])) as rest, \
                mock.patch.object(liberar_producao, "load_estado", return_value={"teste_ok": True}), \
                mock.patch.object(liberar_producao, "salvar_estado"):
            codigo = liberar_producao.main(["--sim"])
        self.assertEqual(codigo, 1)
        for chamada in rest.call_args_list:
            self.assertNotEqual(chamada.args[1], "PATCH")


class Nichos(unittest.TestCase):
    def test_cinco_nichos_validos(self):
        todos = nichos.carregar_todos()
        self.assertEqual(len(todos), 5)
        for nome, dados in todos.items():
            self.assertEqual(nichos.validar(dados, nome), [], nome)

    def test_sem_pyyaml_da_mensagem_amigavel(self):
        with mock.patch.object(nichos, "yaml", None):
            with self.assertRaises(SystemExit) as ctx:
                nichos.carregar_todos()
        self.assertIn("pyyaml", str(ctx.exception).lower())

    def test_yaml_vazio_vira_erro_claro(self):
        with tempfile.TemporaryDirectory() as d:
            vazio = Path(d) / "vazio.yaml"
            vazio.write_text("", encoding="utf-8")
            with mock.patch.object(nichos, "arquivos", return_value=[vazio]):
                with self.assertRaises(SystemExit) as ctx:
                    nichos.carregar_todos()
        self.assertIn("vazio.yaml", str(ctx.exception))

    def test_promessa_de_parar_existe_no_motor(self):
        from motor.conversa import is_opt_out
        textos = [f["texto"] for d in nichos.carregar_todos().values() for f in d.get("followup_messages", []) if "PARAR" in f["texto"]]
        self.assertTrue(textos)
        self.assertTrue(is_opt_out("PARAR"))


class EtapaBanco(unittest.TestCase):
    def test_dry_run_de_credenciais_nao_grava_config(self):
        args = mock.Mock(url="https://abcdefghijklmnopqrst.supabase.co", anon_key="anon-publica-1234567", trocar_chaves=True, dry_run=True)
        with mock.patch.object(etapa_banco, "load_config", return_value={}), \
                mock.patch.dict(os.environ, {"SDR_SUPABASE_SERVICE_ROLE_KEY": "service-secreta-9876543"}), \
                mock.patch.object(etapa_banco, "atualizar_config") as grava:
            self.assertEqual(etapa_banco.cmd_credenciais(args), 0)
        grava.assert_not_called()

    def test_tudo_para_no_primeiro_erro(self):
        args = mock.Mock(dry_run=False)
        with mock.patch.object(etapa_banco, "cmd_credenciais", return_value=1), \
                mock.patch.object(etapa_banco, "cmd_migrar") as m, mock.patch.object(etapa_banco, "cmd_dono") as dn:
            self.assertEqual(etapa_banco.cmd_tudo(args), 1)
        m.assert_not_called()
        dn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
