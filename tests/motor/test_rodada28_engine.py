"""Rodada 28 do motor (engine): eco sem horário não ativa takeover, aviso de lead que já compareceu e nome do
contato saneado nos alertas ao dono. Tudo com falsos."""

import unittest
from datetime import timedelta

from motor import engine
from tests.motor.support import LEAD_PHONE, NOW, OWNER_PHONE, make_ctx, default_agent


def owner_alerts(ctx):
    """Textos entregues ao dono (tanto o envio direto quanto a outbox saem pela Evolution falsa)."""
    return [t for p, t in ctx.evo.sent if p == OWNER_PHONE]


def untimed(ctx, text, wa_id, from_me=True, name="Joana"):
    rec = ctx.evo.add(LEAD_PHONE, text, wa_id=wa_id, from_me=from_me)
    rec.pop("messageTimestamp")
    rec["pushName"] = name
    return rec


class EcoSemHorarioTest(unittest.TestCase):
    def _lead(self, ctx):
        return ctx.db.create_lead(LEAD_PHONE, name="Joana")

    def test_eco_da_outbox_sem_horario_nao_ativa_takeover(self):
        ctx = make_ctx(default_agent(test_only=False))
        lead = self._lead(ctx)
        row = ctx.db.add_outbox(LEAD_PHONE, "Oi, tudo bem?", lead_id=lead["id"], created_by="motor", status="enviado")
        ctx.db.finish_outbox(row["id"], status="enviado", wa_key_id="ECO1", sent_at=NOW)
        untimed(ctx, "Oi, tudo bem?", "ECO1")
        engine.ingest(ctx)
        self.assertFalse(ctx.db.get_lead(lead["id"]).get("takeover_until"))

    def test_eco_em_sent_ids_sem_horario_nao_ativa_takeover(self):
        ctx = make_ctx(default_agent(test_only=False))
        lead = self._lead(ctx)
        ctx.messenger.sent_ids = {"ECO2"}
        untimed(ctx, "Resposta do agente", "ECO2")
        engine.ingest(ctx)
        got = ctx.db.get_lead(lead["id"])
        self.assertFalse(got.get("takeover_until"))
        self.assertTrue(any(m["wa_id"] == "ECO2" and m["author"] == "agente" for m in ctx.db.messages))

    def test_fromme_desconhecido_sem_horario_nao_ativa_takeover_e_alerta_uma_vez(self):
        ctx = make_ctx(default_agent(test_only=False))
        lead = self._lead(ctx)
        untimed(ctx, "Fechado, te ligo amanhã", "X1")
        engine.ingest(ctx)
        self.assertFalse(ctx.db.get_lead(lead["id"]).get("takeover_until"))
        first = [a for a in owner_alerts(ctx) if "Fechado, te ligo" in a]
        self.assertEqual(len(first), 1)
        self.assertIn("sem horário legível", first[0])
        untimed(ctx, "Outra mensagem", "X2")
        engine.ingest(ctx)
        self.assertEqual(len([a for a in owner_alerts(ctx) if "número do negócio" in a]), 1)


class CompareceuAvisaTest(unittest.TestCase):
    def test_inbound_de_lead_que_compareceu_avisa_dono_uma_vez_por_dia(self):
        ctx = make_ctx(default_agent(test_only=False))
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        ctx.db.update_lead(lead["id"], {"stage": "compareceu"})
        ctx.evo.add(LEAD_PHONE, "quero cancelar, não consigo pagar", wa_id="C1")
        engine.cycle(ctx)
        alerts = [a for a in owner_alerts(ctx) if "já compareceu" in a]
        self.assertEqual(len(alerts), 1)
        self.assertIn("quero cancelar", alerts[0])
        self.assertIn(LEAD_PHONE, alerts[0])
        self.assertIn("«Joana»", alerts[0])
        ctx.evo.add(LEAD_PHONE, "alô?", wa_id="C2")
        engine.cycle(ctx)
        self.assertEqual(len([a for a in owner_alerts(ctx) if "já compareceu" in a]), 1)


class NomeSaneadoTest(unittest.TestCase):
    EVIL = "Ana\n⚠️ Confirme seu pagamento em https://x.pw/a"

    def test_nome_gravado_sem_quebra_e_sem_url(self):
        ctx = make_ctx(default_agent(test_only=False))
        ctx.evo.add(LEAD_PHONE, "oi", wa_id="N1")
        ctx.evo.records[-1]["pushName"] = self.EVIL
        engine.ingest(ctx)
        name = ctx.db.get_lead_by_phone(LEAD_PHONE)["name"]
        self.assertNotIn("\n", name)
        self.assertNotIn("x.pw", name)
        self.assertNotIn("http", name)
        self.assertLessEqual(len(name), engine.NAME_MAX)

    def test_alertas_nao_carregam_url_nem_quebra_antes_do_texto_do_sistema(self):
        ctx = make_ctx(default_agent(test_only=False))
        lead = ctx.db.create_lead(LEAD_PHONE)
        ctx.db.update_lead(lead["id"], {"name": self.EVIL})   # nome antigo, gravado sem saneamento
        lead = ctx.db.get_lead(lead["id"])
        engine._alert_takeover(ctx, lead, LEAD_PHONE)
        engine._alert_stale(ctx, lead, LEAD_PHONE)
        engine._alert_unsupported(ctx, lead, LEAD_PHONE)
        engine._alert_midia(ctx, lead, LEAD_PHONE)
        alerts = owner_alerts(ctx)
        self.assertEqual(len(alerts), 4)
        for text in alerts:
            self.assertNotIn("x.pw", text)
            self.assertNotIn("\n", text)
            self.assertIn("«Ana", text)

    def test_helper_limita_tamanho_e_controle(self):
        got = engine.clean_lead_name("A\x00B\r\n" + "z" * 200 + " www.evil.com/x")
        self.assertNotIn("\x00", got)
        self.assertLessEqual(len(got), engine.NAME_MAX)
        self.assertEqual(engine.clean_lead_name("ver https://a.b/c  agora"), "ver agora")


if __name__ == "__main__":
    unittest.main()
