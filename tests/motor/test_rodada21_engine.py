"""Rodada 21 do motor (banco/engine/evolution): estágios terminais numa constante só, ingestão sem lista de closers
falha fechada e findMessages com formato desconhecido deixa de parecer 'sem mensagens'. Tudo com falsos."""

import unittest
from unittest import mock

from motor import engine, jobs
from motor.db import TERMINAL_STAGES, Database
from motor.evolution import EvolutionClient, EvolutionError
from tests.motor.support import LEAD_PHONE, OWNER_PHONE, inbound, make_ctx


class TerminalStagesTest(unittest.TestCase):
    def test_constante_e_membros(self):
        self.assertEqual(set(TERMINAL_STAGES), {"venda", "perdido", "desqualificado", "compareceu"})
        self.assertNotIn("no_show", TERMINAL_STAGES)
        self.assertNotIn("reagendado", TERMINAL_STAGES)

    def test_active_leads_do_banco_real_exclui_cada_terminal(self):
        db = Database.__new__(Database)
        calls = []
        db.select = lambda table, params: calls.append(params) or []
        db.active_leads()
        excluded = calls[0]["stage"]
        for stage in ("venda", "perdido", "desqualificado", "compareceu"):
            with self.subTest(stage=stage):
                self.assertIn(stage, excluded)
        for stage in ("no_show", "reagendado"):
            self.assertNotIn(stage, excluded)

    def test_recover_unanswered_ignora_cada_terminal(self):
        for stage in ("venda", "perdido", "desqualificado", "compareceu"):
            with self.subTest(stage=stage):
                ctx = make_ctx()
                lead = inbound(ctx, "obrigado pela aula")
                ctx.db.update_lead(lead["id"], {"stage": stage})
                # o banco falso antigo não filtra 'compareceu': o motor não pode depender só do filtro do banco
                ctx.db.active_leads = lambda limit=1000, db=ctx.db: [dict(l) for l in db.leads.values()]
                self.assertEqual(engine.recover_unanswered(ctx), set())

    def test_no_show_e_reagendado_seguem_ativos(self):
        for stage in ("no_show", "reagendado"):
            with self.subTest(stage=stage):
                ctx = make_ctx()
                lead = inbound(ctx, "pode remarcar?")
                ctx.db.update_lead(lead["id"], {"stage": stage})
                self.assertEqual(engine.recover_unanswered(ctx), {lead["id"]})

    def test_lead_compareceu_que_escreve_nao_recebe_resposta_da_ia(self):
        ctx = make_ctx()
        ctx.evo.add(LEAD_PHONE, "valeu, adorei a aula", wa_id="W1")
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        ctx.db.update_lead(lead["id"], {"stage": "compareceu"})
        engine.cycle(ctx)
        self.assertEqual(ctx.ai.calls, [])
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])


class IngestaoSemListaDeClosersTest(unittest.TestCase):
    def _sem_lista(self, ctx):
        def boom(active_only=False):
            raise RuntimeError("supabase 503")
        ctx.db.list_closers = boom
        ctx.state.pop("closer_phones", None)
        engine.refresh(ctx)
        self.assertIsNone(ctx.state["ignored"])

    def test_ingest_chat_levanta_ingestao_indisponivel(self):
        ctx = make_ctx()
        self._sem_lista(ctx)
        with self.assertRaises(engine.IngestaoIndisponivel):
            engine.ingest_chat(ctx, LEAD_PHONE)

    def test_sem_lista_nada_sai_para_o_lead_e_no_ciclo_seguinte_responde(self):
        ctx = make_ctx()
        lead = inbound(ctx, "quero saber mais")
        real = ctx.db.list_closers
        self._sem_lista(ctx)
        ctx.ai.push({"reply": "Oi! Posso ajudar.", "action": "responder"})
        engine.cycle(ctx)
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])
        self.assertEqual(ctx.ai.calls, [])   # nem a IA foi chamada
        self.assertIn(lead["id"], ctx.state.get("pending", set()))   # o turno ficou para o próximo ciclo
        ctx.db.list_closers = real   # a lista carrega
        engine.cycle(ctx)
        self.assertEqual(len(ctx.evo.texts_to(LEAD_PHONE)), 1)

    def test_sem_lista_aviso_ao_dono_continua_saindo(self):
        ctx = make_ctx()
        self._sem_lista(ctx)
        self.assertTrue(engine.queue_owner_alert(ctx, "teste interno"))
        engine.cycle(ctx)
        self.assertIn("teste interno", " ".join(ctx.evo.texts_to(OWNER_PHONE)))

    def test_jobs_que_falam_com_lead_sao_pulados_sem_a_lista(self):
        ctx = make_ctx()
        self._sem_lista(ctx)
        with mock.patch.object(jobs, "followups") as fu:
            result = jobs.run_all(ctx)
        fu.assert_not_called()
        self.assertTrue(result["followups"].startswith("pulado"))


class FormatoFindMessagesTest(unittest.TestCase):
    def _client(self, body):
        client = EvolutionClient("http://x", "k", "inst")
        client._call = lambda *a, **k: body
        return client

    def test_vazios_reais_devolvem_lista_vazia(self):
        for body in ([], {"messages": {"records": []}}, {"messages": {"total": 0, "pages": 0, "currentPage": 1}},
                     {"messages": []}, {"records": []}):
            with self.subTest(body=body):
                client = self._client(body)
                self.assertEqual(client.recent_messages(), [])
                self.assertEqual(client.chat_messages(LEAD_PHONE), [])

    def test_formato_desconhecido_levanta_evolution_error_sem_o_corpo(self):
        client = self._client({"foo": [], "segredo": "TOKEN-123"})
        for call in (client.recent_messages, lambda: client.chat_messages(LEAD_PHONE)):
            with self.assertRaises(EvolutionError) as cm:
                call()
            self.assertIn("formato de resposta desconhecido em findMessages", str(cm.exception))
            self.assertIn("foo", str(cm.exception))
            self.assertNotIn("TOKEN-123", str(cm.exception))

    def test_formato_desconhecido_alerta_o_dono_uma_vez_por_dia(self):
        ctx = make_ctx()
        err = EvolutionError("formato de resposta desconhecido em findMessages: chaves foo")
        from motor.evolution import EvolutionFormatoDesconhecido
        ctx.evo.recent_messages = lambda limit=50, page=1: (_ for _ in ()).throw(
            EvolutionFormatoDesconhecido(str(err)))
        engine.cycle(ctx)
        engine.cycle(ctx)
        sent = [t for t in ctx.evo.texts_to(OWNER_PHONE) if "resposta da Evolution" in t]
        self.assertEqual(len(sent), 1)


if __name__ == "__main__":
    unittest.main()
