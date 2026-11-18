"""Reconexão do WhatsApp pelo painel: o instalador conhece a tabela/RPC novas e avisa o aluno."""
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.setup import _isolamento

RAIZ = Path(__file__).resolve().parents[2]
import etapa_banco  # noqa: E402
import etapa_config  # noqa: E402

AVISO = "Se o WhatsApp cair depois, reconecte pelo painel: menu WhatsApp."


class EsperadosDoBanco(unittest.TestCase):
    def setUp(self):
        _isolamento.ligar()

    def tearDown(self):
        _isolamento.desligar()

    def test_tabela_e_rpc_novas_sao_esperadas(self):
        self.assertIn("whatsapp_connection", etapa_banco.TABELAS)
        self.assertIn("request_whatsapp_command", etapa_banco.RPCS)

    def test_rpc_ausente_e_apontada_pelo_verificar(self):
        paths = {"/rpc/" + r: {} for r in etapa_banco.RPCS if r != "request_whatsapp_command"}
        with mock.patch.object(etapa_banco, "supabase_rest", lambda c, m, p, *a, **k: (200, {"paths": paths})):
            ausentes, falha = etapa_banco._ler_rpcs({})
        self.assertIsNone(falha)
        self.assertEqual(ausentes, ["/rpc/request_whatsapp_command"])

    def test_migration_nova_existe_e_cria_tabela_e_funcao(self):
        sql = (RAIZ / "supabase" / "migrations" / "20261118000002_whatsapp_conexao.sql")
        if not sql.exists():
            self.skipTest("migration da fatia A ainda não está no repositório")
        texto = sql.read_text(encoding="utf-8")
        self.assertIn("whatsapp_connection", texto)
        self.assertIn("request_whatsapp_command", texto)
        self.assertIn(sql.name, [a.name for a in etapa_banco.migracoes()])


class MensagemDoEvolution(unittest.TestCase):
    def setUp(self):
        _isolamento.ligar()

    def tearDown(self):
        _isolamento.desligar()

    def _rodar(self):
        args = SimpleNamespace(url="http://localhost:8080", instance="sdr", trocar_chave=False, criar=False)
        out = io.StringIO()
        with mock.patch.object(etapa_config, "load_config", lambda: {"evolution": {"api_key": "k" * 12}}), \
                mock.patch.object(etapa_config, "_estado_instancia", lambda u, c, i: (200, "open", {})), \
                mock.patch.object(etapa_config, "atualizar_config", lambda *a, **k: None), \
                mock.patch.object(etapa_config, "_invalidar_teste", lambda *a, **k: None), \
                mock.patch.object(etapa_config, "_apagar_qr", lambda: None), \
                redirect_stdout(out):
            codigo = etapa_config.cmd_evolution(args)
        return codigo, out.getvalue()

    def test_mensagem_do_painel_aparece_ao_conectar(self):
        codigo, saida = self._rodar()
        self.assertEqual(codigo, 0)
        self.assertIn(AVISO, saida)

    def test_textos_sem_seu_mac(self):
        for rel in ("README.md", "CLAUDE.md", "skills/sdr-agendamento/SKILL.md", "skills/sdr-instalar/SKILL.md"):
            self.assertNotIn("seu mac", (RAIZ / rel).read_text(encoding="utf-8").lower(), rel)
        # docs/PLAN.md é interno e não vai para o repositório público.
        plano = RAIZ / "docs" / "PLAN.md"
        if plano.exists():
            self.assertNotIn("seu mac", plano.read_text(encoding="utf-8").lower())


if __name__ == "__main__":
    unittest.main()
