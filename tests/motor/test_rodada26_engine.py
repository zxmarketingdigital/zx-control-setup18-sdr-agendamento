"""Rodada 26 do motor (engine/jobs): entrega atrasada não vira 'já respondido', a partida do motor não derruba o
laço, 4xx com instância fora de 'open' é transitório, vigia pula as rotinas quando o aviso falha e o alerta de falha
permanente sai uma vez por rotina por dia. Tudo com falsos."""

import unittest
from datetime import timedelta
from unittest import mock

from motor import engine, jobs
from motor.conversa import handle_turn
from motor.http import HttpError
from motor.util import iso, parse_ts
from tests.motor.support import LEAD_PHONE, NOW, OWNER_PHONE, default_agent, make_ctx
from tests.motor.test_jobs import book, silent_lead


def _sorted_by_created(ctx):
    """O banco real devolve o histórico ordenado por created_at; o falso devolve por inserção."""
    real = ctx.db.recent_messages

    def ordered(lead_id, limit=20):
        rows = sorted((m for m in ctx.db.messages if m["lead_id"] == lead_id),
                      key=lambda m: parse_ts(m["created_at"]) if isinstance(m["created_at"], str) else m["created_at"])
        return rows[-limit:]
    ctx.db.recent_messages = ordered
    return real


def _lead_respondido_as_10h03(ctx):
    """Agente respondeu às 10:03 (relógio do teste = 10:05); o lead escreve depois uma mensagem com carimbo 10:00."""
    ctx.clock.advance(minutes=0)
    lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
    out_at = ctx.now() - timedelta(minutes=2)
    ctx.db.add_message(lead["id"], "out", "agente", "Oi Joana!", None, "OUT0", out_at)
    ctx.db.update_lead(lead["id"], {"last_outbound_at": iso(out_at), "stage": "qualificado"})
    return lead


class EntregaAtrasadaTest(unittest.TestCase):
    def test_carimbo_anterior_a_resposta_do_agente_e_respondido(self):
        ctx = make_ctx(default_agent(test_only=False))
        _sorted_by_created(ctx)
        lead = _lead_respondido_as_10h03(ctx)
        ctx.evo.add(LEAD_PHONE, "qual o valor?", wa_id="ATRASADA", age_seconds=300)   # carimbo 10:00
        engine.ingest(ctx)
        msg = [m for m in ctx.db.messages if m["wa_id"] == "ATRASADA"][0]
        self.assertGreaterEqual(parse_ts(msg["created_at"]), ctx.now())   # ficou DEPOIS da resposta
        got = ctx.db.get_lead(lead["id"])
        self.assertEqual(parse_ts(got["last_inbound_at"]), ctx.now() - timedelta(seconds=300))   # monotônico: carimbo
        ctx.ai.push({"reply": "Claro, são R$ 100.", "action": "responder"})
        result = handle_turn(ctx, lead["id"])
        self.assertEqual(result.get("status"), "ok")
        self.assertEqual(len(ctx.ai.calls), 1)
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), ["Claro, são R$ 100."])

    def test_mensagem_em_ordem_normal_mantem_o_carimbo(self):
        ctx = make_ctx(default_agent(test_only=False))
        _sorted_by_created(ctx)
        _lead_respondido_as_10h03(ctx)
        ctx.evo.add(LEAD_PHONE, "oi de novo", wa_id="NORMAL", age_seconds=20)
        engine.ingest(ctx)
        msg = [m for m in ctx.db.messages if m["wa_id"] == "NORMAL"][0]
        self.assertEqual(parse_ts(msg["created_at"]), ctx.now() - timedelta(seconds=20))

    def test_pedido_de_parar_atrasado_aplica_opt_out_e_nenhum_followup_sai(self):
        ctx = make_ctx(default_agent(test_only=False))
        _sorted_by_created(ctx)
        lead = _lead_respondido_as_10h03(ctx)
        ctx.evo.add(LEAD_PHONE, "para de me mandar mensagem", wa_id="PARE", age_seconds=300)
        engine.ingest(ctx)
        self.assertTrue(ctx.db.get_lead(lead["id"])["opt_out"])
        ctx.clock.advance(hours=30)
        jobs.followups(ctx)
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])

    def test_leitura_do_historico_falha_usa_so_last_outbound_at(self):
        ctx = make_ctx(default_agent(test_only=False))
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        ctx.db.update_lead(lead["id"], {"last_outbound_at": iso(ctx.now() - timedelta(minutes=2))})
        ctx.db.recent_messages = mock.Mock(side_effect=HttpError(503, "fora"))
        ctx.evo.add(LEAD_PHONE, "atrasada", wa_id="X1", age_seconds=300)
        engine.ingest(ctx)
        msg = [m for m in ctx.db.messages if m["wa_id"] == "X1"][0]
        self.assertGreaterEqual(parse_ts(msg["created_at"]), ctx.now())


class _Stop(Exception):
    pass


class PartidaDoMotorTest(unittest.TestCase):
    def test_startup_503_no_primeiro_ciclo_nao_derruba_e_o_motor_segue(self):
        ctx = make_ctx(default_agent(test_only=False))
        real = ctx.db.get_agent_config
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise HttpError(503, "indisponível")
            return real()
        ctx.db.get_agent_config = flaky
        ctx.evo.add(LEAD_PHONE, "oi", wa_id="W1", age_seconds=5)
        ctx.ai.push({"reply": "Olá!", "action": "responder"})
        sleeps = []

        def fake_sleep(secs):
            sleeps.append(secs)
            if len(sleeps) >= 3:
                raise _Stop()
        with mock.patch.object(engine.time, "sleep", fake_sleep):
            with self.assertRaises(_Stop):
                engine.run_forever(ctx)
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), ["Olá!"])   # o 2º ciclo fez a partida e processou
        self.assertGreaterEqual(len(sleeps), 2)

    def test_erro_nao_transitorio_na_partida_avisa_o_dono_uma_vez_e_tenta_de_novo(self):
        ctx = make_ctx(default_agent(test_only=False))
        ctx.db.get_agent_config = mock.Mock(side_effect=ValueError("config quebrada"))
        ctx.state["agent"] = default_agent(test_only=False)
        sleeps = []

        def fake_sleep(secs):
            sleeps.append(secs)
            if len(sleeps) >= 3:
                raise _Stop()
        with mock.patch.object(engine.time, "sleep", fake_sleep):
            with self.assertRaises(_Stop):
                engine.run_forever(ctx)
        self.assertGreaterEqual(ctx.db.get_agent_config.call_count, 3)   # tentou a cada ciclo
        avisos = [t for t in ctx.evo.texts_to(OWNER_PHONE) if "partida" in t]
        self.assertEqual(len(avisos), 1)


class Http4xxInstanciaFechadaTest(unittest.TestCase):
    def _ctx(self):
        ctx = make_ctx(default_agent(test_only=False))
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        book(ctx, lead, NOW + timedelta(hours=23))
        return ctx

    def test_400_com_instancia_close_reverte_o_lembrete_e_sai_depois_de_reconectar(self):
        ctx = self._ctx()
        ctx.evo.state = "close"
        ctx.evo.fail_send, ctx.evo.fail_status = True, 400
        self.assertEqual(jobs.reminders(ctx), 0)
        self.assertIsNone(ctx.db.appointments[0]["reminder_24h_at"])   # flag revertida
        self.assertEqual([r for r in ctx.db.outbox if r["created_by"] == "alerta"], [])   # sem alerta de "não repete"
        ctx.evo.state, ctx.evo.fail_send, ctx.evo.fail_status = "open", False, None
        self.assertEqual(jobs.reminders(ctx), 1)
        self.assertEqual(len(ctx.evo.texts_to(LEAD_PHONE)), 1)
        self.assertIsNotNone(ctx.db.appointments[0]["reminder_24h_at"])

    def test_400_com_estado_ilegivel_tambem_reverte(self):
        ctx = self._ctx()
        ctx.evo.state = "erro"   # connection_state levanta
        ctx.evo.fail_send, ctx.evo.fail_status = True, 400
        jobs.reminders(ctx)
        self.assertIsNone(ctx.db.appointments[0]["reminder_24h_at"])

    def test_400_com_instancia_open_continua_definitivo(self):
        ctx = self._ctx()
        ctx.evo.fail_send, ctx.evo.fail_status = True, 400
        jobs.reminders(ctx)
        self.assertIsNotNone(ctx.db.appointments[0]["reminder_24h_at"])   # estado mantido
        self.assertEqual(len([r for r in ctx.db.outbox if r["created_by"] == "alerta"]), 1)


class VigiaFalhaDoAvisoTest(unittest.TestCase):
    def test_notify_owner_levantando_pula_as_rotinas_de_envio(self):
        ctx = make_ctx(default_agent(test_only=False))
        ctx.evo.state = "close"
        silent_lead(ctx, hours_since_inbound=30)
        with mock.patch.object(jobs, "notify_owner", side_effect=RuntimeError("banco fora")):
            result = jobs.run_all(ctx)
        self.assertEqual(result["vigia"], "fora")
        self.assertTrue(str(result["followups"]).startswith("pulado"))
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])
        self.assertFalse(jobs._load_vigia(ctx).get("down_since"))   # o próximo ciclo tenta avisar de novo

    def test_connection_state_com_erro_inesperado_tambem_pula(self):
        ctx = make_ctx(default_agent(test_only=False))
        ctx.evo.connection_state = mock.Mock(side_effect=RuntimeError("estranho"))
        with mock.patch.object(jobs, "notify_owner", side_effect=RuntimeError("fora")):
            self.assertEqual(jobs.vigia(ctx), "fora")


class AlertaPermanenteUmPorDiaTest(unittest.TestCase):
    def test_tres_leads_com_falha_permanente_no_mesmo_dia_geram_um_alerta_por_destinatario(self):
        ctx = make_ctx(default_agent(test_only=False))
        phones = ["+5585999000001", "+5585999000002", "+5585999000003"]
        for i, phone in enumerate(phones):
            lead = ctx.db.create_lead(phone, name=f"Lead {i}")
            book(ctx, lead, NOW + timedelta(hours=23, minutes=i * 40))
        ctx.evo.fail_send, ctx.evo.fail_status = True, 400
        jobs.reminders(ctx)
        alertas = [r for r in ctx.db.outbox if r["created_by"] == "alerta"]
        self.assertEqual(len(alertas), 2)   # r28: um por destinatário (2 vencem dentro de 24h), não um por rotina
        jobs.reminders(ctx)   # mesmos destinatários, mesmo dia: não repete (flags já marcadas e dedupe)
        self.assertEqual(len([r for r in ctx.db.outbox if r["created_by"] == "alerta"]), 2)
        ctx.clock.advance(days=1)   # outro dia: avisa de novo
        for appt in ctx.db.appointments:
            appt["reminder_24h_at"] = None
            appt["starts_at"] = iso(ctx.now() + timedelta(hours=23))
        jobs.reminders(ctx)
        self.assertEqual(len([r for r in ctx.db.outbox if r["created_by"] == "alerta"]), 5)   # no dia seguinte os 3 entram na janela


if __name__ == "__main__":
    unittest.main()
