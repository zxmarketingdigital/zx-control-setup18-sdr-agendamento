"""Rodada 11 do motor: leitura direcionada do chat antes de cada envio, filtro de links, e-mail da IA, alertas de
reserva/pós-reserva, mensagens durante a pausa da IA, repasse incerto e mensagem de ingestão com falha permanente."""

import unittest
from unittest import mock

from motor import agenda, engine, jobs
from motor.ai import AIError
from motor.conversa import (NEUTRAL_MESSAGE, OPT_OUT_CONFIRMATION, PAUSE_INBOUND_KEY, handle_turn,
                            strip_unknown_links)
from motor.evolution import EvolutionTimeout
from motor.util import iso
from tests.motor.support import CLOSER_PHONE, LEAD_PHONE, OWNER_PHONE, default_agent, inbound, make_ctx, sp
from tests.motor.test_jobs import book

STOP = "pare de me mandar mensagem"


def owner_texts(ctx):
    """Alertas ao dono (a outbox e o envio direto são a mesma mensagem: conta uma vez)."""
    texts = [r["body"] for r in ctx.db.outbox if r["phone"] == OWNER_PHONE]
    return texts + [t for t in ctx.evo.texts_to(OWNER_PHONE) if t not in texts]


def stop_arrives_during(ctx, wa_id="STOP1"):
    """A IA 'demora': enquanto ela responde, o lead escreve um pedido de saída que só existe na Evolution."""
    original = ctx.ai.complete

    def slow(system, messages):
        ctx.evo.add(LEAD_PHONE, STOP, wa_id=wa_id)
        return original(system, messages)
    ctx.ai.complete = slow


class LeituraDirecionadaTest(unittest.TestCase):
    def test_pare_so_na_evolution_durante_a_ia_nao_envia_a_resposta(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        stop_arrives_during(ctx)
        ctx.ai.push({"reply": "Claro! O que você procura?", "action": "responder"})
        handle_turn(ctx, lead["id"])
        self.assertTrue(ctx.db.leads[lead["id"]]["opt_out"])
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [OPT_OUT_CONFIRMATION])   # só a confirmação de saída
        self.assertTrue(any(m["body"] == STOP for m in ctx.db.messages))          # a mensagem foi ingerida

    def test_pare_durante_a_ia_que_falha_nao_envia_nem_a_mensagem_neutra(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        stop_arrives_during(ctx)
        ctx.ai.push(AIError("fora"), AIError("fora"))
        handle_turn(ctx, lead["id"])
        self.assertTrue(ctx.db.leads[lead["id"]]["opt_out"])
        self.assertNotIn(NEUTRAL_MESSAGE, ctx.evo.texts_to(LEAD_PHONE))

    def test_pare_durante_a_agenda_que_falha_nao_envia_a_mensagem_neutra(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        with mock.patch.object(agenda, "load_slots", side_effect=RuntimeError("agenda fora")):
            ctx.evo.add(LEAD_PHONE, STOP, wa_id="STOP9")
            handle_turn(ctx, lead["id"])
        self.assertTrue(ctx.db.leads[lead["id"]]["opt_out"])
        self.assertNotIn(NEUTRAL_MESSAGE, ctx.evo.texts_to(LEAD_PHONE))

    def test_falha_da_leitura_direcionada_nao_trava_a_resposta(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        ctx.evo.fail_chat_read = True
        ctx.ai.push({"reply": "Claro! O que você procura?", "action": "responder"})
        self.assertEqual(handle_turn(ctx, lead["id"])["status"], "ok")
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), ["Claro! O que você procura?"])

    def test_takeover_do_dono_que_chega_durante_a_ia_bloqueia_o_envio(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        original = ctx.ai.complete

        def slow(system, messages):
            ctx.evo.add(LEAD_PHONE, "Oi Joana, aqui é o dono, já te respondo", wa_id="DONO1", from_me=True)
            return original(system, messages)
        ctx.ai.complete = slow
        ctx.ai.push({"reply": "Claro!", "action": "responder"})
        self.assertEqual(handle_turn(ctx, lead["id"])["status"], "takeover")
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])


class LinksTest(unittest.TestCase):
    """O literal «SEGREDO-REDIGIDO» do relatório é redação do revisor: o regex está íntegro (confirmado)."""

    def setUp(self):
        self.ctx = make_ctx(default_agent(knowledge="Escreva para contato@clinica.com.br"))
        self.ctx.state["settings"] = {"public_slug": "agenda.clinica.com.br/ana"}

    def clean(self, text):
        return strip_unknown_links(self.ctx, self.ctx.state["agent"], text)

    def test_links_fora_da_base_saem(self):
        for bad in ("promo-garantida.xyz/pix", "https://site-estranho.com/x", "www.golpe.com.br"):
            with self.subTest(bad=bad):
                out = self.clean(f"Para garantir o seu horário com a nossa equipe confira em {bad} agora")
                self.assertNotIn(bad, out)
                self.assertIn("nossa equipe", out)

    def test_email_e_link_da_pagina_publica_ficam(self):
        text = "Pode escrever para contato@clinica.com.br ou abrir agenda.clinica.com.br/ana quando quiser"
        self.assertEqual(self.clean(text), text)


class EmailDaIATest(unittest.TestCase):
    def turn(self, ctx, lead, email):
        ctx.ai.push({"reply": "Anotado, obrigada!", "action": "responder", "email": email})
        handle_turn(ctx, lead["id"])
        return ctx.db.leads[lead["id"]]["email"]

    def test_grava_quando_vazio_e_valido(self):
        ctx = make_ctx()
        self.assertEqual(self.turn(ctx, inbound(ctx, "meu email é ana@empresa.com"), "ana@empresa.com"), "ana@empresa.com")

    def test_nunca_sobrescreve_email_existente(self):
        ctx = make_ctx()
        lead = inbound(ctx, "manda para x@dominio.xyz")
        ctx.db.update_lead(lead["id"], {"email": "maria@empresa.com"})
        self.assertEqual(self.turn(ctx, lead, "x@dominio.xyz"), "maria@empresa.com")

    def test_invalido_nao_grava(self):
        for bad in ("ana@", "texto com @ qualquer", "a@b", "ana@@x.com", "a" * 70 + "@x.com", "ana@x.com " + "y" * 260):
            with self.subTest(bad=bad[:30]):
                ctx = make_ctx()
                self.assertIsNone(self.turn(ctx, inbound(ctx, "oi"), bad))


class ReservaAlertaTest(unittest.TestCase):
    def setUp(self):
        self.ctx = make_ctx()
        self.lead = inbound(self.ctx, "quero marcar")
        self.agent = self.ctx.state["agent"]

    def test_erro_nao_tratado_avisa_o_dono_e_o_lead_recebe_texto_neutro(self):
        self.ctx.db.book_slot = lambda *a, **k: {"ok": False, "error": "sem_closer_ativo"}
        ok, text = agenda.book_for_lead(self.ctx, self.lead, sp(19, 14), self.agent)
        self.assertFalse(ok)
        alert = owner_texts(self.ctx)
        self.assertEqual(len(alert), 1)
        for part in ("Joana", LEAD_PHONE, "19/11/2026", "14:00", "sem_closer_ativo"):
            self.assertIn(part, alert[0])
        self.assertNotIn("sem_closer_ativo", text)

    def test_falha_do_load_slots_no_horario_ocupado_avisa_o_dono(self):
        self.ctx.db.force_conflict.add(sp(19, 14))
        with mock.patch.object(agenda, "load_slots", side_effect=RuntimeError("agenda fora")):
            ok, _ = agenda.book_for_lead(self.ctx, self.lead, sp(19, 14), self.agent)
        self.assertFalse(ok)
        self.assertEqual(len(owner_texts(self.ctx)), 1)
        self.assertIn("agenda fora", owner_texts(self.ctx)[0])


class PosReservaTest(unittest.TestCase):
    class Gcal:
        def __init__(self):
            self.cancelled, self.created = [], []

        def cancel(self, event_id):
            self.cancelled.append(event_id)
            return True

        def create(self, *a, **k):
            self.created.append(a)
            return "EV2"

    def test_update_lead_falhando_nao_pula_cancelamento_nem_criacao_e_avisa_dono_e_closer(self):
        ctx = make_ctx()
        ctx.gcal = self.Gcal()
        lead = inbound(ctx, "pode remarcar?")
        old = book(ctx, lead, sp(19, 14), created_hours_ago=0)
        old["google_event_id"] = "EV1"
        real_update = ctx.db.update_lead

        def flaky(lead_id, fields):
            if fields.get("stage") == "reagendado":
                raise RuntimeError("503 do banco")
            return real_update(lead_id, fields)
        ctx.db.update_lead = flaky
        ok, _ = agenda.book_for_lead(ctx, ctx.db.get_lead(lead["id"]), sp(20, 10), ctx.state["agent"],
                                     queue_confirmation=True)
        self.assertTrue(ok)
        self.assertEqual(ctx.gcal.cancelled, ["EV1"])
        self.assertEqual(len(ctx.gcal.created), 1)
        alerts = [t for t in owner_texts(ctx) if "reagendado" in t]
        self.assertEqual(len(alerts), 1)
        for part in ("20/11/2026", "10:00", "Joana", "Marcos"):
            self.assertIn(part, alerts[0])
        self.assertTrue(any("reagendado" in t for t in ctx.evo.texts_to(CLOSER_PHONE)))


class PausaDaIATest(unittest.TestCase):
    def pause(self, ctx, **extra):
        ctx.db.agent.update(paused=True, **extra)
        engine.refresh(ctx)

    def test_marca_o_lead_e_avisa_o_dono_uma_vez_por_pausa(self):
        ctx = make_ctx()
        self.pause(ctx, paused_at="2026-11-18T10:00:00Z")
        a = inbound(ctx, "oi")
        b = inbound(ctx, "olá", phone="+5511999990002", name="Bia")
        self.assertEqual(handle_turn(ctx, a["id"])["status"], "pausado")
        self.assertEqual(handle_turn(ctx, b["id"])["status"], "pausado")
        for lead in (a, b):
            self.assertTrue(ctx.db.leads[lead["id"]]["qualification"][PAUSE_INBOUND_KEY])
        self.assertEqual(len(owner_texts(ctx)), 1)
        self.pause(ctx, paused_at="2026-11-18T11:00:00Z")   # nova ativação da pausa
        handle_turn(ctx, a["id"])
        self.assertEqual(len(owner_texts(ctx)), 2)

    def test_marca_preserva_o_resto_da_qualificacao(self):
        ctx = make_ctx()
        self.pause(ctx)
        lead = inbound(ctx, "oi")
        ctx.db.update_lead(lead["id"], {"qualification": {"interesse": "limpeza"}})
        handle_turn(ctx, lead["id"])
        q = ctx.db.leads[lead["id"]]["qualification"]
        self.assertEqual(q["interesse"], "limpeza")
        self.assertIn(PAUSE_INBOUND_KEY, q)

    def test_depois_da_pausa_recover_ignora_a_janela_e_o_turno_limpa_a_marca(self):
        ctx = make_ctx()
        self.pause(ctx)
        lead = inbound(ctx, "oi")
        handle_turn(ctx, lead["id"])
        ctx.clock.advance(hours=12)              # muito além do catch-up de 360 min
        self.assertEqual(engine.recover_unanswered(ctx), set())   # ainda pausada: nada a fazer
        ctx.db.agent["paused"] = False
        engine.refresh(ctx)
        self.assertEqual(engine.recover_unanswered(ctx), {lead["id"]})
        ctx.ai.push({"reply": "Oi! O que você procura?", "action": "responder"})
        self.assertEqual(handle_turn(ctx, lead["id"])["status"], "ok")
        self.assertNotIn(PAUSE_INBOUND_KEY, ctx.db.leads[lead["id"]]["qualification"])
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), ["Oi! O que você procura?"])

    def test_sem_marca_a_janela_continua_valendo(self):
        ctx = make_ctx()
        inbound(ctx, "oi")
        ctx.clock.advance(hours=12)
        self.assertEqual(engine.recover_unanswered(ctx), set())

    def test_opt_out_e_takeover_nao_voltam(self):
        ctx = make_ctx()
        self.pause(ctx)
        a, b = inbound(ctx, "oi"), inbound(ctx, "olá", phone="+5511999990002")
        handle_turn(ctx, a["id"])
        handle_turn(ctx, b["id"])
        ctx.clock.advance(hours=12)
        ctx.db.agent["paused"] = False
        engine.refresh(ctx)
        ctx.db.update_lead(a["id"], {"opt_out": True})
        ctx.db.update_lead(b["id"], {"takeover_until": iso(ctx.now() + engine.timedelta(hours=2))})
        self.assertEqual(engine.recover_unanswered(ctx), set())


class RepasseIncertoTest(unittest.TestCase):
    def test_timeout_no_repasse_mantem_a_flag_sem_reenviar_e_avisa_o_dono(self):
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana Silva")
        appt = book(ctx, lead, sp(19, 14), created_hours_ago=0)
        original = ctx.evo.send_text

        def flaky(phone, text, delay_ms=0):
            if phone == CLOSER_PHONE:
                raise EvolutionTimeout("timed out")
            return original(phone, text, delay_ms)
        ctx.evo.send_text = flaky
        self.assertEqual(jobs.handoffs(ctx), 0)
        self.assertIsNotNone(ctx.db.appointments[0]["handoff_sent_at"])   # flag mantida
        self.assertEqual(ctx.evo.texts_to(CLOSER_PHONE), [])
        alerts = owner_texts(ctx)
        self.assertEqual(len(alerts), 1)
        for part in ("19/11/2026", "14:00", "Joana Silva", "Marcos", "confira"):
            self.assertIn(part, alerts[0])
        self.assertEqual(jobs.handoffs(ctx), 0)   # sem reenvio automático


class FalhaPermanenteDeLeituraTest(unittest.TestCase):
    def test_tres_falhas_viram_permanente_com_alerta_proprio_e_sem_forcar_paginacao(self):
        ctx = make_ctx()
        ctx.evo.add(LEAD_PHONE, "mensagem que quebra", wa_id="BAD1")
        original = ctx.db.add_message

        def broken(lead_id, direction, author, body, media_type=None, wa_id=None, created_at=None):
            if wa_id == "BAD1":
                raise RuntimeError("payload ruim")
            return original(lead_id, direction, author, body, media_type, wa_id, created_at)
        ctx.db.add_message = broken
        calls = []
        real_read = ctx.evo.recent_messages
        ctx.evo.recent_messages = lambda limit=50, page=1: calls.append(page) or real_read(limit, page)
        engine.ingest(ctx)
        engine.ingest(ctx)
        self.assertIn("BAD1", ctx.state["failed_wa_ids"])
        engine.ingest(ctx)
        self.assertNotIn("BAD1", ctx.state["failed_wa_ids"])
        specific = [t for t in owner_texts(ctx) if "não pôde ser lida" in t]
        self.assertEqual(len(specific), 1)
        self.assertIn(LEAD_PHONE, specific[0])
        self.assertFalse(any("mais mensagens do que" in t for t in owner_texts(ctx)))
        attempts = {"n": 0}
        original_record = engine._ingest_record

        def spy(*a, **k):
            attempts["n"] += 1
            return original_record(*a, **k)
        with mock.patch.object(engine, "_ingest_record", spy):
            engine.ingest(ctx)
        self.assertEqual(attempts["n"], 0)   # não tenta de novo
        self.assertEqual(len([t for t in owner_texts(ctx) if "não pôde ser lida" in t]), 1)


if __name__ == "__main__":
    unittest.main()
