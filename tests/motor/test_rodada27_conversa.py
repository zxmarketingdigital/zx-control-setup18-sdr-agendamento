"""Rodada 27, fatia C: frase de estado do agendamento com horário errado, estágio compareceu terminal,
links com TLD fora da lista e regra de prompt para mídia não transcrita. Tudo com Evolution/banco/IA falsos."""
import unittest

from motor import agenda
from motor.agenda import tz_of
from motor.conversa import Decision, apply_decision, build_system_prompt, handle_turn, strip_unknown_links
from tests.motor.support import LEAD_PHONE, OWNER_PHONE, default_agent, inbound, make_ctx, sp


def _lead_agendado(ctx, texto="que horas é minha call?"):
    ctx.db.slots.append(sp(19, 10))           # 10h de amanhã livre: o horário errado "existe" na agenda
    lead = inbound(ctx, texto)
    ctx.db.book_slot(lead["id"], sp(19, 15))   # a call real é amanhã às 15h
    return lead


class EstadoDoAgendamentoTest(unittest.TestCase):
    def _reply(self, ia_reply, action="responder"):
        ctx = make_ctx()
        lead = _lead_agendado(ctx)
        ctx.ai.push({"reply": ia_reply, "action": action})
        self.assertEqual(handle_turn(ctx, lead["id"])["status"], "ok")
        return ctx.evo.texts_to(LEAD_PHONE)[-1]

    def test_horario_errado_vira_horario_real(self):
        out = self._reply("Sua call está marcada para amanhã às 10h")
        self.assertIn("19/11/2026 às 15:00", out)
        self.assertNotIn("10h", out)

    def test_dia_errado_vira_horario_real(self):
        out = self._reply("Seu horário está confirmado para sexta às 15h.")
        self.assertIn("19/11/2026 às 15:00", out)

    def test_horario_certo_passa_intacto(self):
        reply = "Sua call está marcada para amanhã às 15h, combinado?"
        self.assertEqual(self._reply(reply), reply)

    def test_so_a_frase_errada_e_trocada(self):
        out = self._reply("Sua call está marcada para amanhã às 10h. Qualquer dúvida é só falar.")
        self.assertIn("19/11/2026 às 15:00", out)
        self.assertIn("Qualquer dúvida é só falar.", out)

    def test_oferta_de_remarcacao_segue_a_regra_de_horarios_livres(self):
        reply = "Sua call é amanhã às 15h. Posso remarcar para amanhã às 10h?"
        self.assertEqual(self._reply(reply), reply)

    def test_funcao_pura(self):
        ctx = make_ctx()
        tz, now = tz_of(ctx), ctx.now()
        start = sp(19, 15)
        fix = agenda.fix_appointment_statement
        self.assertIn("15:00", fix("Sua reunião está agendada para 10h", start, tz, now))
        self.assertEqual(fix("Sua reunião está agendada para as 15h", start, tz, now), "Sua reunião está agendada para as 15h")
        self.assertEqual(fix("Atendemos das 9h às 18h.", start, tz, now), "Atendemos das 9h às 18h.")


class CompareceuTerminalTest(unittest.TestCase):
    def test_lead_compareceu_nao_recebe_resposta_nem_muda_de_estagio(self):
        ctx = make_ctx()
        lead = inbound(ctx, "quero a próxima turma")
        ctx.db.update_lead(lead["id"], {"stage": "compareceu"})
        ctx.ai.push({"reply": "Vamos marcar!", "action": "agendar", "slot": "2026-11-19T18:00:00+00:00"})
        self.assertEqual(handle_turn(ctx, lead["id"])["status"], "estagio_final")
        self.assertEqual(ctx.db.leads[lead["id"]]["stage"], "compareceu")
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])
        self.assertEqual(ctx.db.appointments, [])
        self.assertEqual(len([t for t in ctx.evo.texts_to(OWNER_PHONE) if "compareceu" in t]), 1)

    def test_apply_decision_nao_grava_agendado_sobre_terminal(self):
        ctx = make_ctx()
        ctx.db.promote_stage_on_book = False
        lead = inbound(ctx, "quero outra data")
        ctx.db.update_lead(lead["id"], {"stage": "compareceu"})
        snapshot = ctx.db.get_lead(lead["id"])
        d = Decision(reply="Marquei!", action="agendar", slot="2026-11-19T18:00:00+00:00")
        apply_decision(ctx, snapshot, default_agent(), d, list(ctx.db.slots), None, "quero outra data")
        self.assertEqual(ctx.db.leads[lead["id"]]["stage"], "compareceu")


class LinksTest(unittest.TestCase):
    def _strip(self, text, base="Site: minhaempresa.com.br"):
        ctx = make_ctx()
        ctx.state["agent"] = dict(ctx.state["agent"], knowledge=base)
        return strip_unknown_links(ctx, ctx.state["agent"], text)

    def test_links_com_esquema_e_qualquer_host_saem(self):
        for url in ("https://conta-verificacao.pw/entrar", "http://203.0.113.5/x", "www.golpe.xx/a"):
            out = self._strip(f"Confirme seu pagamento em {url} agora mesmo por favor")
            self.assertNotIn(url, out)

    def test_dominios_sem_esquema_com_tld_comum_saem(self):
        for dom in ("conta.pw", "banco.cc", "x.cn", "pagamento.de", "a.uk", "b.ml", "c.tk", "d.top", "e.xyz",
                    "f.info", "g.site", "h.online", "i.click", "j.us", "k.eu", "l.es", "m.fr", "n.gg", "o.in", "p.ly"):
            out = self._strip(f"Acesse {dom}/entrar para continuar a nossa conversa agora")
            self.assertNotIn(dom, out, dom)

    def test_link_da_base_e_mantido(self):
        reply = "Veja os detalhes em https://minhaempresa.com.br/planos e em www.minhaempresa.com.br hoje mesmo"
        self.assertEqual(self._strip(reply), reply)

    def test_negativos_nao_viram_link(self):
        for text in ("O valor é R$ 1.500,00 e fica ótimo para você neste mês",
                     "Nos falamos às 9.30h então, combinado para amanhã cedo",
                     "Sr. João, o horário das 10.30 está livre para a conversa",
                     "Tudo certo.Obrigado por responder, vamos seguir com a conversa"):
            self.assertEqual(self._strip(text), text)


class PromptTest(unittest.TestCase):
    def test_regra_de_midia_nao_transcrita(self):
        ctx = make_ctx()
        lead = inbound(ctx, "[áudio]")
        prompt = build_system_prompt(ctx, lead, ctx.state["agent"], [], None, [])
        self.assertIn("[áudio]", prompt)
        self.assertIn("escreva a mensagem", prompt)


if __name__ == "__main__":
    unittest.main()
