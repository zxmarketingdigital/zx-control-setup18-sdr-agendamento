"""Rodada 14 do motor: registro do envio em passos com retry próprio, resposta já gravada não repete o turno,
mensagens embrulhadas ou de tipo desconhecido, fila com backoff, flags de alerta só após o enfileiramento,
aviso de cancelamento pela outbox, chave do Google Agenda por horário, horário com dia e queda do WhatsApp."""

import unittest
from datetime import timedelta

from motor import agenda, engine, jobs, outbox
from motor.conversa import handle_turn
from motor.util import iso, get_tz
from tests.motor.support import CLOSER_PHONE, LEAD_PHONE, OWNER_PHONE, default_agent, inbound, make_ctx, sp
from tests.motor.test_jobs import book


def alerts(ctx, phone=OWNER_PHONE):
    return [r for r in ctx.db.outbox if r["phone"] == phone and r["created_by"] == "alerta"]


class RegistroDoEnvioTest(unittest.TestCase):
    def test_update_lead_falha_duas_vezes_recover_nao_chama_a_ia(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        ctx.clock.advance(seconds=5)
        real = ctx.db.update_lead

        def boom(lead_id, fields):
            if "last_outbound_at" in fields:
                raise RuntimeError("timeout")
            return real(lead_id, fields)
        ctx.db.update_lead = boom
        self.assertTrue(ctx.messenger.send(LEAD_PHONE, "Olá!", lead_id=lead["id"]))
        ctx.db.update_lead = real
        self.assertIsNone(ctx.db.leads[lead["id"]]["last_outbound_at"])   # o PATCH nunca passou
        self.assertEqual(engine.recover_unanswered(ctx), set())            # a conversa já mostra a resposta
        self.assertEqual(ctx.ai.calls, [])

    def test_turno_inbound_pula_lead_cuja_ultima_mensagem_e_do_agente(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        ctx.clock.advance(seconds=5)
        ctx.db.add_message(lead["id"], "out", "agente", "Olá!", wa_id="OUT1")
        result = handle_turn(ctx, lead["id"])
        self.assertEqual(result["status"], "ja_respondido")
        self.assertEqual(ctx.ai.calls, [])
        self.assertEqual(ctx.evo.sent, [])

    def test_mensagem_dono_no_fim_nao_conta_como_resposta_do_agente(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        ctx.clock.advance(seconds=5)
        ctx.db.add_message(lead["id"], "out", "dono", "Já te respondo", wa_id="OUT1")
        ctx.ai.push({"reply": "Olá!", "action": "responder"})
        self.assertNotEqual(handle_turn(ctx, lead["id"])["status"], "ja_respondido")

    def test_add_message_e_update_lead_tem_retry_proprio(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        real_add, real_upd = ctx.db.add_message, ctx.db.update_lead
        calls = {"add": 0, "upd": 0}

        def add(*a, **k):
            calls["add"] += 1
            raise RuntimeError("fora")

        def upd(lead_id, fields):
            calls["upd"] += 1
            if calls["upd"] == 1 and "last_outbound_at" in fields:
                raise RuntimeError("fora")
            return real_upd(lead_id, fields)
        ctx.db.add_message, ctx.db.update_lead = add, upd
        ctx.messenger.send(LEAD_PHONE, "Olá!", lead_id=lead["id"])
        self.assertEqual(calls["add"], 2)    # a mensagem: 2 tentativas, falhando
        self.assertEqual(calls["upd"], 2)    # e o last_outbound_at ainda tenta (1ª falha, 2ª passa)
        self.assertIsNotNone(ctx.db.leads[lead["id"]]["last_outbound_at"])


class MensagemEmbrulhadaTest(unittest.TestCase):
    def ingest_one(self, ctx, message):
        record = ctx.evo.add(LEAD_PHONE, "", wa_id="W1")
        record["message"] = message
        return engine.ingest(ctx)

    def test_ephemeral_com_texto(self):
        ctx = make_ctx()
        self.ingest_one(ctx, {"ephemeralMessage": {"message": {"extendedTextMessage": {"text": "quero agendar quinta"}}}})
        self.assertEqual([m["body"] for m in ctx.db.messages], ["quero agendar quinta"])

    def test_view_once_v2_com_imagem_vira_midia(self):
        ctx = make_ctx()
        self.ingest_one(ctx, {"viewOnceMessageV2": {"message": {"imageMessage": {"caption": "veja"}}}})
        self.assertEqual(len(ctx.db.messages), 1)
        self.assertEqual(ctx.db.messages[0]["media_type"], "image")
        self.assertIn("veja", ctx.db.messages[0]["body"])

    def test_documento_com_legenda_e_edicao_aninhados(self):
        ctx = make_ctx()
        inner = {"documentWithCaptionMessage": {"message": {"documentMessage": {"caption": "contrato"}}}}
        self.ingest_one(ctx, {"ephemeralMessage": {"message": inner}})
        self.assertEqual(ctx.db.messages[0]["media_type"], "document")
        ctx2 = make_ctx()
        self.ingest_one(ctx2, {"editedMessage": {"message": {"protocolMessage": {"editedMessage": {
            "conversation": "x"}}, "conversation": "texto editado"}}})
        self.assertEqual(ctx2.db.messages[0]["body"], "texto editado")

    def test_reacao_e_protocolo_sao_descartados_em_silencio(self):
        for message in ({"reactionMessage": {"text": "👍"}}, {"protocolMessage": {"type": 0}},
                        {"senderKeyDistributionMessage": {}}, {"messageContextInfo": {}}):
            ctx = make_ctx()
            self.ingest_one(ctx, message)
            self.assertEqual(ctx.db.messages, [])
            self.assertEqual(ctx.db.outbox, [])

    def test_tipo_desconhecido_grava_marcador_e_avisa_uma_vez_por_lead(self):
        ctx = make_ctx()
        self.ingest_one(ctx, {"pollCreationMessage": {"name": "enquete"}})
        self.assertEqual([m["body"] for m in ctx.db.messages], [engine.UNSUPPORTED_BODY])
        self.assertEqual(len(alerts(ctx)), 1)
        record = ctx.evo.add(LEAD_PHONE, "", wa_id="W2")
        record["message"] = {"liveLocationMessage": {}}
        engine.ingest(ctx)
        self.assertEqual(len(ctx.db.messages), 2)
        self.assertEqual(len(alerts(ctx)), 1)    # dedupe por lead

    def test_tipo_desconhecido_do_dono_grava_placeholder_e_ativa_takeover(self):
        # r16: contato/localização/enquete do dono no celular também pausa o agente (antes era ignorado)
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        record = ctx.evo.add(LEAD_PHONE, "", wa_id="W3", from_me=True)
        record["message"] = {"pollCreationMessage": {"name": "x"}}
        engine.ingest(ctx)
        self.assertEqual(len(ctx.db.messages), 2)
        self.assertEqual((ctx.db.messages[-1]["author"], ctx.db.messages[-1]["body"]),
                         ("dono", "[mensagem do dono sem texto]"))
        self.assertIsNotNone(ctx.db.get_lead(lead["id"])["takeover_until"])


class FilaComBackoffTest(unittest.TestCase):
    def test_25_linhas_em_espera_e_uma_elegivel_a_elegivel_e_tentada(self):
        ctx = make_ctx()
        for _ in range(25):
            ctx.db.add_outbox(LEAD_PHONE, "antiga", created_by="painel")
        ctx.state["outbox_next_at"] = {i: ctx.now() + timedelta(hours=1) for i in range(1, 26)}
        ctx.db.add_outbox(LEAD_PHONE, "nova", created_by="painel")
        self.assertEqual(outbox.process_outbox(ctx), 1)
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), ["nova"])

    def test_falhas_definitivas_do_mesmo_ciclo_saem_em_um_aviso(self):
        ctx = make_ctx()
        ctx.evo.fail_send = True
        ctx.evo.fail_status = 400
        for text in ("um", "dois", "tres"):
            ctx.db.add_outbox(LEAD_PHONE, text, created_by="painel")
        outbox.process_outbox(ctx)
        self.assertEqual([r["status"] for r in ctx.db.outbox if r["created_by"] == "painel"], ["erro"] * 3)
        found = alerts(ctx)
        self.assertEqual(len(found), 1)
        self.assertIn("3 mensagens", found[0]["body"])


class FlagsDeAlertaTest(unittest.TestCase):
    def test_agenda_ilegivel_flag_so_depois_do_enfileiramento_e_neutra_nao_repete(self):
        ctx = make_ctx()
        ctx.db.available_slots = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("500"))
        lead = inbound(ctx, "quero marcar")
        real = ctx.db.enqueue_outbox

        def flaky(phone, body, lead_id=None, created_by="motor", client_key=None):
            if created_by == "alerta":
                raise RuntimeError("banco fora")
            return real(phone, body, lead_id=lead_id, created_by=created_by, client_key=client_key)
        ctx.db.enqueue_outbox = flaky
        result = handle_turn(ctx, lead["id"])            # não levanta: o turno não é repetido
        self.assertEqual(result["status"], "agenda_falhou")
        self.assertFalse(ctx.state.get("agenda_alerted"))
        self.assertEqual(len(ctx.evo.texts_to(LEAD_PHONE)), 1)   # a neutra saiu uma vez
        ctx.db.enqueue_outbox = real
        inbound(ctx, "e então?")
        handle_turn(ctx, lead["id"])
        self.assertEqual(len(alerts(ctx)), 1)            # o alerta saiu no turno seguinte
        self.assertTrue(ctx.state.get("agenda_alerted"))


class CancelamentoAoCloserTest(unittest.TestCase):
    def test_aviso_ao_closer_vai_pela_outbox_quando_a_evolution_falha(self):
        ctx = make_ctx()
        lead = inbound(ctx, "14h")
        ctx.ai.push({"reply": "x", "action": "agendar", "slot": iso(sp(19, 14))})
        handle_turn(ctx, lead["id"])
        ctx.evo.fail_send = True
        ctx.evo.fail_status = 500
        inbound(ctx, "cancela")
        ctx.ai.push({"reply": "Sem problema!", "action": "cancelar"})
        handle_turn(ctx, lead["id"])
        found = alerts(ctx, CLOSER_PHONE)
        self.assertEqual(len(found), 1)
        self.assertIn("cancelou", found[0]["body"])


class FakeGcal:
    def __init__(self, event_id="EV-NEW"):
        self.event_id, self.keys, self.last_uncertain = event_id, [], False

    def create(self, title, description, start, end, guest_email="", appointment_id=None):
        self.keys.append(appointment_id)
        return self.event_id


class ChaveDoGoogleAgendaTest(unittest.TestCase):
    def booked(self, appt, start):
        return {"appointment_id": appt["id"], "starts_at": iso(start), "ends_at": iso(start + timedelta(minutes=30)),
                "closer_id": "c1"}

    def test_remarcacao_muda_a_chave_de_idempotencia(self):
        ctx = make_ctx()
        ctx.gcal = FakeGcal()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        appt = book(ctx, lead, sp(19, 14))
        agenda._gcal_create(ctx, lead, self.booked(appt, sp(19, 14)), {})
        agenda._gcal_create(ctx, lead, self.booked(appt, sp(20, 10)), {})
        self.assertEqual(len(set(ctx.gcal.keys)), 2)
        self.assertTrue(all(k.startswith(f"{appt['id']}:") for k in ctx.gcal.keys))

    def test_ponte_devolvendo_o_evento_antigo_nao_conta_como_criado(self):
        ctx = make_ctx()
        ctx.gcal = FakeGcal(event_id="EV-OLD")
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        appt = book(ctx, lead, sp(19, 14))
        agenda._gcal_create(ctx, lead, self.booked(appt, sp(20, 10)), {}, previous_event_id="EV-OLD")
        self.assertIsNone(ctx.db.appointments[0]["google_event_id"])
        self.assertEqual(len(alerts(ctx)), 1)


class HorarioComDiaTest(unittest.TestCase):
    def setUp(self):
        self.tz = get_tz("America/Sao_Paulo")
        self.now = sp(18, 9)
        self.slots = [sp(19, 14), sp(19, 15), sp(20, 10), sp(21, 16)]   # quinta 14h/15h, sexta 10h, sábado 16h

    def unlisted(self, text):
        return agenda.mentions_unlisted_time(text, self.slots, self.tz, self.now)

    def test_hora_que_so_existe_em_outro_dia_e_inventada(self):
        self.assertTrue(self.unlisted("Sexta às 15h fica bom para você?"))
        self.assertTrue(self.unlisted("Que tal 21/11 às 10h?"))
        self.assertTrue(self.unlisted("Amanhã às 10h?"))              # amanhã = quinta; 10h só existe na sexta
        self.assertTrue(self.unlisted("às 15h de sábado"))

    def test_dia_e_hora_que_batem_passam(self):
        self.assertFalse(self.unlisted("Quinta às 15h fica bom?"))
        self.assertFalse(self.unlisted("Amanhã às 14h?"))
        self.assertFalse(self.unlisted("Sexta-feira às 10h"))
        self.assertFalse(self.unlisted("dia 21 às 16h"))
        self.assertFalse(self.unlisted("quinta, 19/11, às 14:00"))

    def test_sem_dia_vale_so_a_hora_como_antes(self):
        self.assertFalse(self.unlisted("Posso às 15h?"))
        self.assertTrue(self.unlisted("Posso às 17h?"))

    def test_turno_remove_a_menção_com_dia_errado(self):
        ctx = make_ctx()
        lead = inbound(ctx, "tem horário?")
        ctx.ai.push({"reply": "Perfeito, sexta às 15h. Combinado, qualquer coisa me avise.", "action": "responder"})
        result = handle_turn(ctx, lead["id"])
        self.assertNotIn("15h", result["reply"])


class QuedaDoWhatsappTest(unittest.TestCase):
    def test_estado_de_queda_so_apos_o_aviso_enfileirado(self):
        ctx = make_ctx()
        ctx.evo.state = "close"
        ctx.evo.fail_send = True
        ctx.evo.fail_status = 429
        real = ctx.db.enqueue_outbox
        ctx.db.enqueue_outbox = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("banco fora"))
        self.assertEqual(jobs.vigia(ctx), "fora")   # r26: aviso que falha não levanta, pula as rotinas de envio
        self.assertNotIn("down_since", jobs._load_vigia(ctx))
        ctx.db.enqueue_outbox = real
        self.assertEqual(jobs.vigia(ctx), "caiu")           # a próxima rodada tenta de novo
        self.assertIn("down_since", jobs._load_vigia(ctx))
        self.assertEqual(len(alerts(ctx)), 1)


if __name__ == "__main__":
    unittest.main()
