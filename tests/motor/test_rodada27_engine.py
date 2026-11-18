"""Rodada 27 do motor (engine/evolution): áudio/imagem sem transcrição não vira silêncio (marcador, alerta, cadência
e retomada paradas), eco antigo do dono anterior ao lead não ativa takeover e edição de mensagem do lead é lida.
Tudo com falsos (nenhum efeito externo)."""

import dataclasses
import unittest
from datetime import timedelta

from motor import engine, jobs
from motor.conversa import handle_turn
from motor.evolution import is_silent_record, record_text
from motor.util import iso
from tests.motor.support import LEAD_PHONE, OWNER_PHONE, default_agent, make_ctx, sp
from tests.motor.test_jobs import book, silent_lead

MARKER = "_midia_nao_ouvida"


def _audio(ctx, wa_id="AUD1", age_seconds=20):
    record = ctx.evo.add(LEAD_PHONE, "", wa_id=wa_id, age_seconds=age_seconds)
    record["message"] = {"audioMessage": {"url": "x"}}
    return record


def _alerts_midia(ctx):
    return [t for t in ctx.evo.texts_to(OWNER_PHONE) if "não consegui ouvir" in t]


class MidiaNaoOuvidaTest(unittest.TestCase):
    def _anthropic(self):
        ctx = make_ctx(default_agent(ai_provider="anthropic"))
        ctx.cfg = dataclasses.replace(ctx.cfg, ai_provider="anthropic")
        return ctx

    def test_audio_com_provedor_sem_transcricao_marca_alerta_e_segura_a_cadencia_ate_o_texto(self):
        ctx = self._anthropic()
        _audio(ctx)
        engine.ingest(ctx)
        self.assertEqual([m["body"] for m in ctx.db.messages], ["[áudio]"])   # mensagem gravada, turno roda
        lead = ctx.db.get_lead_by_phone(LEAD_PHONE)
        self.assertTrue(lead["qualification"].get(MARKER))
        self.assertEqual(len(_alerts_midia(ctx)), 1)
        self.assertIn(LEAD_PHONE, _alerts_midia(ctx)[0])
        ctx.ai.push({"reply": "Pode me escrever o que precisa?", "action": "responder"})
        self.assertEqual(handle_turn(ctx, lead["id"]).get("status"), "ok")
        self.assertTrue(ctx.db.get_lead(lead["id"])["qualification"].get(MARKER))   # o turno não apaga o marcador
        # outro ciclo e outro áudio no mesmo dia: continua 1 alerta por lead por dia
        engine.ingest(ctx)
        self.assertEqual(len(_alerts_midia(ctx)), 1)
        # follow-up do dia seguinte não sai
        ctx.clock.advance(hours=3)
        sent_before = len(ctx.evo.texts_to(LEAD_PHONE))
        self.assertEqual(jobs.followups(ctx), 0)
        ctx.clock.advance(hours=24)
        self.assertEqual(jobs.followups(ctx), 0)
        self.assertEqual(len(ctx.evo.texts_to(LEAD_PHONE)), sent_before)
        # texto do lead tira o marcador e a cadência volta
        ctx.evo.add(LEAD_PHONE, "ok", wa_id="TXT1", age_seconds=10)
        engine.ingest(ctx)
        lead = ctx.db.get_lead(lead["id"])
        self.assertNotIn(MARKER, lead["qualification"])
        ctx.ai.push({"reply": "Perfeito, anotei.", "action": "responder"})
        handle_turn(ctx, lead["id"])
        ctx.clock.advance(hours=3)
        self.assertEqual(jobs.followups(ctx), 1)

    def test_gemini_com_falha_depois_dos_retries_tambem_marca(self):
        ctx = make_ctx(default_agent())
        ctx.evo.media = (b"x", "audio/ogg")
        ctx.ai.transcribe = lambda *a: (_ for _ in ()).throw(RuntimeError("429"))
        _audio(ctx)
        engine.ingest(ctx)
        self.assertTrue(ctx.db.get_lead_by_phone(LEAD_PHONE)["qualification"].get(MARKER))
        self.assertEqual(len(_alerts_midia(ctx)), 1)

    def test_transcricao_bem_sucedida_nao_marca_nem_alerta(self):
        ctx = make_ctx(default_agent())
        ctx.evo.media = (b"x", "audio/ogg")
        _audio(ctx)
        engine.ingest(ctx)
        self.assertIn("quero saber o preço", ctx.db.messages[0]["body"])
        self.assertNotIn(MARKER, ctx.db.get_lead_by_phone(LEAD_PHONE)["qualification"])
        self.assertEqual(_alerts_midia(ctx), [])

    def test_retomada_nao_sai_com_marcador_mas_lembrete_de_call_sai(self):
        ctx = make_ctx()
        lead = silent_lead(ctx, hours_since_inbound=3, qualification={
            "_retomar": {"em": iso(ctx.now() - timedelta(minutes=5))}, MARKER: iso(ctx.now() - timedelta(hours=3))})
        self.assertEqual(jobs.retomar(ctx), 0)
        self.assertEqual(ctx.ai.calls, [])
        self.assertIn("_retomar", ctx.db.get_lead(lead["id"])["qualification"])   # intacto: volta quando ele escrever
        # lembrete é de agendamento já feito: continua saindo
        appt_lead = ctx.db.create_lead("+5511988887777", name="Ana")
        ctx.db.update_lead(appt_lead["id"], {"qualification": {MARKER: iso(ctx.now())}})
        book(ctx, appt_lead, sp(19, 14))
        ctx.clock.current = sp(18, 14, 30)
        self.assertEqual(jobs.reminders(ctx), 1)

    def test_marcador_mais_novo_nao_e_apagado_por_texto_antigo(self):
        ctx = self._anthropic()
        ctx.evo.add(LEAD_PHONE, "oi", wa_id="T0", age_seconds=200)
        _audio(ctx, "A1", age_seconds=100)
        ctx.evo.add(LEAD_PHONE, "texto velho", wa_id="T1", age_seconds=150)   # anterior ao áudio, relido depois
        engine.ingest(ctx)
        self.assertTrue(ctx.db.get_lead_by_phone(LEAD_PHONE)["qualification"].get(MARKER))


class EcoAnteriorAoLeadTest(unittest.TestCase):
    def test_fromme_relido_de_antes_do_lead_nao_ativa_takeover(self):
        ctx = make_ctx(default_agent())
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")   # criado "agora"
        ctx.evo.add(LEAD_PHONE, "oi, vi seu contato", wa_id="DONO0", from_me=True, age_seconds=8 * 3600)   # antes do lead
        engine.ingest(ctx)
        got = ctx.db.get_lead(lead["id"])
        self.assertIsNone(got["takeover_until"])
        self.assertEqual([t for t in ctx.evo.texts_to(OWNER_PHONE) if "pelo celular" in t], [])
        self.assertEqual([m["author"] for m in ctx.db.messages], ["dono"])   # entra no histórico
        # o lead escreve e a IA responde normalmente
        ctx.evo.add(LEAD_PHONE, "quero informações", wa_id="IN9", age_seconds=5)
        fresh = engine.ingest(ctx)
        self.assertIn(lead["id"], fresh)
        ctx.ai.push({"reply": "Claro!", "action": "responder"})
        self.assertEqual(handle_turn(ctx, lead["id"]).get("status"), "ok")

    def test_fromme_antigo_de_lead_ja_existente_continua_ativando_takeover(self):
        ctx = make_ctx(default_agent())
        lead = ctx.db.create_lead(LEAD_PHONE, name="Joana")
        ctx.db.update_lead(lead["id"], {"created_at": iso(ctx.now() - timedelta(days=2))})
        ctx.evo.add(LEAD_PHONE, "oi", wa_id="DONO1", from_me=True, age_seconds=8 * 3600)
        engine.ingest(ctx)
        self.assertIsNotNone(ctx.db.get_lead(lead["id"])["takeover_until"])


class EdicaoDeMensagemTest(unittest.TestCase):
    EDIT = {"protocolMessage": {"type": 14, "editedMessage": {"conversation": "não quero mais receber, pode parar"},
                                "key": {"id": "ORIG"}}}

    def test_unwrap_le_o_texto_editado(self):
        record = {"message": self.EDIT}
        self.assertFalse(is_silent_record(record))
        self.assertEqual(record_text(record), "não quero mais receber, pode parar")
        wrapped = {"message": {"editedMessage": {"message": self.EDIT}}}
        self.assertEqual(record_text(wrapped), "não quero mais receber, pode parar")

    def test_edicao_com_pedido_de_saida_aplica_opt_out(self):
        ctx = make_ctx(default_agent())
        lead = silent_lead(ctx, hours_since_inbound=3)
        record = ctx.evo.add(LEAD_PHONE, "", wa_id="EDIT1", age_seconds=10)
        record["message"] = self.EDIT
        engine.ingest(ctx)
        self.assertTrue(ctx.db.get_lead(lead["id"])["opt_out"])
        self.assertEqual(ctx.db.messages[0]["body"], "não quero mais receber, pode parar")
        self.assertEqual(ctx.db.messages[0]["direction"], "in")

    def test_revoke_continua_silencioso(self):
        revoke = {"protocolMessage": {"type": 0, "key": {"id": "X"}}}
        self.assertTrue(is_silent_record({"message": revoke}))
        ctx = make_ctx(default_agent())
        record = ctx.evo.add(LEAD_PHONE, "", wa_id="REV1", age_seconds=10)
        record["message"] = revoke
        engine.ingest(ctx)
        self.assertEqual(ctx.db.messages, [])


if __name__ == "__main__":
    unittest.main()
