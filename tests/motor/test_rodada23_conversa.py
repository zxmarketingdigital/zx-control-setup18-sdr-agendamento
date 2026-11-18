"""Rodada 23, fatia M1: opt-out silencioso não fica pendente depois da reativação e exceção não mapeada depois da
chamada à Evolution é envio incerto. Tudo com Evolution/banco/IA falsos."""
import unittest
from datetime import timedelta

from motor import conversa, engine
from motor.conversa import handle_turn

OPTOUT_ATE_KEY = "_optout_ate"
from motor.util import iso
from tests.motor.support import LEAD_PHONE, OWNER_PHONE, inbound, make_ctx


def lead_texts(ctx):
    return [t for t in ctx.evo.texts_to(LEAD_PHONE)]


class OptOutSilenciosoTest(unittest.TestCase):
    def _takeover_com_pedido(self):
        ctx = make_ctx()
        lead = inbound(ctx, "para de mandar mensagem")
        ctx.db.update_lead(lead["id"], {"takeover_until": iso(ctx.now() + timedelta(hours=2))})
        self.assertEqual(handle_turn(ctx, lead["id"])["status"], "opt_out_registrado")
        row = ctx.db.leads[lead["id"]]
        self.assertTrue(row["opt_out"])
        self.assertIn(OPTOUT_ATE_KEY, row["qualification"])
        self.assertEqual(lead_texts(ctx), [])
        return ctx, lead

    def test_reativacao_pelo_painel_nao_reaplica_o_pedido_antigo(self):
        ctx, lead = self._takeover_com_pedido()
        ctx.clock.advance(minutes=5)
        ctx.db.update_lead(lead["id"], {"opt_out": False, "takeover_until": None, "stage": "respondeu"})
        ctx.db.add_message(lead["id"], "in", "lead", "oi, pode me chamar de novo")
        ctx.ai.push({"reply": "Claro, como posso ajudar?", "action": "responder"})
        self.assertEqual(handle_turn(ctx, lead["id"])["status"], "ok")
        self.assertFalse(ctx.db.leads[lead["id"]]["opt_out"])
        self.assertEqual(lead_texts(ctx), ["Claro, como posso ajudar?"])

    def test_takeover_que_expira_nao_manda_confirmacao_depois_da_reativacao(self):
        ctx, lead = self._takeover_com_pedido()
        ctx.clock.advance(minutes=5)
        ctx.db.update_lead(lead["id"], {"opt_out": False, "stage": "respondeu"})   # painel reativa, takeover segue
        ctx.db.add_message(lead["id"], "in", "lead", "oi, pode me chamar de novo")
        self.assertEqual(handle_turn(ctx, lead["id"])["status"], "takeover")
        ctx.clock.advance(hours=3)   # takeover expira
        ctx.ai.push({"reply": "Oi! Posso ajudar?", "action": "responder"})
        handle_turn(ctx, lead["id"])
        self.assertNotIn(conversa.OPT_OUT_CONFIRMATION, lead_texts(ctx))
        self.assertFalse(ctx.db.leads[lead["id"]]["opt_out"])

    def test_novo_pedido_depois_da_reativacao_aplica_a_saida(self):
        ctx, lead = self._takeover_com_pedido()
        ctx.clock.advance(minutes=5)
        ctx.db.update_lead(lead["id"], {"opt_out": False, "takeover_until": None, "stage": "respondeu"})
        ctx.clock.advance(minutes=5)
        ctx.db.add_message(lead["id"], "in", "lead", "pare de me mandar mensagem")
        self.assertEqual(handle_turn(ctx, lead["id"])["status"], "opt_out_confirmado")
        self.assertTrue(ctx.db.leads[lead["id"]]["opt_out"])
        self.assertEqual(lead_texts(ctx), [conversa.OPT_OUT_CONFIRMATION])

    def test_fronteira_preserva_outras_chaves_da_qualificacao(self):
        ctx = make_ctx()
        lead = inbound(ctx, "não quero mais receber")
        ctx.db.update_lead(lead["id"], {"qualification": {"nicho": "dentista"}})
        handle_turn(ctx, lead["id"])
        q = ctx.db.leads[lead["id"]]["qualification"]
        self.assertEqual(q.get("nicho"), "dentista")
        self.assertIn(OPTOUT_ATE_KEY, q)


class EnvioComExcecaoNaoMapeadaTest(unittest.TestCase):
    def _arma(self, ctx, exc):
        calls = []
        real = ctx.evo.send_text

        def boom(phone, text, delay_ms=0):
            if phone != LEAD_PHONE:
                return real(phone, text, delay_ms)
            calls.append(text)
            raise exc
        ctx.evo.send_text = boom
        return calls

    def test_excecao_depois_da_evolution_e_incerta_sem_segunda_resposta_e_um_alerta(self):
        ctx = make_ctx()
        lead = inbound(ctx, "oi, quero saber mais")
        calls = self._arma(ctx, ValueError("json inválido"))
        ctx.ai.push({"reply": "Olá! Conte mais.", "action": "responder"},
                    {"reply": "Olá de novo!", "action": "responder"})
        handle_turn(ctx, lead["id"])   # não levanta
        ctx.clock.advance(minutes=1)
        # ciclo seguinte: o catch-up não reabre o turno (marcador durável do envio incerto)
        self.assertNotIn(lead["id"], engine.recover_unanswered(ctx))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(ctx.ai.calls), 1)
        self.assertEqual(len([t for t in ctx.evo.texts_to(OWNER_PHONE) if "Não tenho certeza" in t]), 1)

    def test_excecao_antes_da_evolution_continua_subindo(self):
        ctx = make_ctx()
        lead = inbound(ctx)
        original = ctx.messenger.allowed

        def quebra(phone, internal=False):
            raise RuntimeError("antes da Evolution")
        ctx.messenger.allowed = quebra
        with self.assertRaises(RuntimeError):
            conversa.deliver(ctx, ctx.db.get_lead(lead["id"]), "oi")
        ctx.messenger.allowed = original


class MessengerSendExcecaoTest(unittest.TestCase):
    """r23: o próprio Messenger.send trata erro não mapeado da Evolution como envio incerto (cobre jobs e outbox)."""

    def test_send_com_valueerror_devolve_none_incerto_e_nao_levanta(self):
        ctx = make_ctx()

        def boom(phone, text, delay_ms=0):
            raise ValueError("json inválido")
        ctx.evo.send_text = boom
        key = ctx.messenger.send(LEAD_PHONE, "oi", internal=True)
        self.assertIsNone(key)
        self.assertTrue(ctx.messenger.evolution_called)
        self.assertEqual(ctx.messenger.last_failure, "timeout_incerto")


class JobsReleituraUsaFronteiraTest(unittest.TestCase):
    def test_pedido_antigo_antes_da_fronteira_nao_bloqueia_o_envio_do_job(self):
        from motor import jobs
        ctx = make_ctx()
        lead = inbound(ctx, "para de me mandar mensagem")
        msgs = ctx.db.recent_messages(lead["id"], 20)
        ultimo = max(m["created_at"] for m in msgs)
        fresh = ctx.db.get_lead(lead["id"])
        ctx.db.update_lead(lead["id"], {"opt_out": False,
                                        "qualification": {**(fresh.get("qualification") or {}), "_optout_ate": ultimo}})
        snap = ctx.db.get_lead(lead["id"])
        from unittest import mock
        with mock.patch.object(jobs, "_directed_chat_read", lambda c, s: None):
            self.assertFalse(jobs._changed(ctx, snap))
            # sem a fronteira o mesmo pedido antigo bloquearia (prova de que o teste mede a fronteira)
            q = dict(snap.get("qualification") or {}); q.pop("_optout_ate", None)
            ctx.db.update_lead(lead["id"], {"qualification": q})
            self.assertTrue(jobs._changed(ctx, ctx.db.get_lead(lead["id"])))


if __name__ == "__main__":
    unittest.main()
