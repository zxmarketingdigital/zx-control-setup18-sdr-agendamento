"""Leitura da decisão da IA (JSON), detectores de texto e a regra de nunca inventar horário."""

import unittest

from motor import agenda
from motor.ai import AIError
from motor.conversa import (asks_identity, clean_reply, decide, is_opt_out, looks_like_autoresponder,
                            parse_decision)
from motor.util import get_tz
from tests.motor.support import FakeAI, make_ctx, sp

TZ = get_tz("America/Sao_Paulo")


class ParseDecisionTest(unittest.TestCase):
    def test_json_completo(self):
        d = parse_decision('{"reply":"Oi!","action":"agendar","slot":"2026-11-19T17:00:00Z","nome":"Joana",'
                           '"email":"j@x.com","perfil":"dona de clínica","qualificacao":{"interesse":"avaliação"},'
                           '"score":82,"objecao":{"categoria":"preco","trecho":"tá caro"},"slots":["a","b"]}')
        self.assertTrue(d.valid)
        self.assertEqual((d.action, d.reply, d.name, d.email), ("agendar", "Oi!", "Joana", "j@x.com"))
        self.assertEqual(d.slot, "2026-11-19T17:00:00Z")
        self.assertEqual(d.qualification, {"interesse": "avaliação"})
        self.assertEqual(d.score, 82)
        self.assertEqual(d.objection["categoria"], "preco")
        self.assertEqual(d.slots, ["a", "b"])

    def test_bloco_markdown_e_texto_em_volta(self):
        d = parse_decision('Claro!\n```json\n{"reply":"Olá","action":"qualificar"}\n```')
        self.assertTrue(d.valid)
        self.assertEqual((d.reply, d.action), ("Olá", "qualificar"))

    def test_texto_puro_vira_resposta_simples(self):
        d = parse_decision("Olá, tudo bem?")
        self.assertTrue(d.valid)
        self.assertEqual((d.reply, d.action), ("Olá, tudo bem?", "responder"))

    def test_json_quebrado_e_invalido(self):
        for broken in ('{"reply": "oi", "action": "agendar"', "", '{"action": "agendar", ', "```json\n{\n```"):
            self.assertFalse(parse_decision(broken).valid, broken)

    def test_json_cortado_sem_acao_aproveita_a_resposta(self):
        d = parse_decision('{"reply": "Posso ajudar!", "perfil": "dona de cl')
        self.assertTrue(d.valid)
        self.assertEqual((d.reply, d.action), ("Posso ajudar!", "responder"))

    def test_acao_desconhecida_vira_responder_e_alias_funciona(self):
        self.assertEqual(parse_decision('{"reply":"x","action":"inventada"}').action, "responder")
        self.assertEqual(parse_decision('{"reply":"x","action":"book"}').action, "agendar")
        self.assertEqual(parse_decision('{"reply":"x","action":"OPT-OUT"}').action, "opt_out")
        self.assertEqual(parse_decision('{"reply":"x","action":"ofertar horarios"}').action, "ofertar_horarios")

    def test_score_fica_entre_0_e_100_e_lixo_vira_none(self):
        self.assertEqual(parse_decision('{"reply":"x","score":250}').score, 100)
        self.assertEqual(parse_decision('{"reply":"x","score":-5}').score, 0)
        self.assertIsNone(parse_decision('{"reply":"x","score":"alto"}').score)
        self.assertIsNone(parse_decision('{"reply":"x","score":true}').score)

    def test_objecao_em_texto_simples(self):
        self.assertEqual(parse_decision('{"reply":"x","objecao":"preco"}').objection["categoria"], "preco")

    def test_clean_reply_troca_negrito_do_markdown_pelo_do_whatsapp(self):
        self.assertEqual(clean_reply("Isso é **muito** bom"), "Isso é *muito* bom")


class DecideTest(unittest.TestCase):
    def test_tenta_de_novo_uma_vez_quando_o_json_vem_ilegivel(self):
        ctx = make_ctx()
        ctx.ai = FakeAI(['{"reply": "x", "action": "agend', {"reply": "ok", "action": "responder"}])
        self.assertEqual(decide(ctx, "sys", "conversa").reply, "ok")
        self.assertEqual(len(ctx.ai.calls), 2)

    def test_duas_respostas_ilegiveis_viram_aiError(self):
        ctx = make_ctx()
        ctx.ai = FakeAI(['{"action": "agend', "```"])
        with self.assertRaises(AIError):
            decide(ctx, "sys", "conversa")


class DetectoresTest(unittest.TestCase):
    def test_opt_out(self):
        for text in ("parar", "Pare por favor", "STOP", "sair da lista", "não quero mais receber mensagens",
                     "para de me mandar mensagem", "tire meu número da lista", "Não me envie mais nada"):
            self.assertTrue(is_opt_out(text), text)

    def test_nao_e_opt_out(self):
        for text in ("quero parar de pagar aluguel", "parar de pagar aluguel", "quero sair do aluguel",
                     "não quero mais morar aqui", "oi", "preciso pensar", ""):
            self.assertFalse(is_opt_out(text), text)

    def test_identidade_e_autoresponder(self):
        self.assertTrue(asks_identity("Você é um robô?"))
        self.assertTrue(asks_identity("estou falando com uma pessoa ou bot"))
        self.assertFalse(asks_identity("oi, tudo bem?"))
        self.assertTrue(looks_like_autoresponder("Atendimento automático: digite 1 para vendas"))
        self.assertFalse(looks_like_autoresponder("quero marcar uma conversa"))


class RedeDeSegurancaTest(unittest.TestCase):
    def test_horario_fora_da_agenda_e_detectado(self):
        slots = [sp(19, 14), sp(19, 15, 30)]
        self.assertFalse(agenda.mentions_unlisted_time("Pode ser às 14h?", slots, TZ))
        self.assertFalse(agenda.mentions_unlisted_time("às 15:30 fica bom", slots, TZ))
        self.assertTrue(agenda.mentions_unlisted_time("Pode ser às 16h?", slots, TZ))
        self.assertTrue(agenda.mentions_unlisted_time("às 14:45", slots, TZ))

    def test_horas_de_duracao_nao_sao_horario(self):
        self.assertFalse(agenda.mentions_unlisted_time("A resposta sai em 24 horas ou em 2 horas", [], TZ))

    def test_promessa_de_agendamento(self):
        self.assertTrue(agenda.claims_booking("Agendei para amanhã!"))
        self.assertTrue(agenda.claims_booking("Pronto, ficou marcado para sexta"))
        self.assertFalse(agenda.claims_booking("Qual horário fica melhor?"))
        self.assertFalse(agenda.claims_booking("Sua conversa está marcada para sexta", has_appointment=True))
        self.assertTrue(agenda.claims_booking("Remarquei para sexta", has_appointment=True))

    def test_oferta_escolhe_dois_horarios_de_dias_diferentes_quando_da(self):
        slots = [sp(19, 14), sp(19, 15), sp(20, 10)]
        picked = agenda.pick_offers(slots, TZ)
        self.assertEqual(len(picked), 2)
        self.assertNotEqual(picked[0].astimezone(TZ).date(), picked[1].astimezone(TZ).date())

    def test_oferta_sem_horarios(self):
        self.assertEqual(agenda.pick_offers([], TZ), [])



class HorarioEAutorespostaTest(unittest.TestCase):
    def test_formas_comuns_de_horario_fora_da_agenda_sao_pegas(self):
        from tests.motor.support import sp
        allowed = [sp(19, 14)]
        for text in ("pode ser 16hs", "às 16 horas", "16h30", "3 da tarde", "às 9", "9 da manhã"):
            self.assertTrue(agenda.mentions_unlisted_time(text, allowed, TZ), text)
        for text in ("às 14 horas", "14hs", "2 da tarde", "responde em 2 horas"):
            self.assertFalse(agenda.mentions_unlisted_time(text, allowed, TZ), text)

    def test_pergunta_sobre_horario_de_atendimento_nao_e_autoresposta(self):
        from motor.conversa import looks_like_autoresponder
        self.assertFalse(looks_like_autoresponder("Qual o horário de atendimento de vocês?"))
        self.assertFalse(looks_like_autoresponder("isso é um atendimento automático?"))
        self.assertTrue(looks_like_autoresponder("Esta é uma mensagem automática. No momento não podemos atender."))

    def test_variantes_de_opt_out_e_identidade(self):
        from motor.conversa import asks_identity, is_opt_out
        self.assertTrue(is_opt_out("pode parar de mandar mensagem"))
        self.assertTrue(asks_identity("vocês são um robô?"))
        self.assertFalse(is_opt_out("preciso parar de pagar aluguel"))


if __name__ == "__main__":
    unittest.main()
