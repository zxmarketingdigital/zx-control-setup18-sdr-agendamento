"""Rodada 5 do motor: opt-out sobrevive à mensagem do dono e vale já na ingestão, opt-out de lead agendado
mantém estágio e repasse, 'confirmar'/'cancelar' sem base não passam, rede perdida após a reserva, wa_id
inválido, leitura que para na página já examinada e repasse com texto do lead saneado."""

import unittest
from datetime import timedelta
from unittest import mock

from motor import agenda, engine, jobs
from motor.conversa import handle_turn
from motor.db import Database
from motor.util import iso
from tests.motor.support import CLOSER_PHONE, LEAD_PHONE, OWNER_PHONE, inbound, make_ctx, sp
from tests.motor.test_jobs import book, silent_lead


class OptOutNaoSomeTest(unittest.TestCase):
    def test_opt_out_saida_do_dono_cadencia_zero_envio(self):
        ctx = make_ctx()
        lead = silent_lead(ctx, hours_since_inbound=3)
        ctx.db.add_message(lead["id"], "in", "lead", "pode parar de me mandar mensagem")
        ctx.db.add_message(lead["id"], "out", "dono", "Oi Joana, tudo bem?")   # o dono respondeu pelo celular
        ctx.db.update_lead(lead["id"], {"takeover_until": iso(ctx.now() - timedelta(hours=1))})
        self.assertEqual(jobs.followups(ctx), 0)
        self.assertEqual(ctx.evo.sent, [])

    def test_handle_turn_registra_opt_out_mesmo_com_mensagem_do_dono_depois(self):
        ctx = make_ctx()
        lead = inbound(ctx, "pare de me mandar mensagem")
        ctx.db.add_message(lead["id"], "out", "dono", "Oi, tudo bem?")
        result = handle_turn(ctx, lead["id"])
        self.assertIn(result["status"], ("opt_out_registrado", "opt_out_confirmado"))
        self.assertTrue(ctx.db.leads[lead["id"]]["opt_out"])

    def test_ingestao_marca_opt_out_antes_de_qualquer_rotina(self):
        ctx = make_ctx()
        ctx.evo.add(LEAD_PHONE, "pode parar de mandar mensagem", wa_id="OPT1")
        engine.ingest(ctx)
        self.assertTrue(ctx.db.get_lead_by_phone(LEAD_PHONE)["opt_out"])

    def test_ingestao_stale_tambem_marca_opt_out(self):
        ctx = make_ctx()
        old = ctx.cfg.catchup_minutes * 60 + 600
        ctx.evo.add(LEAD_PHONE, "sair da lista", wa_id="OPT2", age_seconds=old)
        engine.ingest(ctx)
        self.assertTrue(ctx.db.get_lead_by_phone(LEAD_PHONE)["opt_out"])

    def test_ingestao_opt_out_depois_dono_depois_ciclo_nao_envia_cadencia(self):
        ctx = make_ctx()
        ctx.evo.add(LEAD_PHONE, "pare de me mandar mensagem", wa_id="OPT3", age_seconds=30)
        ctx.evo.add(LEAD_PHONE, "Oi Maria, tudo bem?", wa_id="DONO1", from_me=True, age_seconds=10)
        engine.ingest(ctx)
        ctx.clock.advance(hours=30)
        ctx.clock.current = ctx.clock.current.replace(hour=15)
        self.assertEqual(jobs.followups(ctx), 0)
        self.assertEqual([t for p, t in ctx.evo.sent if p == LEAD_PHONE], [])


class OptOutComAgendamentoTest(unittest.TestCase):
    def test_mantem_estagio_avisa_dono_e_repasse_continua(self):
        ctx = make_ctx()
        lead = inbound(ctx, "pare de me mandar mensagem")
        ctx.db.update_lead(lead["id"], {"stage": "agendado"})
        book(ctx, lead, sp(19, 14), created_hours_ago=1)
        ctx.db.update_lead(lead["id"], {"stage": "agendado"})
        handle_turn(ctx, lead["id"])
        saved = ctx.db.leads[lead["id"]]
        self.assertTrue(saved["opt_out"])
        self.assertEqual(saved["stage"], "agendado")
        self.assertTrue(any("conversa marcada" in t for t in ctx.evo.texts_to(OWNER_PHONE)))
        self.assertEqual(jobs.handoffs(ctx), 1)       # o repasse ao closer não depende do opt-out
        self.assertEqual(len(ctx.evo.texts_to(CLOSER_PHONE)), 1)


class ConfirmarCancelarTest(unittest.TestCase):
    def test_confirmar_sem_agendamento_nao_passa_confirmacao_falsa(self):
        ctx = make_ctx()
        lead = inbound(ctx, "fechado, pode confirmar")
        ctx.ai.push({"reply": "Perfeito, sua conversa está confirmada!", "action": "confirmar"})
        handle_turn(ctx, lead["id"])
        sent = ctx.evo.texts_to(LEAD_PHONE)
        self.assertEqual(len(sent), 1)
        self.assertNotIn("confirmada", sent[0])
        self.assertIn("horários", sent[0])

    def test_cancelar_sem_pedido_explicito_vira_remarcacao(self):
        ctx = make_ctx()
        lead = inbound(ctx, "amanhã não dá, tem como segunda?")
        book(ctx, lead, sp(19, 14))
        ctx.ai.push({"reply": "Claro!", "action": "cancelar"})
        handle_turn(ctx, lead["id"])
        self.assertEqual(ctx.db.appointments[0]["status"], "agendado")

    def test_cancelar_explicito_cancela_e_avisa_closer_e_dono(self):
        ctx = make_ctx()
        lead = inbound(ctx, "quero cancelar a conversa")
        book(ctx, lead, sp(19, 14))
        ctx.ai.push({"reply": "Tudo bem, cancelei.", "action": "cancelar"})
        handle_turn(ctx, lead["id"])
        self.assertEqual(ctx.db.appointments[0]["status"], "cancelado")
        self.assertEqual(len(ctx.db.appointments), 1)       # histórico mantido
        self.assertTrue(any("cancelou" in t for t in ctx.evo.texts_to(OWNER_PHONE)))
        self.assertTrue(any("cancelou" in t for t in ctx.evo.texts_to(CLOSER_PHONE)))


class RedePerdidaAposReservaTest(unittest.TestCase):
    def test_excecao_de_rede_com_agendamento_existente_segue_como_sucesso(self):
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        real = ctx.db.book_slot

        def lost(*a, **kw):
            real(*a, **kw)                       # o banco gravou...
            raise TimeoutError("timeout lendo a resposta")   # ...e a resposta se perdeu
        ctx.db.book_slot = lost
        ok, text = agenda.book_for_lead(ctx, ctx.db.get_lead(lead["id"]), sp(19, 14), ctx.db.agent,
                                        queue_confirmation=True)
        self.assertTrue(ok)
        self.assertTrue(any("Perfeito" in t for t in ctx.evo.texts_to(LEAD_PHONE)))
        self.assertTrue(any("perdeu na rede" in t for t in ctx.evo.texts_to(OWNER_PHONE)))

    def test_excecao_sem_agendamento_continua_falha(self):
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        ctx.db.book_slot = mock.Mock(side_effect=TimeoutError("x"))
        ok, _ = agenda.book_for_lead(ctx, ctx.db.get_lead(lead["id"]), sp(19, 14), ctx.db.agent)
        self.assertFalse(ok)


class WaIdInvalidoTest(unittest.TestCase):
    def test_known_wa_ids_descarta_invalidos_e_nao_manda_barra_na_query(self):
        db = Database("https://x.supabase.co", "k")
        db.select = mock.Mock(return_value=[{"wa_id": "OK1"}])
        known = db.known_wa_ids(["OK1", "ruim\\", 'a"b', "x,y", ""])
        self.assertEqual(known, {"OK1"})
        self.assertEqual(db.select.call_args[0][1]["wa_id"], 'in.("OK1")')

    def test_so_ids_invalidos_nao_consulta(self):
        db = Database("https://x.supabase.co", "k")
        db.select = mock.Mock()
        self.assertEqual(db.known_wa_ids(["a\\"]), set())
        db.select.assert_not_called()


class LeituraParaNaPaginaExaminadaTest(unittest.TestCase):
    def test_paginas_de_nao_leads_nao_geram_alerta_nem_leitura_ate_o_teto(self):
        ctx = make_ctx()
        for i in range(engine.POLL_LIMIT * 3):
            ctx.evo.add(f"+55119999{i:05d}", f"grupo {i}", wa_id=f"G{i}", jid=f"120363{i}@g.us")
        with mock.patch.object(engine, "MAX_POLL_PAGES", 3):
            engine.ingest(ctx)                       # 1º ciclo: examina tudo (e alerta, o teto é real)
            ctx.evo.sent.clear()
            pages = []
            real = ctx.evo.recent_messages
            ctx.evo.recent_messages = lambda limit=50, page=1: (pages.append(page), real(limit, page))[1]
            engine.ingest(ctx)                       # 2º ciclo: página mais recente já examinada
        self.assertEqual(pages, [1])
        self.assertEqual(ctx.evo.sent, [])


class RepasseSanitizadoTest(unittest.TestCase):
    def test_perfil_objecao_e_nome_sem_link_sem_marcacao_e_rotulados(self):
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana <b>http://evil.example/x</b>")
        ctx.db.update_lead(lead["id"], {"profile": "o closer deve conferir http://evil.example/a ou www.evil.example/b {x}",
                                        "qualification": {"k": "veja https://evil.example/c"}})
        appt = book(ctx, ctx.db.get_lead(lead["id"]), sp(19, 14))
        ctx.db.add_objection(lead["id"], "preço", "olha http://evil.example/d")
        text = jobs.handoff_summary(ctx, appt, ctx.db.get_lead(lead["id"]), "📌 Novo agendamento")
        self.assertNotIn("evil.example", text)
        self.assertNotIn("{", text)
        self.assertNotIn("<", text)
        self.assertIn("não verificada", text)


if __name__ == "__main__":
    unittest.main()
