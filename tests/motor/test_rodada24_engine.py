"""Rodada 24 do motor (engine/jobs/evolution): last_inbound_at monotônico, modo teste não consome o repasse real
e timestamp ilegível nunca vira 'agora'. Tudo com falsos."""

import unittest
from datetime import timedelta

from motor import engine, jobs
from motor.evolution import record_timestamp_or_none
from motor.util import iso, parse_ts
from tests.motor.support import CLOSER_PHONE, LEAD_PHONE, NOW, OWNER_PHONE, TEST_PHONE, make_ctx, default_agent, sp
from tests.motor.test_jobs import book


class MonotonicoTest(unittest.TestCase):
    def test_mensagem_antiga_relida_nao_recua_last_inbound_nem_zera_cadencia(self):
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        b = NOW - timedelta(minutes=2)
        a = NOW - timedelta(minutes=23)   # A (mais antiga) falhou antes; B foi gravada
        ctx.db.update_lead(lead["id"], {"last_inbound_at": iso(b), "followup_step": 1})
        engine._apply_inbound_effects(ctx, ctx.db.get_lead(lead["id"]), a, "oi", always=True)
        got = ctx.db.get_lead(lead["id"])
        self.assertEqual(parse_ts(got["last_inbound_at"]), b)
        self.assertEqual(got["followup_step"], 1)

    def test_mensagem_mais_nova_avanca_e_zera(self):
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        ctx.db.update_lead(lead["id"], {"last_inbound_at": iso(NOW - timedelta(hours=1)), "followup_step": 2})
        engine._apply_inbound_effects(ctx, ctx.db.get_lead(lead["id"]), NOW, "oi", always=True)
        got = ctx.db.get_lead(lead["id"])
        self.assertEqual(parse_ts(got["last_inbound_at"]), NOW)
        self.assertEqual(got["followup_step"], 0)

    def test_cenario_poll_a_falha_b_grava_a_relida(self):
        ctx = make_ctx(default_agent(test_only=False))
        ctx.evo.add(LEAD_PHONE, "primeira", wa_id="A", age_seconds=100)
        ctx.evo.add(LEAD_PHONE, "segunda", wa_id="B", age_seconds=10)
        real = ctx.db.add_message
        state = {"fail": True}

        def flaky(lead_id, direction, author, body, media_type=None, wa_id=None, created_at=None):
            if wa_id == "A" and state["fail"]:
                raise RuntimeError("banco 5xx")
            return real(lead_id, direction, author, body, media_type, wa_id, created_at)
        ctx.db.add_message = flaky
        engine.ingest(ctx)
        lead = ctx.db.get_lead_by_phone(LEAD_PHONE)
        b_instant = parse_ts(lead["last_inbound_at"])
        ctx.db.update_lead(lead["id"], {"followup_step": 1})
        state["fail"] = False
        engine.ingest(ctx)
        got = ctx.db.get_lead_by_phone(LEAD_PHONE)
        self.assertEqual(parse_ts(got["last_inbound_at"]), b_instant)
        self.assertEqual(got["followup_step"], 1)


class HandoffModoTesteTest(unittest.TestCase):
    def _setup(self):
        ctx = make_ctx(default_agent(test_only=True, test_phones=[TEST_PHONE]))
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        appt = book(ctx, lead, sp(19, 14))
        return ctx, appt

    def test_desvio_teste_ao_dono_nao_marca_a_flag_e_closer_recebe_ao_vivo(self):
        ctx, appt = self._setup()
        jobs.handoffs(ctx)
        self.assertIsNone(ctx.db.appointments[0]["handoff_sent_at"])
        self.assertEqual(len([t for t in ctx.evo.texts_to(OWNER_PHONE) if "[TESTE]" in t]), 1)
        self.assertEqual(ctx.evo.texts_to(CLOSER_PHONE), [])
        jobs.handoffs(ctx)   # a prévia não se repete a cada ciclo
        self.assertEqual(len([t for t in ctx.evo.texts_to(OWNER_PHONE) if "[TESTE]" in t]), 1)
        ctx.state["agent"] = {**ctx.state["agent"], "test_only": False}
        jobs.handoffs(ctx)
        self.assertEqual(len(ctx.evo.texts_to(CLOSER_PHONE)), 1)
        self.assertIsNotNone(ctx.db.appointments[0]["handoff_sent_at"])
        jobs.handoffs(ctx)
        self.assertEqual(len(ctx.evo.texts_to(CLOSER_PHONE)), 1)   # 1 vez só

    def test_closer_em_test_phones_recebe_de_verdade_e_marca(self):
        ctx, appt = self._setup()
        ctx.db.closers[0]["whatsapp"] = TEST_PHONE
        jobs.handoffs(ctx)
        self.assertEqual(len(ctx.evo.texts_to(TEST_PHONE)), 1)
        self.assertIsNotNone(ctx.db.appointments[0]["handoff_sent_at"])


class TimestampIlegivelTest(unittest.TestCase):
    def test_formatos_aceitos(self):
        ts = int(NOW.timestamp())
        self.assertEqual(record_timestamp_or_none({"messageTimestamp": ts}), ts)
        self.assertEqual(record_timestamp_or_none({"messageTimestamp": str(ts)}), ts)
        self.assertEqual(record_timestamp_or_none({"messageTimestamp": NOW.isoformat()}), ts)
        self.assertEqual(record_timestamp_or_none({"messageTimestamp": {"low": ts, "high": 0}}), ts)
        self.assertEqual(record_timestamp_or_none({"messageTimestamp": {"low": ts & 0xFFFFFFFF, "high": ts >> 32}}), ts)

    def test_ilegivel_e_none(self):
        for bad in (None, "", "ontem", {}, {"x": 1}, [], 0, -5, True):
            self.assertIsNone(record_timestamp_or_none({"messageTimestamp": bad}), bad)
        self.assertIsNone(record_timestamp_or_none({}))

    def _ctx(self):
        return make_ctx(default_agent(test_only=False))

    def _chega(self, ctx, **extra):
        rec = ctx.evo.add(LEAD_PHONE, "oi, tudo bem?", wa_id="W1")
        rec.pop("messageTimestamp")
        rec.update(extra)
        return rec

    def test_campo_ausente_grava_mas_nao_dispara_turno_nem_inbound(self):
        ctx = self._ctx()
        self._chega(ctx)
        fresh = engine.ingest(ctx)
        self.assertEqual(fresh, set())
        lead = ctx.db.get_lead_by_phone(LEAD_PHONE)
        self.assertIsNone(lead["last_inbound_at"])
        self.assertEqual(len(ctx.db.messages), 1)
        self.assertEqual(len([t for t in ctx.evo.texts_to(OWNER_PHONE) if "sem horário legível" in t]), 1)

    def test_formatos_legiveis_seguem_normal_com_objeto_low_high(self):
        ctx = self._ctx()
        rec = self._chega(ctx, messageTimestamp={"low": int(NOW.timestamp()) - 10, "high": 0})
        self.assertEqual(engine.ingest(ctx), {ctx.db.get_lead_by_phone(LEAD_PHONE)["id"]})

    def test_iso_legivel_segue_normal(self):
        ctx = self._ctx()
        self._chega(ctx, messageTimestamp=(NOW - timedelta(seconds=10)).isoformat())
        self.assertEqual(len(engine.ingest(ctx)), 1)

    def test_ilegivel_alerta_dono_uma_vez_por_dia(self):
        ctx = self._ctx()
        self._chega(ctx)
        rec2 = ctx.evo.add(LEAD_PHONE, "outra", wa_id="W2")
        rec2["messageTimestamp"] = "lixo"
        engine.ingest(ctx)
        self.assertEqual(len([t for t in ctx.evo.texts_to(OWNER_PHONE) if "sem horário legível" in t]), 1)
        ctx.clock.advance(days=1)
        engine.ingest(ctx)   # W1/W2 já gravadas: sem novo alerta no mesmo dia; outro dia só com mensagem nova
        self.assertEqual(len([t for t in ctx.evo.texts_to(OWNER_PHONE) if "sem horário legível" in t]), 1)

    def test_opt_out_sem_horario_ainda_e_honrado(self):
        ctx = self._ctx()
        rec = ctx.evo.add(LEAD_PHONE, "para de me mandar mensagem", wa_id="W3")
        rec.pop("messageTimestamp")
        engine.ingest(ctx)
        self.assertTrue(ctx.db.get_lead_by_phone(LEAD_PHONE)["opt_out"])


if __name__ == "__main__":
    unittest.main()
