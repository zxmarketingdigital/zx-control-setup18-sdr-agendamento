"""Conexão do WhatsApp pelo painel (motor/whatsapp.py): Evolution e banco falsos, nenhum efeito externo."""

import base64
import unittest
from datetime import timedelta

from motor import engine, whatsapp
from motor.engine import ReadOnlyDB
from motor.evolution import EvolutionError
from motor.util import iso, parse_ts
from tests.motor.support import make_ctx

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"fake-png-body").decode()
PNG2 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"second-body").decode()


def new_row(**over):
    row = {"id": 1, "state": "desconhecido", "owner_number": None, "qr_base64": None, "pairing_code": None,
           "qr_expires_at": None, "command": None, "command_id": None, "command_status": None,
           "command_error": None, "command_requested_at": None, "checked_at": None}
    row.update(over)
    return row


def setup(row=None, evo_state="close"):
    ctx = make_ctx()
    store = {"row": row if row is not None else new_row(), "writes": [], "reads": 0, "missing": False,
             "offset": timedelta(0), "on_read": None}

    def get():
        """Imita a RPC whatsapp_status: relógio e idades calculados no BANCO (ctx.now() + offset)."""
        store["reads"] += 1
        if store["missing"]:
            raise RuntimeError("relation does not exist")
        out = dict(store["row"])
        snow = ctx.now() + store["offset"]

        def age(key):
            moment = parse_ts(out.get(key))
            return None if moment is None else (snow - moment).total_seconds()

        expires = parse_ts(out.get("qr_expires_at"))
        out["server_now"] = iso(snow)
        out["checked_age_seconds"] = age("checked_at")
        out["command_age_seconds"] = age("command_requested_at")
        out["qr_valid"] = bool(out.get("qr_base64")) and expires is not None and expires > snow \
            and out.get("command_status") == "aguardando_leitura"
        out["qr_remaining_seconds"] = None if expires is None else max(0, (expires - snow).total_seconds())
        if store["on_read"]:
            store["on_read"]()
        return out

    def update(fields, command_id=None):
        store["writes"].append(dict(fields))
        if command_id is not None and store["row"].get("command_id") != command_id:
            return 0
        store["row"].update(fields)
        return 1

    ctx.db.get_whatsapp_connection = get
    ctx.db.update_whatsapp_connection = update
    evo = ctx.evo
    evo.state = evo_state
    evo.calls = []
    evo.qrs = [{"base64": "data:image/png;base64," + PNG, "pairingCode": "ABCD1234"}]
    evo.connect_errors = []
    evo.owner = "5511999990001@s.whatsapp.net"

    def connect():
        evo.calls.append("connect")
        if evo.connect_errors:
            raise evo.connect_errors.pop(0)
        return evo.qrs.pop(0) if len(evo.qrs) > 1 else evo.qrs[0]

    def logout():
        evo.calls.append("logout")
        if getattr(evo, "logout_error", None):
            raise evo.logout_error

    evo.connect = connect
    evo.logout = logout
    evo.create_instance = lambda: evo.calls.append("create")
    evo.owner_jid = lambda: evo.owner
    ctx.store = store
    ctx.logs = []

    class Capture:
        def __getattr__(self, name):
            return lambda msg, *a, **k: ctx.logs.append(str(msg) % a if a else str(msg))

    ctx.log = Capture()
    return ctx


def request(ctx, command="connect", status="pendente"):
    ctx.store["row"].update(command=command, command_status=status, command_id="11111111-1111-1111-1111-111111111111",
                            command_requested_at=iso(ctx.now()))


class ConectarTest(unittest.TestCase):
    def test_connect_grava_qr_como_data_url(self):
        ctx = setup()
        request(ctx)
        whatsapp.tick(ctx, ctx.now())
        row = ctx.store["row"]
        self.assertEqual(row["command_status"], "aguardando_leitura")
        self.assertEqual(row["qr_base64"], "data:image/png;base64," + PNG)
        self.assertEqual(row["pairing_code"], "ABCD1234")
        self.assertEqual(row["qr_expires_at"], iso(ctx.now() + timedelta(seconds=whatsapp.QR_TTL_SECONDS)))
        self.assertEqual(row["state"], "close")

    def test_base64_sem_prefixo_e_normalizado(self):
        ctx = setup()
        ctx.evo.qrs = [{"base64": PNG}]
        request(ctx)
        whatsapp.tick(ctx, ctx.now())
        self.assertEqual(ctx.store["row"]["qr_base64"], "data:image/png;base64," + PNG)

    def test_base64_invalido_vira_erro(self):
        for bad in ["isso nao e base64!!", base64.b64encode(b"nao e png").decode(), "", None,
                    "data:image/jpeg;base64," + PNG]:
            ctx = setup()
            ctx.evo.qrs = [{"base64": bad}]
            request(ctx)
            whatsapp.tick(ctx, ctx.now())
            row = ctx.store["row"]
            self.assertEqual(row["command_status"], "erro", bad)
            self.assertIn("QR válido", row["command_error"])
            self.assertIsNone(row["qr_base64"])

    def test_state_open_conclui_apaga_qr_e_grava_numero_so_digitos(self):
        ctx = setup()
        request(ctx)
        whatsapp.tick(ctx, ctx.now())
        ctx.evo.state = "open"
        ctx.evo.owner = "5511988887777:12@s.whatsapp.net"
        ctx.clock.advance(seconds=5)
        whatsapp.tick(ctx, ctx.now())
        row = ctx.store["row"]
        self.assertEqual(row["command_status"], "concluido")
        self.assertIsNone(row["qr_base64"])
        self.assertIsNone(row["pairing_code"])
        self.assertIsNone(row["qr_expires_at"])
        self.assertEqual(row["owner_number"], "5511988887777")
        self.assertEqual(row["state"], "open")

    def test_ja_conectado_conclui_sem_pedir_qr(self):
        ctx = setup(evo_state="open")
        request(ctx)
        whatsapp.tick(ctx, ctx.now())
        self.assertEqual(ctx.store["row"]["command_status"], "concluido")
        self.assertNotIn("connect", ctx.evo.calls)

    def test_qr_expirado_dentro_da_janela_renova(self):
        ctx = setup()
        ctx.evo.qrs = [{"base64": PNG}, {"base64": PNG2}]
        request(ctx)
        whatsapp.tick(ctx, ctx.now())
        ctx.clock.advance(seconds=whatsapp.QR_TTL_SECONDS + 1)
        whatsapp.tick(ctx, ctx.now())
        row = ctx.store["row"]
        self.assertEqual(row["command_status"], "aguardando_leitura")
        self.assertEqual(row["qr_base64"], "data:image/png;base64," + PNG2)
        self.assertEqual(ctx.evo.calls.count("connect"), 2)

    def test_qr_ainda_valido_nao_renova(self):
        ctx = setup()
        request(ctx)
        whatsapp.tick(ctx, ctx.now())
        ctx.clock.advance(seconds=10)
        whatsapp.tick(ctx, ctx.now())
        self.assertEqual(ctx.evo.calls.count("connect"), 1)

    def test_fora_da_janela_expira_e_apaga_qr(self):
        ctx = setup()
        request(ctx)
        whatsapp.tick(ctx, ctx.now())
        ctx.clock.advance(seconds=whatsapp.QR_WINDOW_SECONDS + 1)
        whatsapp.tick(ctx, ctx.now())
        row = ctx.store["row"]
        self.assertEqual(row["command_status"], "expirado")
        self.assertIsNone(row["qr_base64"])
        self.assertEqual(ctx.evo.calls.count("connect"), 1)

    def test_404_cria_instancia_e_pede_qr_de_novo(self):
        ctx = setup()
        ctx.evo.connect_errors = [EvolutionError("nao existe", status=404)]
        request(ctx)
        whatsapp.tick(ctx, ctx.now())
        self.assertEqual(ctx.evo.calls, ["connect", "create", "connect"])
        self.assertEqual(ctx.store["row"]["command_status"], "aguardando_leitura")

    def test_erro_da_evolution_vira_erro_curto_sem_segredo(self):
        ctx = setup()
        ctx.evo.connect_errors = [EvolutionError("http://host/apikey=SEGREDO", status=500)]
        request(ctx)
        whatsapp.tick(ctx, ctx.now())
        row = ctx.store["row"]
        self.assertEqual(row["command_status"], "erro")
        self.assertIn("HTTP 500", row["command_error"])
        self.assertNotIn("SEGREDO", row["command_error"])


class TrocarNumeroTest(unittest.TestCase):
    def test_logout_so_desloga_e_conclui_sem_qr(self):
        ctx = setup(evo_state="open")
        ctx.store["row"]["owner_number"] = "5511999990001"
        request(ctx, "logout")
        whatsapp.tick(ctx, ctx.now())
        self.assertEqual(ctx.evo.calls, ["logout"])
        row = ctx.store["row"]
        self.assertEqual(row["command_status"], "concluido")
        self.assertIsNone(row["qr_base64"])
        self.assertIsNone(row["owner_number"])
        self.assertEqual(row["state"], "close")

    def test_logout_ja_desconectado_conta_como_sucesso(self):
        ctx = setup(evo_state="close")
        ctx.evo.logout_error = EvolutionError("not connected", status=400)
        request(ctx, "logout")
        whatsapp.tick(ctx, ctx.now())
        self.assertEqual(ctx.store["row"]["command_status"], "concluido")
        self.assertNotIn("connect", ctx.evo.calls)

    def test_logout_com_erro_real_vira_erro(self):
        ctx = setup(evo_state="open")
        ctx.evo.logout_error = EvolutionError("boom", status=500)
        request(ctx, "logout")
        whatsapp.tick(ctx, ctx.now())
        self.assertEqual(ctx.store["row"]["command_status"], "erro")
        self.assertNotIn("connect", ctx.evo.calls)


class RelogioDoBancoTest(unittest.TestCase):
    def test_pedido_velho_de_8h_com_motor_desligado_nao_desloga(self):
        ctx = setup(evo_state="open")
        ctx.store["row"].update(command="logout", command_status="pendente", command_id="old",
                                command_requested_at=iso(ctx.now() - timedelta(hours=8)),
                                qr_base64="data:image/png;base64," + PNG)
        whatsapp.tick(ctx, ctx.now())
        self.assertNotIn("logout", ctx.evo.calls)
        self.assertEqual(ctx.store["row"]["command_status"], "expirado")
        self.assertIsNone(ctx.store["row"]["qr_base64"])

    def test_pedido_com_120s_expira_e_com_119s_executa(self):
        ctx = setup()
        ctx.store["row"].update(command="connect", command_status="pendente", command_id="a",
                                command_requested_at=iso(ctx.now() - timedelta(seconds=whatsapp.COMMAND_TTL_SECONDS)))
        whatsapp.tick(ctx, ctx.now())
        self.assertEqual(ctx.store["row"]["command_status"], "expirado")
        self.assertEqual(ctx.evo.calls, [])
        ctx = setup()
        ctx.store["row"].update(command="connect", command_status="pendente", command_id="a",
                                command_requested_at=iso(ctx.now() - timedelta(seconds=119)))
        whatsapp.tick(ctx, ctx.now())
        self.assertEqual(ctx.store["row"]["command_status"], "aguardando_leitura")

    def test_comando_trocado_no_meio_nao_e_sobrescrito(self):
        ctx = setup(evo_state="open")
        request(ctx, "logout")
        ctx.store["row"]["command_id"] = "antigo"

        # o motor lê o comando 'antigo'; antes da 1ª escrita de comando chega um novo
        real_update = ctx.db.update_whatsapp_connection

        def update(fields, command_id=None):
            if "command_status" in fields and ctx.store["row"]["command_id"] == "antigo":
                ctx.store["row"].update(command_id="novo", command="connect", command_status="pendente")
            return real_update(fields, command_id=command_id)

        ctx.db.update_whatsapp_connection = update
        whatsapp.tick(ctx, ctx.now())
        row = ctx.store["row"]
        self.assertEqual(row["command_id"], "novo")
        self.assertEqual(row["command_status"], "pendente")
        self.assertNotIn("logout", ctx.evo.calls)
        self.assertTrue(any("trocado" in m for m in ctx.logs))

    def test_relogio_local_1h_adiantado_nao_muda_validade_do_qr(self):
        ctx = setup()
        ctx.store["offset"] = -timedelta(hours=1)   # banco 1 h atrás do relógio local
        ctx.store["row"].update(command="connect", command_status="pendente", command_id="a",
                                command_requested_at=iso(ctx.now() + ctx.store["offset"]))
        whatsapp.tick(ctx, ctx.now())
        row = ctx.store["row"]
        self.assertEqual(row["command_status"], "aguardando_leitura")
        self.assertEqual(row["qr_expires_at"], iso(ctx.now() + ctx.store["offset"]
                                                   + timedelta(seconds=whatsapp.QR_TTL_SECONDS)))
        ctx.clock.advance(seconds=10)
        whatsapp.tick(ctx, ctx.now())
        self.assertEqual(ctx.evo.calls.count("connect"), 1)   # QR ainda vale: não renova
        self.assertEqual(ctx.store["row"]["command_status"], "aguardando_leitura")
        ctx.clock.advance(seconds=whatsapp.QR_TTL_SECONDS)
        whatsapp.tick(ctx, ctx.now())
        self.assertEqual(ctx.evo.calls.count("connect"), 2)   # venceu pelo relógio do banco: renova

    def test_heartbeat_grava_horario_do_banco(self):
        ctx = setup()
        ctx.store["offset"] = timedelta(hours=1)
        whatsapp.tick(ctx, ctx.now())
        self.assertEqual(ctx.store["row"]["checked_at"], iso(ctx.now() + timedelta(hours=1)))

    def test_open_sem_comando_limpa_qr_residual(self):
        ctx = setup(evo_state="open")
        ctx.store["row"].update(qr_base64="data:image/png;base64," + PNG, pairing_code="ABCD1234",
                                qr_expires_at=iso(ctx.now() + timedelta(seconds=30)),
                                command="connect", command_status="concluido", command_id="z")
        whatsapp.tick(ctx, ctx.now())
        row = ctx.store["row"]
        self.assertIsNone(row["qr_base64"])
        self.assertIsNone(row["pairing_code"])
        self.assertIsNone(row["qr_expires_at"])


class DbWhatsappTest(unittest.TestCase):
    def test_update_condicional_e_contagem(self):
        from motor import db as dbmod
        calls = []

        class D(dbmod.Database):
            def update(self, table, filters, fields):
                calls.append((table, dict(filters), fields))
                return [{"id": 1}] if filters.get("command_id", "eq.ok") == "eq.ok" else []

        d = D("http://x", "k")
        self.assertEqual(d.update_whatsapp_connection({"state": "open"}), 1)
        self.assertEqual(calls[-1][1], {"id": "eq.1"})
        self.assertEqual(d.update_whatsapp_connection({"a": 1}, command_id="ok"), 1)
        self.assertEqual(d.update_whatsapp_connection({"a": 1}, command_id="outro"), 0)
        self.assertEqual(calls[-1][1], {"id": "eq.1", "command_id": "eq.outro"})

    def test_get_chama_rpc_e_tolera_funcao_ausente(self):
        from motor import db as dbmod
        from motor.http import HttpError

        class D(dbmod.Database):
            def rpc(self, name, args):
                self.seen = name
                if self.fail:
                    raise HttpError(404, "no function")
                return {"id": 1, "server_now": "2026-01-01T00:00:00Z"}

        d = D("http://x", "k")
        d.fail = False
        self.assertEqual(d.get_whatsapp_connection()["id"], 1)
        self.assertEqual(d.seen, "whatsapp_status")
        d.fail = True
        self.assertIsNone(d.get_whatsapp_connection())


class HeartbeatTest(unittest.TestCase):
    def test_respeita_30_segundos(self):
        ctx = setup()
        whatsapp.tick(ctx, ctx.now())
        first = len(ctx.store["writes"])
        self.assertEqual(first, 1)
        ctx.clock.advance(seconds=whatsapp.CHECK_INTERVAL_SECONDS - 1)
        whatsapp.tick(ctx, ctx.now())
        self.assertEqual(len(ctx.store["writes"]), first)
        ctx.clock.advance(seconds=2)
        whatsapp.tick(ctx, ctx.now())
        self.assertEqual(len(ctx.store["writes"]), first + 1)
        self.assertEqual(ctx.store["row"]["checked_at"], iso(ctx.now()))

    def test_estado_open_grava_numero(self):
        ctx = setup(evo_state="open")
        whatsapp.tick(ctx, ctx.now())
        self.assertEqual(ctx.store["row"]["state"], "open")
        self.assertEqual(ctx.store["row"]["owner_number"], "5511999990001")

    def test_comando_ativo_confere_todo_ciclo(self):
        ctx = setup()
        request(ctx)
        whatsapp.tick(ctx, ctx.now())
        n = len(ctx.store["writes"])
        ctx.clock.advance(seconds=3)
        whatsapp.tick(ctx, ctx.now())
        self.assertGreater(len(ctx.store["writes"]), n)


class RobustezTest(unittest.TestCase):
    def test_tabela_ausente_nao_derruba_cycle_e_loga_uma_vez(self):
        ctx = setup()
        ctx.store["missing"] = True
        logs = []
        real_log = ctx.log
        ctx.log = type("L", (), {"__getattr__": lambda self, n: (lambda m, *a, **k: logs.append(str(m) % a if a else str(m)))})()
        engine.cycle(ctx)
        engine.cycle(ctx)
        hints = [m for m in logs if "etapa_banco.py migrar" in m]
        self.assertEqual(len(hints), 1)

    def test_erro_inesperado_no_tick_nao_derruba_cycle(self):
        ctx = setup()
        ctx.db.get_whatsapp_connection = lambda: (_ for _ in ()).throw(ValueError("x"))
        self.assertIsInstance(engine.cycle(ctx), dict)

    def test_evolution_fora_nao_derruba(self):
        ctx = setup()
        ctx.evo.state = "erro"
        whatsapp.tick(ctx, ctx.now())
        self.assertEqual(ctx.store["row"]["state"], "desconhecido")

    def test_read_only_db_nao_grava(self):
        ctx = setup()
        real = ctx.db
        sim = ReadOnlyDB(real)
        self.assertEqual(sim.get_whatsapp_connection()["id"], 1)
        sim.update_whatsapp_connection({"state": "open"})
        self.assertEqual(ctx.store["writes"], [])
        self.assertEqual(sim.writes[0][0], "update_whatsapp_connection")

    def test_tick_nao_roda_em_simulacao(self):
        ctx = setup()
        ctx.db = ReadOnlyDB(ctx.db)
        whatsapp.tick(ctx, ctx.now())
        self.assertEqual(ctx.store["reads"], 0)
        ctx2 = setup()
        ctx2.evo = None
        whatsapp.tick(ctx2, ctx2.now())
        self.assertEqual(ctx2.store["reads"], 0)

    def test_qr_nunca_aparece_em_log(self):
        ctx = setup()
        request(ctx)
        whatsapp.tick(ctx, ctx.now())
        ctx.clock.advance(seconds=60)
        whatsapp.tick(ctx, ctx.now())
        ctx.evo.state = "open"
        whatsapp.tick(ctx, ctx.now())
        self.assertTrue(ctx.logs)
        for line in ctx.logs:
            self.assertNotIn(PNG, line)
            self.assertNotIn("data:image", line)

    def test_normalize_qr(self):
        self.assertEqual(whatsapp.normalize_qr(" " + PNG + "\n"), "data:image/png;base64," + PNG)
        self.assertIsNone(whatsapp.normalize_qr("<script>"))


if __name__ == "__main__":
    unittest.main()
