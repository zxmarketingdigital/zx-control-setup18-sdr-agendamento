"""Rodada 7 do motor: key.id vazio nunca é sucesso, recover não duplica resposta, 5xx é incerto, opt-out em qualquer
posição, autoresponder avisa o dono, mensagem fora da janela segura a cadência, estágio relido, keyset e opt-out
por telefone na outbox."""

import unittest
from datetime import timedelta

from motor import engine, jobs, outbox
from motor.conversa import apply_decision, Decision, handle_turn, is_opt_out
from motor.db import Database
from motor.evolution import EvolutionError
from motor.util import iso
from tests.motor.support import LEAD_PHONE, OWNER_PHONE, inbound, make_ctx, sp
from tests.motor.test_jobs import silent_lead


def alerts(ctx):
    return [r for r in ctx.db.outbox if r["created_by"] == "alerta"]


class SemKeyIdTest(unittest.TestCase):
    def _no_key(self, ctx, value):
        calls = []

        def send_text(phone, text, delay_ms=0):
            calls.append((phone, text))
            return value
        ctx.evo.send_text = send_text
        return calls

    def test_key_vazia_nao_e_sucesso_nem_chama_on_sent(self):
        for value in (None, ""):
            ctx = make_ctx()
            self._no_key(ctx, value)
            hits = []
            self.assertIsNone(ctx.messenger.send(LEAD_PHONE, "oi", on_sent=hits.append))
            self.assertEqual((ctx.messenger.last_failure, hits, ctx.messenger.sent_ids), ("sem_key_id", [], set()))

    def test_linha_da_outbox_vira_erro_e_nunca_volta_a_pendente(self):
        ctx = make_ctx()
        calls = self._no_key(ctx, None)
        ctx.db.add_outbox(LEAD_PHONE, "oi", created_by="painel")
        self.assertEqual(outbox.process_outbox(ctx), 0)
        self.assertEqual((ctx.db.outbox[0]["status"], ctx.db.outbox[0]["last_error"]),
                         ("erro", "resposta_sem_key_id"))
        self.assertEqual(outbox.process_outbox(ctx), 0)
        self.assertEqual(len([c for c in calls if c[0] == LEAD_PHONE]), 1)   # nunca reenviada

    def test_alerta_ao_dono_uma_vez_por_linha(self):
        # r19: o dedupe é por linha da outbox (antes era um único alerta por processo)
        ctx = make_ctx()
        self._no_key(ctx, "")
        ctx.db.add_outbox(LEAD_PHONE, "oi", created_by="painel")
        ctx.db.add_outbox("+5511999990002", "oi", created_by="painel")
        outbox.process_outbox(ctx)
        self.assertEqual(len(alerts(ctx)), 2)
        self.assertEqual(alerts(ctx)[0]["phone"], OWNER_PHONE)
        outbox.process_outbox(ctx)   # as mesmas linhas (já em erro) não alertam de novo
        self.assertEqual(len(alerts(ctx)), 2)

    def test_followup_sem_key_nao_reverte_o_passo(self):
        ctx = make_ctx()
        lead = silent_lead(ctx, hours_since_inbound=3)
        self._no_key(ctx, None)
        self.assertEqual(jobs.followups(ctx), 0)
        self.assertEqual(ctx.db.leads[lead["id"]]["followup_step"], 1)


class Http5xxIncertoTest(unittest.TestCase):
    def test_502_503_504_sao_incertos_e_nao_reenviam(self):
        for status in (408, 500, 502, 503, 504):
            ctx = make_ctx()

            def boom(*a, **k):
                raise EvolutionError("proxy", status=status)
            ctx.evo.send_text = boom
            ctx.db.add_outbox(LEAD_PHONE, "oi", created_by="painel")
            outbox.process_outbox(ctx)
            self.assertEqual((ctx.db.outbox[0]["status"], ctx.db.outbox[0]["last_error"]),
                             ("erro", "timeout_incerto"), status)

    def test_falha_comum_volta_para_pendente(self):
        # r12: 4xx permanente agora é falha definitiva (ver test_rodada12); a falha transitória (429) segue pendente
        ctx = make_ctx()

        def boom(*a, **k):
            raise EvolutionError("bad", status=429)
        ctx.evo.send_text = boom
        ctx.db.add_outbox(LEAD_PHONE, "oi", created_by="painel")
        outbox.process_outbox(ctx)
        self.assertEqual(ctx.db.outbox[0]["status"], "pendente")

    def test_cliente_http_mapeia_status_e_desconexao(self):
        from unittest import mock
        from motor import http
        from motor.evolution import EvolutionClient, EvolutionTimeout
        import http.client as hc
        client = EvolutionClient("http://evo.local", "k", "i")
        import urllib.error
        import io
        exc = urllib.error.HTTPError("u", 504, "x", {}, io.BytesIO(b"gw"))
        with mock.patch.object(http, "urlopen", side_effect=exc):
            with self.assertRaises(EvolutionError) as caught:
                client.send_text(LEAD_PHONE, "oi")
        self.assertEqual(caught.exception.status, 504)
        with mock.patch.object(http, "urlopen", side_effect=hc.RemoteDisconnected("fechou")):
            with self.assertRaises(EvolutionTimeout):
                client.send_text(LEAD_PHONE, "oi")


class RecoverNaoDuplicaTest(unittest.TestCase):
    def test_evolution_fora_por_tres_ciclos_enfileira_uma_resposta(self):
        ctx = make_ctx()
        ctx.evo.fail_send = True
        lead = inbound(ctx, "quero saber mais")
        for _ in range(3):
            for lead_id in engine.recover_unanswered(ctx):
                handle_turn(ctx, lead_id)
            ctx.clock.advance(minutes=6)
        self.assertEqual(len([r for r in ctx.db.outbox if r["lead_id"] == lead["id"]]), 1)
        self.assertEqual(len(ctx.ai.calls), 1)

    def test_sem_resposta_na_outbox_continua_pendente(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        self.assertEqual(engine.recover_unanswered(ctx), {lead["id"]})

    def test_linha_anterior_a_mensagem_do_lead_nao_conta(self):
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        ctx.db.add_outbox(LEAD_PHONE, "antiga", lead_id=lead["id"], created_by="motor")
        ctx.clock.advance(minutes=10)
        inbound(ctx, "oi de novo")
        self.assertEqual(engine.recover_unanswered(ctx), {lead["id"]})


class OptOutAmplo2Test(unittest.TestCase):
    def test_frases_em_qualquer_posicao(self):
        for text in ("Não tenho interesse, pode me tirar da lista", "pode me tirar da lista", "para de me procurar",
                     "olha, já falei com meu sócio e não tenho interesse, me tira", "obrigado mas não me mande mais nada"):
            self.assertTrue(is_opt_out(text), text)

    def test_negativos_continuam(self):
        for text in ("para quando é a call?", "pode parar o horário das 10?", "tirar dúvida da lista de preços",
                     "meu fornecedor para de mandar boleto toda hora e eu quero resolver isso com vocês",
                     "não mande o link ainda, estou no carro", "não tenho interesse em pós, só em curso"):
            self.assertFalse(is_opt_out(text), text)

    def test_mensagem_longa_demais_nao_conta(self):
        self.assertFalse(is_opt_out("pode me tirar da lista " + "palavra " * 20))


class AutoresponderAlertaTest(unittest.TestCase):
    def test_alerta_o_dono_uma_vez_por_lead(self):
        ctx = make_ctx()
        lead = inbound(ctx, "No momento não podemos atender, mas tenho interesse, me chama amanhã")
        self.assertEqual(handle_turn(ctx, lead["id"])["status"], "autoresponder")
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])
        self.assertEqual(len(alerts(ctx)), 1)
        handle_turn(ctx, lead["id"])
        self.assertEqual(len(alerts(ctx)), 1)


class ForaDaJanelaTest(unittest.TestCase):
    def test_stale_atualiza_last_inbound_e_zera_passo(self):
        ctx = make_ctx()
        lead = silent_lead(ctx, hours_since_inbound=10, followup_step=1)
        ctx.evo.add(LEAD_PHONE, "voltei, ainda tenho interesse", age_seconds=7 * 3600)
        engine.ingest(ctx)
        row = ctx.db.leads[lead["id"]]
        self.assertGreater(row["last_inbound_at"], iso(ctx.now() - timedelta(hours=10)))
        self.assertEqual(row["followup_step"], 0)
        # a saída foi há 10h; a mensagem nova é depois dela: cadência não cobra
        self.assertEqual(jobs.followups(ctx), 0)
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])


class ReleituraDeEstagioTest(unittest.TestCase):
    def _run(self, new_stage, action="qualificar"):
        ctx = make_ctx()
        lead = inbound(ctx, "quero marcar")
        ctx.db.update_lead(lead["id"], {"stage": "respondeu"})
        original = ctx.ai.complete

        def slow(system, messages):
            ctx.db.update_lead(lead["id"], {"stage": new_stage})   # o dono mexeu no painel enquanto a IA pensava
            return original(system, messages)
        ctx.ai.complete = slow
        decision = {"reply": "Ótimo!", "action": action, "qualification": {"interesse": "alto"}}
        if action == "agendar":
            ctx.db.slots.append(sp(19, 14))
            decision["slot"] = iso(sp(19, 14))
        ctx.ai.push(decision)
        result = handle_turn(ctx, lead["id"])
        return ctx, lead, result

    def test_estagio_terminal_aborta_resposta(self):
        for stage in ("perdido", "venda", "desqualificado", "compareceu"):
            ctx, lead, result = self._run(stage)
            self.assertEqual(result["status"], "estagio_mudou", stage)
            self.assertEqual(ctx.db.leads[lead["id"]]["stage"], stage)
            self.assertNotIn("Ótimo!", ctx.evo.texts_to(LEAD_PHONE))

    def test_terminal_aborta_agendamento(self):
        ctx, lead, result = self._run("perdido", action="agendar")
        self.assertEqual(ctx.db.appointments, [])

    def test_estagio_nao_terminal_nao_e_sobrescrito_pelo_snapshot(self):
        ctx, lead, result = self._run("horarios_ofertados")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(ctx.db.leads[lead["id"]]["stage"], "horarios_ofertados")
        self.assertEqual(ctx.db.leads[lead["id"]]["qualification"].get("interesse"), "alto")


class ActiveLeadsKeysetTest(unittest.TestCase):
    def test_keyset_com_insercao_no_meio_nao_duplica(self):
        db = Database("https://x.supabase.co", "k")
        calls = []

        def fake_select(table, params=None):
            calls.append(dict(params))
            if len(calls) > 5:
                raise AssertionError("paginação não é por keyset (id > último)")
            if "id" not in params:
                return [{"id": "a"}, {"id": "b"}]
            if params["id"] == "gt.b":
                return [{"id": "b"}, {"id": "c"}]   # o servidor devolveu o da fronteira de novo
            return [{"id": "c"}]
        db.select = fake_select
        rows = db.active_leads(limit=2)
        self.assertEqual([r["id"] for r in rows], ["a", "b", "c"])
        self.assertTrue(all("offset" not in c for c in calls))
        self.assertEqual(calls[0]["order"], "id.asc")


class OutboxSemLeadIdTest(unittest.TestCase):
    def test_opt_out_vale_pelo_telefone(self):
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        ctx.db.update_lead(lead["id"], {"opt_out": True})
        row = ctx.db.add_outbox(LEAD_PHONE, "promo", lead_id=None, created_by="painel")
        self.assertFalse(outbox.send_row(ctx, row))
        self.assertEqual(ctx.evo.sent, [])
        self.assertEqual(ctx.db.outbox[0]["status"], "erro")

    def test_sem_lead_conhecido_envia_normal(self):
        ctx = make_ctx()
        row = ctx.db.add_outbox(LEAD_PHONE, "oi", lead_id=None, created_by="painel")
        self.assertTrue(outbox.send_row(ctx, row))

    def test_destino_interno_continua_isento(self):
        ctx = make_ctx()
        owner = ctx.db.create_lead(OWNER_PHONE, name="Dono")
        ctx.db.update_lead(owner["id"], {"opt_out": True})
        row = ctx.db.add_outbox(OWNER_PHONE, "aviso", lead_id=None, created_by="alerta")
        self.assertTrue(outbox.send_row(ctx, row))


if __name__ == "__main__":
    unittest.main()
