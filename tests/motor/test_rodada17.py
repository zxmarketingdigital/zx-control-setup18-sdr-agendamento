"""Rodada 17 do motor: takeover na outbox, envio incerto ao lead, rotinas relendo o chat, remarcação de volta,
falha transitória na ingestão e owner_number ilegível. Tudo com Evolution/banco/IA falsos."""

import re
import unittest
from datetime import timedelta

from motor import agenda, engine, jobs, outbox, whatsapp
from motor.http import HttpError, NetworkError
from motor.util import iso
from tests.motor.support import LEAD_PHONE, OWNER_PHONE, inbound, make_ctx, sp
from tests.motor.test_jobs import silent_lead
from tests.motor.test_whatsapp_conexao import new_row, request, setup


def owner_texts(ctx):
    return ctx.evo.texts_to(OWNER_PHONE)


class OutboxTakeoverTest(unittest.TestCase):
    def _row(self, ctx, lead):
        return ctx.db.add_outbox(LEAD_PHONE, "Resposta da IA", lead_id=lead["id"], created_by="motor")

    def test_takeover_ativo_cancela_a_linha_e_avisa_uma_vez(self):
        ctx = make_ctx()
        lead = inbound(ctx)
        row = self._row(ctx, lead)
        ctx.db.update_lead(lead["id"], {"takeover_until": iso(ctx.now() + timedelta(hours=6))})
        self.assertEqual(outbox.process_outbox(ctx), 0)
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])
        self.assertEqual((row["status"], row["last_error"]), ("erro", "cancelado: takeover"))
        self.assertEqual(len([t for t in owner_texts(ctx) if "foi cancelada porque você assumiu" in t]), 1)
        self._row(ctx, lead)   # segunda linha do mesmo lead: cancela, mas NÃO avisa de novo
        outbox.process_outbox(ctx)
        self.assertEqual(len([t for t in owner_texts(ctx) if "foi cancelada porque você assumiu" in t]), 1)

    def test_estagio_final_cancela(self):
        ctx = make_ctx()
        lead = inbound(ctx)
        row = self._row(ctx, lead)
        ctx.db.update_lead(lead["id"], {"stage": "perdido"})
        outbox.process_outbox(ctx)
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])
        self.assertEqual(row["last_error"], "cancelado: estágio final")

    def test_sem_takeover_envia(self):
        ctx = make_ctx()
        lead = inbound(ctx)
        row = self._row(ctx, lead)
        self.assertEqual(outbox.process_outbox(ctx), 1)
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), ["Resposta da IA"])
        self.assertEqual(row["status"], "enviado")

    def test_mensagem_do_dono_depois_da_linha_cancela(self):
        ctx = make_ctx()
        lead = inbound(ctx)
        row = self._row(ctx, lead)
        ctx.clock.advance(minutes=1)
        ctx.db.add_message(lead["id"], "out", "dono", "assumi aqui")
        outbox.process_outbox(ctx)
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])
        self.assertEqual(row["last_error"], "cancelado: takeover")

    def test_mensagem_do_dono_ANTES_da_linha_nao_cancela(self):
        ctx = make_ctx()
        lead = inbound(ctx)
        ctx.db.add_message(lead["id"], "out", "dono", "oi, sou eu")
        ctx.clock.advance(minutes=1)
        self._row(ctx, lead)
        self.assertEqual(outbox.process_outbox(ctx), 1)

    def test_falha_ao_reler_o_lead_nao_envia_e_fica_pendente(self):
        ctx = make_ctx()
        lead = inbound(ctx)
        row = self._row(ctx, lead)
        ctx.db.get_lead = lambda _id: (_ for _ in ()).throw(RuntimeError("503"))
        self.assertEqual(outbox.process_outbox(ctx), 0)
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])
        self.assertEqual(row["status"], "pendente")

    def test_linha_do_painel_e_aviso_interno_nao_passam_pelo_filtro(self):
        ctx = make_ctx()
        lead = inbound(ctx)
        ctx.db.update_lead(lead["id"], {"takeover_until": iso(ctx.now() + timedelta(hours=6))})
        ctx.db.add_outbox(LEAD_PHONE, "Do dono pelo painel", lead_id=lead["id"], created_by="painel")
        ctx.db.add_outbox(OWNER_PHONE, "aviso", created_by="alerta")
        self.assertEqual(outbox.process_outbox(ctx), 2)
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), ["Do dono pelo painel"])


def timeout_only_to_lead(ctx):
    """Timeout só nos envios ao lead; o aviso ao dono sai normalmente."""
    from motor.evolution import EvolutionTimeout
    real = ctx.evo.send_text

    def send(phone, text, delay_ms=0):
        if phone == LEAD_PHONE and getattr(ctx.evo, "lead_timeout", False):
            raise EvolutionTimeout("timed out")
        return real(phone, text, delay_ms)
    ctx.evo.send_text = send


class EnvioIncertoAoLeadTest(unittest.TestCase):
    def test_deliver_incerto_alerta_o_dono_uma_vez_por_turno(self):
        from motor.conversa import deliver
        ctx = make_ctx()
        lead = inbound(ctx, "quero saber o preço")
        ctx.db.update_lead(lead["id"], {"name": "Joana Silva"})
        lead = ctx.db.get_lead(lead["id"])
        timeout_only_to_lead(ctx); ctx.evo.lead_timeout = True
        self.assertFalse(deliver(ctx, lead, "Claro, o valor da avaliação é gratuito."))
        ctx.evo.lead_timeout = False
        alerts = [t for t in owner_texts(ctx) if "não tenho certeza" in t.lower()]
        self.assertEqual(len(alerts), 1)
        self.assertIn("Joana Silva", alerts[0])
        self.assertIn(LEAD_PHONE, alerts[0])
        self.assertIn("Claro, o valor", alerts[0])
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])   # sem reenvio
        timeout_only_to_lead(ctx); ctx.evo.lead_timeout = True
        deliver(ctx, lead, "Outra mensagem do mesmo turno")
        ctx.evo.lead_timeout = False
        self.assertEqual(len([t for t in owner_texts(ctx) if "não tenho certeza" in t.lower()]), 1)

    def test_turno_novo_alerta_de_novo(self):
        from motor.conversa import deliver
        ctx = make_ctx()
        lead = inbound(ctx)
        timeout_only_to_lead(ctx); ctx.evo.lead_timeout = True
        deliver(ctx, ctx.db.get_lead(lead["id"]), "um")
        ctx.clock.advance(minutes=10)
        ctx.db.update_lead(lead["id"], {"last_inbound_at": iso(ctx.now())})
        deliver(ctx, ctx.db.get_lead(lead["id"]), "dois")
        ctx.evo.lead_timeout = False
        self.assertEqual(len([t for t in owner_texts(ctx) if "não tenho certeza" in t.lower()]), 2)

    def test_rotina_incerta_ao_lead_tambem_alerta(self):
        ctx = make_ctx()
        silent_lead(ctx, hours_since_inbound=3)
        timeout_only_to_lead(ctx); ctx.evo.lead_timeout = True
        jobs.followups(ctx)
        ctx.evo.lead_timeout = False
        self.assertEqual(len([t for t in owner_texts(ctx) if "não tenho certeza" in t.lower()]), 1)


class RotinasRelemOChatTest(unittest.TestCase):
    def test_dono_respondeu_so_na_evolution_followup_nao_sai(self):
        ctx = make_ctx()
        lead = silent_lead(ctx, hours_since_inbound=3)
        # o dono respondeu pelo celular: só existe na Evolution, o banco ainda não sabe
        ctx.evo.add(LEAD_PHONE, "oi, sou eu, já te respondo", wa_id="DONO-X", from_me=True)
        self.assertIsNone(ctx.db.get_lead(lead["id"])["takeover_until"])
        self.assertEqual(jobs.followups(ctx), 0)
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])
        self.assertIsNotNone(ctx.db.get_lead(lead["id"])["takeover_until"])

    def test_takeover_no_banco_durante_o_laco_bloqueia(self):
        ctx = make_ctx()
        lead = silent_lead(ctx, hours_since_inbound=3)
        snapshot = ctx.db.get_lead(lead["id"])
        ctx.db.update_lead(lead["id"], {"takeover_until": iso(ctx.now() + timedelta(hours=6))})
        self.assertTrue(jobs._changed(ctx, snapshot))
        self.assertFalse(jobs._changed(ctx, snapshot, internal=True))   # repasse interno não é bloqueado

    def test_falha_na_leitura_do_chat_segue_com_o_banco(self):
        ctx = make_ctx()
        silent_lead(ctx, hours_since_inbound=3)
        ctx.evo.fail_chat_read = True
        self.assertEqual(jobs.followups(ctx), 1)
        self.assertEqual(len(ctx.evo.texts_to(LEAD_PHONE)), 1)

    def test_sem_takeover_followup_sai(self):
        ctx = make_ctx()
        silent_lead(ctx, hours_since_inbound=3)
        self.assertEqual(jobs.followups(ctx), 1)


class RemarcarDeVoltaTest(unittest.TestCase):
    def test_10h_14h_10h_a_terceira_confirmacao_chega(self):
        ctx = make_ctx()
        lead = inbound(ctx, "quero marcar")
        ten, two = sp(20, 10), sp(19, 14)
        db = ctx.db
        real_enqueue, real_by_key = db.enqueue_outbox, db.outbox_by_client_key

        def same_id(key):
            # a RPC real pode devolver o MESMO appointment_id ao remarcar: a chave repetiria o par (id, horário)
            return re.sub(r"^booking:[^:]+:", "booking:A-same:", key) if key and key.startswith("booking:") else key

        db.enqueue_outbox = lambda phone, body, lead_id=None, created_by="motor", client_key=None: real_enqueue(
            phone, body, lead_id=lead_id, created_by=created_by, client_key=same_id(client_key))
        db.outbox_by_client_key = lambda key: real_by_key(same_id(key))
        agent = ctx.state["agent"]
        for start in (ten, two, ten):
            ctx.clock.advance(minutes=5)
            db.update_lead(lead["id"], {"last_inbound_at": iso(ctx.now())})
            ok, _text = agenda.book_for_lead(ctx, db.get_lead(lead["id"]), start, agent, queue_confirmation=True)
            self.assertTrue(ok)
            outbox.process_outbox(ctx)
        confirmations = [t for t in ctx.evo.texts_to(LEAD_PHONE) if "Sua conversa com" in t]
        self.assertEqual(len(confirmations), 3, confirmations)

    def test_apply_decision_entrega_quando_nenhuma_confirmacao_foi_tratada(self):
        from motor import conversa
        ctx = make_ctx()
        lead = inbound(ctx, "quero marcar")
        real = agenda.book_for_lead

        def fake_book(ctx_, lead_, start, agent, notes=None, queue_confirmation=False):
            ctx_.state["confirmation_handled"] = False
            return True, "Confirmado para você."
        agenda.book_for_lead = fake_book
        try:
            d = conversa.Decision(reply="", action="agendar", slot=iso(sp(19, 14)))
            conversa.apply_decision(ctx, ctx.db.get_lead(lead["id"]), ctx.state["agent"], d, [], None, "quero marcar")
        finally:
            agenda.book_for_lead = real
        self.assertIn("Confirmado para você.", ctx.evo.texts_to(LEAD_PHONE))


class IngestaoTransitoriaTest(unittest.TestCase):
    def _flaky(self, ctx, exc):
        real = ctx.db.add_message

        def add(lead_id, direction, author, body, *a, **k):
            if direction == "in":
                raise exc
            return real(lead_id, direction, author, body, *a, **k)
        ctx.db.add_message = add
        return real

    def test_erro_transitorio_nao_vira_falha_permanente(self):
        for exc in (NetworkError("rede"), HttpError(503, "x"), HttpError(429, "x"), HttpError(408, "x")):
            ctx = make_ctx()
            ctx.evo.add(LEAD_PHONE, "oi", wa_id="W1")
            real = self._flaky(ctx, exc)
            for _ in range(engine.MAX_WA_ATTEMPTS + 3):
                engine.ingest(ctx)
            self.assertNotIn("W1", ctx.state.get("failed_wa_permanent", set()), exc)
            self.assertIn("W1", ctx.state["failed_wa_ids"])
            ctx.db.add_message = real   # o banco volta: a mensagem entra
            engine.ingest(ctx)
            self.assertEqual(len([m for m in ctx.db.messages if m["wa_id"] == "W1"]), 1)

    def test_erro_de_dado_conta_e_vira_permanente_no_limite(self):
        for exc in (HttpError(400, "x"), ValueError("parse")):
            ctx = make_ctx()
            ctx.evo.add(LEAD_PHONE, "oi", wa_id="W2")
            self._flaky(ctx, exc)
            for _ in range(engine.MAX_WA_ATTEMPTS):
                engine.ingest(ctx)
            self.assertIn("W2", ctx.state["failed_wa_permanent"], exc)

    def test_transitorio_fora_da_janela_de_catchup_volta_a_contar(self):
        ctx = make_ctx()
        ctx.evo.add(LEAD_PHONE, "oi", wa_id="W3", age_seconds=ctx.cfg.catchup_minutes * 60 + 600)
        record = ctx.evo.records[0]
        self.assertFalse(engine._within_catchup(ctx, record))


class OwnerNumberIlegivelTest(unittest.TestCase):
    def test_open_com_owner_ilegivel_nao_apaga_o_numero(self):
        ctx = setup(row=new_row(state="open", owner_number="5511999990001"), evo_state="open")
        ctx.evo.owner = ""
        whatsapp.tick(ctx, ctx.now())
        self.assertEqual(ctx.store["row"]["owner_number"], "5511999990001")
        self.assertTrue(all("owner_number" not in w for w in ctx.store["writes"]), ctx.store["writes"])

    def test_open_com_owner_legivel_atualiza(self):
        ctx = setup(row=new_row(state="open", owner_number="5511999990001"), evo_state="open")
        ctx.evo.owner = "5511988887777@s.whatsapp.net"
        whatsapp.tick(ctx, ctx.now())
        self.assertEqual(ctx.store["row"]["owner_number"], "5511988887777")

    def test_close_continua_limpando(self):
        ctx = setup(row=new_row(state="open", owner_number="5511999990001"), evo_state="close")
        whatsapp.tick(ctx, ctx.now())
        self.assertIsNone(ctx.store["row"]["owner_number"])


if __name__ == "__main__":
    unittest.main()
