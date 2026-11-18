"""Rodada 24, fatia C: «não,» interjetivo não anula o pedido de saída, adiamento e restrição não são saída permanente
(nem pela IA) e mídia sem suporte ou repetição fora da janela não é laço. Tudo com Evolution/banco/IA falsos."""
import unittest
from datetime import timedelta

from motor import conversa
from motor.conversa import handle_turn, is_opt_out
from tests.motor.support import LEAD_PHONE, OWNER_PHONE, inbound, make_ctx


def alerts(ctx, needle):
    return [t for t in ctx.evo.texts_to(OWNER_PHONE) if needle in t]


class NegacaoInterjetivaTest(unittest.TestCase):
    def test_nao_com_pontuacao_antes_do_pedido_e_saida(self):
        for text in ("Não, para de me mandar mensagens.", "Não. Pare.", "Não! Pare de me mandar mensagem",
                     "Não, me tira da lista", "Não; pare de me enviar mensagens"):
            self.assertTrue(is_opt_out(text), text)

    def test_negacao_na_mesma_oracao_segue_a_regra_antiga(self):
        self.assertFalse(is_opt_out("não para de mandar?"))
        self.assertFalse(is_opt_out("nunca pare de me mandar mensagens"))
        self.assertFalse(is_opt_out("Não, obrigado"))


class AdiamentoTest(unittest.TestCase):
    def test_adiamento_nao_e_saida(self):
        for text in ("não quero contato agora, me chama semana que vem", "agora não, me chama mais tarde",
                     "hoje não, fala comigo amanhã", "no momento não, me chame mês que vem"):
            self.assertFalse(is_opt_out(text), text)
            self.assertTrue(conversa.is_restriction_flag(text), text)

    def test_sem_adiamento_continua_saida(self):
        self.assertTrue(is_opt_out("não quero mais contato"))

    def test_adiamento_no_turno_alerta_o_dono_e_a_ia_responde(self):
        ctx = make_ctx()
        lead = inbound(ctx, "não quero contato agora, me chama semana que vem")
        ctx.ai.push({"reply": "Combinado, falo com você na semana que vem.", "action": "retomar",
                     "retomar_em": "2030-01-01"})
        result = handle_turn(ctx, lead["id"])
        self.assertNotIn(result["status"], ("opt_out_confirmado", "opt_out_registrado"))
        self.assertFalse(ctx.db.leads[lead["id"]]["opt_out"])
        self.assertNotIn(conversa.OPT_OUT_CONFIRMATION, ctx.evo.texts_to(LEAD_PHONE))
        self.assertEqual(len(alerts(ctx, "restrição de horário ou canal")), 1)


class IAOptOutEmRestricaoTest(unittest.TestCase):
    def test_ia_opt_out_com_restricao_de_horario_vira_resposta(self):
        ctx = make_ctx()
        lead = inbound(ctx, "para de me mandar mensagem depois das 18h")
        ctx.ai.push({"reply": "Claro, evito escrever depois das 18h.", "action": "opt_out"})
        result = handle_turn(ctx, lead["id"])
        self.assertFalse(ctx.db.leads[lead["id"]]["opt_out"])
        self.assertNotEqual(result["status"], "opt_out_confirmado")
        texts = ctx.evo.texts_to(LEAD_PHONE)
        self.assertEqual(texts, ["Claro, evito escrever depois das 18h."])
        self.assertFalse([t for t in texts if "não vou mais te enviar" in t])
        self.assertEqual(len(alerts(ctx, "restrição de horário ou canal")), 1)

    def test_ia_opt_out_sem_restricao_continua_saida(self):
        ctx = make_ctx()
        lead = inbound(ctx, "quero sair, me deixa em paz")
        ctx.ai.push({"reply": "", "action": "opt_out"})
        handle_turn(ctx, lead["id"])
        self.assertTrue(ctx.db.leads[lead["id"]]["opt_out"])

    def test_prompt_diz_que_restricao_nao_e_opt_out(self):
        rule = [l for l in conversa.build_system_prompt.__code__.co_consts if isinstance(l, str) and "opt_out" in l]
        self.assertTrue(any("NÃO é opt_out" in r for r in rule))


class LacoTest(unittest.TestCase):
    def test_tres_figurinhas_seguidas_nao_pausam(self):
        ctx = make_ctx()
        lead = None
        for _ in range(3):
            lead = inbound(ctx, "[mensagem não suportada pelo agente]")
            ctx.clock.advance(seconds=20)
        ctx.ai.push({"reply": "Recebi, pode me escrever em texto?", "action": "responder"})
        result = handle_turn(ctx, lead["id"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(ctx.ai.calls), 1)
        self.assertEqual(alerts(ctx, "parece outro robô"), [])

    def test_midia_com_media_type_tambem_nao_conta(self):
        ctx = make_ctx()
        lead = None
        for _ in range(3):
            lead = inbound(ctx, "Olha essa foto aqui do meu consultório, ok?")
            ctx.db.messages[-1]["media_type"] = "image"
            ctx.clock.advance(seconds=20)
        ctx.ai.push({"reply": "Vi sim!", "action": "responder"})
        self.assertEqual(handle_turn(ctx, lead["id"])["status"], "ok")

    def test_mesma_pergunta_com_horas_entre_elas_nao_pausa(self):
        ctx = make_ctx()
        results = []
        for _ in range(3):
            lead = inbound(ctx, "Qual o preço do atendimento completo?")
            ctx.ai.push({"reply": "Te explico na conversa.", "action": "responder"})
            results.append(handle_turn(ctx, lead["id"])["status"])
            ctx.clock.advance(hours=3)
        self.assertEqual(results, ["ok", "ok", "ok"])
        self.assertEqual(alerts(ctx, "parece outro robô"), [])

    def test_laco_real_em_sequencia_rapida_continua_pausando(self):
        ctx = make_ctx()
        robo = "Obrigado pelo contato! Nosso horário de atendimento é das 9h às 18h."
        results = []
        for _ in range(3):
            lead = inbound(ctx, robo)
            ctx.ai.push({"reply": "Oi! Sou a Ana.", "action": "responder"})
            results.append(handle_turn(ctx, lead["id"])["status"])
            ctx.clock.advance(seconds=30)
        self.assertEqual(results, ["ok", "ok", "laco_pausado"])


if __name__ == "__main__":
    unittest.main()
