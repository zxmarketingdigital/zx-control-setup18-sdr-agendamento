"""Rodada 28 (jobs): retomada que falha depois de enviar não reenvia no ciclo seguinte; falha permanente avisa
uma vez por rotina E destinatário por dia. Tudo com falsos."""

import unittest
from datetime import timedelta
from unittest import mock

from motor import jobs
from motor.util import iso
from tests.motor.support import LEAD_PHONE, NOW, default_agent, make_ctx
from tests.motor.test_jobs import book, silent_lead


class RetomadaFalhaDepoisDoEnvioTest(unittest.TestCase):
    def _falha_apos_enviar(self, ctx, lead, *, via_outbox=False):
        def fake(c, lead_id, **kwargs):
            if via_outbox:
                c.db.enqueue_outbox(LEAD_PHONE, "Oi, voltei!", lead_id=lead_id)
                c.db.outbox[-1]["created_at"] = iso(c.now())
            else:
                c.db.add_message(lead_id, "out", "agente", "Oi, voltei!", None, "OUT-R", c.now())
            raise RuntimeError("5xx ao gravar a qualification")
        return fake

    def test_envio_feito_e_gravacao_falha_nao_reenvia_no_proximo_ciclo(self):
        for via_outbox in (False, True):
            with self.subTest(via_outbox=via_outbox):
                ctx = make_ctx(default_agent(test_only=False))
                lead = silent_lead(ctx, qualification={"_retomar": {"em": iso(NOW - timedelta(minutes=5))}})
                calls = []
                fake = self._falha_apos_enviar(ctx, lead, via_outbox=via_outbox)
                with mock.patch.object(jobs, "handle_turn",
                                       side_effect=lambda *a, **k: (calls.append(1), fake(*a, **k))[1]):
                    jobs.retomar(ctx)
                    ctx.clock.advance(minutes=10)
                    jobs.retomar(ctx)
                self.assertEqual(len(calls), 1)
                self.assertNotIn("_retomar", ctx.db.leads[lead["id"]]["qualification"])

    def test_falha_sem_envio_continua_restaurando_o_marcador(self):
        ctx = make_ctx(default_agent(test_only=False))
        lead = silent_lead(ctx, qualification={"_retomar": {"em": iso(NOW - timedelta(minutes=5))}})
        with mock.patch.object(jobs, "handle_turn", side_effect=RuntimeError("fora")):
            jobs.retomar(ctx)
        marker = ctx.db.leads[lead["id"]]["qualification"].get("_retomar")
        self.assertEqual(marker.get("tentativas"), 1)


class AlertaPermanentePorDestinatarioTest(unittest.TestCase):
    def _alertas(self, ctx):
        return [r for r in ctx.db.outbox if r["created_by"] == "alerta"]

    def test_dois_destinatarios_geram_dois_alertas_e_o_mesmo_so_um(self):
        ctx = make_ctx(default_agent(test_only=False))
        ctx.evo.fail_send, ctx.evo.fail_status = True, 400
        jobs._alert_permanent_failure(ctx, "+5585999000001", "repasse", "HTTP 400")
        jobs._alert_permanent_failure(ctx, "+5585999000001", "repasse", "HTTP 400")
        self.assertEqual(len(self._alertas(ctx)), 1)
        jobs._alert_permanent_failure(ctx, "+5585999000002", "repasse", "HTTP 400")
        self.assertEqual(len(self._alertas(ctx)), 2)
        jobs._alert_permanent_failure(ctx, "+5585999000002", "lembrete", "HTTP 400")   # outra rotina
        self.assertEqual(len(self._alertas(ctx)), 3)

    def test_lembretes_a_numeros_diferentes_avisam_cada_um(self):
        ctx = make_ctx(default_agent(test_only=False))
        for i, phone in enumerate(["+5585999000001", "+5585999000002"]):
            lead = ctx.db.create_lead(phone, name=f"Lead {i}")
            book(ctx, lead, NOW + timedelta(hours=23, minutes=i * 40))
        ctx.evo.fail_send, ctx.evo.fail_status = True, 400
        jobs.reminders(ctx)
        self.assertEqual(len(self._alertas(ctx)), 2)


if __name__ == "__main__":
    unittest.main()
