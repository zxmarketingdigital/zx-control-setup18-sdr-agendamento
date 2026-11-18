"""Rodada 12 do motor: leitura direcionada não perde mensagem nova, modo teste não libera closer, falha definitiva
da outbox, timeout da ponte do Google Agenda como resultado incerto e checagem de horário só em contexto de agenda."""

import unittest
import urllib.error
from datetime import timedelta
from unittest import mock

from motor import agenda, engine, outbox
from motor.conversa import handle_turn
from motor.evolution import EvolutionError
from motor.util import iso
from tests.motor.support import CLOSER_PHONE, LEAD_PHONE, OWNER_PHONE, default_agent, inbound, make_ctx, sp
from tests.motor.test_clients import Net
from tests.motor.test_jobs import book

OTHER_PHONE = "+5511999990055"


def owner_rows(ctx):
    return [r for r in ctx.db.outbox if r["phone"] == OWNER_PHONE]


class LeituraDirecionadaNaoDescartaTest(unittest.TestCase):
    def test_segunda_mensagem_so_na_evolution_roda_o_turno_de_novo(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        original = ctx.ai.complete
        fired = []

        def slow(system, messages):
            if not fired:
                fired.append(1)
                ctx.evo.add(LEAD_PHONE, "quero marcar amanhã", wa_id="SEG2")
            return original(system, messages)
        ctx.ai.complete = slow
        ctx.ai.push({"reply": "Oi! O que você procura?", "action": "responder"},
                    {"reply": "Claro, vou ver os horários.", "action": "responder"})
        ctx.state["pending"] = {lead["id"]}
        engine.cycle(ctx)
        self.assertEqual(len(ctx.ai.calls), 1)
        self.assertIn(lead["id"], ctx.state.get("pending", set()))   # a mensagem nova não se perde
        engine.cycle(ctx)
        self.assertEqual(len(ctx.ai.calls), 2)                        # o turno rodou de novo para esse lead
        self.assertEqual(ctx.state.get("pending", set()), set())

    def test_lead_de_outro_telefone_devolvido_pelo_ingest_entra_no_pending(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        with mock.patch.object(engine, "ingest_chat", return_value={"outro-lead"}):
            ctx.ai.push({"reply": "Olá!", "action": "responder"})
            handle_turn(ctx, lead["id"])
        self.assertIn("outro-lead", ctx.state["pending"])

    def test_sem_mensagem_nova_nada_entra_no_pending(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        ctx.ai.push({"reply": "Olá!", "action": "responder"})
        handle_turn(ctx, lead["id"])
        self.assertEqual(ctx.state.get("pending", set()), set())


class ModoTesteNaoLiberaCloserTest(unittest.TestCase):
    def _ctx(self, **agent):
        ctx = make_ctx(default_agent(test_only=True, test_phones=[LEAD_PHONE], **agent))
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        appt = book(ctx, lead, sp(19, 14))
        booked = {"appointment_id": appt["id"], "starts_at": appt["starts_at"], "ends_at": appt["ends_at"],
                  "closer_id": "c1"}
        return ctx, lead, booked

    def test_alerta_de_agenda_em_modo_teste_vai_ao_dono_com_prefixo_e_nunca_ao_closer(self):
        ctx, lead, booked = self._ctx()
        agenda._alert_gcal_create_failed(ctx, lead, booked)
        self.assertEqual(ctx.evo.texts_to(CLOSER_PHONE), [])
        self.assertFalse([r for r in ctx.db.outbox if r["phone"] == CLOSER_PHONE and r["status"] == "enviado"])
        sent = ctx.evo.texts_to(OWNER_PHONE)
        self.assertTrue(any(t.startswith("[TESTE] ") for t in sent), sent)

    def test_pos_reserva_e_cancelamento_gcal_tambem(self):
        ctx, lead, booked = self._ctx()
        agenda._post_booking_step(ctx, lead, booked, "criar algo", lambda: 1 / 0)
        agenda._alert_gcal_cancel_failed(ctx, ctx.db.appointments[0])
        self.assertEqual(ctx.evo.texts_to(CLOSER_PHONE), [])
        self.assertEqual(len([t for t in ctx.evo.texts_to(OWNER_PHONE) if t.startswith("[TESTE] ")]), 2)

    def test_closer_em_test_phones_recebe_normalmente(self):
        ctx, lead, booked = self._ctx()
        ctx.state["agent"]["test_phones"] = [LEAD_PHONE, CLOSER_PHONE]
        agenda._alert_gcal_create_failed(ctx, lead, booked)
        closer_texts = ctx.evo.texts_to(CLOSER_PHONE)
        self.assertEqual(len(closer_texts), 1)
        self.assertFalse(closer_texts[0].startswith("[TESTE]"))

    def test_internal_nao_e_liberacao_generica_em_modo_teste(self):
        ctx, _, _ = self._ctx()
        self.assertFalse(ctx.messenger.allowed(CLOSER_PHONE, internal=True))
        self.assertTrue(ctx.messenger.allowed(OWNER_PHONE, internal=True))
        self.assertTrue(ctx.messenger.allowed(LEAD_PHONE))

    def test_ao_vivo_nada_muda(self):
        ctx = make_ctx(default_agent(test_only=False))
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        appt = book(ctx, lead, sp(19, 14))
        booked = {"appointment_id": appt["id"], "starts_at": appt["starts_at"], "ends_at": appt["ends_at"],
                  "closer_id": "c1"}
        agenda._alert_gcal_create_failed(ctx, lead, booked)
        self.assertEqual(len(ctx.evo.texts_to(CLOSER_PHONE)), 1)
        self.assertFalse(ctx.evo.texts_to(CLOSER_PHONE)[0].startswith("[TESTE]"))


class FalhaDefinitivaDaOutboxTest(unittest.TestCase):
    def _fail(self, ctx, status):
        def boom(*a, **k):
            raise EvolutionError("número inexistente", status=status)
        ctx.evo.send_text = boom

    def test_4xx_vira_erro_com_um_alerta_ao_dono(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        self._fail(ctx, 400)
        ctx.db.add_outbox(LEAD_PHONE, "Olá, tudo bem? esta é a resposta", lead_id=lead["id"], created_by="motor")
        outbox.process_outbox(ctx)
        row = ctx.db.outbox[0]
        self.assertEqual(row["status"], "erro")
        self.assertTrue(row["last_error"].startswith("falha_definitiva"))
        alerts = [r for r in owner_rows(ctx) if r["created_by"] == "alerta"]
        self.assertEqual(len(alerts), 1)
        for word in ("Joana", LEAD_PHONE, "Olá, tudo bem?", "400"):
            self.assertIn(word, alerts[0]["body"])

    def test_tentativas_esgotadas_alertam_uma_vez(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        self._fail(ctx, 429)
        ctx.db.add_outbox(LEAD_PHONE, "resposta", lead_id=lead["id"], created_by="motor")
        for _ in range(outbox.MAX_ATTEMPTS + 2):
            outbox.process_outbox(ctx)
            ctx.clock.advance(hours=2)   # r13: espera do backoff entre as tentativas
        self.assertEqual(ctx.db.outbox[0]["status"], "erro")
        self.assertEqual(len([r for r in owner_rows(ctx) if r["created_by"] == "alerta"]), 1)

    def test_linha_de_alerta_que_falha_nao_gera_alerta_de_alerta(self):
        ctx = make_ctx()
        self._fail(ctx, 400)
        outbox.queue_owner_alert(ctx, "algo")
        outbox.process_outbox(ctx)
        self.assertEqual(len(ctx.db.outbox), 1)

    def test_recover_nao_chama_a_ia_de_novo_depois_de_erro_definitivo(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        self._fail(ctx, 400)
        ctx.ai.push({"reply": "Olá!", "action": "responder"})
        handle_turn(ctx, lead["id"])
        outbox.process_outbox(ctx)
        self.assertEqual(ctx.db.outbox[0]["status"], "erro")
        ctx.db.update_lead(lead["id"], {"last_outbound_at": None})
        ctx.clock.advance(minutes=1)
        self.assertEqual(engine.recover_unanswered(ctx), set())
        self.assertEqual(len(ctx.ai.calls), 1)


class GcalTimeoutIncertoTest(unittest.TestCase):
    def test_timeout_na_criacao_alerta_pode_ter_sido_criado_e_envia_appointment_id(self):
        ctx = make_ctx()
        ctx.gcal = agenda.GoogleCalendar("https://script.example/exec", "segredo", ctx.log)
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        appt = book(ctx, lead, sp(19, 14))
        booked = {"appointment_id": appt["id"], "starts_at": appt["starts_at"], "ends_at": appt["ends_at"],
                  "closer_id": "c1"}
        with Net(urllib.error.URLError(TimeoutError("timed out"))) as net:
            agenda._gcal_create(ctx, lead, booked, {"agent_name": "Ana"})
        self.assertEqual(len(net.calls), 1)                       # sem retentativa automática
        self.assertEqual(net.calls[0]["body"]["appointment_id"], f"{appt['id']}:{appt['starts_at']}")   # r14: id + horário
        owner = [r for r in owner_rows(ctx) if r["created_by"] == "alerta"]
        self.assertEqual(len(owner), 1)
        for word in ("pode ter sido criado", "confira a agenda", "Joana", "19", "Marcos"):
            self.assertIn(word, owner[0]["body"])
        self.assertNotIn("manualmente", owner[0]["body"])

    def test_resposta_ok_false_continua_pedindo_insercao_manual(self):
        ctx = make_ctx()
        ctx.gcal = agenda.GoogleCalendar("https://script.example/exec", "segredo", ctx.log)
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        appt = book(ctx, lead, sp(19, 14))
        booked = {"appointment_id": appt["id"], "starts_at": appt["starts_at"], "ends_at": appt["ends_at"],
                  "closer_id": "c1"}
        with Net({"ok": False, "error": "x"}):
            agenda._gcal_create(ctx, lead, booked, {})
        body = [r for r in owner_rows(ctx) if r["created_by"] == "alerta"][0]["body"]
        self.assertIn("manualmente", body)


class HorarioInventadoSoEmContextoDeAgendaTest(unittest.TestCase):
    def _turn(self, text, decision):
        ctx = make_ctx()
        lead = inbound(ctx, text)
        ctx.ai.push(decision)
        return ctx, handle_turn(ctx, lead["id"])

    def test_pergunta_de_horario_de_atendimento_mantem_a_resposta(self):
        ctx, result = self._turn("vocês atendem até que horas?",
                                 {"reply": "Atendemos de segunda a sexta, até as 18h, sem fila de espera.",
                                  "action": "responder"})
        self.assertEqual(result["action"], "responder")
        text = ctx.evo.texts_to(LEAD_PHONE)[0]
        self.assertEqual(text, "Atendemos de segunda a sexta, até as 18h, sem fila de espera.")   # r19: informativo passa
        self.assertNotIn("•", text)

    def test_sentido_esvaziado_vira_texto_neutro(self):
        ctx, result = self._turn("até que horas?", {"reply": "Das 9h às 18h", "action": "responder"})
        self.assertEqual(result["action"], "responder")
        text = ctx.evo.texts_to(LEAD_PHONE)[0]
        self.assertEqual(text, "Das 9h às 18h")   # r19: horário informativo não é oferta

    def test_retomar_nao_vira_oferta_de_agenda(self):
        ctx, result = self._turn("me chama amanhã",
                                 {"reply": "Combinado, falo com você amanhã às 10h.", "action": "retomar",
                                  "retomar_em": "2026-11-19"})
        self.assertEqual(result["action"], "retomar")
        self.assertNotIn("•", ctx.evo.texts_to(LEAD_PHONE)[0])

    def test_promessa_de_agendamento_continua_sendo_substituida(self):
        ctx, result = self._turn("pode ser", {"reply": "Agendei para você, até lá!", "action": "responder"})
        self.assertEqual(result["action"], "ofertar_horarios")
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE)[0].count("•"), 2)

    def test_acao_de_agenda_com_horario_inventado_oferece_os_reais(self):
        ctx, result = self._turn("tem 18h?", {"reply": "Tenho às 18h sim!", "action": "ofertar_horarios"})
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE)[0].count("•"), 2)


if __name__ == "__main__":
    unittest.main()
