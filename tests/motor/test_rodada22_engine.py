"""Rodada 22 do motor (engine/outbox): eco com outbox ilegível, janela do eco ancorada na mensagem, modo teste na
recuperação, efeitos da ingestão reaplicados e resposta direta enviada sem gravação. Tudo com falsos."""

import unittest
from datetime import timedelta
from unittest import mock

from motor import engine
from motor.http import HttpError
from motor.outbox import SENT_UNRECORDED_ERROR
from motor.util import iso
from tests.motor.support import LEAD_PHONE, TEST_PHONE, default_agent, inbound, make_ctx


def _takeover(ctx):
    return ctx.db.get_lead_by_phone(LEAD_PHONE).get("takeover_until")


class EcoOutboxIlegivelTest(unittest.TestCase):
    def test_outbox_503_nao_ativa_takeover_e_poll_seguinte_reconhece_o_eco(self):
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        ctx.db.add_outbox(LEAD_PHONE, "Oi Joana!", lead_id=lead["id"], status="enviado")
        ctx.db.outbox[0]["wa_key_id"] = "ECO1"
        ctx.evo.add(LEAD_PHONE, "Oi Joana!", wa_id="ECO1", from_me=True)
        real = ctx.db.outbox_by_wa_key

        def boom(wa_key_id):
            raise HttpError(503, "indisponível")
        ctx.db.outbox_by_wa_key = boom
        engine.ingest(ctx)
        self.assertIsNone(_takeover(ctx))
        self.assertEqual([m for m in ctx.db.messages if m["author"] == "dono"], [])
        self.assertEqual(ctx.db.outbox_for_lead_since(lead["id"], ctx.now() - timedelta(days=1))[0]["status"], "enviado")
        self.assertIn("ECO1", ctx.state["failed_wa_ids"])
        self.assertEqual(ctx.state.get("failed_wa_attempts", {}).get("ECO1", 0), 0)   # transitório: não conta
        ctx.db.outbox_by_wa_key = real
        engine.ingest(ctx)
        self.assertIsNone(_takeover(ctx))
        self.assertEqual([m["author"] for m in ctx.db.messages], ["agente"])   # reconhecido como eco do agente


class JanelaDoEcoTest(unittest.TestCase):
    def test_envio_incerto_as_10h_motor_volta_as_10h20_sem_takeover(self):
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        row = ctx.db.add_outbox(LEAD_PHONE, "Combinado, te espero!", lead_id=lead["id"], status="erro")
        row["last_error"] = "timeout_incerto"
        ctx.clock.advance(minutes=20)   # o motor volta 20 min depois; o eco carrega o instante do envio
        ctx.evo.add(LEAD_PHONE, "Combinado, te espero!", wa_id="ECOX", from_me=True, age_seconds=20 * 60)
        engine.ingest(ctx)
        self.assertIsNone(_takeover(ctx))
        self.assertEqual([m["author"] for m in ctx.db.messages], ["agente"])
        self.assertEqual(ctx.db.outbox[0]["wa_key_id"], "ECOX")

    def test_memoria_de_envio_incerto_tambem_ancora_no_instante_do_eco(self):
        ctx = make_ctx()
        ctx.db.create_lead(LEAD_PHONE, name="Joana")
        ctx.messenger._remember_uncertain(LEAD_PHONE, "Oi!")
        ctx.clock.advance(minutes=30)
        ctx.evo.add(LEAD_PHONE, "Oi!", wa_id="ECOM", from_me=True, age_seconds=30 * 60)
        engine.ingest(ctx)
        self.assertIsNone(_takeover(ctx))

    def test_dono_de_verdade_continua_ativando_takeover(self):
        ctx = make_ctx()
        ctx.db.create_lead(LEAD_PHONE, name="Joana")
        ctx.evo.add(LEAD_PHONE, "Aqui é o dono", wa_id="DONO1", from_me=True)
        engine.ingest(ctx)
        self.assertIsNotNone(_takeover(ctx))


class ModoTesteRecuperacaoTest(unittest.TestCase):
    def _ctx(self):
        ctx = make_ctx(default_agent(test_only=True, test_phones=[TEST_PHONE]))
        lead = inbound(ctx, "quero saber mais")   # lead real, fora de test_phones
        return ctx, lead

    def test_recover_unanswered_filtra_lead_real(self):
        ctx, lead = self._ctx()
        self.assertEqual(engine.recover_unanswered(ctx), set())
        test_lead = inbound(ctx, "oi", phone=TEST_PHONE)
        self.assertEqual(engine.recover_unanswered(ctx), {test_lead["id"]})

    def test_startup_e_periodico_nao_enfileiram_lead_real(self):
        ctx, lead = self._ctx()
        engine.startup(ctx)
        self.assertEqual(ctx.state["pending"], set())
        ctx.state["last_recover"] = None
        self.assertEqual(engine._periodic_recover(ctx), set())

    def test_cycle_nao_chama_handle_turn_para_lead_real_em_modo_teste(self):
        ctx, lead = self._ctx()
        ctx.state["pending"] = {lead["id"]}   # mesmo que algo o enfileire
        with mock.patch.object(engine, "handle_turn") as turn:
            engine.cycle(ctx)
        turn.assert_not_called()
        self.assertEqual(ctx.ai.calls, [])
        self.assertEqual(ctx.db.get_lead(lead["id"]).get("stage"), lead.get("stage"))

    def test_ao_vivo_segue_normal(self):
        ctx = make_ctx()
        lead = inbound(ctx, "quero saber mais")
        self.assertEqual(engine.recover_unanswered(ctx), {lead["id"]})


class EfeitoDaIngestaoTest(unittest.TestCase):
    def test_patch_503_na_primeira_vez_opt_out_gravado_no_poll_seguinte(self):
        ctx = make_ctx()
        ctx.evo.add(LEAD_PHONE, "não quero mais receber", wa_id="W1")
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        real = ctx.db.update_lead
        state = {"n": 0}

        def flaky(lead_id, fields):
            state["n"] += 1
            if state["n"] == 1:
                raise HttpError(503, "indisponível")
            return real(lead_id, fields)
        ctx.db.update_lead = flaky
        engine.ingest(ctx)
        self.assertFalse(ctx.db.get_lead(lead["id"]).get("opt_out"))
        self.assertIsNone(ctx.db.get_lead(lead["id"]).get("last_inbound_at"))
        engine.ingest(ctx)   # a mensagem já está no banco: o efeito é reaplicado
        got = ctx.db.get_lead(lead["id"])
        self.assertTrue(got["opt_out"])
        self.assertIsNotNone(got["last_inbound_at"])
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])   # nunca responde ao lead por causa disso
        self.assertEqual(len([m for m in ctx.db.messages if m["wa_id"] == "W1"]), 1)


class RespostaDiretaSemGravacaoTest(unittest.TestCase):
    def test_banco_cai_apos_key_id_recover_nao_chama_a_ia_de_novo(self):
        ctx = make_ctx()
        lead = inbound(ctx, "quero saber mais")
        real_add, real_upd = ctx.db.add_message, ctx.db.update_lead
        real_enq = ctx.db.enqueue_outbox
        down = {"on": True}

        def add(*a, **k):
            if down["on"]:
                raise HttpError(503, "fora")
            return real_add(*a, **k)

        def upd(*a, **k):
            if down["on"]:
                raise HttpError(503, "fora")
            return real_upd(*a, **k)
        ctx.db.add_message, ctx.db.update_lead = add, upd
        key = ctx.messenger.send(LEAD_PHONE, "Olá, posso ajudar!", lead_id=lead["id"])
        self.assertTrue(key)
        rows = ctx.db.outbox
        self.assertEqual(len(rows), 1)   # marcador durável com o key.id
        self.assertEqual((rows[0]["status"], rows[0]["last_error"], rows[0]["wa_key_id"]),
                         ("erro", SENT_UNRECORDED_ERROR, key))
        down["on"] = False
        ctx.db.add_message, ctx.db.update_lead = real_add, real_upd
        self.assertEqual(engine.recover_unanswered(ctx), set())
        engine._flush_unrecorded(ctx)
        alerts = [t for p, t in ctx.evo.sent if p != LEAD_PHONE]
        self.assertEqual(len(alerts), 1)
        engine._flush_unrecorded(ctx)
        self.assertEqual(len([1 for p, _ in ctx.evo.sent if p != LEAD_PHONE]), 1)   # aviso 1x

    def test_ate_o_marcador_falha_memoria_do_processo_segura_a_recuperacao(self):
        ctx = make_ctx()
        lead = inbound(ctx, "quero saber mais")
        real_add, real_upd, real_enq = ctx.db.add_message, ctx.db.update_lead, ctx.db.enqueue_outbox

        def boom(*a, **k):
            raise HttpError(503, "fora")
        ctx.db.add_message = ctx.db.update_lead = ctx.db.enqueue_outbox = boom
        self.assertTrue(ctx.messenger.send(LEAD_PHONE, "Olá!", lead_id=lead["id"]))
        ctx.db.add_message, ctx.db.update_lead, ctx.db.enqueue_outbox = real_add, real_upd, real_enq
        self.assertEqual(ctx.db.outbox, [])
        self.assertEqual(engine.recover_unanswered(ctx), set())   # memória: não reprocessa enquanto o processo vive
        engine._flush_unrecorded(ctx)   # o banco voltou: o marcador é gravado
        self.assertEqual(ctx.db.outbox[0]["last_error"], SENT_UNRECORDED_ERROR)


if __name__ == "__main__":
    unittest.main()
