"""Linha de comando: python -m motor run | once | check | simulate."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .config import ConfigError, load_config
from .lock import InstanceLock, LockError


def _line(ok: bool, label: str, detail: str) -> bool:
    print(f"{'✅' if ok else '❌'} {label}: {detail}")
    return ok


def check(ctx) -> bool:
    """Confere cada ligação do motor. Devolve True só se tudo estiver certo."""
    results = []
    problems = ctx.cfg.problems()
    results.append(_line(not problems, "Configuração", "completa" if not problems else "; ".join(problems)))
    try:
        results.append(_line(True, "Supabase", ctx.db.ping()))
    except Exception as exc:  # noqa: BLE001
        results.append(_line(False, "Supabase", str(exc)[:200]))
    try:
        state = ctx.evo.connection_state()
        results.append(_line(state == "open", "Evolution", f"instância '{ctx.cfg.instance}' no estado '{state}'"
                             + ("" if state == "open" else " (leia o QR code de novo)")))
    except Exception as exc:  # noqa: BLE001
        results.append(_line(False, "Evolution", str(exc)[:200]))
    try:
        results.append(_line(True, "IA", ctx.ai.ping()))
    except Exception as exc:  # noqa: BLE001
        results.append(_line(False, "IA", str(exc)[:200]))
    if ctx.gcal:
        try:
            ok = ctx.gcal.ping()
            results.append(_line(ok, "Google Agenda", "ponte respondeu" if ok else "ponte não respondeu (confira a URL e o segredo)"))
        except Exception as exc:  # noqa: BLE001
            results.append(_line(False, "Google Agenda", str(exc)[:200]))
    return all(results)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="motor", description="Motor do Agente SDR de Agendamento")
    parser.add_argument("--config", help="caminho do config.json (padrão: config/config.json)")
    parser.add_argument("--version", action="version", version=f"motor {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="roda o motor em laço contínuo")
    sub.add_parser("once", help="roda um ciclo completo e sai")
    sub.add_parser("check", help="confere Supabase, Evolution e IA")
    sim = sub.add_parser("simulate", help="simula uma conversa sem enviar nada pelo WhatsApp")
    sim.add_argument("--phone", required=True, help="telefone do lead no formato +5511999990000")
    sim.add_argument("--text", required=True, help="mensagem que o lead mandou")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"❌ {exc}")
        return 2
    from . import engine  # import tardio: `--help` não precisa de rede nem de config

    log = engine.setup_logging(cfg, console=args.command in ("run", "once"))
    ctx = engine.build_ctx(cfg, log)
    if args.command == "check":
        return 0 if check(ctx) else 1
    problems = cfg.problems(need_evolution=args.command != "simulate")
    if problems:
        print("❌ Configuração incompleta: " + "; ".join(problems))
        return 2
    if args.command == "simulate":
        try:
            engine.simulate(ctx, args.phone, args.text)
        except ValueError as exc:
            print(f"❌ {exc}")
            return 2
        return 0
    try:
        with InstanceLock(cfg.data_dir / "motor.lock"):
            if args.command == "once":
                print(engine.run_once(ctx))
            else:
                engine.run_forever(ctx)
    except LockError as exc:
        print(f"❌ {exc}")
        return 3
    except KeyboardInterrupt:
        print("Motor encerrado.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
