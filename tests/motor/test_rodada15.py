"""Rodada 15 do motor: negação antes do verbo de saída, opt-out vindo da IA com releitura, eco do agente
reconhecido pela outbox depois de um reinício e alerta de teto de páginas só com lead não examinado."""

import unittest
from unittest import mock

from motor import engine
from motor.conversa import Decision, apply_decision, is_opt_out
from motor.outbox import OPT_OUT_CONFIRMATION
from motor.util import iso
from datetime import timedelta
from tests.motor.support import LEAD_PHONE, OWNER_PHONE, default_agent, inbound, make_ctx


class NegacaoOptOutTest(unittest.TestCase):
    def test_negacao_antes_do_verbo_nao_e_saida(self):
        for text in ["Não para de me mandar mensagens, por favor", "não apague meu contato, me chama amanhã",
                     "não tire meu número", "nunca pare de me mandar novidades", "Nao me tire da lista",
                     "jamais me descadastre"]:
            self.assertFalse(is_opt_out(text), text)

    def test_pedidos_de_saida_continuam(self):
        for text in ["para de me mandar mensagem", "apague meu contato", "tire meu número", "nao me mande mais",
                     "não tenho interesse, me tira da lista", "pare", "descadastrar", "deixa me em paz"]:
            self.assertTrue(is_opt_out(text), text)


class OptOutDaIATest(unittest.TestCase):
    def _decision(self):
        return Decision(reply="", action="opt_out")

    def test_takeover_detectado_na_releitura_registra_em_silencio(self):
        ctx = make_ctx()
        lead = inbound(ctx, "talvez depois")
        snapshot = ctx.db.get_lead(lead["id"])   # o turno saiu com este snapshot, sem takeover
        ctx.db.update_lead(lead["id"], {"takeover_until": iso(ctx.now() + timedelta(hours=2))})
        result = apply_decision(ctx, snapshot, default_agent(), self._decision(), [], None, "talvez depois")
        self.assertEqual(result["status"], "opt_out_registrado")
        self.assertEqual(result["reply"], "")
        self.assertTrue(ctx.db.get_lead(lead["id"])["opt_out"])
        self.assertEqual(ctx.evo.sent, [])
        self.assertEqual([r for r in ctx.db.outbox if r["body"] == OPT_OUT_CONFIRMATION], [])

    def test_sem_takeover_confirma_como_antes(self):
        ctx = make_ctx()
        lead = inbound(ctx, "talvez depois")
        result = apply_decision(ctx, ctx.db.get_lead(lead["id"]), default_agent(), self._decision(), [], None, "x")
        self.assertEqual(result["status"], "opt_out_confirmado")
        self.assertTrue(ctx.db.get_lead(lead["id"])["opt_out"])
        self.assertIn(OPT_OUT_CONFIRMATION, ctx.evo.texts_to(LEAD_PHONE))

    def test_estagio_final_registra_em_silencio(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        snapshot = ctx.db.get_lead(lead["id"])
        ctx.db.update_lead(lead["id"], {"stage": "venda"})
        result = apply_decision(ctx, snapshot, default_agent(), self._decision(), [], None, "x")
        self.assertEqual(result["status"], "opt_out_registrado")
        self.assertEqual(ctx.evo.sent, [])
        self.assertEqual(ctx.db.get_lead(lead["id"])["stage"], "venda")


class EcoDoAgenteTest(unittest.TestCase):
    def test_eco_conhecido_pela_outbox_apos_reinicio_nao_e_takeover(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        row = ctx.db.add_outbox(LEAD_PHONE, "Olá!", lead_id=None, created_by="painel", status="enviado")
        row["wa_key_id"] = "OUTX"
        ctx.evo.add(LEAD_PHONE, "Olá!", wa_id="OUTX", from_me=True)   # sent_ids vazio: processo reiniciado
        engine.ingest(ctx)
        fresh = ctx.db.get_lead(lead["id"])
        self.assertIsNone(fresh["takeover_until"])
        self.assertEqual(ctx.db.messages[-1]["author"], "agente")
        self.assertEqual(ctx.db.messages[-1]["wa_id"], "OUTX")

    def test_sem_registro_na_outbox_continua_sendo_takeover(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        ctx.evo.add(LEAD_PHONE, "Olá!", wa_id="MANUAL9", from_me=True)
        engine.ingest(ctx)
        self.assertIsNotNone(ctx.db.get_lead(lead["id"])["takeover_until"])

    def test_match_ambiguo_aceita_linha_enviada_com_o_mesmo_key_id(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        row = ctx.db.add_outbox(LEAD_PHONE, "Olá!", lead_id=lead["id"], status="enviado")
        row["wa_key_id"] = "OUTY"
        self.assertTrue(engine._match_ambiguous_send(ctx, lead, LEAD_PHONE, "Olá!", "OUTY", ctx.now(), ctx.now()))
        # outro key.id com o mesmo texto é outra mensagem: não casa
        self.assertFalse(engine._match_ambiguous_send(ctx, lead, LEAD_PHONE, "Olá!", "OUTZ", ctx.now(), ctx.now()))

    def test_send_sem_lead_id_resolve_o_lead_pelo_telefone(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        key = ctx.messenger.send(LEAD_PHONE, "Olá!", lead_id=None, author="painel")
        stored = [m for m in ctx.db.messages if m.get("wa_id") == key]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["lead_id"], lead["id"])

    def test_send_sem_lead_id_e_sem_lead_nao_grava(self):
        ctx = make_ctx()
        key = ctx.messenger.send(LEAD_PHONE, "Olá!", lead_id=None, author="painel")
        self.assertTrue(key)
        self.assertEqual(ctx.db.messages, [])


class TetoDePaginasTest(unittest.TestCase):
    def _alerts(self, ctx):
        return [t for t in ctx.evo.texts_to(OWNER_PHONE) if "consegue ler" in t]

    def test_so_grupo_no_teto_tambem_dispara_o_alerta(self):
        # r23: o teto corta mensagens antigas de qualquer tipo; o lead pode estar
        # além da última página, então o alerta sai mesmo com só grupo nela.
        ctx = make_ctx()
        ctx.state["agent"] = ctx.db.get_agent_config()
        with mock.patch.object(engine, "MAX_POLL_PAGES", 2), mock.patch.object(engine, "POLL_LIMIT", 5):
            for i in range(12):
                ctx.evo.add("+5511999990000", "oi grupo", wa_id=f"G{i}", jid="12036300000000@g.us")
            engine._read_records(ctx)
        self.assertEqual(len(self._alerts(ctx)), 1)

    def test_conversa_de_lead_nao_examinada_dispara_o_alerta(self):
        ctx = make_ctx()
        ctx.state["agent"] = ctx.db.get_agent_config()
        with mock.patch.object(engine, "MAX_POLL_PAGES", 2), mock.patch.object(engine, "POLL_LIMIT", 5):
            for i in range(12):
                ctx.evo.add(LEAD_PHONE, f"oi {i}", wa_id=f"L{i}")
            engine._read_records(ctx)
        self.assertEqual(len(self._alerts(ctx)), 1)


if __name__ == "__main__":
    unittest.main()
