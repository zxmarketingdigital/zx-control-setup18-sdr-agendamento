"""Rodada 12 (instalador): ia reverte o banco se o config falhar, guard destrutivo sem coluna e UPDATE sem WHERE,
verificar com tabela faltando, timestamp com fração curta e validação de perguntas (subprocess e HTTP mockados)."""
import argparse
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "setup"))

import _comum  # noqa: E402
import etapa_banco  # noqa: E402
import etapa_config  # noqa: E402
import teste_guiado  # noqa: E402

SUPA = {"url": "https://abcdefghijklmnopqrst.supabase.co", "service_role_key": "x"}


def setUpModule():
    from tests.setup import _isolamento
    _isolamento.ligar()


def tearDownModule():
    from tests.setup import _isolamento
    _isolamento.desligar()


class IaReverteBanco(unittest.TestCase):
    def setUp(self):
        _comum.CONFIG_PATH.write_text(json.dumps({"ai": {"provider": "gemini", "api_key": "k-velha", "model": ""}, "supabase": SUPA}))
        os.environ["SDR_AI_API_KEY"] = "chave-nova"
        self.addCleanup(os.environ.pop, "SDR_AI_API_KEY", None)

    def _rodar(self, revert_status=204):
        chamadas = []

        def rest(config, metodo, caminho, corpo=None, *a, **k):
            chamadas.append((metodo, corpo))
            if metodo == "GET":
                return 200, [{"ai_provider": "openai" if len([c for c in chamadas if c[0] == "PATCH"]) else "gemini"}]
            if corpo == {"ai_provider": "gemini"}:
                return revert_status, [{"id": 1}]
            return 204, [{"id": 1}]
        with mock.patch.object(etapa_config, "supabase_rest", rest), \
                mock.patch.object(etapa_config, "atualizar_config", side_effect=PermissionError(13, "Permission denied")), \
                mock.patch.object(etapa_config, "erro") as erro:
            codigo = etapa_config.cmd_ia(argparse.Namespace(provider="openai", model=None))
        return codigo, chamadas, " ".join(c[0][0] for c in erro.call_args_list)

    def test_config_falha_nao_toca_no_banco(self):  # r19: o config vem antes do banco
        codigo, chamadas, msg = self._rodar()
        self.assertNotEqual(codigo, 0)
        self.assertFalse([c for c in chamadas if c[0] == "PATCH"])
        self.assertIn("Nada foi alterado", msg)


class GuardDestrutivo(unittest.TestCase):
    def test_where_sem_coluna_e_trivial(self):
        for w in ("1 > 0", "NOT (1 = 0)", "1 = 1::int", "true IS TRUE", "'a' <> 'b'", "(1 = 1)"):
            self.assertIn("DELETE FROM sem WHERE", etapa_banco.sql_destrutivo("DELETE FROM leads WHERE %s;" % w), w)

    def test_update_sem_where_ou_trivial(self):
        self.assertIn("UPDATE sem WHERE", etapa_banco.sql_destrutivo("UPDATE leads SET x = 1;"))
        self.assertIn("UPDATE sem WHERE", etapa_banco.sql_destrutivo("update public.leads l set x = 1 where 1 > 0;"))

    def test_negativos(self):
        self.assertEqual(etapa_banco.sql_destrutivo("DELETE FROM leads WHERE id = 5;"), [])
        self.assertEqual(etapa_banco.sql_destrutivo("DELETE FROM leads WHERE id > 0;"), [])
        self.assertEqual(etapa_banco.sql_destrutivo("UPDATE leads SET x = 1 WHERE id = 5;"), [])
        self.assertEqual(etapa_banco.sql_destrutivo(
            "insert into t(a) values (1) on conflict (a) do update set a = 2;"), [])

    def test_migration_do_repo_nao_aciona(self):
        self.assertEqual(etapa_banco.perigos_das(etapa_banco.migracoes()), {})


class VerificarTabelaFaltando(unittest.TestCase):
    def test_grava_false_e_so_migrations_das_tabelas(self):
        with tempfile.TemporaryDirectory() as d:
            m = Path(d)
            (m / "001_a.sql").write_text("create table if not exists public.leads (id int);")
            (m / "002_b.sql").write_text("create table public.objections (id int);")
            _comum.CONFIG_PATH.write_text(json.dumps({"supabase": SUPA}))
            _comum.salvar_estado({"banco_migrado": True})

            def rest(config, metodo, caminho, *a, **k):
                return (404, "x") if caminho.startswith("/objections") else (200, [])
            with mock.patch.object(etapa_banco, "MIGRATIONS_DIR", m), mock.patch.object(etapa_banco, "supabase_rest", rest), \
                    mock.patch.object(etapa_banco, "instrucoes_sql_editor") as instr:
                codigo = etapa_banco.cmd_verificar(argparse.Namespace())
            self.assertEqual(codigo, 1)
            self.assertFalse(_comum.load_estado().get("banco_migrado"))
            pend = instr.call_args[0][0]
            self.assertEqual([p.name for p in pend], ["002_b.sql"])


class MarcaFracao(unittest.TestCase):
    def test_fracao_de_1_e_5_digitos(self):
        a = teste_guiado._marca("2026-11-18T12:00:00.5+00:00")
        b = teste_guiado._marca("2026-11-18T12:00:00.12345+00:00")
        self.assertEqual(a, datetime(2026, 11, 18, 12, 0, 0, 500000, tzinfo=timezone.utc))
        self.assertEqual(b, datetime(2026, 11, 18, 12, 0, 0, 123450, tzinfo=timezone.utc))
        self.assertIsNotNone(teste_guiado._marca("2026-11-18T12:00:00.123456789Z"))


class PerguntasInvalidas(unittest.TestCase):
    def _erros(self, perguntas):
        d = json.loads(json.dumps(etapa_config.MODELO)) if hasattr(etapa_config, "MODELO") else None
        if d is None:
            d = etapa_config.entrevista_modelo()
        d.setdefault("qualificacao", {})["perguntas"] = perguntas
        return [e for e in etapa_config.validar_entrevista(d) if "perguntas" in e]

    def test_itens_invalidos_com_indice(self):
        for ruim in ({"x": 1}, 5, "  ", {"pergunta": ""}, {"pergunta": "ok", "obrigatoria": "sim"}):
            erros = self._erros(["boa", ruim])
            self.assertEqual(len(erros), 1, ruim)
            self.assertIn("perguntas[1]", erros[0])

    def test_itens_validos(self):
        self.assertEqual(self._erros(["a?", {"pergunta": "b?"}, {"pergunta": "c?", "obrigatoria": False}]), [])


if __name__ == "__main__":
    unittest.main()
