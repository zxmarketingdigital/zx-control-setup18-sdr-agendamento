"""Rodada 3 do motor: retomada com no máximo 1 neutra, reserva promove o estágio, closers fail-closed,
opt-out relido antes de cada envio de rotina e confirmação que não se perde."""

import unittest
from datetime import timedelta

from motor import engine, jobs
from motor.ai import AIError
from motor.conversa import NEUTRAL_MESSAGE, handle_turn
from motor.util import iso
from tests.motor.support import LEAD_PHONE, NOW, OWNER_PHONE, CLOSER_PHONE, inbound, make_ctx, sp
from tests.motor.test_jobs import silent_lead


class RetomadaNeutraTest(unittest.TestCase):
    def test_ia_fora_manda_uma_neutra_so_e_avisa_o_dono_uma_vez(self):
        ctx = make_ctx()
        lead = silent_lead(ctx, qualification={"_retomar": {"em": iso(NOW - timedelta(minutes=5))}})
        ctx.ai.push(*[AIError("fora do ar")] * 6)
        for _ in range(3):
            jobs.retomar(ctx)
            ctx.clock.advance(minutes=10)
            ctx.db.update_lead(lead["id"], {"last_outbound_at": iso(NOW - timedelta(hours=3))})
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE).count(NEUTRAL_MESSAGE), 1)
        # r18: 1 aviso de IA fora + 1 aviso específico da retomada desistida na 3ª tentativa
        self.assertEqual(len(ctx.evo.texts_to(OWNER_PHONE)), 2)
        self.assertNotIn("_retomar", ctx.db.leads[lead["id"]]["qualification"])   # desistiu (limite de tentativas)

    def test_retomada_que_funciona_depois_da_neutra_limpa_o_pedido(self):
        ctx = make_ctx()
        lead = silent_lead(ctx, qualification={"_retomar": {"em": iso(NOW - timedelta(minutes=5))}})
        ctx.ai.push(AIError("x"))
        jobs.retomar(ctx)
        ctx.ai.push({"reply": "Oi, voltei!", "action": "responder"})
        self.assertEqual(jobs.retomar(ctx), 1)
        self.assertNotIn("_retomar", ctx.db.leads[lead["id"]]["qualification"])


class ReservaPromoveEstagioTest(unittest.TestCase):
    def test_reserva_nova_grava_agendado_e_followup_vencido_nao_envia(self):
        ctx = make_ctx()
        ctx.db.promote_stage_on_book = False      # isola o lado do motor (a RPC real também promove)
        lead = inbound(ctx, "pode ser 14h")
        ctx.ai.push({"reply": "ok", "action": "agendar", "slot": iso(sp(19, 14))})
        self.assertTrue(handle_turn(ctx, lead["id"])["booked"])
        self.assertEqual(ctx.db.leads[lead["id"]]["stage"], "agendado")
        ctx.clock.advance(hours=30)
        ctx.evo.sent.clear()
        ctx.db.update_lead(lead["id"], {"last_inbound_at": iso(NOW), "last_outbound_at": iso(NOW + timedelta(minutes=1))})
        self.assertEqual(jobs.followups(ctx), 0)
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])


class ClosersFailClosedTest(unittest.TestCase):
    def test_falha_ao_ler_closers_mantem_o_ultimo_conjunto_valido(self):
        ctx = make_ctx()
        def boom(active_only=True):
            raise RuntimeError("timeout")
        ctx.db.list_closers = boom
        engine.refresh(ctx)
        self.assertIn(CLOSER_PHONE, ctx.state["ignored"])
        ctx.evo.add(CLOSER_PHONE, "ok, pode fechar")
        self.assertEqual(engine.ingest(ctx), set())
        self.assertEqual(ctx.db.leads, {})

    def test_sem_nenhuma_lista_valida_o_ingest_aborta(self):
        ctx = make_ctx()
        ctx.state.pop("closer_phones")
        def boom(active_only=True):
            raise RuntimeError("timeout")
        ctx.db.list_closers = boom
        engine.refresh(ctx)
        self.assertIsNone(ctx.state["ignored"])
        ctx.evo.add(CLOSER_PHONE, "ok, pode fechar")
        self.assertEqual(engine.ingest(ctx), set())
        self.assertEqual(ctx.db.leads, {})


class OptOutRelidoTest(unittest.TestCase):
    def test_optout_que_chega_durante_a_rodada_barra_o_followup(self):
        ctx = make_ctx()
        lead = silent_lead(ctx)
        real = ctx.db.active_leads

        def snapshot_then_optout(limit=1000):
            rows = real(limit)
            ctx.db.update_lead(lead["id"], {"opt_out": True})   # chegou depois do snapshot
            return rows
        ctx.db.active_leads = snapshot_then_optout
        self.assertEqual(jobs.followups(ctx), 0)
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])

    def test_pare_ja_gravado_mas_ainda_nao_processado_barra_o_followup(self):
        ctx = make_ctx()
        lead = silent_lead(ctx)
        real = ctx.db.active_leads

        def snapshot_then_stop(limit=1000):
            rows = real(limit)
            ctx.db.add_message(lead["id"], "in", "lead", "pode parar de me mandar mensagens")
            return rows
        ctx.db.active_leads = snapshot_then_stop
        self.assertEqual(jobs.followups(ctx), 0)
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])

    def test_estagio_que_mudou_barra_o_lembrete(self):
        ctx = make_ctx()
        lead = silent_lead(ctx)
        ctx.db.slots.append(sp(19, 15))
        ctx.db.book_slot(lead["id"], sp(19, 15))
        ctx.db.appointments[-1]["created_at"] = iso(NOW - timedelta(hours=72))
        ctx.clock.current = sp(18, 16)   # 23h antes: o lembrete de 24h está vencido
        real = ctx.db.get_lead
        calls = {"n": 0}

        def optout_on_second_read(lead_id):
            calls["n"] += 1
            row = real(lead_id)
            if calls["n"] >= 2 and row:
                row["opt_out"] = True
            return row
        ctx.db.get_lead = optout_on_second_read
        self.assertEqual(jobs.reminders(ctx), 0)
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])
        self.assertGreaterEqual(calls["n"], 2)   # a releitura aconteceu antes do envio


class ConfirmacaoNaoSePerdeTest(unittest.TestCase):
    def test_outbox_falhando_apos_reserva_envia_direto_e_nao_oferece_outros_horarios(self):
        ctx = make_ctx()
        lead = inbound(ctx, "pode ser 14h")

        def boom(*a, **k):
            raise RuntimeError("timeout PostgREST")
        ctx.db.enqueue_outbox = boom
        ctx.ai.push({"reply": "ok", "action": "agendar", "slot": iso(sp(19, 14))})
        result = handle_turn(ctx, lead["id"])
        self.assertTrue(result["booked"])
        texts = ctx.evo.texts_to(LEAD_PHONE)
        self.assertEqual(len(texts), 1)
        self.assertIn("ficou para", texts[0])
        self.assertNotIn("ocupado", texts[0])
        self.assertTrue(ctx.evo.texts_to(OWNER_PHONE))   # alerta registrado

    def test_outbox_devolvendo_vazio_tambem_envia_direto(self):
        ctx = make_ctx()
        lead = inbound(ctx, "pode ser 14h")
        ctx.db.enqueue_outbox = lambda *a, **k: None
        ctx.ai.push({"reply": "ok", "action": "agendar", "slot": iso(sp(19, 14))})
        handle_turn(ctx, lead["id"])
        self.assertEqual(len(ctx.evo.texts_to(LEAD_PHONE)), 1)


if __name__ == "__main__":
    unittest.main()
