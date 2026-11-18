"""Rodada 6 do motor: telefone fora do E.164 não sai, opt-out ampliado e relido antes de responder, 'cancelar' com
data vira remarcação, chaves internas da IA descartadas, isenção de opt-out só para destino interno, fila do
modo teste não trava e alerta com timeout incerto não duplica."""

import unittest
from datetime import timedelta

from motor import jobs, outbox
from motor.conversa import asks_reschedule, handle_turn, is_opt_out
from motor.db import Database
from motor.util import iso
from tests.motor.support import (CLOSER_PHONE, LEAD_PHONE, OWNER_PHONE, TEST_PHONE, default_agent, inbound,
                                 make_ctx, sp)
from tests.motor.test_jobs import book, silent_lead


class TelefoneInvalidoTest(unittest.TestCase):
    def test_send_row_telefone_fora_do_e164_vira_erro_e_nao_envia(self):
        ctx = make_ctx()
        row = ctx.db.add_outbox("abc", "oi", created_by="painel")
        self.assertFalse(outbox.send_row(ctx, row))
        self.assertEqual((ctx.db.outbox[0]["status"], ctx.db.outbox[0]["last_error"]), ("erro", "telefone_invalido"))
        self.assertEqual(ctx.evo.sent, [])

    def test_deliver_com_telefone_ruim_nao_envia_nem_enfileira(self):
        from motor.conversa import deliver
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        lead["phone"] = "xx"
        self.assertFalse(deliver(ctx, lead, "oi"))
        self.assertEqual((ctx.evo.sent, ctx.db.outbox), ([], []))


class OptOutAmpliadoTest(unittest.TestCase):
    def test_frases_de_saida(self):
        for text in ("Para de mandar mensagem", "para de me mandar mensagem", "pare de enviar", "parem de me chamar",
                     "me tira da lista", "tira meu numero da lista", "remova da lista", "exclua meu numero dessa lista",
                     "não envie mais mensagens", "nao me envie mais", "nao manda mais", "não mande mais", "não me chame",
                     "nao quero mais receber", "não quero mensagens", "sai da lista", "sair da lista", "descadastrar",
                     "descadastre", "stop", "sair", "parar de me mandar"):
            self.assertTrue(is_opt_out(text), text)

    def test_frases_que_nao_sao_saida(self):
        for text in ("para quando é a call?", "pode parar o horário das 10?", "não mande o link ainda, estou no carro",
                     "meu fornecedor para de mandar boleto toda hora e eu quero resolver isso com vocês",
                     "quero sair às 15h", "não envio documentos por aqui"):
            self.assertFalse(is_opt_out(text), text)

    def test_cadencia_bloqueada_para_frase_nova(self):
        ctx = make_ctx()
        lead = silent_lead(ctx, hours_since_inbound=3)
        ctx.db.add_message(lead["id"], "in", "lead", "Para de mandar mensagem.")
        self.assertEqual(jobs.followups(ctx), 0)
        self.assertEqual(ctx.evo.sent, [])


class ReleituraAntesDeResponderTest(unittest.TestCase):
    def test_pare_que_chega_durante_a_ia_aborta_a_resposta(self):
        ctx = make_ctx()
        lead = inbound(ctx, "quero marcar uma conversa")
        original = ctx.ai.complete

        def slow(system, messages):
            ctx.db.add_message(lead["id"], "in", "lead", "pare")   # chegou enquanto a IA pensava
            return original(system, messages)
        ctx.ai.complete = slow
        ctx.ai.push({"reply": "Ótimo, tenho horários!", "action": "responder"})
        result = handle_turn(ctx, lead["id"])
        self.assertTrue(ctx.db.leads[lead["id"]]["opt_out"])
        self.assertNotIn("Ótimo, tenho horários!", ctx.evo.texts_to(LEAD_PHONE))
        self.assertNotEqual(result["status"], "ok")

    def test_pare_durante_a_ia_nao_agenda(self):
        ctx = make_ctx()
        lead = inbound(ctx, "pode ser as 10")
        slot = sp(19, 14)
        ctx.db.slots.append(slot)
        original = ctx.ai.complete

        def slow(system, messages):
            ctx.db.add_message(lead["id"], "in", "lead", "não me envie mais")
            return original(system, messages)
        ctx.ai.complete = slow
        ctx.ai.push({"reply": "Fechado!", "action": "agendar", "slot": iso(slot)})
        handle_turn(ctx, lead["id"])
        self.assertEqual(ctx.db.appointments, [])


class CancelarComRemarcacaoTest(unittest.TestCase):
    def test_detector(self):
        for text in ("preciso desmarcar, consegue sexta às 10h?", "cancela, pode amanhã?", "quero trocar o horário",
                     "desmarcar e marcar 14:30", "vou remarcar"):
            self.assertTrue(asks_reschedule(text), text)
        for text in ("pode cancelar", "quero cancelar", "pode desmarcar por favor"):
            self.assertFalse(asks_reschedule(text), text)

    def test_cancelar_com_data_vira_oferta_de_horarios(self):
        ctx = make_ctx()
        lead = inbound(ctx, "Preciso desmarcar, consegue sexta às 10h?")
        appt = book(ctx, lead, sp(19, 14))
        ctx.db.update_lead(lead["id"], {"stage": "agendado"})
        ctx.ai.push({"reply": "Claro, cancelei.", "action": "cancelar"})
        handle_turn(ctx, lead["id"])
        self.assertEqual(appt["status"], "agendado")
        self.assertFalse(any("cancelou" in t for t in ctx.evo.texts_to(OWNER_PHONE)))

    def test_cancelar_explicito_continua_cancelando(self):
        ctx = make_ctx()
        lead = inbound(ctx, "pode cancelar minha conversa")
        appt = book(ctx, lead, sp(19, 14))
        ctx.db.update_lead(lead["id"], {"stage": "agendado"})
        ctx.ai.push({"reply": "Cancelado.", "action": "cancelar"})
        handle_turn(ctx, lead["id"])
        self.assertEqual(appt["status"], "cancelado")


class ChavesInternasTest(unittest.TestCase):
    def test_ia_nao_grava_chave_com_underscore(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        ctx.ai.push({"reply": "Olá!", "action": "responder", "qualificacao": {"_retomar": "amanhã", "porte": "médio"}})
        handle_turn(ctx, lead["id"])
        qual = ctx.db.leads[lead["id"]]["qualification"]
        self.assertNotIn("_retomar", qual)
        self.assertEqual(qual.get("porte"), "médio")

    def test_retomar_string_e_limpo_e_nao_derruba_os_outros(self):
        ctx = make_ctx()
        bad = silent_lead(ctx, hours_since_inbound=3, qualification={"_retomar": "amanhã"})
        good = silent_lead(ctx, hours_since_inbound=3, phone="+5511977770000",
                           qualification={"_retomar": {"em": iso(ctx.now() - timedelta(minutes=5))}})
        ctx.clock.current = ctx.clock.current.replace(hour=15)
        self.assertEqual(jobs.retomar(ctx), 1)
        self.assertNotIn("_retomar", ctx.db.leads[bad["id"]]["qualification"])
        self.assertTrue(ctx.evo.texts_to("+5511977770000"))
        self.assertIsNotNone(good)

    def test_excecao_em_um_lead_nao_para_o_laco(self):
        ctx = make_ctx()
        a = silent_lead(ctx, hours_since_inbound=3, qualification={"_retomar": {"em": iso(ctx.now() - timedelta(minutes=5))}})
        silent_lead(ctx, hours_since_inbound=3, phone="+5511977770000",
                    qualification={"_retomar": {"em": iso(ctx.now() - timedelta(minutes=5))}})
        ctx.clock.current = ctx.clock.current.replace(hour=15)
        real = ctx.messenger.allowed

        def boom(phone, internal=False):
            if phone == LEAD_PHONE:
                raise RuntimeError("falha isolada")
            return real(phone, internal)
        ctx.messenger.allowed = boom
        self.assertEqual(jobs.retomar(ctx), 1)
        self.assertIsNotNone(a)


class IsencaoOptOutTest(unittest.TestCase):
    def test_rotulo_alerta_para_lead_com_opt_out_nao_sai(self):
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        ctx.db.update_lead(lead["id"], {"opt_out": True})
        row = ctx.db.add_outbox(LEAD_PHONE, "promoção!", lead_id=lead["id"], created_by="alerta")
        self.assertFalse(outbox.send_row(ctx, row))
        self.assertEqual(ctx.db.outbox[0]["status"], "erro")
        self.assertEqual(ctx.evo.sent, [])

    def test_alerta_para_o_dono_continua_saindo(self):
        ctx = make_ctx()
        row = ctx.db.add_outbox(OWNER_PHONE, "aviso", created_by="alerta")
        self.assertTrue(outbox.send_row(ctx, row))


class ModoTesteFilaTest(unittest.TestCase):
    def test_bloqueadas_nao_travam_a_fila(self):
        ctx = make_ctx(default_agent(test_only=True, test_phones=[TEST_PHONE]))
        for i in range(25):
            ctx.db.add_outbox(f"+55119999{i:05d}", "x", created_by="painel")
        ctx.db.add_outbox(TEST_PHONE, "do aluno", created_by="painel")
        self.assertEqual(outbox.process_outbox(ctx), 1)
        self.assertEqual(ctx.evo.texts_to(TEST_PHONE), ["do aluno"])
        self.assertEqual(sum(1 for r in ctx.db.outbox if r["status"] == "pendente"), 25)

    def test_sem_test_phones_so_internos(self):
        ctx = make_ctx(default_agent(test_only=True, test_phones=[]))
        ctx.db.add_outbox(LEAD_PHONE, "x", created_by="painel")
        ctx.db.add_outbox(OWNER_PHONE, "aviso", created_by="alerta")
        self.assertEqual(outbox.process_outbox(ctx), 1)

    def test_filtro_postgrest_escapado(self):
        seen = {}

        class Fake(Database):
            def select(self, table, params=None):
                seen.update(params)
                return []
        Fake("https://x.supabase.co", "k").pending_outbox(20, ['+5511"9', "+5585999"])
        self.assertTrue(seen["phone"].startswith("in.("))
        self.assertIn('"+5585999"', seen["phone"])
        self.assertIn('\\"', seen["phone"])


class NotifyOwnerTimeoutTest(unittest.TestCase):
    def test_timeout_incerto_nao_reenfileira(self):
        ctx = make_ctx()
        ctx.evo.timeout_send = True
        self.assertFalse(outbox.notify_owner(ctx, "alerta"))
        self.assertEqual(ctx.db.outbox, [])

    def test_falha_comum_reenfileira(self):
        ctx = make_ctx()
        ctx.evo.fail_send = True
        outbox.notify_owner(ctx, "alerta")
        self.assertEqual(len(ctx.db.outbox), 1)


if __name__ == "__main__":
    unittest.main()
