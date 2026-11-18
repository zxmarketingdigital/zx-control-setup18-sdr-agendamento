"""Rodada 23 do motor (jobs/engine): marca de aguardando humano restaurada se o turno falha, teto de páginas do poll
sempre alerta e retomada com 'em' inválido não bloqueia a cadência. Tudo com falsos."""

import unittest
from datetime import timedelta
from unittest import mock

from motor import engine, jobs
from motor.util import iso
from tests.motor.support import LEAD_PHONE, NOW, OWNER_PHONE, inbound, make_ctx
from tests.motor.test_jobs import silent_lead


def owner_texts(ctx, needle):
    return [t for t in ctx.evo.texts_to(OWNER_PHONE) if needle in t]


class AguardandoHumanoRestauraMarcaTest(unittest.TestCase):
    def _ctx(self):
        ctx = make_ctx()
        stamp = iso(NOW - timedelta(hours=1))
        lead = silent_lead(ctx, qualification={jobs.AGUARDA_HUMANO_KEY: stamp, "nota_dono": "x"})
        return ctx, lead, stamp

    def test_handle_turn_falha_marca_volta_e_proximo_run_tenta_de_novo(self):
        ctx, lead, stamp = self._ctx()
        with mock.patch.object(jobs, "handle_turn", side_effect=RuntimeError("supabase fora")):
            self.assertEqual(jobs.aguardando_humano(ctx), 0)
        q = ctx.db.leads[lead["id"]]["qualification"]
        self.assertEqual(q.get(jobs.AGUARDA_HUMANO_KEY), stamp)
        self.assertEqual(q.get("nota_dono"), "x")
        calls = []
        with mock.patch.object(jobs, "handle_turn", lambda *a, **k: calls.append(a) or {"status": "ok"}):
            self.assertEqual(jobs.aguardando_humano(ctx), 1)
        self.assertEqual(len(calls), 1)
        self.assertNotIn(jobs.AGUARDA_HUMANO_KEY, ctx.db.leads[lead["id"]]["qualification"])
        self.assertEqual(len(owner_texts(ctx, "pausa da IA")), 1)   # a nova tentativa não repete o aviso

    def test_falha_ao_restaurar_vira_log_de_erro(self):
        ctx, lead, stamp = self._ctx()
        real = ctx.db.update_lead
        calls = {"n": 0}

        def flaky(lead_id, fields):
            calls["n"] += 1
            if calls["n"] >= 2:   # 1ª gravação (remover a marca) passa; a de restaurar falha
                raise RuntimeError("banco fora")
            return real(lead_id, fields)
        ctx.db.update_lead = flaky
        with mock.patch.object(jobs, "handle_turn", side_effect=RuntimeError("turno")), \
                mock.patch.object(ctx.log, "error") as err:
            jobs.aguardando_humano(ctx)
        self.assertTrue(any("restaurar a marca" in str(c.args[0]) for c in err.call_args_list))


class TetoDePaginasTest(unittest.TestCase):
    def _grupos(self, ctx, pages):
        for i in range(pages * engine.POLL_LIMIT):
            ctx.evo.add("+5511999990000", f"g{i}", wa_id=f"G{i}", jid="120363000000000001@g.us", age_seconds=30)

    def test_dez_paginas_de_grupo_e_lead_na_11a_alerta_uma_vez_por_dia(self):
        ctx = make_ctx()
        ctx.evo.add(LEAD_PHONE, "oi, quero saber", wa_id="LEAD1", age_seconds=60)
        self._grupos(ctx, engine.MAX_POLL_PAGES)
        engine.ingest(ctx)
        found = owner_texts(ctx, "limite de")
        self.assertEqual(len(found), 1)
        self.assertIn("mensagens mais antigas podem não ter sido lidas", found[0])
        self.assertIn("confira o WhatsApp", found[0])
        ctx.state["seen_wa_ids"] = set()
        engine.ingest(ctx)
        self.assertEqual(len(owner_texts(ctx, "limite de")), 1)   # mesmo dia: sem novo alerta

    def test_abaixo_do_teto_nao_alerta(self):
        ctx = make_ctx()
        self._grupos(ctx, 2)
        engine.ingest(ctx)
        self.assertEqual(owner_texts(ctx, "limite de"), [])


class RetomarEmInvalidoTest(unittest.TestCase):
    def test_em_que_nao_vira_data_remove_marcador_alerta_e_libera_a_cadencia(self):
        ctx = make_ctx()
        ctx.db.agent["followup_messages"] = [{"apos_horas": 1, "texto": "Oi {nome}, ainda por aí?"}]
        lead = silent_lead(ctx, hours_since_inbound=3, qualification={"_retomar": {"em": "amanhã cedo"}})
        self.assertEqual(jobs.followups(ctx), 0)   # ainda bloqueado pelo marcador
        self.assertEqual(jobs.retomar(ctx), 0)
        self.assertNotIn("_retomar", ctx.db.leads[lead["id"]]["qualification"])
        found = owner_texts(ctx, "retomada combinada")
        self.assertEqual(len(found), 1)
        self.assertIn("manualmente", found[0])
        self.assertIn(LEAD_PHONE.lstrip("+")[-8:], found[0])
        self.assertEqual(jobs.retomar(ctx), 0)
        self.assertEqual(len(owner_texts(ctx, "retomada combinada")), 1)   # marcador saiu: sem repetir
        self.assertEqual(jobs.followups(ctx), 1)   # a cadência voltou a funcionar


if __name__ == "__main__":
    unittest.main()
