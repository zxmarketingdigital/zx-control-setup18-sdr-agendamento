"""Rodada 22, fatia M1: negação ampla de contato é saída, claims_booking com 'combinado'/slot livre e links por host.
Tudo com Evolution/banco/IA falsos."""
import unittest

from motor import agenda, conversa
from motor.conversa import handle_turn, strip_unknown_links
from motor.agenda import tz_of
from tests.motor.support import LEAD_PHONE, OWNER_PHONE, inbound, make_ctx, sp


def alerts(ctx, needle="restrição de horário ou canal"):
    return [t for t in ctx.evo.texts_to(OWNER_PHONE) if needle in t]


class OptOutNegacaoAmplaTest(unittest.TestCase):
    def test_negacao_ampla_e_saida_mesmo_citando_ligacao_ou_com_pergunta_tag(self):
        for text in ("Não quero mais receber mensagens nem ligações.", "Não quero mais receber mensagens, ok?",
                     "não quero mais contato", "Não quero mais contato, certo?", "não me incomode", "não me incomode mais",
                     "pare de falar comigo", "Pare de falar comigo, entendeu?", "não me procure mais",
                     "Pare de me mandar mensagens, tá?", "pare, beleza?", "não quero mais receber, viu?"):
            self.assertTrue(conversa.is_opt_out(text), text)

    def test_restricao_positiva_nao_e_saida(self):
        for text in ("só por e-mail", "Não quero mais receber mensagens, só por e-mail", "só depois das 18h",
                     "só pelo zap", "não quero receber o boleto por aqui", "pare de me mandar depois das 18h",
                     "não me incomode antes das 9h", "Não quero receber ligação, pode me chamar pelo WhatsApp"):
            self.assertFalse(conversa.is_opt_out(text), text)

    def test_so_recusa_de_canal_continua_restricao(self):
        for text in ("Não quero receber ligação", "não quero receber mais nenhuma ligação", "Não quero receber mensagem de voz"):
            self.assertFalse(conversa.is_opt_out(text), text)

    def test_pergunta_genuina_continua_nao_sendo_saida(self):
        for text in ("você remove meu número da base depois da reunião?", "posso descadastrar?",
                     "você pode não me incomodar depois da reunião?"):
            self.assertFalse(conversa.is_opt_out(text), text)

    def test_negacao_ampla_que_nao_vira_saida_alerta_o_dono_uma_vez(self):
        ctx = make_ctx()
        lead = inbound(ctx, "não quero receber o boleto por aqui")
        ctx.ai.push({"reply": "Sem problema, não envio boleto por aqui.", "action": "responder"})
        self.assertEqual(handle_turn(ctx, lead["id"])["status"], "ok")
        self.assertFalse(ctx.db.leads[lead["id"]].get("opt_out"))
        self.assertEqual(len(alerts(ctx)), 1)
        conversa._alert_restriction(ctx, ctx.db.leads[lead["id"]], "não quero receber o boleto por aqui")
        self.assertEqual(len(alerts(ctx)), 1)

    def test_saida_por_negacao_ampla_registra_opt_out_sem_alerta_de_restricao(self):
        ctx = make_ctx()
        lead = inbound(ctx, "Não quero mais receber mensagens nem ligações.")
        self.assertEqual(handle_turn(ctx, lead["id"])["status"], "opt_out_confirmado")
        self.assertTrue(ctx.db.leads[lead["id"]].get("opt_out"))
        self.assertEqual(alerts(ctx), [])


class PromessaDeAgendamentoTest(unittest.TestCase):
    def _slots(self):
        return [sp(19, 14), sp(19, 15), sp(20, 10), sp(21, 16)]

    def test_palavras_de_combinado_com_dia_ou_hora_sao_promessa(self):
        for t in ("Combinado, amanhã às 10h, te espero!", "Garantido, quinta às 15h.", "Fechado para sexta às 10h!",
                  "Te aguardo amanhã.", "Nos vemos quinta às 14h.", "Combinado! Amanhã às 10h."):
            self.assertTrue(agenda.claims_booking(t), t)

    def test_sem_dia_ou_hora_ou_em_pergunta_ou_horario_de_funcionamento_nao_conta(self):
        for t in ("Combinado, qual horário você prefere?", "Fechado!", "Nos vemos em breve.", "Tudo garantido por aqui.",
                  "Combinado quinta às 15h?", "Estamos fechados aos domingos.",
                  "Atendemos de segunda a sexta das 9h às 18h; aos domingos estamos fechados."):
            self.assertFalse(agenda.claims_booking(t), t)

    def test_agendamento_ativo_nao_conta_combinado(self):
        self.assertFalse(agenda.claims_booking("Combinado, quinta às 15h, te espero!", True))

    def test_dia_e_hora_de_slot_livre_sem_oferta_e_promessa(self):
        ctx = make_ctx()
        tz, now = tz_of(ctx), ctx.now()
        kw = dict(slots=self._slots(), tz=tz, now=now)
        self.assertTrue(agenda.claims_booking("Amanhã às 14h.", **kw))        # amanhã = 19/11, 14h é slot livre
        self.assertFalse(agenda.claims_booking("Amanhã às 10h.", **kw))       # 10h só existe no dia 20
        self.assertTrue(agenda.claims_booking("Quinta às 15h.", **kw))
        self.assertTrue(agenda.claims_booking("Então é quinta, 15h", **kw))

    def test_oferta_pergunta_ou_horario_que_nao_e_slot_nao_conta(self):
        ctx = make_ctx()
        kw = dict(slots=self._slots(), tz=tz_of(ctx), now=ctx.now())
        for t in ("Tenho quinta às 15h livre, serve?", "Quinta às 15h pode ser?", "Quinta às 15h está disponível.",
                  "Atendemos quinta às 18h.", "Qual prefere, quinta às 14h ou às 15h?", "Quinta às 9h."):
            self.assertFalse(agenda.claims_booking(t, **kw), t)

    def test_fluxo_completo_combinado_vira_oferta_de_horarios_reais(self):
        ctx = make_ctx()
        lead = inbound(ctx, "quinta às 15h pode?")
        ctx.ai.push({"reply": "Combinado, quinta às 15h, te espero!", "action": "responder"})
        self.assertEqual(handle_turn(ctx, lead["id"])["action"], "ofertar_horarios")
        self.assertNotIn("te espero", ctx.evo.texts_to(LEAD_PHONE)[0])

    def test_fluxo_slot_livre_sem_palavra_chave_vira_oferta(self):
        ctx = make_ctx()
        lead = inbound(ctx, "quinta às 15h pode?")
        ctx.ai.push({"reply": "Quinta às 15h então.", "action": "responder"})
        self.assertEqual(handle_turn(ctx, lead["id"])["action"], "ofertar_horarios")


class LinksPorHostTest(unittest.TestCase):
    def _ctx(self, base):
        ctx = make_ctx()
        ctx.state["agent"] = dict(ctx.state["agent"], knowledge=base)
        return ctx

    def test_host_que_e_substring_de_dominio_da_base_e_removido(self):
        ctx = self._ctx("Nosso site é minhaempresa.com.br e o e-mail é oi@minhaempresa.com.br")
        out = strip_unknown_links(ctx, ctx.state["agent"], "Veja o catálogo completo e as condições em https://empresa.com.br hoje mesmo")
        self.assertNotIn("empresa.com.br", out)

    def test_host_da_base_com_www_ou_subdominio_e_mantido(self):
        ctx = self._ctx("Site: minhaempresa.com.br")
        for url in ("www.minhaempresa.com.br", "https://app.minhaempresa.com.br/agenda", "https://minhaempresa.com.br/x"):
            reply = f"Veja o catálogo completo e as condições em {url} hoje mesmo"
            self.assertIn(url, strip_unknown_links(ctx, ctx.state["agent"], reply), url)

    def test_email_da_base_e_mantido(self):
        ctx = self._ctx("Escreva para oi@minhaempresa.com.br")
        reply = "Se preferir, escreva para oi@minhaempresa.com.br com os detalhes."
        self.assertIn("oi@minhaempresa.com.br", strip_unknown_links(ctx, ctx.state["agent"], reply))


if __name__ == "__main__":
    unittest.main()
