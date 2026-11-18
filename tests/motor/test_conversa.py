"""Turno de conversa: takeover, opt-out, modo teste, agendamento, horário tomado, rede de segurança e falha da IA."""

import unittest
from datetime import timedelta

from motor import engine
from motor.ai import AIError
from motor.conversa import NEUTRAL_MESSAGE, OPT_OUT_CONFIRMATION, handle_turn
from motor.util import iso
from tests.motor.support import (CLOSER_PHONE, LEAD_PHONE, OWNER_PHONE, TEST_PHONE, default_agent, inbound,
                                 make_ctx, sp)


def turn(ctx, text, decision, phone=LEAD_PHONE):
    lead = inbound(ctx, text, phone)
    ctx.ai.push(decision)
    return lead, handle_turn(ctx, lead["id"])


class ResponderTest(unittest.TestCase):
    def test_primeiro_contato_recebe_resposta_registrada_com_key_id(self):
        ctx = make_ctx()
        lead, result = turn(ctx, "Oi, vi o anúncio", {"reply": "Oi! Sou a Ana, o que você procura?", "action": "responder"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), ["Oi! Sou a Ana, o que você procura?"])
        out = [m for m in ctx.db.messages if m["direction"] == "out"]
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0]["author"], out[0]["wa_id"]), ("agente", "OUT1"))
        self.assertEqual(ctx.db.leads[lead["id"]]["stage"], "respondeu")
        self.assertIsNotNone(ctx.db.leads[lead["id"]]["last_outbound_at"])

    def test_nome_e_empresa_vem_do_banco_nao_do_codigo(self):
        ctx = make_ctx(default_agent(agent_name="Bia", company="Imobiliária Horizonte", knowledge="Vendemos casas."))
        turn(ctx, "oi", {"reply": "oi", "action": "responder"})
        system = ctx.ai.calls[0]["system"]
        self.assertIn("Você é Bia", system)
        self.assertIn("Imobiliária Horizonte", system)
        self.assertIn("Vendemos casas.", system)
        self.assertIn("O que você procura?", system)

    def test_qualificar_grava_perfil_score_e_estagio(self):
        ctx = make_ctx()
        lead, _ = turn(ctx, "quero uma avaliação", {
            "reply": "Ótimo!", "action": "qualificar", "perfil": "dona de clínica", "score": 70,
            "qualificacao": {"interesse": "avaliação"}, "nome": "Joana Silva", "email": "joana@exemplo.com"})
        saved = ctx.db.leads[lead["id"]]
        self.assertEqual((saved["stage"], saved["score"], saved["profile"]), ("qualificado", 70, "dona de clínica"))
        self.assertEqual(saved["qualification"], {"interesse": "avaliação"})
        self.assertEqual(saved["email"], "joana@exemplo.com")

    def test_desqualificar_registra_motivo(self):
        ctx = make_ctx()
        lead, _ = turn(ctx, "só curiosidade", {"reply": "Obrigada!", "action": "desqualificar", "motivo": "sem orçamento"})
        saved = ctx.db.leads[lead["id"]]
        self.assertEqual((saved["stage"], saved["disqualify_reason"]), ("desqualificado", "sem orçamento"))

    def test_objecao_e_gravada_com_trecho(self):
        ctx = make_ctx()
        lead, _ = turn(ctx, "tá caro", {"reply": "Entendo.", "action": "objecao",
                                        "objecao": {"categoria": "preco", "trecho": "tá caro"}})
        self.assertEqual(ctx.db.objections, [{"lead_id": lead["id"], "category": "preco", "excerpt": "tá caro"}])

    def test_agente_pausado_nao_responde(self):
        ctx = make_ctx(default_agent(paused=True))
        lead = inbound(ctx)
        self.assertEqual(handle_turn(ctx, lead["id"])["status"], "pausado")
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])   # nada ao lead (r11: o dono recebe UM alerta da pausa)

    def test_atendimento_automatico_nao_recebe_resposta(self):
        ctx = make_ctx()
        lead = inbound(ctx, "Atendimento automático. Digite 1 para continuar")
        self.assertEqual(handle_turn(ctx, lead["id"])["status"], "autoresponder")
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])   # r7: o lead não recebe nada, mas o dono é avisado


class TakeoverTest(unittest.TestCase):
    def test_dono_responde_pelo_celular_e_o_agente_cala_por_24h(self):
        ctx = make_ctx()
        ctx.evo.add(LEAD_PHONE, "oi, quero saber mais", wa_id="IN-A")
        engine.ingest(ctx)
        lead = ctx.db.get_lead_by_phone(LEAD_PHONE)
        ctx.evo.add(LEAD_PHONE, "Oi! Aqui é a equipe, já te atendo", wa_id="HUMANO-1", from_me=True)
        engine.ingest(ctx)
        humano = [m for m in ctx.db.messages if m["wa_id"] == "HUMANO-1"][0]
        self.assertEqual((humano["author"], humano["direction"]), ("dono", "out"))
        self.assertEqual(iso(ctx.now().replace()), iso(ctx.now()))
        self.assertGreater(ctx.db.leads[lead["id"]]["takeover_until"], iso(ctx.now()))
        ctx.evo.add(LEAD_PHONE, "pode ser amanhã?", wa_id="IN-B")
        engine.ingest(ctx)
        self.assertEqual(handle_turn(ctx, lead["id"])["status"], "takeover")
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [])   # só o aviso ao dono (r8-4) sai
        self.assertEqual(ctx.ai.calls, [])

    def test_depois_das_24h_o_agente_volta(self):
        ctx = make_ctx()
        lead = inbound(ctx)
        ctx.db.update_lead(lead["id"], {"takeover_until": iso(ctx.now())})
        ctx.clock.advance(hours=1)
        ctx.ai.push({"reply": "voltei", "action": "responder"})
        self.assertEqual(handle_turn(ctx, lead["id"])["status"], "ok")

    def test_mensagem_enviada_pelo_proprio_motor_nao_conta_como_dono(self):
        ctx = make_ctx()
        ctx.evo.add(LEAD_PHONE, "oi", wa_id="IN-1")
        engine.ingest(ctx)
        lead = ctx.db.get_lead_by_phone(LEAD_PHONE)
        ctx.ai.push({"reply": "Oi, tudo bem?", "action": "responder"})
        handle_turn(ctx, lead["id"])
        # a Evolution devolve a própria mensagem do agente (fromMe) com o id que o envio gerou
        ctx.evo.add(LEAD_PHONE, "Oi, tudo bem?", wa_id="OUT1", from_me=True)
        engine.ingest(ctx)
        self.assertIsNone(ctx.db.leads[lead["id"]]["takeover_until"])
        self.assertEqual([m["author"] for m in ctx.db.messages if m["direction"] == "out"], ["agente"])


class OptOutTest(unittest.TestCase):
    def test_opt_out_durante_takeover_e_registrado_sem_enviar_nada(self):
        ctx = make_ctx()
        lead = inbound(ctx, "pode parar de mandar mensagens")
        ctx.db.update_lead(lead["id"], {"takeover_until": iso(ctx.now() + timedelta(hours=5))})
        result = handle_turn(ctx, lead["id"])
        self.assertEqual(result["status"], "opt_out_registrado")
        self.assertTrue(ctx.db.leads[lead["id"]]["opt_out"])
        self.assertEqual(ctx.evo.sent, [])

    def test_assunto_do_lead_com_para_de_mandar_nao_e_opt_out(self):
        from motor.conversa import is_opt_out
        self.assertFalse(is_opt_out("meu fornecedor para de mandar boleto atrasado, quero ajuda com cobrança"))
        self.assertFalse(is_opt_out("o sistema para de mandar mensagem quando cai a internet"))

    def test_parar_confirma_uma_vez_e_depois_fica_em_silencio(self):
        ctx = make_ctx()
        lead = inbound(ctx, "parar")
        result = handle_turn(ctx, lead["id"])
        self.assertEqual(result["status"], "opt_out_confirmado")
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [OPT_OUT_CONFIRMATION])
        saved = ctx.db.leads[lead["id"]]
        self.assertTrue(saved["opt_out"])
        self.assertEqual(saved["stage"], "perdido")
        self.assertEqual(ctx.ai.calls, [])            # nem chamou a IA
        inbound(ctx, "oi de novo")
        self.assertEqual(handle_turn(ctx, lead["id"])["status"], "opt_out")
        self.assertEqual(len(ctx.evo.sent), 1)

    def test_opt_out_decidido_pela_ia_tambem_respeita(self):
        ctx = make_ctx()
        lead, result = turn(ctx, "chega de mensagem por favor", {"reply": "ok", "action": "opt_out"})
        self.assertEqual(result["status"], "opt_out_confirmado")
        self.assertTrue(ctx.db.leads[lead["id"]]["opt_out"])
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [OPT_OUT_CONFIRMATION])

    def test_opt_out_nao_recebe_followup_nem_lembrete(self):
        from motor import jobs
        ctx = make_ctx()
        lead = inbound(ctx, "sair")
        handle_turn(ctx, lead["id"])
        ctx.clock.advance(hours=5)
        self.assertEqual(jobs.followups(ctx), 0)


class ModoTesteTest(unittest.TestCase):
    def agent(self):
        return default_agent(test_only=True, test_phones=[TEST_PHONE])

    def test_numero_fora_da_lista_nem_vira_lead_e_nao_recebe_nada(self):
        ctx = make_ctx(self.agent())
        ctx.evo.add(LEAD_PHONE, "oi")
        self.assertEqual(engine.ingest(ctx), set())
        self.assertEqual(ctx.db.leads, {})
        self.assertEqual(engine.cycle(ctx), {"respostas": 0, "outbox": 0})
        self.assertEqual(ctx.evo.sent, [])

    def test_numero_de_teste_e_atendido(self):
        ctx = make_ctx(self.agent())
        ctx.evo.add(TEST_PHONE, "oi, é um teste")
        ctx.ai.push({"reply": "Oi! Modo teste ativo.", "action": "responder"})
        summary = engine.cycle(ctx)
        self.assertEqual(summary["respostas"], 1)
        self.assertEqual(ctx.evo.texts_to(TEST_PHONE), ["Oi! Modo teste ativo."])

    def test_envio_a_lead_fora_do_teste_e_barrado_no_messenger(self):
        ctx = make_ctx(self.agent())
        lead = ctx.db.create_lead(LEAD_PHONE)
        self.assertIsNone(ctx.messenger.send(LEAD_PHONE, "oi", lead_id=lead["id"]))
        self.assertEqual(ctx.messenger.last_failure, "gate")
        self.assertEqual(ctx.evo.sent, [])
        self.assertEqual(handle_turn(ctx, lead["id"])["status"], "modo_teste")

    def test_avisos_internos_passam_pelo_modo_teste(self):
        ctx = make_ctx(self.agent())
        self.assertTrue(ctx.messenger.send(OWNER_PHONE, "aviso", internal=True))


class AgendamentoTest(unittest.TestCase):
    def test_agendar_usa_a_rpc_e_responde_com_o_modelo_de_confirmacao(self):
        ctx = make_ctx()
        lead, result = turn(ctx, "pode ser quinta 14h", {
            "reply": "Agendei! Às 99h, combinado!", "action": "agendar", "slot": iso(sp(19, 14)),
            "perfil": "dona de clínica", "qualificacao": {"interesse": "avaliação"}})
        self.assertTrue(result["booked"])
        self.assertEqual(result["reply"], "Perfeito, Joana! Sua conversa com Marcos ficou para 19/11/2026 às 14:00.")
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [result["reply"]])        # o texto da IA nunca vai
        self.assertEqual(len(ctx.db.appointments), 1)
        self.assertEqual(ctx.db.appointments[0]["source"], "agente")
        self.assertIn("dona de clínica", ctx.db.appointments[0]["notes"])
        self.assertEqual(ctx.db.leads[lead["id"]]["stage"], "agendado")

    def test_horario_tomado_oferece_dois_outros(self):
        ctx = make_ctx()
        ctx.db.force_conflict = {sp(19, 14)}    # alguém reservou entre a oferta e a confirmação
        lead, result = turn(ctx, "pode ser 14h", {"reply": "Fechado!", "action": "agendar", "slot": iso(sp(19, 14))})
        self.assertFalse(result["booked"])
        reply = ctx.evo.texts_to(LEAD_PHONE)[0]
        self.assertIn("acabou de ser ocupado", reply)
        self.assertNotIn("14:00", reply)
        self.assertEqual(reply.count("•"), 2)
        self.assertEqual(ctx.db.appointments, [])
        self.assertEqual(ctx.db.leads[lead["id"]]["stage"], "horarios_ofertados")

    def test_horario_fora_da_agenda_tambem_oferece_outros(self):
        ctx = make_ctx()
        lead, result = turn(ctx, "domingo 8h", {"reply": "ok", "action": "agendar", "slot": iso(sp(22, 8))})
        self.assertFalse(result["booked"])
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE)[0].count("•"), 2)

    def test_sem_nenhum_horario_livre_avisa_com_honestidade(self):
        ctx = make_ctx()
        ctx.db.slots = []
        lead, _ = turn(ctx, "quero marcar", {"reply": "Claro", "action": "ofertar_horarios"})
        self.assertIn("não tenho horários livres", ctx.evo.texts_to(LEAD_PHONE)[0])

    def test_sem_horario_livre_alerta_o_dono_pela_outbox(self):
        ctx = make_ctx()
        ctx.db.slots = []
        turn(ctx, "quero marcar", {"reply": "Claro", "action": "ofertar_horarios"})
        alerts = [r for r in ctx.db.outbox if r["created_by"] == "alerta"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["phone"], OWNER_PHONE)
        self.assertIn("não há horário livre", alerts[0]["body"])

    def test_agenda_que_falha_nao_vira_mentira_de_sem_horario(self):
        ctx = make_ctx()
        ctx.db.available_slots = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("500"))
        lead = inbound(ctx, "quero marcar")
        result = handle_turn(ctx, lead["id"])
        self.assertEqual(result["status"], "agenda_falhou")
        self.assertNotIn("horários livres", " ".join(ctx.evo.texts_to(LEAD_PHONE)))
        self.assertTrue(any(r["created_by"] == "alerta" for r in ctx.db.outbox))

    def test_excecao_apos_agendar_nao_perde_a_confirmacao(self):
        ctx = make_ctx()
        lead = inbound(ctx, "14h")
        ctx.ai.push({"reply": "x", "action": "agendar", "slot": iso(sp(19, 14))})
        real = ctx.db.update_lead
        calls = {"n": 0}

        def flaky(lead_id, fields):
            if "qualification" in fields and fields.get("qualification") is not None and calls["n"] == 0:
                calls["n"] += 1
                raise RuntimeError("timeout")
            return real(lead_id, fields)
        ctx.db.update_lead = flaky
        result = handle_turn(ctx, lead["id"])
        self.assertTrue(result["booked"])
        self.assertEqual(len(ctx.db.appointments), 1)
        confirmations = [t for t in ctx.evo.texts_to(LEAD_PHONE) if "ficou para" in t]
        self.assertEqual(len(confirmations), 1)

    def test_ofertar_horarios_mostra_so_horarios_reais(self):
        ctx = make_ctx()
        lead, _ = turn(ctx, "quando dá?", {"reply": "Tenho estes horários:", "action": "ofertar_horarios"})
        reply = ctx.evo.texts_to(LEAD_PHONE)[0]
        self.assertEqual(reply.count("•"), 2)
        self.assertEqual(ctx.db.leads[lead["id"]]["stage"], "horarios_ofertados")

    def test_ia_cita_horario_que_nao_existe_e_a_resposta_e_trocada_pelas_opcoes_reais(self):
        # r12: a troca pelas opções reais vale para ação de agenda; em "responder" só o horário inventado sai
        ctx = make_ctx()
        lead, result = turn(ctx, "tem 18h?", {"reply": "Tenho às 18h sim!", "action": "ofertar_horarios"})
        reply = ctx.evo.texts_to(LEAD_PHONE)[0]
        self.assertNotIn("18h", reply)
        self.assertEqual(reply.count("•"), 2)
        self.assertEqual(result["action"], "ofertar_horarios")

    def test_ia_diz_que_agendou_sem_a_acao_e_a_resposta_e_trocada(self):
        ctx = make_ctx()
        lead, _ = turn(ctx, "pode ser", {"reply": "Agendei para você, até lá!", "action": "responder"})
        self.assertNotIn("Agendei", ctx.evo.texts_to(LEAD_PHONE)[0])
        self.assertEqual(ctx.db.appointments, [])

    def test_agendar_sem_informar_o_slot_oferece_os_reais(self):
        ctx = make_ctx()
        lead, result = turn(ctx, "ok", {"reply": "Agendado!", "action": "agendar", "slot": ""})
        self.assertEqual(ctx.db.appointments, [])
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE)[0].count("•"), 2)

    def test_remarcar_cancela_o_antigo_e_cria_o_novo(self):
        ctx = make_ctx()
        lead, _ = turn(ctx, "14h", {"reply": "x", "action": "agendar", "slot": iso(sp(19, 14))})
        inbound(ctx, "preciso mudar para sexta 16h")
        ctx.ai.push({"reply": "x", "action": "agendar", "slot": iso(sp(21, 16))})
        result = handle_turn(ctx, lead["id"])
        self.assertTrue(result["booked"])
        statuses = [(a["starts_at"], a["status"]) for a in ctx.db.appointments]
        self.assertEqual(statuses, [(iso(sp(19, 14)), "reagendado"), (iso(sp(21, 16)), "agendado")])
        self.assertEqual(ctx.db.leads[lead["id"]]["stage"], "reagendado")

    def test_remarcar_para_horario_tomado_mantem_o_agendamento_original(self):
        ctx = make_ctx()
        lead, _ = turn(ctx, "14h", {"reply": "x", "action": "agendar", "slot": iso(sp(19, 14))})
        ctx.db.force_conflict = {sp(21, 16)}
        inbound(ctx, "muda para sexta 16h")
        ctx.ai.push({"reply": "x", "action": "agendar", "slot": iso(sp(21, 16))})
        result = handle_turn(ctx, lead["id"])
        self.assertFalse(result["booked"])
        self.assertEqual([a["status"] for a in ctx.db.appointments], ["agendado"])

    def test_cancelar_e_confirmar(self):
        ctx = make_ctx()
        lead, _ = turn(ctx, "14h", {"reply": "x", "action": "agendar", "slot": iso(sp(19, 14))})
        inbound(ctx, "confirmo presença")
        ctx.ai.push({"reply": "Perfeito, te espero!", "action": "confirmar"})
        handle_turn(ctx, lead["id"])
        self.assertEqual(ctx.db.appointments[0]["status"], "confirmado")
        self.assertEqual(ctx.db.leads[lead["id"]]["stage"], "confirmado")
        inbound(ctx, "não vou conseguir, cancela")
        ctx.ai.push({"reply": "Sem problema!", "action": "cancelar"})
        handle_turn(ctx, lead["id"])
        self.assertEqual(ctx.db.appointments[0]["status"], "cancelado")


class FalhaDaIATest(unittest.TestCase):
    def test_ia_falhou_manda_mensagem_neutra_e_avisa_o_dono(self):
        ctx = make_ctx()
        ctx.ai.push(AIError("IA indisponível após 3 tentativas"))
        lead = inbound(ctx, "oi")
        result = handle_turn(ctx, lead["id"])
        self.assertEqual(result["status"], "ia_falhou")
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [NEUTRAL_MESSAGE])
        alerts = ctx.evo.texts_to(OWNER_PHONE)
        self.assertEqual(len(alerts), 1)
        self.assertIn("A IA do agente falhou", alerts[0])
        self.assertIn(LEAD_PHONE, alerts[0])

    def test_alerta_so_uma_vez_por_sequencia_de_falhas_e_volta_a_avisar_depois_de_recuperar(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        ctx.ai.push(AIError("x"), AIError("x"))
        handle_turn(ctx, lead["id"])
        inbound(ctx, "alô?")
        handle_turn(ctx, lead["id"])
        self.assertEqual(len(ctx.evo.texts_to(OWNER_PHONE)), 1)
        inbound(ctx, "voltou?")
        ctx.ai.push({"reply": "Voltei!", "action": "responder"})
        handle_turn(ctx, lead["id"])
        inbound(ctx, "e agora?")
        ctx.ai.push(AIError("de novo"))
        handle_turn(ctx, lead["id"])
        self.assertEqual(len(ctx.evo.texts_to(OWNER_PHONE)), 2)

    def test_json_ilegivel_duas_vezes_tambem_cai_na_mensagem_neutra(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi")
        ctx.ai.push('{"reply": "oi", "action": "agend', "```")
        self.assertEqual(handle_turn(ctx, lead["id"])["status"], "ia_falhou")
        self.assertEqual(ctx.evo.texts_to(LEAD_PHONE), [NEUTRAL_MESSAGE])

    def test_nunca_inventa_horario_quando_a_ia_cai(self):
        ctx = make_ctx()
        lead = inbound(ctx, "quero marcar")
        ctx.ai.push(AIError("x"))
        handle_turn(ctx, lead["id"])
        self.assertNotIn("•", ctx.evo.texts_to(LEAD_PHONE)[0])

    def test_aviso_ao_dono_sem_conexao_vai_para_a_outbox(self):
        ctx = make_ctx()
        ctx.evo.fail_send = True
        lead = inbound(ctx, "oi")
        ctx.ai.push(AIError("x"))
        handle_turn(ctx, lead["id"])
        created = sorted(r["created_by"] for r in ctx.db.outbox)
        self.assertEqual(created, ["alerta", "motor"])      # mensagem neutra e aviso ao dono, ambos esperando a volta
        self.assertTrue(all(r["status"] == "pendente" for r in ctx.db.outbox))

    def test_evolution_fora_ao_responder_deixa_a_resposta_na_outbox(self):
        ctx = make_ctx()
        ctx.evo.fail_send = True
        lead, result = turn(ctx, "oi", {"reply": "Olá!", "action": "responder"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual([(r["body"], r["status"], r["created_by"]) for r in ctx.db.outbox], [("Olá!", "pendente", "motor")])


class RetomarTest(unittest.TestCase):
    def test_lead_adia_e_o_agente_volta_na_data_combinada(self):
        from motor import jobs
        ctx = make_ctx()
        lead, _ = turn(ctx, "me chama semana que vem", {"reply": "Combinado!", "action": "retomar", "retomar_em": "2026-11-25"})
        due = ctx.db.leads[lead["id"]]["qualification"]["_retomar"]["em"]
        self.assertEqual(due, iso(sp(25, 10)))
        self.assertEqual(jobs.retomar(ctx), 0)               # ainda não é a hora
        ctx.clock.current = sp(25, 10, 5)
        ctx.ai.push({"reply": "Oi Joana, voltando como combinado!", "action": "responder"})
        self.assertEqual(jobs.retomar(ctx), 1)
        self.assertIn("voltando como combinado", ctx.evo.texts_to(LEAD_PHONE)[-1])
        self.assertNotIn("_retomar", ctx.db.leads[lead["id"]]["qualification"])
        self.assertEqual(jobs.retomar(ctx), 0)               # não repete


if __name__ == "__main__":
    unittest.main()
