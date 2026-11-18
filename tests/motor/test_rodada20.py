"""Rodada 20 do motor: laço entre robôs, alertas marcados só depois de enfileirar, marcador de envio incerto que
nasce em 'erro', retomada que só conta falha de IA/agenda, reversão do follow-up por compare-and-set e expiração
dos wa_ids que a Evolution não devolve mais. Tudo com Evolution/banco/IA falsos."""

import unittest
from datetime import timedelta
from unittest import mock

from motor import conversa, engine, jobs, outbox
from motor.conversa import handle_turn
from motor.evolution import EvolutionError
from motor.util import iso
from tests.motor.support import LEAD_PHONE, NOW, OWNER_PHONE, inbound, make_ctx
from tests.motor.test_jobs import silent_lead


def alerts(ctx, needle):
    return [t for t in ctx.evo.texts_to(OWNER_PHONE) if needle in t]


def fail_enqueue_once(ctx):
    """O próximo enqueue_outbox levanta (banco fora); os seguintes funcionam."""
    real, state = ctx.db.enqueue_outbox, {"n": 0}

    def enqueue(*args, **kwargs):
        if state["n"] == 0:
            state["n"] = 1
            raise RuntimeError("banco fora")
        return real(*args, **kwargs)
    ctx.db.enqueue_outbox = enqueue


class LacoEntreRobosTest(unittest.TestCase):
    ROBO = "Olá! Como posso ajudar?"

    def test_mesmo_texto_tres_vezes_pausa_e_avisa_uma_vez_sem_chamar_a_ia(self):
        ctx = make_ctx()
        results = []
        for _ in range(3):
            lead = inbound(ctx, self.ROBO)
            ctx.ai.push({"reply": "Oi! Sou a Ana.", "action": "responder"})
            results.append(handle_turn(ctx, lead["id"])["status"])
            ctx.clock.advance(seconds=30)
        self.assertEqual(results, ["ok", "ok", "laco_pausado"])
        self.assertEqual(len(ctx.ai.calls), 2)   # a IA não foi chamada na 3ª
        self.assertTrue(conversa.parse_ts(ctx.db.leads[lead["id"]]["takeover_until"]) > ctx.now() + timedelta(hours=23))
        self.assertEqual(len(alerts(ctx, "parece outro robô")), 1)
        # a próxima mensagem cai no takeover: nenhum alerta novo
        inbound(ctx, self.ROBO)
        self.assertEqual(handle_turn(ctx, lead["id"])["status"], "takeover")
        self.assertEqual(len(alerts(ctx, "parece outro robô")), 1)

    def test_normaliza_caixa_espaco_e_pontuacao_final(self):
        ctx = make_ctx()
        for text in ("Olá! Como posso ajudar?", "olá!   como posso   ajudar", "OLÁ! COMO POSSO AJUDAR???"):
            lead = inbound(ctx, text)
            if text.islower():   # r25: só é laço se o agente respondeu entre as repetições
                ctx.db.add_message(lead["id"], "out", "agente", "Oi!")
        ctx.ai.push({"reply": "Oi!", "action": "responder"})
        self.assertEqual(handle_turn(ctx, lead["id"])["status"], "laco_pausado")
        self.assertEqual(ctx.ai.calls, [])

    def test_vinte_respostas_na_ultima_hora_pausam(self):
        ctx = make_ctx()
        lead = inbound(ctx, "quero marcar")
        for i in range(conversa.MAX_RESPOSTAS_POR_HORA):
            ctx.db.add_message(lead["id"], "out", "agente", f"resposta {i}", created_at=ctx.now() - timedelta(minutes=i + 1))
        ctx.db.add_message(lead["id"], "in", "lead", "e agora?")
        self.assertEqual(handle_turn(ctx, lead["id"])["status"], "laco_pausado")
        self.assertEqual(ctx.ai.calls, [])
        self.assertEqual(len(alerts(ctx, "parece outro robô")), 1)

    def test_dezenove_respostas_ou_fora_da_hora_nao_pausam(self):
        ctx = make_ctx()
        lead = inbound(ctx, "quero marcar")
        for i in range(conversa.MAX_RESPOSTAS_POR_HORA - 1):
            ctx.db.add_message(lead["id"], "out", "agente", f"r{i}", created_at=ctx.now() - timedelta(minutes=i + 1))
        for i in range(10):   # antigas: fora da janela de 1 h
            ctx.db.add_message(lead["id"], "out", "agente", f"velha{i}", created_at=ctx.now() - timedelta(hours=2, minutes=i))
        ctx.db.add_message(lead["id"], "in", "lead", "e agora?")
        ctx.ai.push({"reply": "Claro!", "action": "responder"})
        self.assertEqual(handle_turn(ctx, lead["id"])["status"], "ok")
        self.assertEqual(len(ctx.ai.calls), 1)

    def test_conversa_normal_com_textos_diferentes_nao_pausa(self):
        ctx = make_ctx()
        for text in ("oi", "quero saber o preço", "pode ser amanhã?", "ok"):
            lead = inbound(ctx, text)
            ctx.ai.push({"reply": "Certo.", "action": "responder"})
            self.assertEqual(handle_turn(ctx, lead["id"])["status"], "ok")
            ctx.clock.advance(seconds=30)
        self.assertEqual(alerts(ctx, "parece outro robô"), [])

    def test_aviso_que_falha_ao_enfileirar_nao_gasta_a_marca(self):
        ctx = make_ctx()
        for _ in range(3):
            lead = inbound(ctx, self.ROBO)
            if _ < 2:   # r25: só é laço se o agente respondeu entre as repetições
                ctx.db.add_message(lead["id"], "out", "agente", "Oi!")
        fail_enqueue_once(ctx)
        self.assertEqual(handle_turn(ctx, lead["id"])["status"], "laco_pausado")
        self.assertFalse(ctx.state.get("laco_alerted"))
        self.assertTrue(ctx.db.leads[lead["id"]]["takeover_until"])   # o takeover foi gravado antes de tudo


class AlertaSoDepoisDeEnfileirarTest(unittest.TestCase):
    def test_envio_incerto_ao_lead(self):
        ctx = make_ctx()
        lead = inbound(ctx)
        fail_enqueue_once(ctx)
        outbox._alert_uncertain_lead_send(ctx, LEAD_PHONE, "Confirmado!", lead["id"])
        self.assertEqual(alerts(ctx, "Não tenho certeza"), [])
        outbox._alert_uncertain_lead_send(ctx, LEAD_PHONE, "Confirmado!", lead["id"])   # próximo ciclo
        self.assertEqual(len(alerts(ctx, "Não tenho certeza")), 1)
        outbox._alert_uncertain_lead_send(ctx, LEAD_PHONE, "Confirmado!", lead["id"])
        self.assertEqual(len(alerts(ctx, "Não tenho certeza")), 1)

    def test_sem_key_id(self):
        ctx = make_ctx()
        fail_enqueue_once(ctx)
        outbox._alert_no_key_once(ctx, LEAD_PHONE, "painel", 7)
        self.assertEqual(alerts(ctx, "sem confirmação"), [])
        outbox._alert_no_key_once(ctx, LEAD_PHONE, "painel", 7)
        self.assertEqual(len(alerts(ctx, "sem confirmação")), 1)

    def test_contato_lid_sem_numero(self):
        ctx = make_ctx()
        record = {"message": {"conversation": "oi"}}
        key = {"remoteJid": "98765432109876@lid"}
        call = lambda: engine._warn_unresolved_lid(ctx, record, key, ctx.now(), ctx.now(), timedelta(hours=6), False)
        fail_enqueue_once(ctx)
        call()
        self.assertEqual(alerts(ctx, "identificador oculto"), [])
        call()
        self.assertEqual(len(alerts(ctx, "identificador oculto")), 1)

    def test_mensagem_nao_suportada(self):
        ctx = make_ctx()
        lead = inbound(ctx)
        fail_enqueue_once(ctx)
        engine._alert_unsupported(ctx, lead, LEAD_PHONE)
        self.assertEqual(alerts(ctx, "não consegue ler"), [])
        engine._alert_unsupported(ctx, lead, LEAD_PHONE)
        self.assertEqual(len(alerts(ctx, "não consegue ler")), 1)

    def test_estagio_final(self):
        ctx = make_ctx()
        lead = inbound(ctx)
        ctx.evo.fail_send = True   # notify_owner cai na outbox, que também falha: a exceção sobe
        fail_enqueue_once(ctx)
        conversa._alert_final_stage(ctx, lead, ctx.now())
        self.assertEqual(ctx.state.get("final_alerted", {}), {})
        ctx.evo.fail_send = False
        conversa._alert_final_stage(ctx, lead, ctx.now())
        self.assertEqual(len(alerts(ctx, "escreveu de novo")), 1)

    def test_falha_permanente_de_leitura_e_reenfileirada_no_ciclo_seguinte(self):
        ctx = make_ctx()
        record = {"key": {"remoteJid": f"{LEAD_PHONE.lstrip('+')}@s.whatsapp.net", "id": "W1"}}
        fail_enqueue_once(ctx)
        engine._fail_permanently(ctx, record, "W1", 3)
        self.assertEqual(alerts(ctx, "não pôde ser lida"), [])
        engine.ingest(ctx)   # próximo ciclo
        self.assertEqual(len(alerts(ctx, "não pôde ser lida")), 1)
        engine.ingest(ctx)
        self.assertEqual(len(alerts(ctx, "não pôde ser lida")), 1)


class MarcadorIncertoNasceEmErroTest(unittest.TestCase):
    def test_linha_nasce_erro_e_nunca_e_enviada_mesmo_com_claim_e_finish_falhando(self):
        ctx = make_ctx()
        lead = inbound(ctx)
        ctx.db.claim_outbox = lambda row: (_ for _ in ()).throw(RuntimeError("PATCH falhou"))
        ctx.db.finish_outbox = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("PATCH falhou"))
        outbox.record_uncertain_send(ctx, LEAD_PHONE, "Confirmado!", lead["id"], "timeout_incerto")
        rows = [r for r in ctx.db.outbox if r["body"] == "Confirmado!"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["status"], rows[0]["last_error"]), ("erro", "timeout_incerto"))
        del ctx.db.claim_outbox, ctx.db.finish_outbox   # banco de volta: o ciclo da outbox roda normal
        outbox.process_outbox(ctx)
        self.assertNotIn("Confirmado!", ctx.evo.texts_to(LEAD_PHONE))


class RetomadaStatusTest(unittest.TestCase):
    def _run(self, status, rounds=4):
        ctx = make_ctx()
        lead = silent_lead(ctx, qualification={"_retomar": {"em": iso(NOW - timedelta(minutes=5))}})
        with mock.patch.object(jobs, "handle_turn", lambda *a, **k: {"status": status}):
            for _ in range(rounds):
                jobs.retomar(ctx)
                ctx.clock.advance(minutes=10)
        return ctx, ctx.db.leads[lead["id"]]

    def test_status_que_encerram_removem_o_marcador_sem_alerta(self):
        for status in ("takeover", "opt_out_registrado", "opt_out_confirmado", "estagio_mudou", "laco_pausado"):
            ctx, lead = self._run(status, rounds=1)
            self.assertNotIn("_retomar", lead["qualification"], status)
            self.assertEqual(alerts(ctx, "retomada combinada"), [], status)

    def test_falha_da_ia_conta_e_avisa_ia_fora_do_ar(self):
        ctx, lead = self._run("ia_falhou")
        self.assertNotIn("_retomar", lead["qualification"])
        found = alerts(ctx, "retomada combinada")
        self.assertEqual(len(found), 1)
        self.assertIn("IA está fora do ar", found[0])

    def test_agenda_falhou_conta_e_o_alerta_fala_da_agenda(self):
        ctx, lead = self._run("agenda_falhou")
        found = alerts(ctx, "retomada combinada")
        self.assertEqual(len(found), 1)
        self.assertIn("agenda está indisponível", found[0])
        self.assertNotIn("IA", found[0])


class FollowupReversaoTest(unittest.TestCase):
    def test_lead_que_respondeu_durante_o_envio_nao_tem_o_passo_devolvido(self):
        ctx = make_ctx()
        lead = silent_lead(ctx, hours_since_inbound=25, followup_step=1)
        real = ctx.evo.send_text

        def send_text(phone, text, delay_ms=0):
            # o lead responde durante o envio: a ingestão zera followup_step e atualiza last_inbound_at
            ctx.db.update_lead(lead["id"], {"followup_step": 0, "last_inbound_at": iso(ctx.now())})
            raise EvolutionError("Evolution fora do ar")
        ctx.evo.send_text = send_text
        self.assertEqual(jobs.followups(ctx), 0)
        self.assertEqual(ctx.db.leads[lead["id"]]["followup_step"], 0)
        ctx.evo.send_text = real

    def test_sem_resposta_do_lead_a_falha_transitoria_ainda_reverte(self):
        ctx = make_ctx()
        lead = silent_lead(ctx, hours_since_inbound=25, followup_step=1)
        ctx.evo.fail_send = True
        self.assertEqual(jobs.followups(ctx), 0)
        self.assertEqual(ctx.db.leads[lead["id"]]["followup_step"], 1)


class FailedExpiraTest(unittest.TestCase):
    def _failed(self, ctx):
        ctx.state["failed_wa_ids"] = {"GONE"}
        ctx.state["failed_wa_info"] = {"GONE": {"since": ctx.now(), "phone": LEAD_PHONE, "ausente": 0}}

    def test_expira_ao_passar_da_janela_de_catchup(self):
        ctx = make_ctx()
        self._failed(ctx)
        ctx.clock.advance(minutes=ctx.cfg.catchup_minutes + 5)
        engine.ingest(ctx)
        self.assertNotIn("GONE", ctx.state["failed_wa_ids"])
        self.assertEqual(len(alerts(ctx, "não pôde ser lida")), 1)
        engine.ingest(ctx)
        self.assertEqual(len(alerts(ctx, "não pôde ser lida")), 1)

    def test_expira_depois_de_30_ciclos_sem_reaparecer(self):
        ctx = make_ctx()
        for i in range(engine.POLL_LIMIT * engine.MAX_POLL_PAGES):
            ctx.evo.add(LEAD_PHONE, "x", wa_id=f"G{i}", jid="120363000000@g.us")   # grupo: nunca vira lead
        engine._read_records(ctx)   # aquece o `seen` (grupos já examinados)
        self._failed(ctx)
        for _ in range(engine.FAILED_MAX_CICLOS_AUSENTE - 1):
            engine._read_records(ctx)
        self.assertIn("GONE", ctx.state["failed_wa_ids"])
        self.assertEqual(alerts(ctx, "não pôde ser lida"), [])
        engine._read_records(ctx)
        self.assertNotIn("GONE", ctx.state["failed_wa_ids"])
        self.assertEqual(len(alerts(ctx, "não pôde ser lida")), 1)


if __name__ == "__main__":
    unittest.main()
