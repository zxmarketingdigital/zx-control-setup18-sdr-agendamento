"""Rodada 13 (instalador): chaves de qualificação únicas, key não-string, mensagem do cmd_dono, motor_subiu com PID
morto e passo whatsapp só com saída do agente depois da mensagem do aluno (subprocess e HTTP mockados)."""
import argparse
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "setup"))

import _comum  # noqa: E402
import etapa_banco  # noqa: E402
import etapa_config  # noqa: E402
import servico  # noqa: E402
import teste_guiado  # noqa: E402

SUPA = {"url": "https://abcdefghijklmnopqrst.supabase.co", "service_role_key": "x"}


def setUpModule():
    from tests.setup import _isolamento
    _isolamento.ligar()


def tearDownModule():
    from tests.setup import _isolamento
    _isolamento.desligar()


class ChavesDeQualificacao(unittest.TestCase):
    def test_colisao_por_truncamento_ganha_sufixo_deterministico(self):
        r = etapa_config._normalizar_perguntas([
            "Qual o seu faturamento mensal aproximado?",
            "Qual o seu faturamento mensal atual?",
            "Qual o seu faturamento mensal estimado?"])
        chaves = [p["key"] for p in r]
        self.assertEqual(len(set(chaves)), 3)
        self.assertTrue(chaves[1].endswith("_2") and chaves[2].endswith("_3"))
        self.assertTrue(all(len(c) <= 30 for c in chaves))
        self.assertEqual(chaves, [p["key"] for p in etapa_config._normalizar_perguntas([
            "Qual o seu faturamento mensal aproximado?", "Qual o seu faturamento mensal atual?",
            "Qual o seu faturamento mensal estimado?"])])

    def test_key_explicita_repetida_tambem_e_desduplicada(self):
        r = etapa_config._normalizar_perguntas([{"pergunta": "A?", "key": "x"}, {"pergunta": "B?", "key": "x"}])
        self.assertEqual([p["key"] for p in r], ["x", "x_2"])

    def test_key_nao_string_vira_erro_de_validacao_com_indice(self):
        d = json.loads(json.dumps(etapa_config.MODELO_ENTREVISTA)) if hasattr(etapa_config, "MODELO_ENTREVISTA") else {}
        d.setdefault("qualificacao", {})["perguntas"] = [{"pergunta": "Ok?"}, {"pergunta": "Tem CNPJ?", "key": 1}]
        erros = [e for e in etapa_config.validar_entrevista(d) if "perguntas[1].key" in e]
        self.assertEqual(len(erros), 1)


class DonoMensagem(unittest.TestCase):
    def test_delete_falho_diz_que_o_novo_dono_ja_foi_liberado(self):
        _comum.CONFIG_PATH.write_text(json.dumps({"supabase": SUPA}))
        os.environ["SDR_OWNER_PASSWORD"] = "senha-de-teste-123"
        self.addCleanup(os.environ.pop, "SDR_OWNER_PASSWORD", None)

        def rest(config, metodo, caminho, corpo=None, *a, **k):
            return (500, "boom") if metodo == "DELETE" else (204, [])
        args = argparse.Namespace(email="dono@exemplo.com", dry_run=False)
        with mock.patch.object(etapa_banco, "_achar_usuario", return_value=None), \
                mock.patch.object(etapa_banco, "http_json", return_value=(201, {"id": "uuid-novo"})), \
                mock.patch.object(etapa_banco, "supabase_rest", rest), \
                mock.patch.object(etapa_banco, "erro") as erro:
            codigo = etapa_banco.cmd_dono(args)
        msg = " ".join(c[0][0] for c in erro.call_args_list)
        self.assertEqual(codigo, 1)
        self.assertIn("JÁ FOI liberado", msg)
        self.assertNotIn("Nada foi gravado", msg)


class MotorSubiu(unittest.TestCase):
    def test_trava_com_pid_morto_nao_conta(self):
        with tempfile.TemporaryDirectory() as d:
            lock = Path(d) / "motor.lock"
            lock.write_text("4242", encoding="utf-8")
            with mock.patch.object(servico, "identidade_do_pid", return_value="morto"):
                self.assertFalse(servico.motor_subiu(time.time() - 5, lock, espera=0))
            with mock.patch.object(servico, "identidade_do_pid", return_value="outro"):
                self.assertFalse(servico.motor_subiu(time.time() - 5, lock, espera=0))
            with mock.patch.object(servico, "identidade_do_pid", return_value="motor"):
                self.assertTrue(servico.motor_subiu(time.time() - 5, lock, espera=0))


class RespostaDoAgente(unittest.TestCase):
    def setUp(self):
        self.inicio = datetime.now(timezone.utc)
        self.entrou = (self.inicio + timedelta(seconds=10)).isoformat()
        self.saiu = (self.inicio + timedelta(seconds=20)).isoformat()
        self.lead = {"last_inbound_at": self.entrou, "last_outbound_at": self.saiu}

    def test_sem_mensagem_out_do_agente_depois_do_aluno_nao_passa(self):
        antiga = {"direction": "out", "author": "agente", "created_at": (self.inicio - timedelta(hours=1)).isoformat()}
        dono = {"direction": "out", "author": "dono", "created_at": self.saiu}
        sistema = {"direction": "out", "author": "sistema", "created_at": self.saiu}
        self.assertFalse(teste_guiado.resposta_nova(self.lead, self.inicio, []))
        self.assertFalse(teste_guiado.resposta_nova(self.lead, self.inicio, [antiga, dono, sistema]))

    def test_mensagem_out_do_agente_depois_do_aluno_passa(self):
        boa = {"direction": "out", "author": "agente", "created_at": self.saiu}
        self.assertTrue(teste_guiado.resposta_nova(self.lead, self.inicio, [boa]))

    def test_passo_whatsapp_consulta_messages(self):
        _comum.CONFIG_PATH.write_text(json.dumps({"supabase": SUPA, "evolution": {"instance": "i"}}))
        chamadas = []

        def rest(config, metodo, caminho, *a, **k):
            chamadas.append(caminho)
            if caminho.startswith("/messages"):
                return 200, []   # só saída de sistema/antiga: nada do agente
            return 200, [{"id": "l1", "stage": "novo", "last_inbound_at": datetime.now(timezone.utc).isoformat(),
                          "last_outbound_at": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()}]
        with mock.patch.object(teste_guiado, "supabase_rest", rest), mock.patch.object(teste_guiado, "ESPERA_WHATSAPP", 0.2), \
                mock.patch.object(teste_guiado.time, "sleep"), mock.patch.object(teste_guiado, "erro"), \
                mock.patch.object(teste_guiado, "load_config", return_value={"evolution": {"instance": "i"}}):
            passou = teste_guiado.passo_whatsapp({"evolution": {"instance": "i"}}, {"telefone_simulacao": "+5511999990000"})
        self.assertFalse(passou)
        self.assertTrue(any(c.startswith("/messages") and "author=eq.agente" in c and "direction=eq.out" in c for c in chamadas))


if __name__ == "__main__":
    unittest.main()
