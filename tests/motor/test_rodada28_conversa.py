"""Rodada 28 (conversa): remoção explícita vence o adiamento; período do dia é restrição de horário."""
import unittest

from motor import conversa


class RemocaoVenceAdiamento(unittest.TestCase):
    def test_remocao_com_adiamento_e_opt_out(self):
        for msg in ("Tira meu número da lista agora, me chama semana que vem",
                    "remove meu número agora, me chama semana que vem",
                    "me descadastra agora, me chama depois"):
            with self.subTest(msg=msg):
                self.assertTrue(conversa.is_opt_out(msg))

    def test_adiamento_puro_continua_adiamento(self):
        self.assertFalse(conversa.is_opt_out("agora não, me chama mais tarde"))

    def test_lista_de_espera_nao_e_saida(self):
        self.assertFalse(conversa.is_opt_out("Me tira da lista de espera agora, me chama semana que vem"))


class PeriodoDoDia(unittest.TestCase):
    def test_periodo_e_restricao_nao_saida(self):
        for msg in ("Não me mande mensagem pela manhã.", "não me mande mensagem pela tarde",
                    "Não me mande mensagem de madrugada", "não me mande mensagem à noite",
                    "não me mande mensagem durante a tarde", "não me mande mensagem na parte da manhã",
                    "não me mande mensagem no período da noite", "nao me mande mensagem pela manha"):
            with self.subTest(msg=msg):
                self.assertFalse(conversa.is_opt_out(msg))
                self.assertTrue(conversa.is_restriction_flag(msg))

    def test_saida_simples_continua(self):
        self.assertTrue(conversa.is_opt_out("não me mande mais mensagem"))
        self.assertFalse(conversa.is_restriction_flag("não me mande mais mensagem"))


if __name__ == "__main__":
    unittest.main()
