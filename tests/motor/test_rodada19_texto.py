"""Rodada 19, fatia B1: horário informativo, opt-out com canal/documento e adjetivo 'marcadas'."""
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from motor import agenda, conversa

TZ = ZoneInfo("America/Sao_Paulo")
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=TZ)


def _slots():
    base = datetime(2026, 11, 19, 0, 0, tzinfo=TZ)
    return [(base + timedelta(hours=14)).astimezone(timezone.utc), (base + timedelta(hours=15)).astimezone(timezone.utc)]


class HorarioInformativoTest(unittest.TestCase):
    def test_horario_de_funcionamento_passa_intacto(self):
        t = "Atendemos das 9h às 18h, de segunda a sexta."
        self.assertFalse(agenda.offers_unlisted_time(t, _slots(), TZ, NOW))
        self.assertEqual(agenda.strip_unlisted_offers(t, _slots(), TZ, NOW), t)

    def test_duracao_passa_intacta(self):
        t = "A call dura 1h e é bem objetiva."
        self.assertEqual(agenda.strip_unlisted_offers(t, _slots(), TZ, NOW), t)

    def test_oferta_inventada_e_cortada(self):
        t = "Tenho 10h amanhã."
        self.assertTrue(agenda.offers_unlisted_time(t, _slots(), TZ, NOW))
        self.assertNotIn("10h", agenda.strip_unlisted_offers(t, _slots(), TZ, NOW))

    def test_duas_frases_so_a_oferta_sai(self):
        t = "Atendemos das 9h às 18h. Posso agendar às 10h para você."
        out = agenda.strip_unlisted_offers(t, _slots(), TZ, NOW)
        self.assertIn("Atendemos das 9h às 18h.", out)
        self.assertNotIn("10h", out)

    def test_frase_pendurada_remove_a_frase_nao_a_resposta(self):
        t = "Fico feliz em ajudar com isso. Temos vaga das 9h às 10h."
        out = agenda.strip_unlisted_offers(t, _slots(), TZ, NOW)
        self.assertEqual(out, "Fico feliz em ajudar com isso.")

    def test_so_sobra_nada_cai_na_neutra(self):
        out = agenda.strip_unlisted_offers("Tenho 10h.", _slots(), TZ, NOW)
        self.assertEqual(out, agenda.NEUTRAL_AFTER_STRIP)


class OptOutCanalTest(unittest.TestCase):
    def test_boleto_por_whatsapp_com_pergunta(self):
        self.assertFalse(conversa.is_opt_out("Não quero receber o boleto por WhatsApp, manda por e-mail?"))

    def test_contrato_por_aqui(self):
        self.assertFalse(conversa.is_opt_out("não quero receber o contrato por aqui"))

    def test_pergunta_nao_e_opt_out_solto(self):
        self.assertFalse(conversa.is_opt_out("não quero receber mensagens de áudio, pode ser?"))

    def test_opt_out_real_continua(self):
        for t in ("não quero mais receber mensagens", "não quero mais receber nada", "não me mande mais mensagem"):
            self.assertTrue(conversa.is_opt_out(t), t)


class ClaimsBookingTest(unittest.TestCase):
    def test_descritivo_nao_e_promessa(self):
        self.assertFalse(agenda.claims_booking("As turmas são marcadas para segunda às 19h."))
        self.assertFalse(agenda.claims_booking("As aulas são agendadas às terças 20h."))

    def test_promessa_continua(self):
        for t in ("Agendei para amanhã às 10h.", "Ficou marcado para sexta.", "Sua call está confirmada para quinta às 15h.",
                  "Deixei agendado para você segunda.", "Marquei você para dia 20.",
                  "Horário agendado para você amanhã."):
            self.assertTrue(agenda.claims_booking(t), t)


if __name__ == "__main__":
    unittest.main()
