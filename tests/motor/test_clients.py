"""Clientes e infraestrutura sem rede: PostgREST, IA, Evolution, config, limitador, trava e CLI."""

import io
import json
import logging
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

from motor import agenda, ai as ai_module, cli, engine, http
from motor.agenda import GoogleCalendar
from motor.ai import AIClient, AIError
from motor.config import ConfigError, env_positive, from_dict, load_config
from motor.db import Database
from motor.evolution import EvolutionClient, EvolutionError
from motor.lock import InstanceLock, LockError
from motor.ratelimit import MAX_WAIT_SECONDS, RateLimiter
from tests.motor.support import Clock, make_ctx


class FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(status: int, body: str = "erro") -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x.test", status, "err", {}, io.BytesIO(body.encode()))


class Net:
    """Substitui http.urlopen: devolve as respostas do roteiro e guarda o que foi pedido."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request, timeout=None):
        self.calls.append({"method": request.get_method(), "url": request.full_url, "timeout": timeout,
                           "headers": {k.lower(): v for k, v in request.header_items()},
                           "body": json.loads(request.data.decode()) if request.data else None})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item if isinstance(item, bytes) else json.dumps(item).encode())

    def __enter__(self):
        self._patch = mock.patch.object(http, "urlopen", self)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()


class PostgrestTest(unittest.TestCase):
    def setUp(self):
        self.db = Database("https://x.supabase.co/", "service-key")

    def test_cabecalhos_de_autenticacao_e_url_base(self):
        with Net([{"id": 1}]) as net:
            self.db.get_agent_config()
        call = net.calls[0]
        self.assertTrue(call["url"].startswith("https://x.supabase.co/rest/v1/agent_config?"))
        self.assertEqual(call["headers"]["apikey"], "service-key")
        self.assertEqual(call["headers"]["authorization"], "Bearer service-key")

    def test_insert_idempotente_usa_on_conflict_e_prefer(self):
        with Net([{"id": "L1", "phone": "+5511999990001"}]) as net:
            lead = self.db.create_lead("+5511999990001", name="Joana")
        call = net.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertIn("on_conflict=phone", call["url"])
        self.assertEqual(call["headers"]["prefer"], "resolution=ignore-duplicates,return=representation")
        self.assertEqual(call["body"], {"phone": "+5511999990001", "source": "whatsapp", "name": "Joana"})
        self.assertEqual(lead["id"], "L1")

    def test_lead_ja_existente_e_lido_de_volta(self):
        with Net([], [{"id": "L9", "phone": "+5511999990001"}]) as net:
            lead = self.db.create_lead("+5511999990001")
        self.assertEqual(lead["id"], "L9")
        self.assertEqual(net.calls[1]["method"], "GET")

    def test_add_message_duplicada_devolve_false(self):
        with Net([]) as net:
            self.assertFalse(self.db.add_message("L1", "in", "lead", "oi", wa_id="W1"))
        self.assertIn("on_conflict=wa_id", net.calls[0]["url"])
        with Net([{"id": 1}]):
            self.assertTrue(self.db.add_message("L1", "in", "lead", "oi", wa_id="W2"))

    def test_update_usa_patch_com_filtro_por_id(self):
        with Net([{"id": "L1", "stage": "qualificado"}]) as net:
            self.db.update_lead("L1", {"stage": "qualificado"})
        call = net.calls[0]
        self.assertEqual(call["method"], "PATCH")
        self.assertIn("id=eq.L1", call["url"])
        self.assertEqual(call["body"], {"stage": "qualificado"})

    def test_rpc_book_slot_e_available_slots(self):
        booked = {"ok": True, "appointment_id": "A1"}
        with Net(booked, [{"starts_at": "2026-11-19T17:00:00+00:00"}]) as net:
            from tests.motor.support import sp
            self.assertEqual(self.db.book_slot("L1", sp(19, 14), notes="n"), booked)
            self.assertEqual(len(self.db.available_slots(sp(19, 8), sp(20, 8))), 1)
        self.assertTrue(net.calls[0]["url"].endswith("/rest/v1/rpc/book_slot"))
        self.assertEqual(net.calls[0]["body"]["p_lead"], "L1")
        self.assertEqual(net.calls[0]["body"]["p_source"], "agente")
        self.assertEqual(net.calls[0]["body"]["p_notes"], "n")
        self.assertTrue(net.calls[1]["url"].endswith("/rest/v1/rpc/available_slots"))
        self.assertNotIn("p_closer", net.calls[1]["body"])

    def test_resposta_invalida_das_rpcs_nao_quebra(self):
        from tests.motor.support import sp
        with Net("texto", {"x": 1}):
            self.assertEqual(self.db.book_slot("L1", sp(19, 14)), {"ok": False, "error": "resposta_invalida"})
            self.assertEqual(self.db.available_slots(sp(19, 8), sp(20, 8)), [])

    def test_known_wa_ids_consulta_em_lote_e_ignora_lista_vazia(self):
        with Net([{"wa_id": "A"}]) as net:
            self.assertEqual(self.db.known_wa_ids(["A", "B", None]), {"A"})
            self.assertEqual(self.db.known_wa_ids([]), set())
        self.assertEqual(len(net.calls), 1)
        self.assertIn('wa_id=in.("A","B")', urllib.parse.unquote(net.calls[0]["url"]))

    def test_claim_da_outbox_e_condicional_e_lista_vazia_significa_perdeu(self):
        with Net([], [{"id": 3}]) as net:
            self.assertFalse(self.db.claim_outbox({"id": 3, "attempts": 0}))
            self.assertTrue(self.db.claim_outbox({"id": 3, "attempts": 0}))
        call = net.calls[0]
        self.assertEqual(call["method"], "PATCH")
        self.assertIn("status=eq.pendente", call["url"])
        self.assertEqual(call["body"], {"status": "enviando", "attempts": 1})

    def test_finish_outbox_grava_key_id_e_corta_erro_longo(self):
        from tests.motor.support import NOW
        with Net([{}]) as net:
            self.db.finish_outbox(3, status="enviado", wa_key_id="K1", sent_at=NOW, attempts=2)
        self.assertEqual(net.calls[0]["body"]["wa_key_id"], "K1")
        with Net([{}]) as net:
            self.db.finish_outbox(3, status="erro", error="x" * 900)
        self.assertEqual(len(net.calls[0]["body"]["last_error"]), 500)

    def test_http_4xx_vira_http_error_e_rede_vira_network_error(self):
        with Net(http_error(401, "chave inválida")):
            with self.assertRaises(http.HttpError) as ctx:
                self.db.get_agent_config()
        self.assertEqual(ctx.exception.status, 401)
        with Net(urllib.error.URLError("sem rede")):
            with self.assertRaises(http.NetworkError):
                self.db.get_agent_config()


class AIClientTest(unittest.TestCase):
    def make(self, provider="gemini", **kw):
        self.sleeps = []
        self.log = logging.getLogger(f"test.ai.{provider}")
        self.log.propagate = False
        self.log.addHandler(logging.NullHandler())
        return AIClient(provider, "k", "m", self.log, sleep_fn=self.sleeps.append, **kw)

    GEMINI_OK = {"candidates": [{"content": {"parts": [{"text": '{"reply":"oi"}'}]}}]}

    def test_gemini_ignora_partes_de_raciocinio(self):
        client = self.make()
        body = {"candidates": [{"content": {"parts": [{"text": "pensando...", "thought": True},
                                                      {"text": '{"reply":"oi"}'}]}}]}
        with Net(body):
            self.assertEqual(client.complete("sys", [{"role": "user", "content": "oi"}]), '{"reply":"oi"}')

    def test_gemini_formato_da_requisicao(self):
        client = self.make(timeout=123)
        with Net(self.GEMINI_OK) as net:
            client.complete("sistema", [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])
        call = net.calls[0]
        self.assertIn("/models/m:generateContent", call["url"])
        self.assertEqual(call["headers"]["x-goog-api-key"], "k")
        self.assertEqual(call["timeout"], 123)
        self.assertEqual(call["body"]["systemInstruction"]["parts"][0]["text"], "sistema")
        self.assertEqual([c["role"] for c in call["body"]["contents"]], ["user", "model"])

    def test_openai_e_anthropic_formato_da_requisicao(self):
        with Net({"choices": [{"message": {"content": "ok"}}]}) as net:
            self.assertEqual(self.make("openai").complete("sys", [{"role": "user", "content": "a"}]), "ok")
        self.assertEqual(net.calls[0]["headers"]["authorization"], "Bearer k")
        self.assertEqual(net.calls[0]["body"]["messages"][0], {"role": "system", "content": "sys"})
        with Net({"content": [{"type": "text", "text": "ok2"}]}) as net:
            self.assertEqual(self.make("anthropic").complete("sys", [{"role": "user", "content": "a"}]), "ok2")
        self.assertEqual(net.calls[0]["headers"]["x-api-key"], "k")
        self.assertEqual(net.calls[0]["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(net.calls[0]["body"]["system"], "sys")

    def test_retenta_5xx_e_registra_o_tempo(self):
        client = self.make()
        with Net(http_error(503), self.GEMINI_OK) as net, self.assertLogs(self.log, level="INFO") as logs:
            self.assertEqual(client.complete("s", [{"role": "user", "content": "a"}]), '{"reply":"oi"}')
        self.assertEqual(len(net.calls), 2)
        self.assertEqual(self.sleeps, [2])
        self.assertTrue(any("levou" in line for line in logs.output))

    def test_retenta_429_e_falha_de_rede_com_as_duas_esperas(self):
        client = self.make()
        with Net(http_error(429), urllib.error.URLError("rede"), self.GEMINI_OK) as net:
            client.complete("s", [{"role": "user", "content": "a"}])
        self.assertEqual(self.sleeps, [2, 6])
        self.assertEqual(len(net.calls), 3)

    def test_esgota_tentativas_e_levanta_ai_error(self):
        client = self.make()
        with Net(http_error(500), http_error(502), http_error(503)) as net:
            with self.assertRaises(AIError) as ctx:
                client.complete("s", [{"role": "user", "content": "a"}])
        self.assertEqual(len(net.calls), 3)
        self.assertEqual(self.sleeps, [2, 6])
        self.assertIn("3 tentativas", str(ctx.exception))

    def test_4xx_nao_e_repetido(self):
        client = self.make()
        with Net(http_error(400, "bad"), self.GEMINI_OK) as net:
            with self.assertRaises(AIError):
                client.complete("s", [{"role": "user", "content": "a"}])
        self.assertEqual(len(net.calls), 1)
        self.assertEqual(self.sleeps, [])

    def test_resposta_vazia_conta_como_falha_e_e_repetida(self):
        client = self.make()
        with Net({"candidates": []}, self.GEMINI_OK) as net:
            client.complete("s", [{"role": "user", "content": "a"}])
        self.assertEqual(len(net.calls), 2)

    def test_provedor_desconhecido_e_transcricao_so_com_gemini(self):
        with self.assertRaises(AIError):
            self.make("xpto").complete("s", [{"role": "user", "content": "a"}])
        with self.assertRaises(AIError):
            self.make("openai").transcribe(b"x", "audio/ogg")

    def test_transcricao_envia_a_midia_em_base64(self):
        client = self.make()
        with Net({"candidates": [{"content": {"parts": [{"text": " quero marcar "}]}}]}) as net:
            self.assertEqual(client.transcribe(b"abc", "audio/ogg; codecs=opus"), "quero marcar")
        inline = net.calls[0]["body"]["contents"][0]["parts"][1]["inlineData"]
        self.assertEqual(inline["mimeType"], "audio/ogg")
        self.assertEqual(inline["data"], "YWJj")


class EvolutionClientTest(unittest.TestCase):
    def setUp(self):
        self.evo = EvolutionClient("http://localhost:8080/", "chave", "minha inst")

    def test_formatos_de_resposta_do_find_messages(self):
        record = {"key": {"id": "1"}}
        for payload in ([record], {"messages": {"records": [record]}}, {"messages": [record]}, {"records": [record]}):
            with Net(payload) as net:
                self.assertEqual(self.evo.recent_messages(10), [record])
        self.assertTrue(net.calls[0]["url"].endswith("/chat/findMessages/minha%20inst"))
        with Net({"outro": 1}):   # formato desconhecido não é "sem mensagens" (rodada 21)
            with self.assertRaises(EvolutionError):
                self.evo.recent_messages()

    def test_estado_da_conexao_e_dono(self):
        with Net({"instance": {"instanceName": "i", "state": "open"}}):
            self.assertEqual(self.evo.connection_state(), "open")
        with Net([{"ownerJid": "5511999990055@s.whatsapp.net"}]):
            self.assertEqual(self.evo.owner_jid(), "5511999990055@s.whatsapp.net")
        with Net(urllib.error.URLError("fora")):
            self.assertEqual(self.evo.owner_jid(), "")

    def test_midia_aceita_data_uri(self):
        with Net({"base64": "data:audio/ogg;base64,YWJj", "mimetype": "audio/ogg"}):
            self.assertEqual(self.evo.media_base64({"key": {"id": "1"}}), (b"abc", "audio/ogg"))
        with Net({}):
            self.assertIsNone(self.evo.media_base64({"key": {"id": "1"}}))


class GoogleCalendarTest(unittest.TestCase):
    def setUp(self):
        from tests.motor.support import NOW
        self.now = NOW
        self.log = logging.getLogger("test.gcal")
        self.log.propagate = False
        self.log.addHandler(logging.NullHandler())
        self.cal = GoogleCalendar("https://script.example/exec", "segredo", self.log)

    def test_cria_evento_com_segredo_e_devolve_id(self):
        with Net({"ok": True, "id": "EV1"}) as net:
            self.assertEqual(self.cal.create("t", "d", self.now, self.now, "a@b.com"), "EV1")
        self.assertEqual(net.calls[0]["body"]["secret"], "segredo")
        self.assertEqual(net.calls[0]["body"]["action"], "create")
        self.assertEqual(net.calls[0]["timeout"], agenda.GCAL_TIMEOUT)

    def test_falhas_da_ponte_nunca_levantam(self):
        with Net({"ok": False, "error": "x"}):
            self.assertEqual(self.cal.create("t", "d", self.now, self.now), "")
        with Net(urllib.error.URLError("fora")):
            self.assertFalse(self.cal.ping())
        with Net(http_error(500)):
            self.assertFalse(self.cal.cancel("EV1"))


class ConfigTest(unittest.TestCase):
    def test_env_positive(self):
        name = "ZX_SDR_TESTE_TIMEOUT"
        for raw, expected in (("", 10), ("abc", 10), ("0", 10), ("-5", 10), ("25", 25), ("1.5", 1.5)):
            with mock.patch.dict(os.environ, {name: raw}):
                self.assertEqual(env_positive(name, 10), expected, raw)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(name, None)
            self.assertEqual(env_positive(name, 10), 10)

    def test_timeouts_das_chamadas_longas_tem_padrao_positivo(self):
        self.assertGreaterEqual(ai_module.AI_TIMEOUT, 600) if "ZX_SDR_AI_TIMEOUT" not in os.environ else None
        for value in (ai_module.AI_TIMEOUT, http.HTTP_TIMEOUT, agenda.GCAL_TIMEOUT):
            self.assertGreater(value, 0)

    def test_from_dict_e_problems(self):
        cfg = from_dict({})
        self.assertEqual(cfg.evolution_url, "http://localhost:8080")
        self.assertIn("supabase.url vazio ou inválido", cfg.problems())
        self.assertIn("ai.api_key vazio", cfg.problems())
        full = from_dict({"supabase": {"url": "https://x.supabase.co/", "service_role_key": "k"},
                          "evolution": {"api_key": "k", "instance": "i"}, "ai": {"provider": "OpenAI", "api_key": "k"},
                          "google_calendar": {"apps_script_url": "https://s", "secret": "z"},
                          "motor": {"poll_seconds": 5, "max_msgs_per_minute": "3", "jobs_every_seconds": -1,
                                    "batch_window_seconds": 0}})
        self.assertEqual(full.problems(), [])
        self.assertEqual(full.supabase_url, "https://x.supabase.co")
        self.assertEqual(full.ai_provider, "openai")
        self.assertEqual(full.model, "gpt-4o-mini")
        self.assertTrue(full.gcal_enabled)
        self.assertEqual((full.poll_seconds, full.max_msgs_per_minute, full.jobs_every_seconds,
                          full.batch_window_seconds), (5, 3, 60, 0))
        self.assertIn("ai.provider deve ser um de", " ".join(from_dict({"ai": {"provider": "x"}}).problems()))

    def test_load_config_arquivo_ausente_e_json_quebrado(self):
        tmp = Path(tempfile.mkdtemp())
        with self.assertRaises(ConfigError) as ctx:
            load_config(tmp / "nao-existe.json")
        self.assertIn("config.exemplo.json", str(ctx.exception))
        broken = tmp / "c.json"
        broken.write_text("{quebrado", encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(broken)

    def test_exemplo_de_config_segue_o_contrato_e_nao_traz_segredos(self):
        path = Path(__file__).resolve().parents[2] / "config" / "config.exemplo.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(raw), {"supabase", "evolution", "ai", "google_calendar", "motor"})
        self.assertEqual(raw["supabase"], {"url": "", "service_role_key": ""})
        self.assertEqual(raw["evolution"]["api_key"], "")
        self.assertEqual(raw["ai"]["api_key"], "")
        self.assertEqual(raw["motor"], {"poll_seconds": 15, "jobs_every_seconds": 60, "max_msgs_per_minute": 8})


class RateLimiterTest(unittest.TestCase):
    def make(self, **kw):
        self.clock = Clock()
        return RateLimiter(Path(tempfile.mkdtemp()) / "rl.db", now_fn=self.clock, **kw)

    def test_limite_por_minuto_libera_depois_de_61s(self):
        limiter = self.make(max_per_minute=2, min_interval_seconds=0)
        limiter.record("+1")
        limiter.record("+2")
        self.assertEqual(limiter.wait_seconds("+3"), 61.0)
        self.clock.advance(seconds=62)
        self.assertEqual(limiter.wait_seconds("+3"), 0)

    def test_intervalo_minimo_so_vale_para_envio_proativo(self):
        limiter = self.make(max_per_minute=100, min_interval_seconds=60)
        limiter.record("+1")
        self.assertEqual(limiter.wait_seconds("+1", proactive=False), 0)
        self.assertGreater(limiter.wait_seconds("+1", proactive=True), 0)
        self.assertEqual(limiter.wait_seconds("+2", proactive=True), 0)
        self.clock.advance(seconds=61)
        self.assertEqual(limiter.wait_seconds("+1", proactive=True), 0)

    def test_limites_por_hora_e_por_dia(self):
        hourly = self.make(max_per_minute=0, max_per_hour=2, min_interval_seconds=0)
        hourly.record("+1")
        hourly.record("+2")
        self.assertEqual(hourly.wait_seconds("+3"), 3600.0)
        daily = self.make(max_per_minute=0, max_per_hour=100, max_per_day=1, min_interval_seconds=0)
        daily.record("+1")
        self.assertEqual(daily.wait_seconds("+2"), 86400.0)

    def test_acquire_espera_com_sleep_injetado_e_desiste_quando_passa_do_teto(self):
        limiter = self.make(max_per_minute=1, min_interval_seconds=0)
        limiter.record("+1")
        slept = []

        def fake_sleep(seconds):
            slept.append(seconds)
            self.clock.advance(seconds=seconds)
        self.assertTrue(limiter.acquire("+2", False, fake_sleep))
        self.assertEqual(slept, [61.0])
        hourly = self.make(max_per_minute=0, max_per_hour=1, min_interval_seconds=0)
        hourly.record("+1")
        slept.clear()
        self.assertFalse(hourly.acquire("+2", False, fake_sleep))
        self.assertEqual(slept, [])
        self.assertLessEqual(MAX_WAIT_SECONDS, 75)


class LockTest(unittest.TestCase):
    def test_segunda_instancia_e_recusada_e_libera_depois(self):
        path = Path(tempfile.mkdtemp()) / "data" / "motor.lock"
        first = InstanceLock(path)
        first.acquire()
        try:
            with self.assertRaises(LockError) as ctx:
                InstanceLock(path).acquire()
            self.assertIn("Já existe um motor", str(ctx.exception))
        finally:
            first.release()
        with InstanceLock(path):
            pass


class CliTest(unittest.TestCase):
    FULL = {"supabase": {"url": "https://x.supabase.co", "service_role_key": "k"},
            "evolution": {"api_key": "k", "instance": "i"}, "ai": {"provider": "gemini", "api_key": "k"}}

    def write_config(self, raw) -> str:
        path = Path(tempfile.mkdtemp()) / "config.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return str(path)

    def run_main(self, argv, ctx=None):
        out = io.StringIO()
        patches = [mock.patch("sys.stdout", out)]
        if ctx is not None:
            patches += [mock.patch.object(engine, "build_ctx", lambda cfg, log: ctx),
                        mock.patch.object(engine, "setup_logging", lambda cfg, console=True: ctx.log)]
        for patch in patches:
            patch.start()
        try:
            code = cli.main(argv)
        finally:
            for patch in reversed(patches):
                patch.stop()
        return code, out.getvalue()

    def test_check_tudo_certo_sai_0_com_marcas_verdes(self):
        code, out = self.run_main(["--config", self.write_config(self.FULL), "check"], make_ctx())
        self.assertEqual(code, 0)
        for label in ("Configuração", "Supabase", "Evolution", "IA"):
            self.assertIn(f"✅ {label}", out)
        self.assertNotIn("❌", out)

    def test_check_com_uma_falha_sai_1_e_diz_qual(self):
        ctx = make_ctx()
        ctx.evo.state = "close"
        code, out = self.run_main(["--config", self.write_config(self.FULL), "check"], ctx)
        self.assertEqual(code, 1)
        self.assertIn("❌ Evolution", out)
        self.assertIn("leia o QR code de novo", out)
        self.assertIn("✅ Supabase", out)

    def test_check_mostra_cada_falha_sem_levantar(self):
        ctx = make_ctx()
        ctx.db.ping = lambda: (_ for _ in ()).throw(RuntimeError("tabela ausente"))
        ctx.ai.ping = lambda: (_ for _ in ()).throw(AIError("chave inválida"))
        code, out = self.run_main(["--config", self.write_config(self.FULL), "check"], ctx)
        self.assertEqual(code, 1)
        self.assertIn("❌ Supabase: tabela ausente", out)
        self.assertIn("❌ IA: chave inválida", out)

    def test_check_com_config_incompleta_aponta_o_que_falta(self):
        ctx = make_ctx()
        ctx.cfg.service_role_key = ""
        code, out = self.run_main(["--config", self.write_config(self.FULL), "check"], ctx)
        self.assertEqual(code, 1)
        self.assertIn("❌ Configuração: supabase.service_role_key vazio", out)

    def test_sem_config_sai_2_com_instrucao(self):
        code, out = self.run_main(["--config", "/nao/existe/config.json", "once"])
        self.assertEqual(code, 2)
        self.assertIn("config.exemplo.json", out)

    def test_config_incompleta_bloqueia_once_e_simulate(self):
        for argv in (["once"], ["simulate", "--phone", "+5511999990000", "--text", "oi"]):
            code, out = self.run_main(["--config", self.write_config({})] + argv, make_ctx())
            self.assertEqual(code, 2)
            self.assertIn("Configuração incompleta", out)

    def test_simulate_roda_a_conversa_sem_enviar_nada(self):
        ctx = make_ctx()
        ctx.ai.push({"reply": "Oi! Sou a Ana.", "action": "responder"})
        code, out = self.run_main(["--config", self.write_config(self.FULL), "simulate", "--phone", "+5511999990000",
                                   "--text", "oi"], ctx)
        self.assertEqual(code, 0)
        self.assertIn("Oi! Sou a Ana.", out)
        self.assertEqual(ctx.evo.sent, [])

    def test_simulate_com_telefone_invalido_sai_2(self):
        code, out = self.run_main(["--config", self.write_config(self.FULL), "simulate", "--phone", "abc",
                                   "--text", "oi"], make_ctx())
        self.assertEqual(code, 2)
        self.assertIn("telefone inválido", out)

    def test_once_com_outro_motor_rodando_sai_3(self):
        class Busy:
            def __init__(self, path):
                pass

            def __enter__(self):
                raise LockError("Já existe um motor rodando")

            def __exit__(self, *exc):
                return False
        with mock.patch.object(cli, "InstanceLock", Busy):
            code, out = self.run_main(["--config", self.write_config(self.FULL), "once"], make_ctx())
        self.assertEqual(code, 3)
        self.assertIn("Já existe um motor", out)

    def test_once_roda_um_ciclo_e_sai_0(self):
        ctx = make_ctx()

        class Free:
            def __init__(self, path):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        with mock.patch.object(cli, "InstanceLock", Free):
            code, out = self.run_main(["--config", self.write_config(self.FULL), "once"], ctx)
        self.assertEqual(code, 0)
        self.assertIn("respostas", out)

    def test_parser_exige_subcomando_e_mostra_versao(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
            cli.build_parser().parse_args([])
        with self.assertRaises(SystemExit) as ctx, mock.patch("sys.stdout", io.StringIO()):
            cli.build_parser().parse_args(["--version"])
        self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
