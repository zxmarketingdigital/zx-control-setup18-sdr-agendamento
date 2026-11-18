"""Rodada 19 do motor (envio/QR): alerta de envio incerto na outbox, leitura direcionada pelo @lid,
_alert_once só marca depois de enviar e logout sem QR. Tudo com Evolution/banco/IA falsos."""

import unittest

from motor import engine, outbox, whatsapp
from motor.conversa import _directed_chat_read, _reread_block
from motor.evolution import EvolutionNoKeyId, EvolutionTimeout
from tests.motor.support import LEAD_PHONE, OWNER_PHONE, inbound, make_ctx
from tests.motor.test_whatsapp_conexao import request, setup

OTHER_PHONE = "+5511999990002"


def alerts(ctx, needle):
    return [t for t in ctx.evo.texts_to(OWNER_PHONE) if needle in t]


def only_lead_fails(ctx, exc_factory, phones=(LEAD_PHONE, OTHER_PHONE)):
    """Falha só o envio a leads (o alerta ao dono segue normal)."""
    real = ctx.evo.send_text

    def send_text(phone, text, delay_ms=0):
        if phone in phones:
            raise exc_factory()
        return real(phone, text, delay_ms)

    ctx.evo.send_text = send_text


class OutboxIncertoTest(unittest.TestCase):
    def test_linha_do_painel_com_timeout_alerta_o_dono_uma_vez(self):
        ctx = make_ctx()
        lead = inbound(ctx)
        only_lead_fails(ctx, lambda: EvolutionTimeout("timed out"))
        row = ctx.db.add_outbox(LEAD_PHONE, "Confirmado!", lead_id=lead["id"], created_by="painel")
        outbox.process_outbox(ctx)
        self.assertEqual(row["last_error"], "timeout_incerto")
        self.assertEqual(len(alerts(ctx, "Não tenho certeza")), 1)
        outbox.send_row(ctx, row)   # a mesma linha reprocessada não alerta de novo
        self.assertEqual(len(alerts(ctx, "Não tenho certeza")), 1)

    def test_linha_sem_lead_id_resolve_pelo_telefone(self):
        ctx = make_ctx()
        inbound(ctx)
        only_lead_fails(ctx, lambda: EvolutionTimeout("timed out"))
        ctx.db.add_outbox(LEAD_PHONE, "Confirmado!", created_by="painel")
        outbox.process_outbox(ctx)
        self.assertEqual(len(alerts(ctx, "Não tenho certeza")), 1)

    def test_linha_interna_nao_alerta(self):
        ctx = make_ctx()
        only_lead_fails(ctx, lambda: EvolutionTimeout("timed out"), phones=(OWNER_PHONE,))
        ctx.db.add_outbox(OWNER_PHONE, "Aviso ao dono", created_by="painel")
        outbox.process_outbox(ctx)
        self.assertEqual(ctx.evo.texts_to(OWNER_PHONE), [])   # nenhum aviso (e nenhum laço)


class SemKeyIdPorLinhaTest(unittest.TestCase):
    def test_duas_linhas_sem_key_id_geram_dois_alertas(self):
        ctx = make_ctx()
        only_lead_fails(ctx, lambda: EvolutionNoKeyId("sem key"))
        r1 = ctx.db.add_outbox(LEAD_PHONE, "um", created_by="painel")
        r2 = ctx.db.add_outbox(OTHER_PHONE, "dois", created_by="painel")
        outbox.process_outbox(ctx)
        self.assertEqual((r1["last_error"], r2["last_error"]), ("resposta_sem_key_id",) * 2)
        self.assertEqual(len(alerts(ctx, "sem confirmação")), 2)

    def test_mesma_linha_reprocessada_alerta_uma_vez(self):
        ctx = make_ctx()
        only_lead_fails(ctx, lambda: EvolutionNoKeyId("sem key"))
        row = ctx.db.add_outbox(LEAD_PHONE, "um", created_by="painel")
        outbox.process_outbox(ctx)
        outbox._alert_no_key_once(ctx, LEAD_PHONE, "painel", row["id"])
        self.assertEqual(len(alerts(ctx, "sem confirmação")), 1)


class LeituraPeloLidTest(unittest.TestCase):
    LID = "98765432109876@lid"

    def _fake_chat(self, ctx):
        """Imita a Evolution: cada JID tem o seu histórico."""
        calls = []

        def chat_messages(phone, limit=10, jid=None):
            calls.append(jid)
            want = jid or f"{phone.lstrip('+')}@s.whatsapp.net"
            return [r for r in reversed(ctx.evo.records) if r["key"]["remoteJid"] == want][:limit]

        ctx.evo.chat_messages = chat_messages
        return calls

    def test_pare_escrito_no_lid_durante_a_ia_bloqueia_o_envio(self):
        ctx = make_ctx()
        calls = self._fake_chat(ctx)
        # primeira mensagem do lead chega pelo @lid com o número em remoteJidAlt
        ctx.evo.add(LEAD_PHONE, "oi", wa_id="IN1", jid=self.LID,
                    extra={})
        ctx.evo.records[-1]["key"]["remoteJidAlt"] = f"{LEAD_PHONE.lstrip('+')}@s.whatsapp.net"
        engine.ingest(ctx)
        lead = ctx.db.get_lead_by_phone(LEAD_PHONE)
        self.assertEqual(ctx.state["lid_by_phone"][LEAD_PHONE], self.LID)
        # durante a chamada da IA ele manda o pedido de saída, também pelo @lid
        ctx.evo.add(LEAD_PHONE, "para de me mandar mensagem", wa_id="IN2", jid=self.LID)
        ctx.evo.records[-1]["key"]["remoteJidAlt"] = f"{LEAD_PHONE.lstrip('+')}@s.whatsapp.net"
        block = _reread_block(ctx, lead, ctx.now())
        self.assertEqual(block["status"], "opt_out_registrado")
        self.assertIn(self.LID, calls)
        self.assertTrue(ctx.db.get_lead(lead["id"])["opt_out"])

    def test_dedupe_por_key_id_quando_os_dois_jids_trazem_a_mesma_mensagem(self):
        ctx = make_ctx()
        ctx.state["lid_by_phone"] = {LEAD_PHONE: self.LID}
        rec = {"key": {"remoteJid": self.LID, "id": "DUP", "fromMe": False}, "message": {"conversation": "oi"},
               "messageTimestamp": int(ctx.now().timestamp())}
        ctx.evo.chat_messages = lambda phone, limit=10, jid=None: [rec]
        seen = []
        real = engine._process_records
        engine._process_records = lambda c, records, known, agent, ignored: (seen.append(records), set())[1]
        try:
            engine.ingest_chat(ctx, LEAD_PHONE)
        finally:
            engine._process_records = real
        self.assertEqual(len(seen[0]), 1)

    def test_sem_lid_conhecido_so_consulta_pelo_numero(self):
        ctx = make_ctx()
        calls = self._fake_chat(ctx)
        engine.ingest_chat(ctx, LEAD_PHONE)
        self.assertEqual(calls, [None])


class AlertOnceTest(unittest.TestCase):
    def test_falha_do_envio_nao_gasta_o_alerta_do_dia(self):
        ctx = make_ctx()
        real = outbox.notify_owner
        boom = {"on": True}

        def notify(c, text):
            if boom["on"]:
                raise RuntimeError("supabase fora")
            return real(c, text)

        engine.notify_owner = notify
        try:
            engine._alert_once(ctx, "ingest_ciclo", "motor cego")
            self.assertNotIn("ingest_ciclo", ctx.state.get("alerted_day", {}))
            boom["on"] = False
            engine._alert_once(ctx, "ingest_ciclo", "motor cego")
            self.assertIn("ingest_ciclo", ctx.state["alerted_day"])
            engine._alert_once(ctx, "ingest_ciclo", "motor cego")   # no mesmo dia, uma vez só
        finally:
            engine.notify_owner = real
        self.assertEqual(len(alerts(ctx, "motor cego")), 1)


class LogoutSemQrTest(unittest.TestCase):
    def test_logout_conclui_sem_connect_nem_qr(self):
        ctx = setup(evo_state="open")
        request(ctx, "logout")
        whatsapp.tick(ctx, ctx.now())
        self.assertEqual(ctx.evo.calls, ["logout"])
        row = ctx.store["row"]
        self.assertEqual((row["command_status"], row["state"]), ("concluido", "close"))
        self.assertIsNone(row["qr_base64"])
        self.assertIsNone(row["pairing_code"])


if __name__ == "__main__":
    unittest.main()
