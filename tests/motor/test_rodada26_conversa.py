"""Rodada 26, fatia C: opt-out avaliado por oração (o teto de palavras vale por oração)."""
import unittest

from motor.conversa import is_opt_out


class OptOutPorOracaoTest(unittest.TestCase):
    def test_saida_em_segunda_oracao(self):
        self.assertTrue(is_opt_out("Não tenho interesse. Para de mandar mensagem."))

    def test_mensagem_longa_com_saida_na_ultima_oracao(self):
        text = ("Oi, tudo bem? Vi que vocês me procuraram várias vezes nas últimas semanas sobre aquele assunto e eu "
                "já disse que não preciso. Por favor, me tira da lista de vocês. Obrigado")
        self.assertGreater(len(text.split()), 25)
        self.assertTrue(is_opt_out(text))

    def test_saida_explicita_vence_adiamento_entre_oracoes(self):
        self.assertTrue(is_opt_out("Para de me mandar mensagem. Me chama amanhã"))

    def test_negativos(self):
        for text in ("Para de mandar o boleto por aqui, manda por e-mail", "Me manda mensagem amanhã às 10h?",
                     "Para de me mandar mensagem depois das 18h. Obrigado", "Não para de me mandar mensagem. Ok.",
                     "Oi. Quero saber o preço do plano anual."):
            self.assertFalse(is_opt_out(text), text)

    def test_padrao_sem_me_nao_ancorado(self):
        self.assertTrue(is_opt_out("Já falei que não quero. Por favor, pare de mandar mensagem"))
        self.assertFalse(is_opt_out("O sistema para de mandar mensagem quando cai a internet"))
        self.assertFalse(is_opt_out("Pare de mandar mensagem depois das 18h"))


if __name__ == "__main__":
    unittest.main()
