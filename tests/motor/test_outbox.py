"""Outbox: a linha só vira 'enviado' com o key.id da Evolution; falhas, limite e queda no meio do envio."""

import unittest
from unittest import mock

from motor import http
from motor.evolution import EvolutionClient, EvolutionError
from motor.outbox import MAX_ATTEMPTS, Messenger, notify_owner, process_outbox, recover_stuck_outbox
from motor.ratelimit import RateLimiter
from tests.motor.support import (LEAD_PHONE, OWNER_PHONE, TEST_PHONE, default_agent, inbound, make_ctx)


class FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ProcessOutboxTest(unittest.TestCase):
    def test_timeout_no_envio_vira_erro_incerto_e_nao_reenvia(self):
        ctx = make_ctx()
        ctx.evo.timeout_send = True
        ctx.db.add_outbox(LEAD_PHONE, "oi", created_by="painel")
        self.assertEqual(process_outbox(ctx), 0)
        row = ctx.db.outbox[0]
        self.assertEqual((row["status"], row["last_error"]), ("erro", "timeout_incerto"))
        ctx.evo.timeout_send = False
        self.assertEqual(process_outbox(ctx), 0)
        self.assertEqual(ctx.evo.sent, [])

    def test_resposta_enfileirada_antes_do_opt_out_nao_sai_depois_dele(self):
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        ctx.db.add_outbox(LEAD_PHONE, "resposta de venda", lead_id=lead["id"], created_by="motor")
        from motor.outbox import OPT_OUT_CONFIRMATION
        ctx.db.add_outbox(LEAD_PHONE, OPT_OUT_CONFIRMATION, lead_id=lead["id"], created_by="optout")
        ctx.db.update_lead(lead["id"], {"opt_out": True})
        process_outbox(ctx)
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [OPT_OUT_CONFIRMATION])
        self.assertEqual(ctx.db.outbox[0]["status"], "erro")

    def test_alerta_ao_dono_pela_outbox(self):
        from motor.outbox import queue_owner_alert
        ctx = make_ctx()
        queue_owner_alert(ctx, "algo")
        self.assertEqual(ctx.db.outbox[0]["created_by"], "alerta")
        self.assertEqual(ctx.db.outbox[0]["phone"], OWNER_PHONE)
        self.assertEqual(ctx.evo.texts_to(OWNER_PHONE), ["algo"])

    def test_painel_enfileira_e_o_motor_envia_com_prova(self):
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE)
        ctx.db.add_outbox(LEAD_PHONE, "Oi, aqui é a equipe!", lead_id=lead["id"], created_by="painel")
        self.assertEqual(process_outbox(ctx), 1)
        row = ctx.db.outbox[0]
        self.assertEqual((row["status"], row["wa_key_id"], row["attempts"]), ("enviado", "OUT1", 1))
        self.assertIsNotNone(row["sent_at"])
        sent = ctx.db.messages[0]
        self.assertEqual((sent["author"], sent["wa_id"], sent["direction"]), ("dono", "OUT1", "out"))

    def test_pagina_publica_confirmacao_sai_como_sistema(self):
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE)
        ctx.db.add_outbox(LEAD_PHONE, "Sua conversa está confirmada.", lead_id=lead["id"], created_by="pagina")
        process_outbox(ctx)
        self.assertEqual(ctx.db.messages[0]["author"], "sistema")

    def test_falha_de_envio_nao_marca_enviado_e_tenta_de_novo(self):
        ctx = make_ctx()
        ctx.evo.fail_send = True
        ctx.db.add_outbox(LEAD_PHONE, "oi", created_by="painel")
        self.assertEqual(process_outbox(ctx), 0)
        row = ctx.db.outbox[0]
        self.assertEqual((row["status"], row["attempts"]), ("pendente", 1))
        self.assertIn("Evolution fora do ar", row["last_error"])
        self.assertIsNone(row["wa_key_id"])
        ctx.evo.fail_send = False
        ctx.clock.advance(seconds=31)   # r13: a nova tentativa espera o backoff (30s na 1ª falha)
        self.assertEqual(process_outbox(ctx), 1)
        self.assertEqual(ctx.db.outbox[0]["status"], "enviado")

    def test_depois_de_cinco_falhas_vira_erro_e_para_de_tentar(self):
        ctx = make_ctx()
        ctx.evo.fail_send = True
        ctx.db.add_outbox(LEAD_PHONE, "oi", created_by="painel")
        for _ in range(MAX_ATTEMPTS):
            process_outbox(ctx)
            ctx.clock.advance(hours=2)   # r13: cada tentativa espera o backoff
        row = ctx.db.outbox[0]
        self.assertEqual((row["status"], row["attempts"]), ("erro", MAX_ATTEMPTS))
        process_outbox(ctx)
        self.assertEqual(ctx.db.outbox[0]["attempts"], MAX_ATTEMPTS)

    def test_evolution_sem_key_id_nunca_vira_enviado(self):
        ctx = make_ctx()
        client = EvolutionClient("http://evo.local", "chave", "inst")
        limiter = RateLimiter(ctx.cfg.data_dir / "rl2.db", max_per_minute=100, now_fn=ctx.clock, min_interval_seconds=0)
        ctx.messenger = Messenger(client, limiter, ctx.db, ctx.log, ctx.clock, ctx.sleep_fn, lambda: ctx.state.get("agent"))
        ctx.db.add_outbox(LEAD_PHONE, "oi", created_by="painel")
        with mock.patch.object(http, "urlopen", return_value=FakeResponse(b'{"status":"PENDING"}')):
            self.assertEqual(process_outbox(ctx), 0)
        row = ctx.db.outbox[0]
        # r7: sem key.id a mensagem pode ter saído: erro visível, NUNCA volta a pendente nem é reenviada
        self.assertEqual((row["status"], row["last_error"]), ("erro", "resposta_sem_key_id"))
        with mock.patch.object(http, "urlopen", return_value=FakeResponse(b'{"key":{"id":"3EB0ABC"},"status":"PENDING"}')):
            self.assertEqual(process_outbox(ctx), 0)
        self.assertEqual(ctx.db.outbox[0]["status"], "erro")

    def test_cliente_evolution_exige_key_id(self):
        client = EvolutionClient("http://evo.local", "chave", "inst")
        with mock.patch.object(http, "urlopen", return_value=FakeResponse(b"{}")):
            with self.assertRaises(EvolutionError):
                client.send_text(LEAD_PHONE, "oi")

    def test_outro_processo_pegou_a_linha_primeiro(self):
        ctx = make_ctx()
        ctx.db.lose_claim = True
        ctx.db.add_outbox(LEAD_PHONE, "oi", created_by="painel")
        self.assertEqual(process_outbox(ctx), 0)
        self.assertEqual(ctx.evo.sent, [])

    def test_limite_de_envio_devolve_a_linha_sem_gastar_tentativa(self):
        ctx = make_ctx()
        ctx.messenger.limiter = RateLimiter(ctx.cfg.data_dir / "rl3.db", max_per_minute=1, now_fn=ctx.clock,
                                            min_interval_seconds=0)
        ctx.db.add_outbox(LEAD_PHONE, "primeira", created_by="painel")
        ctx.db.add_outbox(LEAD_PHONE, "segunda", created_by="painel")
        self.assertEqual(process_outbox(ctx), 1)
        second = ctx.db.outbox[1]
        self.assertEqual((second["status"], second["attempts"]), ("pendente", 0))
        ctx.clock.advance(seconds=120)
        self.assertEqual(process_outbox(ctx), 1)

    def test_modo_teste_segura_a_fila_dos_leads_mas_libera_alerta_ao_dono(self):
        ctx = make_ctx(default_agent(test_only=True, test_phones=[TEST_PHONE]))
        ctx.db.add_outbox(LEAD_PHONE, "para um lead de verdade", created_by="painel")
        ctx.db.add_outbox(OWNER_PHONE, "alerta", created_by="alerta")
        self.assertEqual(process_outbox(ctx), 1)
        self.assertEqual([r["status"] for r in ctx.db.outbox], ["pendente", "enviado"])
        self.assertEqual(ctx.db.outbox[0]["attempts"], 0)
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])

    def test_rotulo_alerta_para_numero_de_lead_nao_fura_o_modo_teste(self):
        ctx = make_ctx(default_agent(test_only=True, test_phones=[TEST_PHONE]))
        ctx.db.add_outbox(LEAD_PHONE, "fingindo ser alerta", created_by="alerta")
        self.assertEqual(process_outbox(ctx), 0)
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])

    def test_queda_no_meio_do_envio_nao_reenvia_sozinha(self):
        ctx = make_ctx()
        ctx.db.add_outbox(LEAD_PHONE, "oi", created_by="painel", status="enviando", attempts=1)
        self.assertEqual(recover_stuck_outbox(ctx), 1)
        row = ctx.db.outbox[0]
        self.assertEqual(row["status"], "erro")
        self.assertIn("confira no WhatsApp", row["last_error"])
        process_outbox(ctx)
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])


class NotifyOwnerTest(unittest.TestCase):
    def test_aviso_direto(self):
        ctx = make_ctx()
        self.assertTrue(notify_owner(ctx, "oi dono"))
        self.assertEqual(ctx.evo.texts_to(OWNER_PHONE), ["oi dono"])

    def test_sem_conexao_o_aviso_espera_na_outbox_e_sai_quando_volta(self):
        ctx = make_ctx()
        ctx.evo.fail_send = True
        self.assertFalse(notify_owner(ctx, "A instância caiu"))
        self.assertEqual([(r["created_by"], r["status"]) for r in ctx.db.outbox], [("alerta", "pendente")])
        ctx.evo.fail_send = False
        process_outbox(ctx)
        self.assertEqual(ctx.evo.texts_to(OWNER_PHONE), ["A instância caiu"])
        self.assertEqual(ctx.db.outbox[0]["status"], "enviado")

    def test_sem_telefone_do_dono_nao_quebra(self):
        ctx = make_ctx(default_agent(owner_phone=""))
        self.assertFalse(notify_owner(ctx, "x"))
        self.assertEqual(ctx.db.outbox, [])


if __name__ == "__main__":
    unittest.main()
