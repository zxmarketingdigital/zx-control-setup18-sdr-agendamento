"""Rodada 8 do motor: eco de envio ambíguo não vira takeover (e takeover automático avisa o dono), a remarcação não
zera flags no Python, alertas só marcam depois de enfileirar, cancelamento do Google Agenda falho avisa, confirmar
é condicional e o recover consolida um alerta."""

import unittest
from datetime import timedelta

from motor import agenda, engine, outbox
from motor.conversa import Decision, apply_decision, CONFIRM_NOT_APPLIED_MESSAGE
from motor.evolution import EvolutionError, EvolutionTimeout
from motor.util import iso
from tests.motor.support import CLOSER_PHONE, LEAD_PHONE, NOW, OWNER_PHONE, default_agent, inbound, make_ctx, sp
from tests.motor.test_rodada4 import book


def alerts(ctx):
    return [r for r in ctx.db.outbox if r["created_by"] == "alerta"]


class EcoAmbiguoTest(unittest.TestCase):
    def _lead(self, ctx):
        return ctx.db.get_lead_by_phone(LEAD_PHONE) or ctx.db.create_lead(LEAD_PHONE, name="Joana")

    def test_timeout_incerto_e_eco_nao_ativa_takeover(self):
        ctx = make_ctx()
        lead = self._lead(ctx)
        ctx.evo.timeout_send = True
        self.assertIsNone(ctx.messenger.send(LEAD_PHONE, "Posso  te ajudar?\n", lead_id=lead["id"]))
        ctx.evo.timeout_send = False
        ctx.evo.add(LEAD_PHONE, "Posso te ajudar?", wa_id="ECO1", from_me=True)
        engine.ingest(ctx)
        msg = [m for m in ctx.db.messages if m["wa_id"] == "ECO1"][0]
        self.assertEqual(msg["author"], "agente")
        self.assertIsNone(ctx.db.leads[lead["id"]]["takeover_until"])
        self.assertEqual(alerts(ctx), [])

    def test_sem_key_id_e_eco_nao_ativa_takeover(self):
        ctx = make_ctx()
        lead = self._lead(ctx)
        ctx.evo.send_text = lambda *a, **k: None
        ctx.messenger.send(LEAD_PHONE, "oi tudo bem", lead_id=lead["id"])
        ctx.evo.add(LEAD_PHONE, "oi tudo bem", wa_id="ECO2", from_me=True)
        engine.ingest(ctx)
        self.assertIsNone(ctx.db.leads[lead["id"]]["takeover_until"])

    def test_eco_de_linha_da_outbox_incerta_preenche_wa_key_id(self):
        ctx = make_ctx()
        lead = self._lead(ctx)
        row = ctx.db.add_outbox(LEAD_PHONE, "Segue o resumo", lead_id=lead["id"], created_by="painel",
                                status="erro")
        row["last_error"] = "timeout_incerto"
        ctx.evo.add(LEAD_PHONE, "Segue  o resumo", wa_id="ECO3", from_me=True)
        engine.ingest(ctx)   # processo novo: sem memória, só a outbox
        self.assertEqual(row["wa_key_id"], "ECO3")
        self.assertIsNone(ctx.db.leads[lead["id"]]["takeover_until"])
        self.assertEqual([m["author"] for m in ctx.db.messages if m["wa_id"] == "ECO3"], ["agente"])

    def test_texto_diferente_ou_fora_da_janela_continua_sendo_o_dono(self):
        ctx = make_ctx()
        lead = self._lead(ctx)
        ctx.evo.timeout_send = True
        ctx.messenger.send(LEAD_PHONE, "oi", lead_id=lead["id"])
        ctx.evo.timeout_send = False
        ctx.evo.add(LEAD_PHONE, "outro texto", wa_id="DONO1", from_me=True)
        engine.ingest(ctx)
        self.assertIsNotNone(ctx.db.leads[lead["id"]]["takeover_until"])
        ctx2 = make_ctx()
        lead2 = self._lead(ctx2)
        ctx2.evo.timeout_send = True
        ctx2.messenger.send(LEAD_PHONE, "oi", lead_id=lead2["id"])
        ctx2.evo.timeout_send = False
        ctx2.clock.advance(minutes=16)
        ctx2.evo.add(LEAD_PHONE, "oi", wa_id="DONO2", from_me=True, age_seconds=5)
        engine.ingest(ctx2)
        self.assertIsNotNone(ctx2.db.leads[lead2["id"]]["takeover_until"])

    def test_takeover_automatico_avisa_o_dono_uma_vez_por_ativacao(self):
        ctx = make_ctx()
        lead = self._lead(ctx)
        ctx.evo.add(LEAD_PHONE, "Aqui é a equipe", wa_id="H1", from_me=True)
        engine.ingest(ctx)
        self.assertEqual(len(alerts(ctx)), 1)
        self.assertIn("pausada", alerts(ctx)[0]["body"])
        self.assertEqual(alerts(ctx)[0]["phone"], OWNER_PHONE)
        ctx.evo.add(LEAD_PHONE, "e mais uma", wa_id="H2", from_me=True)
        engine.ingest(ctx)
        self.assertEqual(len(alerts(ctx)), 1)   # prorrogação, não nova ativação


class RemarcacaoSemResetNoPythonTest(unittest.TestCase):
    def test_motor_nao_escreve_flags_na_remarcacao(self):
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        book(ctx, lead, sp(19, 14))
        ctx.db.slots.append(sp(21, 16))
        writes = []
        real = ctx.db.update_appointment
        ctx.db.update_appointment = lambda i, f: (writes.append(f), real(i, f))[1]
        ok, _ = agenda.book_for_lead(ctx, lead, sp(21, 16), default_agent())
        self.assertTrue(ok)
        self.assertFalse([f for f in writes if "reminder_24h_at" in f or "handoff_sent_at" in f])
        self.assertFalse(hasattr(agenda, "_reset_appointment_flags"))


class SemHorarioAlertaTest(unittest.TestCase):
    def test_so_marca_depois_de_enfileirar(self):
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        real = ctx.db.enqueue_outbox
        ctx.db.enqueue_outbox = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("5xx"))
        agenda.alert_no_slots(ctx, lead)   # não levanta
        self.assertNotIn(lead["id"], ctx.state.get("no_slots_alerted", set()))
        ctx.db.enqueue_outbox = real
        agenda.alert_no_slots(ctx, lead)
        self.assertIn(lead["id"], ctx.state["no_slots_alerted"])
        self.assertEqual(len(alerts(ctx)), 1)
        agenda.alert_no_slots(ctx, lead)
        self.assertEqual(len(alerts(ctx)), 1)


class GcalCancelTest(unittest.TestCase):
    class Gcal:
        def __init__(self, result):
            self.result = result

        def cancel(self, event_id):
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    def _appt(self, ctx):
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        appt = book(ctx, lead, sp(19, 14))
        appt["google_event_id"] = "EV1"
        return appt

    def test_retorno_false_avisa_dono_e_closer(self):
        ctx = make_ctx()
        ctx.gcal = self.Gcal(False)
        agenda._gcal_cancel(ctx, self._appt(ctx))
        to_owner = [r for r in alerts(ctx) if r["phone"] == OWNER_PHONE]
        to_closer = [r for r in alerts(ctx) if r["phone"] == CLOSER_PHONE]
        self.assertEqual((len(to_owner), len(to_closer)), (1, 1))
        self.assertIn("Marcos", to_owner[0]["body"])
        self.assertIn("19", to_owner[0]["body"])

    def test_excecao_tambem_avisa_e_sucesso_nao_avisa(self):
        ctx = make_ctx()
        ctx.gcal = self.Gcal(RuntimeError("boom"))
        agenda._gcal_cancel(ctx, self._appt(ctx))
        self.assertTrue(alerts(ctx))
        ctx2 = make_ctx()
        ctx2.gcal = self.Gcal(True)
        agenda._gcal_cancel(ctx2, self._appt(ctx2))
        self.assertEqual(alerts(ctx2), [])


class ConfirmarCondicionalTest(unittest.TestCase):
    def _run(self, ctx, lead, appt):
        ctx.db.update_lead(lead["id"], {"stage": "agendado"})
        decision = Decision(reply="Confirmado, até lá!", action="confirmar")
        return apply_decision(ctx, ctx.db.get_lead(lead["id"]), ctx.state["agent"], decision, [], appt, "confirmo")

    def test_cancelado_no_meio_nao_vira_confirmado_e_resposta_neutra(self):
        ctx = make_ctx()
        lead = inbound(ctx, "confirmo")
        appt = book(ctx, lead, sp(19, 14))
        snapshot = dict(appt)
        appt["status"] = "cancelado"   # o dono cancelou pelo painel enquanto a IA respondia
        result = self._run(ctx, lead, snapshot)
        self.assertEqual(appt["status"], "cancelado")
        self.assertEqual(result["reply"], CONFIRM_NOT_APPLIED_MESSAGE)
        self.assertNotEqual(ctx.db.leads[lead["id"]]["stage"], "confirmado")

    def test_agendado_confirma(self):
        ctx = make_ctx()
        lead = inbound(ctx, "confirmo")
        appt = book(ctx, lead, sp(19, 14))
        result = self._run(ctx, lead, dict(appt))
        self.assertEqual(appt["status"], "confirmado")
        self.assertEqual(ctx.db.leads[lead["id"]]["stage"], "confirmado")
        self.assertNotEqual(result["reply"], CONFIRM_NOT_APPLIED_MESSAGE)


class RecoverAlertaTest(unittest.TestCase):
    def test_um_alerta_consolidado_sem_reenvio(self):
        ctx = make_ctx()
        ctx.db.add_outbox(LEAD_PHONE, "a", status="enviando", attempts=1)
        ctx.db.add_outbox("+5511999990002", "b", status="enviando", attempts=1)
        self.assertEqual(outbox.recover_stuck_outbox(ctx), 2)
        mine = alerts(ctx)
        self.assertEqual(len(mine), 1)
        self.assertIn(LEAD_PHONE, mine[0]["body"])
        self.assertIn("+5511999990002", mine[0]["body"])
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])

    def test_sem_travadas_nao_alerta(self):
        ctx = make_ctx()
        self.assertEqual(outbox.recover_stuck_outbox(ctx), 0)
        self.assertEqual(alerts(ctx), [])


if __name__ == "__main__":
    unittest.main()
