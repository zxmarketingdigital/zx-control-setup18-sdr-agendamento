"""Rodada 18 do motor: pergunta não é opt-out solto, eco antigo do dono vira takeover, retomada com teto de
tentativas e gravações de qualification que preservam edições do dono."""

import unittest
from datetime import timedelta

from motor import engine, jobs
from motor.ai import AIError
from motor.conversa import handle_turn, is_opt_out
from motor.util import iso
from tests.motor.support import LEAD_PHONE, NOW, OWNER_PHONE, make_ctx
from tests.motor.test_jobs import silent_lead


class OptOutPerguntaTest(unittest.TestCase):
    def test_pergunta_com_padrao_solto_nao_e_opt_out(self):
        for text in ("Você remove meu número da base depois da reunião?", "você apaga meu contato?",
                     "posso descadastrar?", "como faço para remover meu número",
                     "vc pode apagar meu contato depois"):
            self.assertFalse(is_opt_out(text), text)

    def test_pedido_solto_e_forte_continuam(self):
        self.assertTrue(is_opt_out("remove meu número"))
        self.assertTrue(is_opt_out("pare de me mandar mensagem?"))
        self.assertTrue(is_opt_out("quero sair da lista"))


class EcoAntigoDoDonoTest(unittest.TestCase):
    def _setup(self):
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        ctx.clock.advance(hours=7)
        return ctx, lead

    def test_fromme_antigo_vira_takeover_e_ia_nao_responde(self):
        ctx, lead = self._setup()
        ctx.evo.add(LEAD_PHONE, "oi, sou o dono, te ligo hoje", wa_id="DONO1", from_me=True, age_seconds=6 * 3600 + 55 * 60)
        engine.ingest(ctx)
        msgs = [m for m in ctx.db.messages if m["wa_id"] == "DONO1"]
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["author"], "dono")
        self.assertTrue(ctx.db.leads[lead["id"]]["takeover_until"])
        ctx.ai.push({"reply": "Olá!", "action": "responder"})
        ctx.evo.add(LEAD_PHONE, "e aí?", wa_id="LEAD2")
        for lead_id in engine.ingest(ctx):
            handle_turn(ctx, lead_id)
        self.assertNotIn("Olá!", ctx.evo.texts_to(LEAD_PHONE))

    def test_fromme_antigo_com_resposta_posterior_so_grava(self):
        ctx, lead = self._setup()
        ctx.db.add_message(lead["id"], "out", "agente", "resposta depois", None, "AG1", ctx.clock() - timedelta(minutes=30))
        ctx.evo.add(LEAD_PHONE, "oi, sou o dono", wa_id="DONO2", from_me=True, age_seconds=6 * 3600 + 55 * 60)
        engine.ingest(ctx)
        self.assertTrue(any(m["wa_id"] == "DONO2" and m["author"] == "dono" for m in ctx.db.messages))
        self.assertFalse(ctx.db.leads[lead["id"]]["takeover_until"])


class RetomadaTetoTest(unittest.TestCase):
    def test_ia_fora_no_maximo_3_tentativas_e_um_alerta(self):
        ctx = make_ctx()
        lead = silent_lead(ctx, qualification={"_retomar": {"em": iso(NOW - timedelta(minutes=5))}})
        ctx.ai.push(*[AIError("fora do ar")] * 40)
        for _ in range(6):
            jobs.retomar(ctx)
            ctx.clock.advance(minutes=10)
            ctx.db.update_lead(lead["id"], {"last_outbound_at": iso(NOW - timedelta(hours=3))})
        self.assertEqual(jobs.MAX_RETOMAR_TENTATIVAS, 3)
        self.assertNotIn("_retomar", ctx.db.leads[lead["id"]]["qualification"])
        alerts = [t for t in ctx.evo.texts_to(OWNER_PHONE) if "retomada combinada" in t]
        self.assertEqual(len(alerts), 1)
        self.assertIn(LEAD_PHONE.lstrip("+")[-8:], alerts[0])
        self.assertIn("manualmente", alerts[0])


class QualificationPreservadaTest(unittest.TestCase):
    def test_retomar_preserva_edicao_do_dono_em_outra_chave(self):
        ctx = make_ctx()
        lead = silent_lead(ctx, qualification={"_retomar": {"em": iso(NOW - timedelta(minutes=5))}, "a": 1})
        stale_snapshot = ctx.db.get_lead(lead["id"])
        ctx.db.update_lead(lead["id"], {"qualification": {**stale_snapshot["qualification"], "nota_dono": "x"}})
        ctx.ai.push({"reply": "Oi, voltei!", "action": "responder"})
        jobs._retomar_lead(ctx, stale_snapshot, ctx.clock())
        q = ctx.db.leads[lead["id"]]["qualification"]
        self.assertEqual(q.get("nota_dono"), "x")
        self.assertNotIn("_retomar", q)

    def test_aguardando_humano_preserva_edicao_do_dono(self):
        ctx = make_ctx()
        lead = silent_lead(ctx, qualification={jobs.AGUARDA_HUMANO_KEY: iso(NOW - timedelta(hours=1))})
        original_active = ctx.db.active_leads

        def snapshot_then_edit(limit=1000):
            rows = original_active(limit)
            ctx.db.leads[lead["id"]]["qualification"] = {**ctx.db.leads[lead["id"]]["qualification"], "nota_dono": "y"}
            return rows
        ctx.db.active_leads = snapshot_then_edit
        ctx.db.add_message(lead["id"], "out", "dono", "oi", None, "D9", NOW)
        jobs.aguardando_humano(ctx)
        q = ctx.db.leads[lead["id"]]["qualification"]
        self.assertEqual(q.get("nota_dono"), "y")
        self.assertNotIn(jobs.AGUARDA_HUMANO_KEY, q)


if __name__ == "__main__":
    unittest.main()
