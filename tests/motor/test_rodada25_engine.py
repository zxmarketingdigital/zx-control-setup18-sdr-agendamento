"""Rodada 25 do motor (jobs/engine): lembrete não vai a lead em estágio terminal e eco do dono sem horário legível
ativa o takeover. Tudo com falsos."""

import unittest
from datetime import timedelta

from motor import engine, jobs
from motor.config import TAKEOVER_HOURS
from motor.conversa import handle_turn
from motor.util import iso, parse_ts
from tests.motor.support import LEAD_PHONE, NOW, make_ctx, default_agent, sp
from tests.motor.test_jobs import book


class LembreteEstagioTerminalTest(unittest.TestCase):
    def _ctx(self, stage):
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        book(ctx, lead, NOW + timedelta(hours=23))
        ctx.db.update_lead(lead["id"], {"stage": stage})
        return ctx

    def test_terminais_nao_recebem_lembrete_nem_marcam_a_flag(self):
        for stage in ("desqualificado", "venda", "perdido", "compareceu"):
            ctx = self._ctx(stage)
            self.assertEqual(jobs.reminders(ctx), 0, stage)
            self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [], stage)
            self.assertIsNone(ctx.db.appointments[0]["reminder_24h_at"], stage)
            ctx.clock.advance(hours=22)   # entra na janela de 2h
            self.assertEqual(jobs.reminders(ctx), 0, stage)
            self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [], stage)
            self.assertIsNone(ctx.db.appointments[0]["reminder_2h_at"], stage)

    def test_lead_ativo_continua_recebendo(self):
        ctx = self._ctx("agendado")
        self.assertEqual(jobs.reminders(ctx), 1)
        self.assertEqual(len(ctx.evo.texts_to(LEAD_PHONE)), 1)


class EcoDoDonoSemHorarioTest(unittest.TestCase):
    def _ctx(self):
        return make_ctx(default_agent(test_only=False))

    def test_eco_fromme_sem_timestamp_desconhecido_nao_ativa_takeover_sem_gravar(self):   # r28 inverte a r25
        ctx = self._ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        rec = ctx.evo.add(LEAD_PHONE, "Aqui é o dono, já te respondo", wa_id="DONO1", from_me=True)
        rec.pop("messageTimestamp")
        engine.ingest(ctx)
        got = ctx.db.get_lead(lead["id"])
        self.assertFalse(got.get("takeover_until"))   # r28: sem casar com envio do agente, não pausa a IA
        self.assertEqual(ctx.db.messages, [])

    def test_lead_com_horario_valido_depois_segue_ativo_apos_fromme_sem_horario(self):   # r28 inverte a r25
        ctx = self._ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        rec = ctx.evo.add(LEAD_PHONE, "Aqui é o dono", wa_id="DONO1", from_me=True)
        rec.pop("messageTimestamp")
        engine.ingest(ctx)
        ctx.evo.add(LEAD_PHONE, "pode me ligar?", wa_id="L1")
        engine.ingest(ctx)
        ctx.ai.push({"reply": "Claro!", "action": "responder"})
        result = handle_turn(ctx, lead["id"])
        self.assertEqual(result.get("status"), "ok")   # r28: o agente segue ativo (o dono foi avisado)

    def test_eco_sem_horario_de_numero_sem_lead_nao_cria_nada(self):
        ctx = self._ctx()
        rec = ctx.evo.add(LEAD_PHONE, "oi", wa_id="DONO2", from_me=True)
        rec.pop("messageTimestamp")
        engine.ingest(ctx)
        self.assertIsNone(ctx.db.get_lead_by_phone(LEAD_PHONE))


if __name__ == "__main__":
    unittest.main()
