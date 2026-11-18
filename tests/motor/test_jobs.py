"""Rotinas periódicas: follow-up na janela, anti-no-show 24h/2h sem duplicar, repasse ao closer e vigia."""

import json
import unittest
from datetime import timedelta

from motor import jobs
from motor.util import iso
from tests.motor.support import (CLOSER_PHONE, LEAD_PHONE, NOW, OWNER_PHONE, TEST_PHONE, default_agent, make_ctx,
                                 sp)


def silent_lead(ctx, hours_since_inbound=3, phone=LEAD_PHONE, **fields):
    """Lead que escreveu há `hours_since_inbound` horas e foi respondido logo depois (ficou sem retorno)."""
    lead = ctx.db.create_lead(phone, name="Joana Silva")
    ctx.db.update_lead(lead["id"], {
        "stage": "qualificado",
        "last_inbound_at": iso(ctx.now() - timedelta(hours=hours_since_inbound)),
        "last_outbound_at": iso(ctx.now() - timedelta(hours=hours_since_inbound) + timedelta(minutes=2)),
        **fields})
    return lead


def book(ctx, lead, start, created_hours_ago=72):
    if start not in ctx.db.slots:
        ctx.db.slots.append(start)
    result = ctx.db.book_slot(lead["id"], start)
    assert result["ok"], result
    appt = ctx.db.appointments[-1]
    appt["created_at"] = iso(ctx.now() - timedelta(hours=created_hours_ago))
    return appt


class FollowupTest(unittest.TestCase):
    def test_segue_a_cadencia_do_banco_a_partir_da_ultima_mensagem_do_lead(self):
        ctx = make_ctx()
        lead = silent_lead(ctx, hours_since_inbound=3)
        self.assertEqual(jobs.followups(ctx), 1)
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), ["Oi Joana, ainda posso te ajudar?"])
        self.assertEqual(ctx.db.leads[lead["id"]]["followup_step"], 1)
        self.assertEqual(jobs.followups(ctx), 0)                 # o 2º passo só com 24h
        ctx.clock.advance(hours=23)          # 08:00 do dia seguinte, dentro da janela
        self.assertEqual(jobs.followups(ctx), 1)
        self.assertEqual(len(ctx.evo.texts_to(LEAD_PHONE)), 2)
        ctx.clock.advance(hours=48)
        self.assertEqual(jobs.followups(ctx), 0)                 # cadência esgotada

    def test_nao_cobra_antes_do_prazo(self):
        ctx = make_ctx()
        silent_lead(ctx, hours_since_inbound=1)
        self.assertEqual(jobs.followups(ctx), 0)

    def test_so_dentro_da_janela_de_horario(self):
        ctx = make_ctx()
        silent_lead(ctx, hours_since_inbound=3)
        ctx.clock.current = sp(18, 20, 30)          # 20:30 em São Paulo: fora da janela 08:00-20:00
        self.assertFalse(jobs.in_followup_window(ctx))
        self.assertEqual(jobs.followups(ctx), 0)
        ctx.clock.current = sp(19, 7, 59)
        self.assertEqual(jobs.followups(ctx), 0)
        ctx.clock.current = sp(19, 8, 0)
        self.assertTrue(jobs.in_followup_window(ctx))
        self.assertEqual(jobs.followups(ctx), 1)

    def test_janela_vem_do_banco(self):
        ctx = make_ctx(default_agent(followup_window_start="10:00", followup_window_end="12:00"))
        silent_lead(ctx, hours_since_inbound=3)
        self.assertEqual(jobs.followups(ctx), 0)             # 09:00 em São Paulo
        ctx.clock.current = sp(18, 11)
        self.assertEqual(jobs.followups(ctx), 1)

    def test_janela_que_atravessa_a_meia_noite(self):
        ctx = make_ctx(default_agent(followup_window_start="22:00", followup_window_end="06:00"))
        ctx.clock.current = sp(18, 23)
        self.assertTrue(jobs.in_followup_window(ctx))
        ctx.clock.current = sp(18, 12)
        self.assertFalse(jobs.in_followup_window(ctx))

    def test_pula_quem_nao_deve_receber(self):
        ctx = make_ctx()
        silent_lead(ctx, phone="+5511999990011", opt_out=True)
        silent_lead(ctx, phone="+5511999990012", stage="agendado")
        silent_lead(ctx, phone="+5511999990013", takeover_until=iso(NOW + timedelta(hours=5)))
        silent_lead(ctx, phone="+5511999990014", stage="desqualificado")
        silent_lead(ctx, phone="+5511999990015", qualification={"_retomar": {"em": iso(NOW + timedelta(days=3))}})
        unanswered = silent_lead(ctx, phone="+5511999990016")
        ctx.db.update_lead(unanswered["id"], {"last_outbound_at": iso(NOW - timedelta(hours=10))})   # lead falou por último
        self.assertEqual(jobs.followups(ctx), 0)
        self.assertEqual(ctx.evo.sent, [])

    def test_agente_pausado_nao_cobra(self):
        ctx = make_ctx(default_agent(paused=True))
        silent_lead(ctx)
        self.assertEqual(jobs.followups(ctx), 0)

    def test_modo_teste_so_cobra_numeros_de_teste(self):
        ctx = make_ctx(default_agent(test_only=True, test_phones=[TEST_PHONE]))
        silent_lead(ctx, phone=LEAD_PHONE)
        silent_lead(ctx, phone=TEST_PHONE)
        self.assertEqual(jobs.followups(ctx), 1)
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])

    def test_sem_cadencia_no_banco_nao_envia(self):
        ctx = make_ctx(default_agent(followup_messages=[]))
        silent_lead(ctx)
        self.assertEqual(jobs.followups(ctx), 0)

    def test_lead_que_responde_zera_a_cadencia(self):
        ctx = make_ctx()
        lead = silent_lead(ctx)
        jobs.followups(ctx)
        from tests.motor.support import inbound
        inbound(ctx, "voltei")
        self.assertEqual(ctx.db.leads[lead["id"]]["followup_step"], 0)


class EstadoAntesDoEnvioTest(unittest.TestCase):
    def test_followup_grava_o_passo_antes_do_envio(self):
        ctx = make_ctx()
        lead = silent_lead(ctx, hours_since_inbound=3)
        seen = []
        real = ctx.evo.send_text
        ctx.evo.send_text = lambda *a, **k: (seen.append(ctx.db.leads[lead["id"]]["followup_step"]), real(*a, **k))[1]
        jobs.followups(ctx)
        self.assertEqual(seen, [1])

    def test_followup_falha_de_envio_reverte_o_passo(self):
        ctx = make_ctx()
        lead = silent_lead(ctx, hours_since_inbound=3)
        ctx.evo.fail_send = True
        self.assertEqual(jobs.followups(ctx), 0)
        self.assertEqual(ctx.db.leads[lead["id"]]["followup_step"], 0)

    def test_followup_timeout_incerto_mantem_o_passo_e_nao_repete(self):
        ctx = make_ctx()
        lead = silent_lead(ctx, hours_since_inbound=3)
        ctx.evo.timeout_send = True
        jobs.followups(ctx)
        ctx.evo.timeout_send = False
        jobs.followups(ctx)
        self.assertEqual(ctx.db.leads[lead["id"]]["followup_step"], 1)
        self.assertEqual(ctx.evo.sent, [])

    def test_lembrete_marcado_antes_e_revertido_se_o_envio_falha(self):
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        appt = book(ctx, lead, sp(18, 20))   # 8h depois de NOW: lembrete de 24h
        seen = []
        real = ctx.evo.send_text
        ctx.evo.send_text = lambda *a, **k: (seen.append(appt["reminder_24h_at"]), real(*a, **k))[1]
        jobs.reminders(ctx)
        self.assertIsNotNone(seen[0])
        ctx.evo.send_text = real
        ctx.db.update_appointment(appt["id"], {"reminder_24h_at": None})
        ctx.evo.fail_send = True
        jobs.reminders(ctx)
        self.assertIsNone(appt["reminder_24h_at"])


class ActiveLeadsPaginadoTest(unittest.TestCase):
    def test_pagina_ate_esgotar_sem_corte_em_1000(self):
        from motor.db import Database
        db = Database("https://x.supabase.co", "k")
        pages = []

        def fake_select(table, params=None):
            limit = int(params["limit"])
            after = int(params["id"].split(".", 1)[1]) if "id" in params else -1   # keyset (r7)
            pages.append(after)
            total = 2500
            return [{"id": i} for i in range(after + 1, min(total, after + 1 + limit))]
        db.select = fake_select
        self.assertEqual(len(db.active_leads()), 2500)
        self.assertEqual(pages, [-1, 999, 1999])


class LembretesTest(unittest.TestCase):
    def test_24h_e_2h_sem_duplicar(self):
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana Silva")
        appt = book(ctx, lead, sp(19, 14))                 # 19/11 às 14:00 SP = 17:00 UTC
        ctx.clock.current = sp(18, 14) - timedelta(hours=1)   # 25h antes
        self.assertEqual(jobs.reminders(ctx), 0)
        ctx.clock.current = sp(18, 14, 30)                    # 23,5h antes
        self.assertEqual(jobs.reminders(ctx), 1)
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), ["Oi Joana, lembrete: amanhã, 19/11/2026 às 14:00."])
        self.assertIsNotNone(ctx.db.appointments[0]["reminder_24h_at"])
        self.assertEqual(jobs.reminders(ctx), 0)               # não duplica
        ctx.clock.current = sp(19, 12, 30)                    # 1,5h antes
        self.assertEqual(jobs.reminders(ctx), 1)
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE)[-1], "Oi Joana, daqui a pouco, às 14:00.")
        self.assertIsNotNone(ctx.db.appointments[0]["reminder_2h_at"])
        self.assertEqual(jobs.reminders(ctx), 0)
        self.assertEqual(len(ctx.evo.texts_to(LEAD_PHONE)), 2)

    def test_reinicio_do_motor_nao_repete_lembrete(self):
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        book(ctx, lead, sp(19, 14))
        ctx.clock.current = sp(18, 15)
        jobs.reminders(ctx)
        fresh = make_ctx()                                  # motor novo, mesmo banco
        fresh.db = ctx.db
        fresh.clock.current = sp(18, 16)
        self.assertEqual(jobs.reminders(fresh), 0)

    def test_agendamento_feito_dentro_da_janela_nao_gera_lembrete_de_24h(self):
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        ctx.clock.current = sp(18, 17)
        book(ctx, lead, sp(19, 10), created_hours_ago=0)   # marcou hoje para amanhã cedo
        ctx.clock.advance(hours=1)
        self.assertEqual(jobs.reminders(ctx), 0)

    def test_lembrete_de_2h_ainda_sai_para_agendamento_feito_no_dia_anterior(self):
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        ctx.clock.current = sp(18, 17)
        book(ctx, lead, sp(19, 10), created_hours_ago=0)
        ctx.clock.current = sp(19, 8, 30)
        self.assertEqual(jobs.reminders(ctx), 1)

    def test_nao_manda_para_quem_pediu_para_parar_nem_a_agendamento_cancelado(self):
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        appt = book(ctx, lead, sp(19, 14))
        ctx.clock.current = sp(18, 15)
        ctx.db.update_lead(lead["id"], {"opt_out": True})
        self.assertEqual(jobs.reminders(ctx), 0)
        ctx.db.update_lead(lead["id"], {"opt_out": False})
        ctx.db.update_appointment(appt["id"], {"status": "cancelado"})
        self.assertEqual(jobs.reminders(ctx), 0)
        self.assertEqual(ctx.evo.sent, [])

    def test_falha_de_envio_nao_marca_como_enviado_e_tenta_no_proximo_ciclo(self):
        ctx = make_ctx()
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        book(ctx, lead, sp(19, 14))
        ctx.clock.current = sp(18, 15)
        ctx.evo.fail_send = True
        self.assertEqual(jobs.reminders(ctx), 0)
        self.assertIsNone(ctx.db.appointments[0]["reminder_24h_at"])
        ctx.evo.fail_send = False
        self.assertEqual(jobs.reminders(ctx), 1)

    def test_modelo_do_lembrete_vem_do_banco_ou_do_padrao(self):
        ctx = make_ctx(default_agent(reminder_24h_template=None))
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        book(ctx, lead, sp(19, 14))
        ctx.clock.current = sp(18, 15)
        jobs.reminders(ctx)
        self.assertIn("Passando para lembrar", ctx.evo.texts_to(LEAD_PHONE)[0])


class RepasseTest(unittest.TestCase):
    def prepare(self, **agent):
        ctx = make_ctx(default_agent(**agent))
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana Silva")
        ctx.db.update_lead(lead["id"], {"profile": "dona de clínica", "score": 80,
                                        "qualification": {"orcamento": "verba definida", "decisor": "sim", "_retomar": {}}})
        ctx.db.add_objection(lead["id"], "preco", "tá caro")
        appt = book(ctx, lead, sp(19, 14), created_hours_ago=0)
        return ctx, lead, appt

    def test_resumo_vai_para_o_whatsapp_do_closer_uma_vez(self):
        ctx, lead, appt = self.prepare()
        self.assertEqual(jobs.handoffs(ctx), 1)
        text = ctx.evo.texts_to(CLOSER_PHONE)[0]
        for part in ("Novo agendamento", "Joana Silva", LEAD_PHONE, "19/11/2026 às 14:00", "dona de clínica",
                     "orcamento: verba definida", "Score: 80/100", "preco", "tá caro"):
            self.assertIn(part, text)
        self.assertNotIn("_retomar", text)
        self.assertIsNotNone(ctx.db.appointments[0]["handoff_sent_at"])
        self.assertEqual(jobs.handoffs(ctx), 0)

    def test_uma_hora_antes_manda_o_segundo_resumo_sem_duplicar(self):
        ctx, lead, appt = self.prepare()
        jobs.handoffs(ctx)
        ctx.clock.current = sp(19, 13, 5)
        self.assertEqual(jobs.handoffs(ctx), 1)
        self.assertIn("começa em cerca de 1 hora", ctx.evo.texts_to(CLOSER_PHONE)[-1])
        self.assertEqual(jobs.handoffs(ctx), 0)
        self.assertEqual(len(ctx.evo.texts_to(CLOSER_PHONE)), 2)

    def test_sem_whatsapp_do_closer_cai_no_telefone_do_dono(self):
        ctx, lead, appt = self.prepare()
        ctx.db.closers[0]["whatsapp"] = ""
        self.assertEqual(jobs.handoffs(ctx), 1)
        self.assertEqual(len(ctx.evo.texts_to(OWNER_PHONE)), 1)

    def test_sem_nenhum_destino_nao_quebra_e_avisa_so_uma_vez(self):
        ctx, lead, appt = self.prepare(owner_phone="")
        ctx.db.closers[0]["whatsapp"] = ""
        self.assertEqual(jobs.handoffs(ctx), 0)
        self.assertEqual(jobs.handoffs(ctx), 0)
        self.assertEqual(ctx.evo.sent, [])

    def test_repasse_passa_pelo_modo_teste_porque_nao_e_para_lead(self):
        ctx, lead, appt = self.prepare(test_only=True, test_phones=[TEST_PHONE])
        self.assertEqual(jobs.handoffs(ctx), 1)


class VigiaTest(unittest.TestCase):
    def test_avisa_uma_vez_por_queda_e_avisa_quando_volta(self):
        ctx = make_ctx()
        ctx.evo.state = "close"
        self.assertEqual(jobs.vigia(ctx), "caiu")
        self.assertEqual(len(ctx.evo.texts_to(OWNER_PHONE)), 1)
        self.assertIn("desconectado", ctx.evo.texts_to(OWNER_PHONE)[0])
        self.assertEqual(jobs.vigia(ctx), "fora")              # continua caída: não repete o aviso
        self.assertEqual(jobs.vigia(ctx), "fora")
        self.assertEqual(len(ctx.evo.texts_to(OWNER_PHONE)), 1)
        ctx.evo.state = "open"
        self.assertEqual(jobs.vigia(ctx), "voltou")
        self.assertIn("voltou", ctx.evo.texts_to(OWNER_PHONE)[-1])
        self.assertEqual(jobs.vigia(ctx), "ok")
        self.assertEqual(len(ctx.evo.texts_to(OWNER_PHONE)), 2)

    def test_estado_da_queda_sobrevive_a_reinicio_do_motor(self):
        ctx = make_ctx()
        ctx.evo.state = "close"
        jobs.vigia(ctx)
        saved = json.loads((ctx.cfg.data_dir / "vigia.json").read_text(encoding="utf-8"))
        self.assertIn("down_since", saved)
        again = make_ctx(tmp=ctx.cfg.data_dir.parent)
        again.evo.state = "close"
        self.assertEqual(jobs.vigia(again), "fora")
        self.assertEqual(again.evo.sent, [])                   # não avisou de novo

    def test_evolution_sem_resposta_tambem_conta_como_queda(self):
        ctx = make_ctx()
        ctx.evo.state = "erro"
        self.assertEqual(jobs.vigia(ctx), "caiu")

    def test_run_all_pula_envios_enquanto_o_whatsapp_esta_fora(self):
        ctx = make_ctx()
        silent_lead(ctx)
        ctx.evo.state = "close"
        results = jobs.run_all(ctx)
        self.assertEqual(results["vigia"], "caiu")
        self.assertIn("pulado", results["followups"])
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])

    def test_run_all_isola_o_erro_de_uma_rotina(self):
        ctx = make_ctx()
        original = ctx.db.upcoming_appointments
        ctx.db.upcoming_appointments = lambda now: (_ for _ in ()).throw(RuntimeError("banco caiu"))
        results = jobs.run_all(ctx)
        ctx.db.upcoming_appointments = original
        self.assertIn("erro", str(results["lembretes"]))
        self.assertEqual(results["vigia"], "ok")


if __name__ == "__main__":
    unittest.main()
