"""Rodada 13 do motor: backoff da outbox, claim que não fica preso, confirmação de remarcação por horário,
rotinas isoladas por linha, filtro de links e contato @lid sem número."""

import unittest
from datetime import timedelta

from motor import agenda, engine, jobs, outbox
from motor.conversa import strip_unknown_links
from tests.motor.support import LEAD_PHONE, OWNER_PHONE, default_agent, inbound, make_ctx, sp
from tests.motor.test_jobs import book, silent_lead
from motor.util import iso


def alerts(ctx):
    return [r["body"] for r in ctx.db.outbox if r["phone"] == OWNER_PHONE and r["created_by"] == "alerta"]


class BackoffDaOutboxTest(unittest.TestCase):
    def test_cinco_ciclos_em_segundos_nao_levam_a_linha_a_erro(self):
        ctx = make_ctx()
        ctx.evo.fail_send = True
        ctx.evo.fail_status = 429
        ctx.db.add_outbox(LEAD_PHONE, "oi", created_by="painel")
        for _ in range(8):
            outbox.process_outbox(ctx)
            ctx.clock.advance(seconds=3)
        row = ctx.db.outbox[0]
        self.assertEqual((row["status"], row["attempts"]), ("pendente", 1))   # só a 1ª tentativa gastou

    def test_espera_dobra_e_a_evolution_de_volta_envia(self):
        ctx = make_ctx()
        ctx.evo.fail_send = True
        ctx.evo.fail_status = 429
        ctx.db.add_outbox(LEAD_PHONE, "oi", created_by="painel")
        outbox.process_outbox(ctx)                    # tentativa 1, espera 30s
        ctx.clock.advance(seconds=31)
        outbox.process_outbox(ctx)                    # tentativa 2, espera 60s
        self.assertEqual(ctx.db.outbox[0]["attempts"], 2)
        ctx.clock.advance(seconds=59)
        outbox.process_outbox(ctx)
        self.assertEqual(ctx.db.outbox[0]["attempts"], 2)
        ctx.evo.fail_send = False
        ctx.clock.advance(seconds=2)
        self.assertEqual(outbox.process_outbox(ctx), 1)
        self.assertEqual(ctx.db.outbox[0]["status"], "enviado")

    def test_backoff_por_tentativas(self):
        self.assertEqual([outbox.backoff_seconds(n) for n in (1, 2, 3, 4)], [30, 60, 120, 240])


class ExcecaoAntesDoEnvioTest(unittest.TestCase):
    def test_excecao_do_limitador_devolve_a_linha_a_pendente(self):
        ctx = make_ctx()
        ctx.db.add_outbox(LEAD_PHONE, "oi", created_by="painel")

        def boom(*a, **k):
            raise RuntimeError("database is locked")
        ctx.messenger.limiter.acquire = boom
        self.assertEqual(outbox.process_outbox(ctx), 0)
        row = ctx.db.outbox[0]
        self.assertEqual((row["status"], row["attempts"]), ("pendente", 0))
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])

    def test_excecao_depois_da_evolution_segue_a_regra_de_incerteza(self):
        ctx = make_ctx()
        ctx.db.add_outbox(LEAD_PHONE, "oi", created_by="painel")

        original = ctx.evo.send_text

        def send_then_raise(*a, **k):
            original(*a, **k)
            raise RuntimeError("queda depois da chamada")
        ctx.evo.send_text = send_then_raise
        # r23: o Messenger trata o erro não mapeado como envio incerto: a linha vai para erro e nunca é reenviada
        outbox.process_outbox(ctx)
        outbox.process_outbox(ctx)
        self.assertEqual(ctx.db.outbox[0]["status"], "erro")
        self.assertEqual(len(ctx.evo.texts_to(LEAD_PHONE)), 1)


class ConfirmacaoDeRemarcacaoTest(unittest.TestCase):
    def _remarca(self, ctx, lead, new_start):
        """RPC que mantém o appointment_id (o caso do relatório)."""
        appt = ctx.db.active_appointment(lead["id"], ctx.now())
        ctx.db.slots.append(new_start)
        ctx.db.reschedule_slot = lambda aid, starts_at, closer_id=None: {
            "ok": True, "appointment_id": aid, "closer_id": "c1", "closer_name": "Marcos",
            "starts_at": iso(starts_at), "ends_at": iso(starts_at + timedelta(minutes=30))}
        return agenda.book_for_lead(ctx, ctx.db.get_lead(lead["id"]), new_start, ctx.state["agent"],
                                    queue_confirmation=True)

    def test_terca_para_quinta_enfileira_duas_confirmacoes(self):
        ctx = make_ctx()
        lead = inbound(ctx, "quero marcar")
        ctx.db.slots += [sp(17, 10)]
        ok, _ = agenda.book_for_lead(ctx, ctx.db.get_lead(lead["id"]), sp(17, 10), ctx.state["agent"],
                                     queue_confirmation=True)
        self.assertTrue(ok)
        ok, _ = self._remarca(ctx, lead, sp(19, 15))
        self.assertTrue(ok)
        confirms = [r for r in ctx.db.outbox if (r.get("client_key") or "").endswith(":confirm")]
        self.assertEqual(len(confirms), 2)
        self.assertEqual(len({r["client_key"] for r in confirms}), 2)
        self.assertEqual(len(ctx.evo.texts_to(LEAD_PHONE)), 2)

    def test_repetir_a_mesma_reserva_nao_duplica(self):
        ctx = make_ctx()
        lead = inbound(ctx, "quero marcar")
        ctx.db.slots += [sp(17, 10)]
        agenda.book_for_lead(ctx, ctx.db.get_lead(lead["id"]), sp(17, 10), ctx.state["agent"], queue_confirmation=True)
        appt = ctx.db.active_appointment(lead["id"], ctx.now())
        ctx.db.book_slot = lambda *a, **k: {
            "ok": True, "appointment_id": appt["id"], "closer_id": "c1", "closer_name": "Marcos",
            "starts_at": appt["starts_at"], "ends_at": appt["ends_at"]}
        ctx.db.active_appointment = lambda *a, **k: None   # a resposta se perdeu: tenta reservar de novo
        agenda.book_for_lead(ctx, ctx.db.get_lead(lead["id"]), sp(17, 10), ctx.state["agent"], queue_confirmation=True)
        confirms = [r for r in ctx.db.outbox if (r.get("client_key") or "").endswith(":confirm")]
        self.assertEqual(len(confirms), 1)
        self.assertEqual(len(ctx.evo.texts_to(LEAD_PHONE)), 1)


class RotinasIsoladasPorLinhaTest(unittest.TestCase):
    def test_followups_pula_lead_com_qualification_string_e_alerta_uma_vez(self):
        ctx = make_ctx()
        a = silent_lead(ctx, phone="+5511999990011")
        b = silent_lead(ctx, phone="+5511999990012", qualification="texto solto")
        c = silent_lead(ctx, phone="+5511999990013")
        self.assertEqual(jobs.followups(ctx), 2)
        self.assertEqual(len(ctx.evo.texts_to("+5511999990011")), 1)
        self.assertEqual(len(ctx.evo.texts_to("+5511999990013")), 1)
        self.assertEqual(ctx.evo.texts_to("+5511999990012"), [])
        self.assertEqual(len(alerts(ctx)), 1)
        self.assertIn("+5511999990012", alerts(ctx)[0])
        jobs.followups(ctx)
        self.assertEqual(len(alerts(ctx)), 1)   # UM alerta por lead

    def test_retomar_nao_para_nos_demais(self):
        ctx = make_ctx()
        silent_lead(ctx, phone="+5511999990012", qualification=["x"])
        jobs.retomar(ctx)
        self.assertEqual(len(alerts(ctx)), 1)

    def test_lembretes_pulam_agendamento_malformado(self):
        ctx = make_ctx()
        l1 = ctx.db.create_lead("+5511999990011", name="Ana")
        l2 = ctx.db.create_lead("+5511999990013", name="Bia")
        book(ctx, l1, ctx.now() + timedelta(hours=1, minutes=30), created_hours_ago=72)
        book(ctx, l2, ctx.now() + timedelta(hours=1, minutes=40), created_hours_ago=72)
        ctx.db.appointments[0]["lead_id"] = None   # linha malformada: get_lead falha/não acha
        real = ctx.db.get_lead

        def get_lead(lead_id):
            if lead_id is None:
                raise ValueError("lead_id nulo")
            return real(lead_id)
        ctx.db.get_lead = get_lead
        self.assertEqual(jobs.reminders(ctx), 1)
        self.assertEqual(len(alerts(ctx)), 1)

    def test_repasse_com_qualification_string_sai_sem_o_bloco_e_nao_para_os_outros(self):
        ctx = make_ctx()
        l1 = ctx.db.create_lead("+5511999990011", name="Ana")
        l2 = ctx.db.create_lead("+5511999990013", name="Bia")
        ctx.db.update_lead(l1["id"], {"qualification": "texto solto"})
        a1 = book(ctx, l1, sp(19, 14))
        a2 = book(ctx, l2, sp(19, 15))
        self.assertEqual(jobs.handoffs(ctx), 2)
        self.assertTrue(a1["handoff_sent_at"] and a2["handoff_sent_at"])
        self.assertEqual(len(alerts(ctx)), 1)


class LinksTest(unittest.TestCase):
    def test_url_inventada_sai(self):
        ctx = make_ctx(default_agent(knowledge="Atendemos de segunda a sexta."))
        out = strip_unknown_links(ctx, ctx.state["agent"],
                                  "Para garantir o seu horário confira em https://oferta-falsa.com/pagamento agora")
        self.assertNotIn("oferta-falsa", out)


class ContatoLidSemNumeroTest(unittest.TestCase):
    def test_alerta_uma_vez_por_lid_e_nao_grava_lead(self):
        ctx = make_ctx()
        ctx.evo.add("+5511000000000", "oi, quero marcar", wa_id="L1", jid="123456789012@lid")
        engine.ingest(ctx)
        self.assertEqual(len(alerts(ctx)), 1)
        self.assertIn("não informou", alerts(ctx)[0])
        self.assertEqual(ctx.db.leads, {})
        ctx.evo.add("+5511000000000", "alô?", wa_id="L2", jid="123456789012@lid")
        engine.ingest(ctx)
        self.assertEqual(len(alerts(ctx)), 1)
        ctx.evo.add("+5511000000000", "oi", wa_id="L3", jid="999999999999@lid")
        engine.ingest(ctx)
        self.assertEqual(len(alerts(ctx)), 2)

    def test_grupo_continua_silencioso(self):
        ctx = make_ctx()
        ctx.evo.add("+5511000000000", "oi grupo", wa_id="G1", jid="1203630@g.us")
        engine.ingest(ctx)
        self.assertEqual(alerts(ctx), [])


if __name__ == "__main__":
    unittest.main()
