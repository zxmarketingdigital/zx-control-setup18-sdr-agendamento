"""Rodada 25, fatia C: pedido explícito de saída vence o adiamento; repetição sem resposta do agente não é laço."""
import unittest

from motor.conversa import handle_turn, is_opt_out
from tests.motor.support import OWNER_PHONE, inbound, make_ctx


class SaidaVenceAdiamentoTest(unittest.TestCase):
    def test_pedido_explicito_de_saida_com_adiamento_e_saida(self):
        for text in ("Para de me mandar mensagem agora, me chama semana que vem.",
                     "Pare de me mandar mensagem hoje, me chama depois."):
            self.assertTrue(is_opt_out(text), text)

    def test_adiamento_puro_continua_adiamento(self):
        for text in ("agora não, me chama mais tarde", "não quero contato agora, me chama semana que vem",
                     "hoje não, fala comigo amanhã"):
            self.assertFalse(is_opt_out(text), text)


class LacoSemRespostaTest(unittest.TestCase):
    TEXTO = "Quero saber o preço do plano anual"

    def test_reenvio_sem_resposta_nao_pausa(self):
        ctx = make_ctx()
        lead = None
        for _ in range(3):
            lead = inbound(ctx, self.TEXTO)
            ctx.clock.advance(minutes=20)
        ctx.ai.push({"reply": "O plano anual custa X.", "action": "responder"})
        result = handle_turn(ctx, lead["id"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(ctx.ai.calls), 1)
        self.assertFalse([t for t in ctx.evo.texts_to(OWNER_PHONE) if "parece outro robô" in t])
        self.assertIsNone(ctx.db.leads[lead["id"]].get("takeover_until"))

    def test_laco_com_respostas_entre_as_repeticoes_pausa(self):
        ctx = make_ctx()
        results = []
        for _ in range(3):
            lead = inbound(ctx, self.TEXTO)
            ctx.ai.push({"reply": "Oi! Sou a Ana.", "action": "responder"})
            results.append(handle_turn(ctx, lead["id"])["status"])
            ctx.clock.advance(minutes=1)
        self.assertEqual(results, ["ok", "ok", "laco_pausado"])


if __name__ == "__main__":
    unittest.main()
