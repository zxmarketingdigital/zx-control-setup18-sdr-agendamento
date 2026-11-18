"""Rodada 24 (outbox/agenda): falha de conexão com a Evolution em resposta ao lead espera pela validade da resposta;
modelo de confirmação com erro não derruba o que vem depois da reserva já gravada."""

import unittest
from datetime import timedelta
from types import SimpleNamespace

from motor import agenda, engine, outbox
from motor.evolution import EvolutionError
from tests.motor.support import LEAD_PHONE, OWNER_PHONE, default_agent, inbound, make_ctx, sp


def owner_alerts(ctx):
    return [r for r in ctx.db.outbox if r["phone"] == OWNER_PHONE and r["created_by"] == "alerta"]


class QuedaDaEvolutionNaoViraErroTest(unittest.TestCase):
    def _reply(self, ctx):
        lead = inbound(ctx, "oi")
        return lead, ctx.db.add_outbox(LEAD_PHONE, "Olá, posso ajudar?", lead_id=lead["id"], created_by="motor")

    def _down(self, ctx):
        def boom(*a, **k):
            raise EvolutionError("connection refused")   # sem status: nada saiu
        ctx.evo.send_text = boom

    def test_evolution_fora_por_10_min_e_volta_a_resposta_sai_uma_vez(self):
        ctx = make_ctx()
        self._reply(ctx)
        real_send = ctx.evo.send_text
        self._down(ctx)
        for _ in range(12):   # 10+ min de falhas, com o backoff entre as tentativas (30s, 60s, 120s...)
            outbox.process_outbox(ctx)
            ctx.clock.advance(seconds=60)
        row = ctx.db.outbox[0]
        self.assertEqual(row["status"], "pendente", row)
        self.assertGreater(row["attempts"], 0)
        self.assertEqual(owner_alerts(ctx), [])
        ctx.evo.send_text = real_send
        ctx.clock.advance(hours=1)   # passa o teto do backoff
        self.assertEqual(outbox.process_outbox(ctx), 1)
        self.assertEqual(ctx.db.outbox[0]["status"], "enviado")
        self.assertEqual(len(ctx.evo.texts_to(LEAD_PHONE)), 1)

    def test_fora_alem_da_validade_vira_erro_e_avisa_o_dono(self):
        ctx = make_ctx()
        lead, _ = self._reply(ctx)
        self._down(ctx)
        limit = ctx.cfg.catchup_minutes
        outbox.process_outbox(ctx)
        self.assertEqual(ctx.db.outbox[0]["status"], "pendente")
        ctx.clock.advance(minutes=limit + 1)
        outbox.process_outbox(ctx)
        row = ctx.db.outbox[0]
        self.assertEqual(row["status"], "erro")
        self.assertTrue(row["last_error"].startswith(outbox.FINAL_FAILURE_PREFIX), row)
        self.assertEqual(len(owner_alerts(ctx)), 1)

    def test_quatro_xx_com_instancia_fechada_fica_pendente(self):
        ctx = make_ctx()
        self._reply(ctx)
        ctx.evo.state = "close"

        def boom(*a, **k):
            raise EvolutionError("Connection Closed", status=400)
        ctx.evo.send_text = boom
        outbox.process_outbox(ctx)
        self.assertEqual(ctx.db.outbox[0]["status"], "pendente")
        self.assertEqual(owner_alerts(ctx), [])

    def test_quatro_xx_com_instancia_aberta_segue_definitivo(self):
        ctx = make_ctx()
        self._reply(ctx)

        def boom(*a, **k):
            raise EvolutionError("número inexistente", status=400)
        ctx.evo.send_text = boom
        outbox.process_outbox(ctx)
        self.assertEqual(ctx.db.outbox[0]["status"], "erro")
        self.assertEqual(len(owner_alerts(ctx)), 1)

    def test_linha_pendente_nao_deixa_o_turno_ser_reprocessado(self):
        ctx = make_ctx()
        lead, _ = self._reply(ctx)
        self._down(ctx)
        outbox.process_outbox(ctx)
        ctx.db.update_lead(lead["id"], {"last_outbound_at": None})
        ctx.clock.advance(minutes=1)
        self.assertEqual(engine.recover_unanswered(ctx), set())   # a linha pendente cobre o turno (sem 2ª IA)

    def test_linha_do_painel_mantem_a_contagem_de_tentativas(self):
        ctx = make_ctx()
        ctx.db.add_outbox(LEAD_PHONE, "oi", created_by="painel")
        self._down(ctx)
        for _ in range(outbox.MAX_ATTEMPTS):
            outbox.process_outbox(ctx)
            ctx.clock.advance(hours=2)
        self.assertEqual(ctx.db.outbox[0]["status"], "erro")


class ModeloDeConfirmacaoComErroTest(unittest.TestCase):
    def _setup(self, template):
        ctx = make_ctx(default_agent(test_only=False, confirm_template=template))
        lead = inbound(ctx, "quero marcar")
        ctx.db.slots.append(sp(19, 14))
        calls = []
        ctx.gcal = SimpleNamespace(create=lambda *a, **k: calls.append(a) or "evt1", last_uncertain=False)
        return ctx, lead, calls

    def test_placeholder_desconhecido_reserva_fica_confirma_com_padrao_e_alerta(self):
        ctx, lead, gcal_calls = self._setup("Turma {turma}: confirmado para {data} às {hora}.")
        ok, text = agenda.book_for_lead(ctx, ctx.db.get_lead(lead["id"]), sp(19, 14), ctx.state["agent"],
                                        queue_confirmation=True)
        self.assertTrue(ok)
        self.assertEqual(len(ctx.db.appointments), 1)                      # a reserva ficou
        self.assertNotIn("{turma}", text)
        sent = ctx.evo.texts_to(LEAD_PHONE)
        self.assertEqual(len(sent), 1)
        self.assertIn("Sua conversa está confirmada", sent[0])             # template padrão
        self.assertNotIn("{", sent[0])
        alerts = owner_alerts(ctx)
        self.assertEqual(len(alerts), 1)
        self.assertIn("{turma}", alerts[0]["body"])
        self.assertEqual(len(gcal_calls), 1)                               # o evento foi tentado mesmo assim

    def test_excecao_do_render_tambem_cai_no_padrao(self):
        ctx, lead, gcal_calls = self._setup("Olá {nome.nao_existe}!")           # AttributeError no format_map
        ok, text = agenda.book_for_lead(ctx, ctx.db.get_lead(lead["id"]), sp(19, 14), ctx.state["agent"],
                                        queue_confirmation=True)
        self.assertTrue(ok)
        self.assertIn("Sua conversa está confirmada", ctx.evo.texts_to(LEAD_PHONE)[0])
        self.assertEqual(len(owner_alerts(ctx)), 1)
        self.assertEqual(len(gcal_calls), 1)

    def test_modelo_valido_nao_alerta(self):
        ctx, lead, _ = self._setup("Fechado, {nome}: {data} às {hora}.")
        agenda.book_for_lead(ctx, ctx.db.get_lead(lead["id"]), sp(19, 14), ctx.state["agent"],
                             queue_confirmation=True)
        self.assertEqual(owner_alerts(ctx), [])
        self.assertIn("Fechado", ctx.evo.texts_to(LEAD_PHONE)[0])

    def test_alerta_do_modelo_sai_uma_vez_por_modelo(self):
        ctx, lead, _ = self._setup("Turma {turma}")
        agent = ctx.state["agent"]
        agenda.safe_confirmation_text(ctx, ctx.db.get_lead(lead["id"]), agent, sp(19, 14))
        agenda.safe_confirmation_text(ctx, ctx.db.get_lead(lead["id"]), agent, sp(19, 14))
        self.assertEqual(len(owner_alerts(ctx)), 1)


if __name__ == "__main__":
    unittest.main()
