"""Rodada 20 (instalador): restauração do `ia` confirmada, `verificar` sem confundir falha de leitura com tabela ausente e
marca de início do teste guiado pelo relógio do banco (tudo mockado, sem rede)."""
import argparse
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "setup"))

import _comum  # noqa: E402
import etapa_banco  # noqa: E402
import etapa_config  # noqa: E402
import teste_guiado  # noqa: E402

SUPA = {"url": "https://abc.supabase.co", "service_role_key": "k"}


def setUpModule():
    from tests.setup import _isolamento
    _isolamento.ligar()


def tearDownModule():
    from tests.setup import _isolamento
    _isolamento.desligar()


class IaRestauracaoConfirmada(unittest.TestCase):
    def setUp(self):
        self.original = json.dumps({"ai": {"provider": "gemini", "api_key": "k-velha", "model": ""}, "supabase": SUPA}, indent=3)
        _comum.CONFIG_PATH.write_text(self.original, encoding="utf-8")
        os.environ["SDR_AI_API_KEY"] = "chave-nova"
        self.addCleanup(os.environ.pop, "SDR_AI_API_KEY", None)
        self.mensagens = []

    def _rodar(self, restaura_ok, banco_final):
        """O PATCH do provedor novo falha (503). `restaura_ok` = o PATCH de restauração; `banco_final` = o que a releitura vê."""
        def rest(config, metodo, caminho, corpo=None, *a, **k):
            if metodo == "PATCH":
                if corpo == {"ai_provider": "openai"}:
                    return 503, "erro"
                return (200, [{"id": 1}]) if restaura_ok else (503, "erro")
            if self.mensagens_lidas == 0:
                self.mensagens_lidas += 1
                return 200, [{"ai_provider": "gemini"}]  # leitura do estado anterior
            return 200, [{"ai_provider": banco_final}]
        self.mensagens_lidas = 0
        with mock.patch.object(etapa_config, "supabase_rest", rest), \
                mock.patch.object(etapa_config, "erro", side_effect=lambda m: self.mensagens.append(m)):
            return etapa_config.cmd_ia(argparse.Namespace(provider="openai", model=None))

    def test_restauracao_confirmada_mantem_a_mensagem_de_sem_divergencia(self):
        codigo = self._rodar(True, "gemini")
        self.assertEqual(codigo, 1)
        self.assertIn("continuam com o provedor anterior", self.mensagens[-1])
        self.assertNotIn("divergem", self.mensagens[-1])

    def test_restauracao_que_falha_avisa_divergencia(self):
        codigo = self._rodar(False, "gemini")
        self.assertNotEqual(codigo, 0)
        self.assertIn("ATENÇÃO: banco e config divergem", self.mensagens[-1])
        self.assertIn("python3 setup/etapa_config.py ia --provider openai", self.mensagens[-1])
        self.assertNotIn("continuam com o provedor anterior", self.mensagens[-1])

    def test_releitura_que_nao_confirma_avisa_divergencia(self):
        codigo = self._rodar(True, "openai")  # PATCH de restauração "ok", mas o banco segue com o provedor novo
        self.assertNotEqual(codigo, 0)
        self.assertIn("banco e config divergem", self.mensagens[-1])


class VerificarFalhaDeLeitura(unittest.TestCase):
    def setUp(self):
        _comum.CONFIG_PATH.write_text(json.dumps({"supabase": SUPA}), encoding="utf-8")
        self.estado = []
        self.saida = []
        for p in (mock.patch.object(etapa_banco, "salvar_estado", side_effect=lambda d: self.estado.append(d)),
                  mock.patch.object(etapa_banco, "instrucoes_sql_editor", side_effect=lambda *a, **k: self.saida.append("plano-b")),
                  mock.patch.object(etapa_banco, "erro", side_effect=lambda m: self.saida.append(m)),
                  mock.patch.object(etapa_banco, "aviso", side_effect=lambda m: self.saida.append(m)),
                  mock.patch.object(etapa_banco, "ok"), mock.patch.object(etapa_banco, "info")):
            p.start()
            self.addCleanup(p.stop)

    def _verificar(self, status, corpo):
        with mock.patch.object(etapa_banco, "supabase_rest", return_value=(status, corpo)):
            return etapa_banco.cmd_verificar(argparse.Namespace())

    def test_401_e_falha_de_leitura_sem_mexer_no_estado(self):
        for st in (401, 403, 408, 429, 503):
            self.estado.clear()
            self.saida.clear()
            self.assertEqual(self._verificar(st, {"message": "x"}), 1, st)
            self.assertEqual(self.estado, [], st)
            self.assertNotIn("plano-b", self.saida, st)
            self.assertTrue(any("HTTP %d" % st in m and "service_role" in m for m in self.saida), st)

    def test_404_continua_tabela_faltando(self):
        self.assertEqual(self._verificar(404, {"code": "PGRST205"}), 1)
        self.assertIn({"banco_migrado": False}, self.estado)
        self.assertTrue(any("Faltam" in m for m in self.saida))

    def test_corpo_de_relacao_inexistente_vale_como_ausente(self):
        self.assertEqual(self._verificar(400, {"code": "42P01", "message": 'relation "x" does not exist'}), 1)
        self.assertIn({"banco_migrado": False}, self.estado)


class InicioPeloRelogioDoBanco(unittest.TestCase):
    def _datetime_com_relogio(self, deslocamento):
        real = datetime

        class Falso(real):
            @classmethod
            def now(cls, tz=None):
                return real.now(tz) + deslocamento
        return Falso

    def _passo(self, deslocamento, entrou, saiu, banco_agora):
        lead = {"id": "l1", "stage": "novo", "last_inbound_at": entrou.isoformat(), "last_outbound_at": saiu.isoformat()}
        agente = [{"direction": "out", "author": "agente", "created_at": saiu.isoformat()}]

        def rest(config, metodo, caminho, *a, **k):
            if caminho.startswith("/messages?select=created_at&order"):
                return 200, [{"created_at": banco_agora.isoformat()}]
            if caminho.startswith("/messages"):
                return 200, agente
            return 200, [lead]
        with mock.patch.object(teste_guiado, "supabase_rest", rest), mock.patch.object(teste_guiado, "ESPERA_WHATSAPP", 0.2), \
                mock.patch.object(teste_guiado, "datetime", self._datetime_com_relogio(deslocamento)), \
                mock.patch.object(teste_guiado.time, "sleep"), mock.patch.object(teste_guiado, "erro"), \
                mock.patch.object(teste_guiado, "aviso"):
            return teste_guiado.passo_whatsapp({"evolution": {"instance": "i"}}, {"telefone_simulacao": "+5511999990000"})

    def test_relogio_local_adiantado_3_min_nao_reprova_resposta_real(self):
        banco = datetime.now(timezone.utc)
        self.assertTrue(self._passo(timedelta(minutes=3), banco + timedelta(seconds=10), banco + timedelta(seconds=20), banco))

    def test_relogio_local_atrasado_3_min_nao_aprova_resposta_antiga(self):
        banco = datetime.now(timezone.utc)
        antigo_in, antigo_out = banco - timedelta(minutes=2), banco - timedelta(minutes=1)
        self.assertFalse(self._passo(-timedelta(minutes=3), antigo_in, antigo_out, banco))

    def test_sem_mensagens_usa_o_date_do_servidor(self):
        servidor = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)
        with mock.patch.object(teste_guiado, "supabase_rest", return_value=(200, [])), \
                mock.patch.object(teste_guiado, "_data_do_servidor", return_value=servidor):
            self.assertEqual(teste_guiado.inicio_pelo_banco({}), (servidor, "servidor"))

    def test_sem_nada_cai_no_relogio_local_com_aviso(self):
        with mock.patch.object(teste_guiado, "supabase_rest", return_value=(200, [])), \
                mock.patch.object(teste_guiado, "_data_do_servidor", return_value=None), \
                mock.patch.object(teste_guiado, "aviso") as av:
            _, fonte = teste_guiado.inicio_pelo_banco({})
        self.assertEqual(fonte, "local")
        av.assert_called_once()


if __name__ == "__main__":
    unittest.main()
