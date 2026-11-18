"""Rodada 9 do motor: opt-out sem falso positivo de canal/horário e com mais saídas, lead que escreve durante o
takeover, merge da qualificação, confirmação idempotente, Google Agenda que falha, falha permanente de rotina e
alertas que só marcam depois de enfileirar."""

import unittest
from datetime import timedelta

from motor import agenda, engine, jobs
from motor.conversa import (AGUARDA_HUMANO_KEY, Decision, _alert_autoresponder, apply_decision, handle_turn,
                            has_pending_opt_out, is_opt_out)
from motor.util import iso
from tests.motor.support import CLOSER_PHONE, LEAD_PHONE, NOW, OWNER_PHONE, inbound, make_ctx, sp
from tests.motor.test_jobs import silent_lead
from tests.motor.test_rodada4 import book


def alerts(ctx):
    return [r for r in ctx.db.outbox if r["created_by"] == "alerta"]


class OptOutTest(unittest.TestCase):
    def test_restricao_de_canal_ou_horario_nao_e_saida(self):
        for text in ("Não quero receber ligação, pode me chamar pelo WhatsApp depois das 18h",
                     "Não mande mensagem de voz, prefiro texto",
                     "Não me envie mensagem de manhã, só pelo zap à tarde"):
            self.assertFalse(is_opt_out(text), text)

    def test_saidas_inequivocas_continuam(self):
        for text in ("não quero mais receber mensagens", "Não quero receber", "pare", "stop",
                     "me tira da lista, não quero receber ligação", "descadastrar"):
            self.assertTrue(is_opt_out(text), text)

    def test_novas_frases_de_saida(self):
        for text in ("Me deixa em paz", "Deixem em paz", "Remove meu número dos seus contatos",
                     "Por favor, apague meu contato", "exclua meu numero", "tira o meu número dos contatos"):
            self.assertTrue(is_opt_out(text), text)

    def test_cadencia_bloqueada_pelo_novo_detector(self):
        ctx = make_ctx()
        lead = silent_lead(ctx, hours_since_inbound=3)
        ctx.db.add_message(lead["id"], "in", "lead", "Me deixa em paz")
        self.assertTrue(has_pending_opt_out(ctx.db.messages))
        self.assertEqual(jobs.followups(ctx), 0)
        self.assertEqual(ctx.evo.sent, [])


class TakeoverAguardaHumanoTest(unittest.TestCase):
    def _takeover_lead(self, ctx):
        lead = inbound(ctx, "qual horário vocês têm?")
        ctx.db.update_lead(lead["id"], {"takeover_until": iso(ctx.now() + timedelta(hours=24)),
                                        "stage": "qualificado"})
        return lead

    def test_mensagem_durante_takeover_grava_a_marca_uma_vez(self):
        ctx = make_ctx()
        lead = self._takeover_lead(ctx)
        self.assertEqual(handle_turn(ctx, lead["id"])["status"], "takeover")
        first = ctx.db.leads[lead["id"]]["qualification"][AGUARDA_HUMANO_KEY]
        self.assertEqual(first, iso(NOW))
        ctx.clock.advance(hours=1)
        handle_turn(ctx, lead["id"])
        self.assertEqual(ctx.db.leads[lead["id"]]["qualification"][AGUARDA_HUMANO_KEY], first)

    def test_expirou_sem_resposta_avisa_uma_vez_e_reabre_o_turno(self):
        ctx = make_ctx()
        lead = self._takeover_lead(ctx)
        handle_turn(ctx, lead["id"])
        self.assertEqual(jobs.aguardando_humano(ctx), 0)   # ainda em takeover: nada
        ctx.clock.advance(hours=25)
        ctx.ai.push({"reply": "Oi! Posso te ajudar com horários.", "action": "responder"})
        self.assertEqual(jobs.aguardando_humano(ctx), 1)
        self.assertEqual(len([a for a in alerts(ctx) if a["phone"] == OWNER_PHONE]), 1)
        self.assertNotIn(AGUARDA_HUMANO_KEY, ctx.db.leads[lead["id"]]["qualification"])
        self.assertTrue(ctx.evo.texts_to(LEAD_PHONE))
        self.assertEqual(jobs.aguardando_humano(ctx), 0)   # a marca saiu: não repete
        self.assertEqual(len(alerts(ctx)), 1)

    def test_dono_respondeu_depois_so_limpa_a_marca(self):
        ctx = make_ctx()
        lead = self._takeover_lead(ctx)
        handle_turn(ctx, lead["id"])
        ctx.clock.advance(hours=2)
        ctx.db.add_message(lead["id"], "out", "dono", "Oi, aqui é o dono")
        ctx.clock.advance(hours=24)
        self.assertEqual(jobs.aguardando_humano(ctx), 0)
        self.assertEqual(alerts(ctx), [])
        self.assertEqual(ctx.evo.sent, [])
        self.assertNotIn(AGUARDA_HUMANO_KEY, ctx.db.leads[lead["id"]]["qualification"])


class MergeQualificacaoTest(unittest.TestCase):
    def test_edicao_do_dono_entre_leitura_e_escrita_sobrevive(self):
        ctx = make_ctx()
        lead = inbound(ctx, "quero saber")
        ctx.db.update_lead(lead["id"], {"stage": "qualificado", "qualification": {"interesse": "bot"}})
        snapshot = ctx.db.get_lead(lead["id"])
        # o dono edita no painel enquanto a IA pensa
        ctx.db.update_lead(lead["id"], {"qualification": {"interesse": "bot", "orcamento": "5 mil",
                                                          "_retomar": {"em": "x"}}})
        d = Decision(reply="Entendi!", action="qualificar", qualification={"prazo": "este mês"})
        apply_decision(ctx, snapshot, ctx.db.get_agent_config(), d, [], None, "quero saber")
        q = ctx.db.leads[lead["id"]]["qualification"]
        self.assertEqual(q["orcamento"], "5 mil")
        self.assertEqual(q["prazo"], "este mês")
        self.assertEqual(q["interesse"], "bot")
        self.assertIn("_retomar", q)

    def test_chave_da_ia_vence_a_atual(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        ctx.db.update_lead(lead["id"], {"qualification": {"prazo": "antigo"}})
        snapshot = ctx.db.get_lead(lead["id"])
        d = Decision(reply="ok", action="responder", qualification={"prazo": "novo"})
        apply_decision(ctx, snapshot, ctx.db.get_agent_config(), d, [], None, "oi")
        self.assertEqual(ctx.db.leads[lead["id"]]["qualification"]["prazo"], "novo")


class ConfirmacaoIdempotenteTest(unittest.TestCase):
    def test_enqueue_que_grava_e_lanca_nao_envia_direto_nem_alerta(self):
        ctx = make_ctx()
        lead = inbound(ctx, "pode ser 14h")
        real = ctx.db.enqueue_outbox

        def grava_e_lanca(*a, **k):
            real(*a, **k)
            raise TimeoutError("resposta perdida")
        ctx.db.enqueue_outbox = grava_e_lanca
        ctx.ai.push({"reply": "ok", "action": "agendar", "slot": iso(sp(19, 14))})
        result = handle_turn(ctx, lead["id"])
        self.assertTrue(result["booked"])
        rows = [r for r in ctx.db.outbox if r["created_by"] == "motor"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["client_key"].startswith("booking:A") and rows[0]["client_key"].endswith(":confirm"))
        self.assertEqual(len(ctx.evo.texts_to(LEAD_PHONE)), 1)   # só a da outbox (send_row), nunca em dobro
        self.assertEqual([a for a in alerts(ctx) if "confirmação" in a["body"]], [])
        self.assertEqual(engine.process_outbox(ctx), 0)

    def test_client_key_repetida_nao_duplica(self):
        ctx = make_ctx()
        first = ctx.db.enqueue_outbox(LEAD_PHONE, "a", client_key="booking:A1:confirm")
        second = ctx.db.enqueue_outbox(LEAD_PHONE, "a", client_key="booking:A1:confirm")
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(ctx.db.outbox), 1)


class GoogleAgendaFalhaTest(unittest.TestCase):
    class Gcal:
        def __init__(self, result):
            self.result = result

        def create(self, *a, **k):
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    def _booked(self, ctx):
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        appt = book(ctx, lead, sp(19, 14))
        return lead, {"appointment_id": appt["id"], "starts_at": appt["starts_at"], "ends_at": appt["ends_at"],
                      "closer_id": "c1"}

    def test_create_vazio_ou_excecao_avisa_dono_e_closer(self):
        for result in ("", RuntimeError("boom")):
            ctx = make_ctx()
            ctx.gcal = self.Gcal(result)
            lead, booked = self._booked(ctx)
            agenda._gcal_create(ctx, lead, booked, {"agent_name": "Ana"})
            owner = [a for a in alerts(ctx) if a["phone"] == OWNER_PHONE]
            closer = [a for a in alerts(ctx) if a["phone"] == CLOSER_PHONE]
            self.assertEqual((len(owner), len(closer)), (1, 1))
            for word in ("Marcos", "Joana", "19", "manualmente"):
                self.assertIn(word, owner[0]["body"])

    def test_sucesso_nao_avisa(self):
        ctx = make_ctx()
        ctx.gcal = self.Gcal("EV9")
        lead, booked = self._booked(ctx)
        agenda._gcal_create(ctx, lead, booked, {})
        self.assertEqual(alerts(ctx), [])
        self.assertEqual(ctx.db.appointments[0]["google_event_id"], "EV9")

    def test_falha_ao_gravar_o_id_avisa_o_dono(self):
        ctx = make_ctx()
        ctx.gcal = self.Gcal("EV9")
        lead, booked = self._booked(ctx)

        def quebra(*a, **k):
            raise RuntimeError("db fora")
        ctx.db.update_appointment = quebra
        agenda._gcal_create(ctx, lead, booked, {})
        owner = [a for a in alerts(ctx) if a["phone"] == OWNER_PHONE]
        self.assertEqual(len(owner), 1)
        self.assertIn("cancelamento", owner[0]["body"])


class FalhaPermanenteDeRotinaTest(unittest.TestCase):
    def test_4xx_nao_reverte_e_avisa_o_dono_uma_vez(self):
        ctx = make_ctx()
        lead = silent_lead(ctx, hours_since_inbound=3)
        ctx.evo.fail_send, ctx.evo.fail_status = True, 400
        self.assertEqual(jobs.followups(ctx), 0)
        self.assertEqual(ctx.db.leads[lead["id"]]["followup_step"], 1)
        owner = [a for a in alerts(ctx) if a["phone"] == OWNER_PHONE]
        self.assertEqual(len(owner), 1)
        self.assertIn(LEAD_PHONE, owner[0]["body"])
        self.assertIn("follow-up", owner[0]["body"])
        self.assertIn("400", owner[0]["body"])
        ctx.evo.fail_send = False
        jobs.followups(ctx)   # não repete o passo que falhou de vez
        self.assertEqual(len([a for a in alerts(ctx) if a["phone"] == OWNER_PHONE]), 1)

    def test_5xx_408_e_429_revertem_sem_alerta(self):
        for status in (429,):   # r16: 500/408 são resultado incerto (sem reenvio, passo mantido)
            ctx = make_ctx()
            lead = silent_lead(ctx, hours_since_inbound=3)
            ctx.evo.fail_send, ctx.evo.fail_status = True, status
            jobs.followups(ctx)
            self.assertEqual(ctx.db.leads[lead["id"]]["followup_step"], 0, status)
            self.assertEqual(alerts(ctx), [], status)

    def test_lembrete_4xx_mantem_a_marca(self):
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        appt = book(ctx, lead, sp(18, 20))
        ctx.evo.fail_send, ctx.evo.fail_status = True, 404
        jobs.reminders(ctx)
        self.assertIsNotNone(ctx.db.appointments[0]["reminder_24h_at"])
        self.assertEqual(len(alerts(ctx)), 1)
        self.assertIn("lembrete", alerts(ctx)[0]["body"])

    def test_telefone_invalido_e_permanente(self):
        ctx = make_ctx()
        lead = silent_lead(ctx, hours_since_inbound=3, phone="abc")
        self.assertEqual(jobs.followups(ctx), 0)
        self.assertEqual([p for p, _ in ctx.evo.sent if p != OWNER_PHONE], [])
        self.assertEqual(ctx.db.leads[lead["id"]]["followup_step"], 1)
        self.assertIn("telefone_invalido", [a for a in alerts(ctx)][0]["body"])


class AlertaSoMarcaDepoisDeEnfileirarTest(unittest.TestCase):
    def _flaky(self, ctx):
        real, calls = ctx.db.enqueue_outbox, {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("supabase fora")
            return real(*a, **k)
        ctx.db.enqueue_outbox = flaky

    def test_autoresponder(self):
        ctx = make_ctx()
        lead = inbound(ctx, "No momento não podemos atender")
        self._flaky(ctx)
        _alert_autoresponder(ctx, lead, "No momento não podemos atender")
        self.assertEqual(alerts(ctx), [])
        _alert_autoresponder(ctx, lead, "No momento não podemos atender")
        self.assertEqual(len(alerts(ctx)), 1)
        _alert_autoresponder(ctx, lead, "No momento não podemos atender")
        self.assertEqual(len(alerts(ctx)), 1)

    def test_stale(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        self._flaky(ctx)
        engine._alert_stale(ctx, lead, LEAD_PHONE)
        self.assertEqual(alerts(ctx), [])
        engine._alert_stale(ctx, lead, LEAD_PHONE)
        self.assertEqual(len(alerts(ctx)), 1)
        engine._alert_stale(ctx, lead, LEAD_PHONE)
        self.assertEqual(len(alerts(ctx)), 1)


class ConfirmeiInteresseTest(unittest.TestCase):
    def test_confirmei_o_seu_interesse_nao_e_promessa_de_agenda(self):
        self.assertFalse(agenda.claims_booking("Confirmei o seu interesse! Qual o maior desafio da equipe?"))
        self.assertTrue(agenda.claims_booking("Confirmei o seu agendamento para sexta"))


if __name__ == "__main__":
    unittest.main()
