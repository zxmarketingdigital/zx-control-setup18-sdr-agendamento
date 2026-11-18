"""Rodada 21, fatia M1: laço só com texto longo repetido, restrição de horário/canal não é opt-out e afirmação de
cancelamento sem cancelamento real. Tudo com Evolution/banco/IA falsos."""
import unittest
from datetime import timedelta

from motor import conversa
from motor.conversa import handle_turn
from motor.util import iso
from tests.motor.support import LEAD_PHONE, OWNER_PHONE, inbound, make_ctx, sp


def alerts(ctx, needle):
    return [t for t in ctx.evo.texts_to(OWNER_PHONE) if needle in t]


class LacoTextoCurtoTest(unittest.TestCase):
    def test_sim_tres_vezes_com_respostas_entre_eles_nao_pausa(self):
        ctx = make_ctx()
        for word in ("sim", "sim", "sim"):
            lead = inbound(ctx, word)
            ctx.ai.push({"reply": "Certo, vamos seguir.", "action": "responder"})
            self.assertEqual(handle_turn(ctx, lead["id"])["status"], "ok")
            ctx.clock.advance(seconds=30)
        self.assertEqual(alerts(ctx, "parece outro robô"), [])

    def test_curtas_da_lista_ou_abaixo_de_12_caracteres_nao_contam(self):
        for text in ("ok", "👍", "pode ser", "Bom dia!", "obrigada", "beleza", "texto curto"):
            self.assertTrue(conversa._is_natural_short_reply(conversa._loop_text(text)), text)
        self.assertFalse(conversa._is_natural_short_reply(conversa._loop_text("Olá! Como posso ajudar?")))

    def test_texto_longo_identico_tres_vezes_ainda_pausa(self):
        ctx = make_ctx()
        robo = "Obrigado pelo contato! Nosso horário de atendimento é das 9h às 18h."
        results = []
        for _ in range(3):
            lead = inbound(ctx, robo)
            ctx.ai.push({"reply": "Oi! Sou a Ana.", "action": "responder"})
            results.append(handle_turn(ctx, lead["id"])["status"])
            ctx.clock.advance(seconds=30)
        self.assertEqual(results, ["ok", "ok", "laco_pausado"])

    def test_vinte_respostas_em_uma_hora_pausa_mesmo_com_texto_curto(self):
        ctx = make_ctx()
        lead = inbound(ctx, "sim")
        for i in range(conversa.MAX_RESPOSTAS_POR_HORA + 1):
            ctx.db.add_message(lead["id"], "out", "agente", f"resposta {i}", created_at=ctx.now() - timedelta(minutes=i + 1))
        ctx.db.add_message(lead["id"], "in", "lead", "sim")
        self.assertEqual(handle_turn(ctx, lead["id"])["status"], "laco_pausado")


class OptOutRestricaoTest(unittest.TestCase):
    def test_restricao_de_horario_dia_ou_canal_nao_e_saida(self):
        for text in ("para de me mandar mensagem depois das 18h", "para de me mandar mensagem no domingo",
                     "para de me mandar o boleto por aqui", "pare de me mandar mensagens de manhã"):
            self.assertFalse(conversa.is_opt_out(text), text)

    def test_saida_explicita_continua(self):
        for text in ("para de me mandar mensagem", "pare de me mandar mensagens", "pare", "pare de me ligar"):
            self.assertTrue(conversa.is_opt_out(text), text)

    def test_restricao_alerta_o_dono_uma_vez_e_o_agente_responde(self):
        ctx = make_ctx()
        lead = inbound(ctx, "para de me mandar mensagem depois das 18h")
        ctx.ai.push({"reply": "Combinado, só falo antes das 18h.", "action": "responder"})
        self.assertEqual(handle_turn(ctx, lead["id"])["status"], "ok")
        self.assertFalse(ctx.db.leads[lead["id"]].get("opt_out"))
        self.assertEqual(len(alerts(ctx, "restrição de horário ou canal")), 1)
        # mesmo pedido de novo (retentativa do turno): sem segundo alerta
        conversa._alert_restriction(ctx, ctx.db.leads[lead["id"]], "para de me mandar mensagem depois das 18h")
        self.assertEqual(len(alerts(ctx, "restrição de horário ou canal")), 1)

    def test_opt_out_de_verdade_nao_gera_alerta_de_restricao(self):
        ctx = make_ctx()
        lead = inbound(ctx, "pare de me mandar mensagens")
        handle_turn(ctx, lead["id"])
        self.assertEqual(alerts(ctx, "restrição de horário ou canal"), [])


class CancelamentoFalsoTest(unittest.TestCase):
    def _agendado(self, ctx):
        lead = inbound(ctx, "14h")
        ctx.ai.push({"reply": "x", "action": "agendar", "slot": iso(sp(19, 14))})
        handle_turn(ctx, lead["id"])
        return lead

    def test_frases_de_afirmacao(self):
        for t in ("Pronto, cancelei sua conversa de amanhã.", "Desmarquei para você!", "Sua call foi cancelada.",
                  "A reunião ficou cancelada, tudo bem?", "Sua conversa está desmarcada."):
            self.assertTrue(conversa.claims_cancellation(t), t)

    def test_frases_descritivas_nao_disparam(self):
        for t in ("Você pode cancelar quando quiser.", "Se precisar cancelar me avise.", "Sua call não foi cancelada.",
                  "Posso desmarcar se você pedir."):
            self.assertFalse(conversa.claims_cancellation(t), t)

    def test_reply_afirma_cancelamento_sem_action_cancelar_e_trocada_e_alerta(self):
        ctx = make_ctx()
        lead = self._agendado(ctx)
        n = len(ctx.evo.texts_to(LEAD_PHONE))
        inbound(ctx, "não sei se consigo ir amanhã")
        ctx.ai.push({"reply": "Pronto, cancelei sua conversa de amanhã.", "action": "responder"})
        handle_turn(ctx, lead["id"])
        sent = ctx.evo.texts_to(LEAD_PHONE)[n:]
        self.assertEqual(len(sent), 1)
        self.assertNotIn("cancelei", sent[0])
        self.assertEqual(ctx.db.appointments[0]["status"], "agendado")
        self.assertEqual(len(alerts(ctx, "nada foi cancelado")), 1)
        self.assertIn(LEAD_PHONE, alerts(ctx, "nada foi cancelado")[0])

    def test_cancelar_sem_agendamento_com_a_frase_tambem_e_trocada(self):
        ctx = make_ctx()
        lead = inbound(ctx, "cancela por favor")
        ctx.ai.push({"reply": "Cancelei, tudo certo.", "action": "cancelar"})
        handle_turn(ctx, lead["id"])
        self.assertTrue(all("Cancelei" not in t for t in ctx.evo.texts_to(LEAD_PHONE)))
        self.assertEqual(len(alerts(ctx, "nada foi cancelado")), 1)

    def test_descritivo_passa_intacto(self):
        ctx = make_ctx()
        lead = inbound(ctx, "como funciona?")
        ctx.ai.push({"reply": "Se precisar cancelar me avise, sem problema.", "action": "responder"})
        handle_turn(ctx, lead["id"])
        self.assertIn("Se precisar cancelar me avise, sem problema.", ctx.evo.texts_to(LEAD_PHONE))
        self.assertEqual(alerts(ctx, "nada foi cancelado"), [])

    def test_cancelar_real_com_a_frase_passa_intacta(self):
        ctx = make_ctx()
        lead = self._agendado(ctx)
        inbound(ctx, "não vou conseguir, cancela")
        ctx.ai.push({"reply": "Pronto, cancelei sua conversa.", "action": "cancelar"})
        handle_turn(ctx, lead["id"])
        self.assertEqual(ctx.db.appointments[0]["status"], "cancelado")
        self.assertIn("Pronto, cancelei sua conversa.", ctx.evo.texts_to(LEAD_PHONE))
        self.assertEqual(alerts(ctx, "nada foi cancelado"), [])


if __name__ == "__main__":
    unittest.main()
