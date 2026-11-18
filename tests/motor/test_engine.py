"""Motor de ponta a ponta com fakes: ingestão idempotente, ciclo, agrupamento, simulação e mídia."""

import unittest
from datetime import timedelta

from motor import engine
from motor.ai import AIError
from tests.motor.support import (CLOSER_PHONE, LEAD_PHONE, OWNER_PHONE, TEST_PHONE, default_agent, inbound,
                                 make_ctx)


class IngestTest(unittest.TestCase):
    def test_idempotente_por_wa_id(self):
        ctx = make_ctx()
        ctx.evo.add(LEAD_PHONE, "oi", wa_id="W1")
        first = engine.ingest(ctx)
        second = engine.ingest(ctx)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, set())
        self.assertEqual(len([m for m in ctx.db.messages if m["wa_id"] == "W1"]), 1)

    def test_cria_lead_com_nome_do_whatsapp_e_marca_ultima_mensagem(self):
        ctx = make_ctx()
        ctx.evo.add(LEAD_PHONE, "oi")
        engine.ingest(ctx)
        lead = ctx.db.get_lead_by_phone(LEAD_PHONE)
        self.assertEqual(lead["name"], "Joana")
        self.assertIsNotNone(lead["last_inbound_at"])

    def test_ignora_grupo_dono_closer_e_historico_antigo(self):
        ctx = make_ctx()
        ctx.evo.add(LEAD_PHONE, "grupo", jid="120363000000000000@g.us")
        ctx.evo.add(OWNER_PHONE, "sou o dono")
        ctx.evo.add(CLOSER_PHONE, "sou o closer")
        self.assertEqual(engine.ingest(ctx), set())
        self.assertEqual(ctx.db.leads, {})

    def test_mensagem_fora_da_janela_e_gravada_sem_resposta_e_avisa_o_dono_uma_vez(self):
        ctx = make_ctx()
        old = ctx.cfg.catchup_minutes * 60 + 600
        ctx.evo.add(LEAD_PHONE, "mensagem velha", wa_id="OLD1", age_seconds=old)
        ctx.evo.add(LEAD_PHONE, "outra velha", wa_id="OLD2", age_seconds=old - 5)
        self.assertEqual(engine.ingest(ctx), set())          # nada para responder
        self.assertEqual([m["body"] for m in ctx.db.messages if m["direction"] == "in"], ["mensagem velha", "outra velha"])
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])
        self.assertEqual(len(ctx.evo.texts_to(OWNER_PHONE)), 1)   # um aviso por lead, não por mensagem

    def test_numero_de_teste_do_dono_continua_valendo_como_lead(self):
        ctx = make_ctx(default_agent(owner_phone=TEST_PHONE, test_phones=[TEST_PHONE], test_only=True))
        ctx.evo.add(TEST_PHONE, "teste")
        self.assertEqual(len(engine.ingest(ctx)), 1)

    def test_em_producao_dono_em_test_phones_continua_ignorado(self):
        ctx = make_ctx(default_agent(owner_phone=TEST_PHONE, test_phones=[TEST_PHONE], test_only=False))
        ctx.evo.add(TEST_PHONE, "oi, sou o dono")
        self.assertEqual(engine.ingest(ctx), set())
        self.assertIsNone(ctx.db.get_lead_by_phone(TEST_PHONE))

    def test_modo_teste_nao_cria_lead_para_quem_nao_e_de_teste(self):
        ctx = make_ctx(default_agent(test_only=True, test_phones=[TEST_PHONE]))
        ctx.evo.add(LEAD_PHONE, "oi")
        ctx.evo.add(TEST_PHONE, "oi")
        fresh = engine.ingest(ctx)
        self.assertEqual(len(fresh), 1)
        self.assertIsNone(ctx.db.get_lead_by_phone(LEAD_PHONE))

    def test_mensagem_do_dono_pelo_celular_vira_takeover(self):
        ctx = make_ctx()
        inbound(ctx, "oi")
        ctx.evo.add(LEAD_PHONE, "deixa comigo", from_me=True, wa_id="MANUAL1")
        engine.ingest(ctx)
        lead = ctx.db.get_lead_by_phone(LEAD_PHONE)
        self.assertIsNotNone(lead["takeover_until"])
        self.assertEqual(ctx.db.messages[-1]["author"], "dono")

    def test_mensagem_enviada_pelo_proprio_motor_nao_e_takeover(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        key = ctx.messenger.send(LEAD_PHONE, "Olá!", lead_id=lead["id"])
        ctx.evo.add(LEAD_PHONE, "Olá!", from_me=True, wa_id=key)   # eco da própria mensagem na Evolution
        engine.ingest(ctx)
        self.assertIsNone(ctx.db.get_lead_by_phone(LEAD_PHONE)["takeover_until"])

    def test_fromme_para_numero_sem_lead_e_ignorada(self):
        ctx = make_ctx()
        ctx.evo.add("+5511999990077", "oi, tudo bem?", from_me=True)
        engine.ingest(ctx)
        self.assertEqual(ctx.db.leads, {})

    def test_reacao_sem_texto_e_ignorada(self):
        ctx = make_ctx()
        ctx.evo.add(LEAD_PHONE, "", extra={"message": {"reactionMessage": {"text": "👍"}}})
        self.assertEqual(engine.ingest(ctx), set())

    def test_lid_com_numero_alternativo(self):
        ctx = make_ctx()
        ctx.evo.add(LEAD_PHONE, "oi", jid="98765@lid")
        ctx.evo.records[-1]["key"]["remoteJidAlt"] = f"{LEAD_PHONE.lstrip('+')}@s.whatsapp.net"
        self.assertEqual(len(engine.ingest(ctx)), 1)
        self.assertIsNotNone(ctx.db.get_lead_by_phone(LEAD_PHONE))

    def test_audio_e_transcrito_pela_ia(self):
        ctx = make_ctx()
        ctx.evo.add(LEAD_PHONE, "", extra={"message": {"audioMessage": {"seconds": 5}}})
        ctx.evo.media = (b"bytes", "audio/ogg")
        engine.ingest(ctx)
        body = ctx.db.messages[-1]["body"]
        self.assertIn("[áudio]", body)
        self.assertIn("quero saber o preço da avaliação", body)

    def test_midia_sem_transcricao_vira_marcador(self):
        ctx = make_ctx()
        ctx.evo.add(LEAD_PHONE, "", extra={"message": {"audioMessage": {"seconds": 5}}})
        ctx.evo.media = None
        engine.ingest(ctx)
        self.assertEqual(ctx.db.messages[-1]["body"], "[áudio]")

    def test_falha_na_transcricao_nao_derruba_a_ingestao(self):
        ctx = make_ctx()
        ctx.evo.add(LEAD_PHONE, "", extra={"message": {"imageMessage": {"caption": "olha"}}})
        ctx.evo.media = (b"x", "image/jpeg")
        ctx.ai.transcribe = lambda *a: (_ for _ in ()).throw(RuntimeError("fora"))
        engine.ingest(ctx)
        self.assertEqual(ctx.db.messages[-1]["body"], "[imagem] olha")


class CycleTest(unittest.TestCase):
    def test_ciclo_completo_responde_e_registra(self):
        ctx = make_ctx()
        ctx.ai.push({"reply": "Oi! Como posso ajudar?", "action": "responder"})
        ctx.evo.add(LEAD_PHONE, "oi")
        summary = engine.cycle(ctx)
        self.assertEqual(summary, {"respostas": 1, "outbox": 0})
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), ["Oi! Como posso ajudar?"])
        self.assertEqual(ctx.db.messages[-1]["direction"], "out")
        self.assertEqual(engine.cycle(ctx), {"respostas": 0, "outbox": 0})     # nada novo: não responde de novo
        self.assertEqual(len(ctx.ai.calls), 1)

    def test_agrupa_mensagens_em_sequencia_numa_unica_resposta(self):
        ctx = make_ctx()
        ctx.cfg.batch_window_seconds = 9
        ctx.evo.add(LEAD_PHONE, "oi", wa_id="A1")
        original = ctx.sleep_fn

        def sleeper(seconds):
            original(seconds)
            ctx.evo.add(LEAD_PHONE, "queria saber o valor", wa_id="A2")   # chega durante a espera
        ctx.sleep_fn = sleeper
        engine.cycle(ctx)
        self.assertEqual(ctx.slept, [9])
        self.assertEqual(len(ctx.ai.calls), 1)
        self.assertEqual(len(ctx.evo.texts_to(LEAD_PHONE)), 1)
        ins = [m["body"] for m in ctx.db.messages if m["direction"] == "in"]
        self.assertEqual(ins, ["oi", "queria saber o valor"])

    def test_um_lead_com_erro_nao_trava_os_outros(self):
        ctx = make_ctx()
        ctx.evo.add("+5511999990021", "oi")
        ctx.evo.add("+5511999990022", "oi")
        ctx.ai.push(AIError("fora do ar"), {"reply": "Olá!", "action": "responder"})
        # a falha vira mensagem neutra + alerta ao dono; o outro lead segue normalmente
        summary = engine.cycle(ctx)
        self.assertEqual(summary["respostas"], 2)
        self.assertEqual(len([t for p, t in ctx.evo.sent if p != OWNER_PHONE]), 2)
        self.assertEqual(len(ctx.evo.texts_to(OWNER_PHONE)), 1)

    def test_erro_inesperado_em_um_lead_nao_para_o_ciclo(self):
        ctx = make_ctx()
        ctx.evo.add("+5511999990021", "oi")
        ctx.evo.add("+5511999990022", "oi")
        ctx.ai.push(RuntimeError("bug"), {"reply": "Olá!", "action": "responder"})
        summary = engine.cycle(ctx)
        self.assertEqual(summary["respostas"], 1)

    def test_evolution_fora_nao_derruba_o_ciclo(self):
        ctx = make_ctx()
        ctx.evo.recent_messages = lambda limit=50, page=1: (_ for _ in ()).throw(engine.EvolutionError("fora"))
        self.assertEqual(engine.cycle(ctx), {"respostas": 0, "outbox": 0})

    def test_ciclo_envia_a_outbox_do_painel(self):
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        ctx.db.add_outbox(LEAD_PHONE, "Mensagem do painel", lead_id=lead["id"])
        summary = engine.cycle(ctx)
        self.assertEqual(summary["outbox"], 1)
        self.assertEqual(ctx.db.outbox[0]["status"], "enviado")
        self.assertEqual(ctx.db.outbox[0]["wa_key_id"], "OUT1")

    def test_edicao_do_painel_vale_na_proxima_conversa(self):
        ctx = make_ctx()
        ctx.db.agent["agent_name"] = "Bia"
        ctx.evo.add(LEAD_PHONE, "oi")
        engine.cycle(ctx)
        self.assertIn("Bia", ctx.ai.calls[0]["system"])


class RetryTest(unittest.TestCase):
    def test_lead_com_excecao_e_reprocessado_no_ciclo_seguinte(self):
        ctx = make_ctx()
        ctx.evo.add(LEAD_PHONE, "oi")
        ctx.ai.push(RuntimeError("bug"), {"reply": "Olá!", "action": "responder"})
        self.assertEqual(engine.cycle(ctx)["respostas"], 0)
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])
        self.assertEqual(engine.cycle(ctx)["respostas"], 1)          # sem mensagem nova: retry do mesmo lead
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), ["Olá!"])
        self.assertEqual(engine.cycle(ctx)["respostas"], 0)          # e não repete depois de responder

    def test_apos_tres_falhas_avisa_o_dono_e_para(self):
        ctx = make_ctx()
        ctx.evo.add(LEAD_PHONE, "oi")
        ctx.ai.push(*[RuntimeError("bug")] * 3)
        for _ in range(4):
            engine.cycle(ctx)
        self.assertEqual(len(ctx.ai.calls), 3)
        alerts = ctx.evo.texts_to(OWNER_PHONE)
        self.assertEqual(len(alerts), 1)
        self.assertIn("3 tentativas", alerts[0])


class ReagendarTest(unittest.TestCase):
    def _booked(self):
        from motor import agenda
        from tests.motor.support import sp
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        ok, _ = agenda.book_for_lead(ctx, lead, sp(19, 14), ctx.state["agent"])
        self.assertTrue(ok)
        return ctx, lead, agenda, sp

    def test_falha_de_rede_no_novo_horario_nao_perde_o_antigo(self):
        from motor.http import NetworkError
        ctx, lead, agenda, sp = self._booked()
        original = ctx.db.reschedule_slot

        def boom(*a, **k):
            raise NetworkError("sem rede")
        ctx.db.reschedule_slot = boom
        ok, text = agenda.book_for_lead(ctx, ctx.db.get_lead(lead["id"]), sp(19, 15), ctx.state["agent"])
        ctx.db.reschedule_slot = original
        self.assertFalse(ok)
        self.assertEqual([a["status"] for a in ctx.db.appointments], ["agendado"])
        self.assertIsNotNone(ctx.db.active_appointment(lead["id"], ctx.now()))
        self.assertEqual(ctx.db.get_lead(lead["id"])["stage"], "agendado")
        self.assertEqual(len(ctx.evo.texts_to(OWNER_PHONE)), 1)

    def test_reagendamento_usa_a_rpc_atomica_e_nunca_marca_o_antigo_por_fora(self):
        """Sem a RPC, o motor marcava o antigo 'reagendado' antes de reservar; uma exceção deixava o lead sem horário."""
        ctx, lead, agenda, sp = self._booked()
        calls = []
        original = ctx.db.reschedule_slot
        ctx.db.reschedule_slot = lambda *a, **k: (calls.append(a), original(*a, **k))[1]
        ctx.db.update_appointment = lambda *a, **k: self.fail("não pode mexer no agendamento antigo fora da RPC")
        ok, _ = agenda.book_for_lead(ctx, ctx.db.get_lead(lead["id"]), sp(19, 15), ctx.state["agent"])
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)

    def test_horario_tomado_restaura_o_antigo(self):
        ctx, lead, agenda, sp = self._booked()
        ctx.db.force_conflict.add(sp(19, 15))
        ok, _ = agenda.book_for_lead(ctx, ctx.db.get_lead(lead["id"]), sp(19, 15), ctx.state["agent"])
        self.assertFalse(ok)
        self.assertEqual([a["status"] for a in ctx.db.appointments], ["agendado"])

    def test_reagendamento_ok_marca_o_antigo(self):
        ctx, lead, agenda, sp = self._booked()
        ok, _ = agenda.book_for_lead(ctx, ctx.db.get_lead(lead["id"]), sp(19, 15), ctx.state["agent"])
        self.assertTrue(ok)
        self.assertEqual([a["status"] for a in ctx.db.appointments], ["reagendado", "agendado"])


class FailClosedEFilaTest(unittest.TestCase):
    def test_test_only_nulo_e_modo_teste_no_envio_e_na_leitura(self):
        ctx = make_ctx(default_agent(test_only=None, test_phones=[TEST_PHONE]))
        self.assertFalse(ctx.messenger.allowed(LEAD_PHONE))
        self.assertTrue(ctx.messenger.allowed(TEST_PHONE))
        ctx.evo.add(LEAD_PHONE, "oi")
        self.assertEqual(engine.ingest(ctx), set())
        self.assertIsNone(ctx.db.get_lead_by_phone(LEAD_PHONE))

    def test_mensagem_do_proprio_motor_com_gravacao_falha_nao_vira_takeover(self):
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        real_add = ctx.db.add_message
        ctx.db.add_message = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("supabase fora"))
        ctx.messenger.send(LEAD_PHONE, "Olá!", lead_id=lead["id"])
        ctx.db.add_message = real_add
        ctx.evo.add(LEAD_PHONE, "Olá!", wa_id="OUT1", from_me=True)
        engine.ingest(ctx)
        self.assertIsNone(ctx.db.get_lead(lead["id"])["takeover_until"])


class StartupTest(unittest.TestCase):
    def test_recover_responde_quem_ficou_sem_resposta_antes_da_queda(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi, alguém aí?")
        engine.startup(ctx)
        self.assertEqual(ctx.state["pending"], {lead["id"]})
        engine.cycle(ctx)
        self.assertEqual(len(ctx.evo.texts_to(LEAD_PHONE)), 1)

    def test_recover_ignora_quem_ja_foi_respondido_ou_e_antigo(self):
        ctx = make_ctx()
        answered = inbound(ctx, "oi")
        ctx.db.update_lead(answered["id"], {"last_outbound_at": ctx.now().isoformat()})
        old = inbound(ctx, "oi", phone="+5511999990031")
        ctx.db.update_lead(old["id"], {"last_inbound_at": (ctx.now() - timedelta(days=3)).isoformat()})
        self.assertEqual(engine.recover_unanswered(ctx), set())

    def test_startup_marca_envio_interrompido_como_erro_sem_reenviar(self):
        ctx = make_ctx()
        ctx.db.add_outbox(LEAD_PHONE, "oi", status="enviando", attempts=1)
        engine.startup(ctx)
        self.assertEqual(ctx.db.outbox[0]["status"], "erro")
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])   # o alerta consolidado vai só ao dono (r8-9)

    def test_run_once_roda_ciclo_e_rotinas(self):
        ctx = make_ctx()
        result = engine.run_once(ctx)
        self.assertEqual(result["respostas"], 0)
        self.assertIn("lembretes", result["rotinas"])


class SimulateTest(unittest.TestCase):
    def test_simulacao_nao_toca_a_evolution_e_ignora_o_modo_teste(self):
        ctx = make_ctx(default_agent(test_only=True, test_phones=[]))
        ctx.ai.push({"reply": "Oi! Sou a Ana.", "action": "responder"})
        printed = []
        engine.simulate(ctx, "+55 11 99999-0000", "oi", out=printed.append)
        self.assertEqual(ctx.evo.sent, [])
        self.assertTrue(any("Oi! Sou a Ana." in p for p in printed))
        self.assertIsNone(ctx.db.get_lead_by_phone(TEST_PHONE))   # a simulação não grava lead
        self.assertEqual(ctx.db.messages, [])
        self.assertTrue(any("nada foi gravado" in p and "create_lead" in p for p in printed))

    def _snapshot(self, db):
        import copy
        return copy.deepcopy((db.leads, db.messages, db.appointments, db.objections, db.outbox))

    def _forbid_writes(self, db):
        def boom(name):
            def fn(*a, **k):
                raise AssertionError(f"simulate chamou {name} no banco real")
            return fn
        for name in ("update_lead", "create_lead", "add_message", "add_objection", "update_appointment",
                     "book_slot", "reschedule_slot", "enqueue_outbox", "add_outbox", "claim_outbox", "finish_outbox"):
            setattr(db, name, boom(name))

    def test_simulacao_cancelar_nao_escreve_no_banco_real(self):
        from tests.motor.support import sp
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        ctx.db.book_slot(lead["id"], sp(19, 14))
        before = self._snapshot(ctx.db)
        self._forbid_writes(ctx.db)
        ctx.ai.push({"reply": "Cancelado.", "action": "cancelar"})
        result = engine.simulate(ctx, LEAD_PHONE, "quero cancelar", out=lambda *_: None)
        self.assertEqual(self._snapshot(ctx.db), before)
        self.assertEqual(ctx.db.appointments[0]["status"], "agendado")
        self.assertIn("update_appointment", [name for name, _ in result["escritas_simuladas"]])
        self.assertIsInstance(ctx.db, type(ctx.db))   # banco real restaurado
        self.assertNotEqual(type(ctx.db).__name__, "ReadOnlyDB")

    def test_simulacao_pare_nao_marca_opt_out_do_lead_real(self):
        ctx = make_ctx()
        inbound(ctx, "oi")
        before = self._snapshot(ctx.db)
        self._forbid_writes(ctx.db)
        result = engine.simulate(ctx, LEAD_PHONE, "pare de me mandar mensagem", out=lambda *_: None)
        self.assertEqual(self._snapshot(ctx.db), before)
        self.assertFalse(ctx.db.get_lead_by_phone(LEAD_PHONE)["opt_out"])
        self.assertEqual(result["status"], "opt_out_confirmado")
        self.assertIn("update_lead", [name for name, _ in result["escritas_simuladas"]])

    def test_simulacao_nao_agenda_horario_real(self):
        ctx = make_ctx()
        from tests.motor.support import sp
        ctx.ai.push({"reply": "Fechado!", "action": "agendar", "slot": sp(19, 14).isoformat(), "nome": "Joana"})
        out = []
        engine.simulate(ctx, TEST_PHONE, "pode ser amanhã às 14h", out=out.append)
        self.assertEqual(ctx.db.appointments, [])
        self.assertEqual(ctx.db.taken, set())
        self.assertTrue(any("simulação" in o for o in out))
        self.assertEqual(ctx.evo.sent, [])

    def test_simulacao_nao_cria_evento_no_google_agenda(self):
        from tests.motor.support import sp

        class Gcal:
            calls = 0

            def create(self, *a, **k):
                Gcal.calls += 1
                return "ev"
            cancel = create
        ctx = make_ctx()
        ctx.gcal = Gcal()
        ctx.ai.push({"reply": "Fechado!", "action": "agendar", "slot": sp(19, 14).isoformat()})
        engine.simulate(ctx, TEST_PHONE, "pode ser 14h", out=lambda *_: None)
        self.assertEqual(ctx.db.appointments, [])
        self.assertEqual(Gcal.calls, 0)

    def test_simulacao_nao_exige_evolution_na_config(self):
        ctx = make_ctx()
        ctx.cfg.evolution_key = ""
        ctx.cfg.instance = ""
        self.assertEqual(ctx.cfg.problems(need_evolution=False), [])
        self.assertTrue(ctx.cfg.problems())

    def test_telefone_invalido_e_recusado(self):
        ctx = make_ctx()
        with self.assertRaises(ValueError):
            engine.simulate(ctx, "abc", "oi")


if __name__ == "__main__":
    unittest.main()
