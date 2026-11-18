"""Rodada 16 do motor: takeover antes da gravação, envio incerto por falha de rede/500/408 e cadência com relógio
local atrasado."""

import socket
import unittest
import urllib.error
from datetime import timedelta
from unittest import mock

from motor import engine, http, jobs, outbox
from motor.evolution import EvolutionClient, EvolutionError, EvolutionTimeout
from motor.util import iso
from tests.motor.support import LEAD_PHONE, default_agent, inbound, make_ctx
from tests.motor.test_jobs import silent_lead


class TakeoverAntesDaGravacaoTest(unittest.TestCase):
    def test_update_falhando_nao_grava_a_mensagem_e_o_proximo_poll_ativa_o_takeover(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        ctx.evo.add(LEAD_PHONE, "assumi aqui", wa_id="DONO1", from_me=True)
        real = ctx.db.update_lead
        calls = {"n": 0}

        def flaky(lead_id, fields):
            if "takeover_until" in fields and calls["n"] == 0:
                calls["n"] += 1
                raise RuntimeError("503")
            return real(lead_id, fields)
        ctx.db.update_lead = flaky
        engine.ingest(ctx)
        self.assertEqual([m for m in ctx.db.messages if m["wa_id"] == "DONO1"], [])   # nada gravado
        self.assertIsNone(ctx.db.get_lead(lead["id"])["takeover_until"])
        engine.ingest(ctx)   # o wa_id volta no próximo poll (failed_wa_ids)
        self.assertEqual(len([m for m in ctx.db.messages if m["wa_id"] == "DONO1"]), 1)
        self.assertIsNotNone(ctx.db.get_lead(lead["id"])["takeover_until"])


class EnvioIncertoTest(unittest.TestCase):
    def _client_with(self, exc):
        client = EvolutionClient("http://evo.local", "k", "i")
        patch = mock.patch.object(http, "urlopen", side_effect=exc)
        return client, patch

    def test_conexao_recusada_e_dns_continuam_reenviaveis(self):
        for reason in (ConnectionRefusedError(61, "refused"), socket.gaierror(8, "dns")):
            client, patch = self._client_with(urllib.error.URLError(reason))
            with patch, self.assertRaises(EvolutionError) as caught:
                client.send_text(LEAD_PHONE, "oi")
            self.assertNotIsInstance(caught.exception, EvolutionTimeout, reason)

    def test_erro_de_rede_depois_do_envio_e_incerto(self):
        for exc in (OSError("Connection aborted"), urllib.error.URLError(OSError(54, "reset by peer")),
                    ConnectionAbortedError("abortada")):
            client, patch = self._client_with(exc)
            with patch, self.assertRaises(EvolutionTimeout):
                client.send_text(LEAD_PHONE, "oi")

    def test_500_e_408_viram_timeout_incerto_sem_reenvio(self):
        for status in (500, 408):
            ctx = make_ctx()
            ctx.evo.fail_send, ctx.evo.fail_status = True, status
            ctx.db.add_outbox(LEAD_PHONE, "oi", created_by="painel")
            outbox.process_outbox(ctx)
            self.assertEqual((ctx.db.outbox[0]["status"], ctx.db.outbox[0]["last_error"]),
                             ("erro", "timeout_incerto"), status)
            outbox.process_outbox(ctx)
            self.assertEqual(ctx.evo.sent, [])


class RelogioAtrasadoTest(unittest.TestCase):
    def test_resposta_nunca_fica_antes_da_ultima_entrada(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        future = ctx.now() + timedelta(minutes=2)   # carimbo do WhatsApp à frente do relógio local
        ctx.db.update_lead(lead["id"], {"last_inbound_at": iso(future)})
        ctx.messenger.send(LEAD_PHONE, "Olá!", lead_id=lead["id"])
        row = ctx.db.get_lead(lead["id"])
        self.assertEqual(row["last_outbound_at"], iso(future + timedelta(seconds=1)))

    def test_relogio_em_dia_mantem_o_relogio_local(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        ctx.clock.advance(seconds=30)
        ctx.messenger.send(LEAD_PHONE, "Olá!", lead_id=lead["id"])
        self.assertEqual(ctx.db.get_lead(lead["id"])["last_outbound_at"], iso(ctx.now()))

    def test_followup_pulado_por_saida_anterior_a_entrada_loga_uma_vez(self):
        ctx = make_ctx()
        lead = silent_lead(ctx, hours_since_inbound=3)
        ctx.db.update_lead(lead["id"], {"last_outbound_at": iso(ctx.now() - timedelta(hours=4))})
        with self.assertLogs(ctx.log, level="WARNING") as logs:
            jobs.followups(ctx)
            jobs.followups(ctx)
        self.assertEqual(len([l for l in logs.output if "follow-up" in l and "relógio" in l]), 1)


if __name__ == "__main__":
    unittest.main()
