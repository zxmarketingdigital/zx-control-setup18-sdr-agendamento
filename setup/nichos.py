#!/usr/bin/env python3
"""Carrega e valida os modelos de nicho (nichos/*.yaml).

Único arquivo do instalador que usa PyYAML. Os campos de um nicho têm os mesmos
nomes das colunas de `agent_config`, mais `titulo` e `descricao` (só para o menu
do instalador; não vão para o banco).

Uso:
  python3 setup/nichos.py listar
  python3 setup/nichos.py validar
  python3 setup/nichos.py mostrar <nicho>
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _comum import NICHOS_DIR, aviso, erro, ok  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover - mensagem amigável
    yaml = None

# Placeholders: os três primeiros o motor preenche a cada mensagem; os outros três
# o instalador resolve uma vez, ao gravar no banco.
PLACEHOLDERS_MOTOR = {"nome", "data", "hora"}
PLACEHOLDERS_INSTALACAO = {"empresa", "agente", "oferta"}
PLACEHOLDERS_VALIDOS = PLACEHOLDERS_MOTOR | PLACEHOLDERS_INSTALACAO

MIN_OBJECOES = 6
MIN_PERGUNTAS = 4
TOQUES_FOLLOWUP = 3
CAMPOS_TEXTO = ("niche", "titulo", "descricao", "tone", "confirm_template",
                "reminder_24h_template", "reminder_2h_template", "knowledge")
_RE_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")


def _erros_leitura():
    """Erros de leitura de um nicho; sem PyYAML (yaml None) não existe YAMLError, então não referenciamos o módulo."""
    erros = [OSError, ValueError]
    if yaml is not None:
        erros.append(yaml.YAMLError)
    return tuple(erros)


_ERROS_LEITURA = _erros_leitura()


def _exigir_yaml():
    if yaml is None:
        raise SystemExit(
            "PyYAML não está instalado. Rode: %s -m pip install pyyaml" % (sys.executable or "python3")
        )


def arquivos():
    return sorted(Path(NICHOS_DIR).glob("*.yaml"))


def carregar(caminho):
    """Lê um YAML de nicho. Lança ValueError se não for um mapa."""
    _exigir_yaml()
    dados = yaml.safe_load(Path(caminho).read_text(encoding="utf-8"))
    if not isinstance(dados, dict):
        raise ValueError("%s: o arquivo precisa ser um mapa YAML" % Path(caminho).name)
    return dados


def carregar_todos():
    """{niche: dados} de todos os nichos/*.yaml. Falha de leitura vira SystemExit com o arquivo."""
    todos = {}
    for caminho in arquivos():
        try:
            dados = carregar(caminho)
        except _ERROS_LEITURA as exc:
            raise SystemExit("Não consegui ler %s: %s" % (caminho.name, exc))
        todos[str(dados.get("niche") or caminho.stem)] = dados
    return todos


def _placeholders(texto):
    return {m.group(1) for m in _RE_PLACEHOLDER.finditer(str(texto))}


def _checar_texto(nome, texto, erros, obrigatorios=()):
    if not isinstance(texto, str) or not texto.strip():
        erros.append("%s: texto ausente ou vazio" % nome)
        return
    desconhecidos = _placeholders(texto) - PLACEHOLDERS_VALIDOS
    if desconhecidos:
        erros.append("%s: placeholder(s) desconhecido(s) %s (válidos: %s)" % (
            nome, sorted("{%s}" % d for d in desconhecidos), sorted("{%s}" % p for p in PLACEHOLDERS_VALIDOS)))
    for obrig in obrigatorios:
        if obrig not in _placeholders(texto):
            erros.append("%s: precisa conter {%s}" % (nome, obrig))


def validar(dados, nome="nicho"):
    """Devolve a lista de erros (vazia = válido). Não lança."""
    erros = []
    if not isinstance(dados, dict):
        return ["%s: não é um mapa" % nome]

    for campo in CAMPOS_TEXTO:
        if campo in ("confirm_template", "reminder_24h_template", "reminder_2h_template", "knowledge", "tone"):
            continue
        if not isinstance(dados.get(campo), str) or not dados[campo].strip():
            erros.append("%s: campo '%s' ausente ou vazio" % (nome, campo))
    if isinstance(dados.get("niche"), str) and not re.fullmatch(r"[a-z0-9][a-z0-9-]*", dados["niche"]):
        erros.append("%s: 'niche' deve ser um identificador minúsculo (letras, números e hífen)" % nome)

    _checar_texto("%s.tone" % nome, dados.get("tone"), erros)
    _checar_texto("%s.confirm_template" % nome, dados.get("confirm_template"), erros, ("nome", "data", "hora"))
    _checar_texto("%s.reminder_24h_template" % nome, dados.get("reminder_24h_template"), erros, ("nome", "hora"))
    _checar_texto("%s.reminder_2h_template" % nome, dados.get("reminder_2h_template"), erros, ("nome", "hora"))
    _checar_texto("%s.knowledge" % nome, dados.get("knowledge"), erros)

    # perguntas de qualificação
    perguntas = dados.get("qualification_questions")
    if not isinstance(perguntas, list) or len(perguntas) < MIN_PERGUNTAS:
        erros.append("%s.qualification_questions: precisa de ao menos %d perguntas" % (nome, MIN_PERGUNTAS))
    else:
        chaves = set()
        for i, p in enumerate(perguntas):
            rot = "%s.qualification_questions[%d]" % (nome, i)
            if not isinstance(p, dict):
                erros.append("%s: precisa ser um mapa" % rot)
                continue
            if not re.fullmatch(r"[a-z][a-z0-9_]*", str(p.get("key") or "")):
                erros.append("%s.key: inválida (use minúsculas e _)" % rot)
            if p.get("key") in chaves:
                erros.append("%s.key: repetida (%s)" % (rot, p.get("key")))
            chaves.add(p.get("key"))
            _checar_texto(rot + ".pergunta", p.get("pergunta"), erros)
            if not isinstance(p.get("obrigatoria"), bool):
                erros.append("%s.obrigatoria: precisa ser true ou false" % rot)
        if not any(isinstance(p, dict) and p.get("obrigatoria") is True for p in perguntas):
            erros.append("%s.qualification_questions: ao menos uma pergunta obrigatória" % nome)

    # desqualificação
    regras = dados.get("disqualify_rules")
    if not isinstance(regras, list) or not regras:
        erros.append("%s.disqualify_rules: precisa de ao menos 1 regra" % nome)
    else:
        for i, r in enumerate(regras):
            rot = "%s.disqualify_rules[%d]" % (nome, i)
            if not isinstance(r, dict):
                erros.append("%s: precisa ser um mapa" % rot)
                continue
            if not re.fullmatch(r"[a-z][a-z0-9_]*", str(r.get("key") or "")):
                erros.append("%s.key: inválida" % rot)
            _checar_texto(rot + ".regra", r.get("regra"), erros)
            _checar_texto(rot + ".mensagem", r.get("mensagem"), erros)

    # objeções
    objecoes = dados.get("objection_playbook")
    if not isinstance(objecoes, list) or len(objecoes) < MIN_OBJECOES:
        erros.append("%s.objection_playbook: precisa de ao menos %d objeções" % (nome, MIN_OBJECOES))
    else:
        cats = set()
        for i, o in enumerate(objecoes):
            rot = "%s.objection_playbook[%d]" % (nome, i)
            if not isinstance(o, dict):
                erros.append("%s: precisa ser um mapa" % rot)
                continue
            if not re.fullmatch(r"[a-z][a-z0-9_]*", str(o.get("categoria") or "")):
                erros.append("%s.categoria: inválida" % rot)
            if o.get("categoria") in cats:
                erros.append("%s.categoria: repetida (%s)" % (rot, o.get("categoria")))
            cats.add(o.get("categoria"))
            sinais = o.get("sinais")
            if not isinstance(sinais, list) or not sinais or not all(isinstance(s, str) and s.strip() for s in sinais):
                erros.append("%s.sinais: lista de textos não vazia" % rot)
            _checar_texto(rot + ".resposta", o.get("resposta"), erros)

    # follow-up
    toques = dados.get("followup_messages")
    if not isinstance(toques, list) or len(toques) != TOQUES_FOLLOWUP:
        erros.append("%s.followup_messages: precisa ter exatamente %d toques" % (nome, TOQUES_FOLLOWUP))
    else:
        anterior = 0
        for i, t in enumerate(toques):
            rot = "%s.followup_messages[%d]" % (nome, i)
            if not isinstance(t, dict):
                erros.append("%s: precisa ser um mapa" % rot)
                continue
            horas = t.get("apos_horas")
            if not isinstance(horas, int) or isinstance(horas, bool) or horas <= anterior:
                erros.append("%s.apos_horas: inteiro maior que o toque anterior" % rot)
            else:
                anterior = horas
            _checar_texto(rot + ".texto", t.get("texto"), erros)
    return erros


def validar_todos():
    """{arquivo: [erros]} para cada nichos/*.yaml (lista vazia = válido). Inclui duplicidade de 'niche'."""
    resultado = {}
    vistos = {}
    for caminho in arquivos():
        try:
            dados = carregar(caminho)
        except (OSError, ValueError) as exc:
            resultado[caminho.name] = [str(exc)]
            continue
        except Exception as exc:  # YAML malformado
            resultado[caminho.name] = ["YAML inválido: %s" % exc]
            continue
        erros = validar(dados, caminho.stem)
        id_ = dados.get("niche")
        if id_ in vistos:
            erros.append("niche '%s' repetido em %s" % (id_, vistos[id_]))
        vistos[id_] = caminho.name
        resultado[caminho.name] = erros
    if not resultado:
        resultado["(nenhum arquivo)"] = ["nichos/ não tem nenhum .yaml"]
    return resultado


def resolver(texto, empresa="", agente="", oferta=""):
    """Troca {empresa}, {agente} e {oferta}; deixa {nome}, {data} e {hora} para o motor."""
    if not isinstance(texto, str):
        return texto
    return (texto.replace("{empresa}", empresa or "a empresa")
                 .replace("{agente}", agente or "a assistente")
                 .replace("{oferta}", oferta or "nossos serviços"))


def aplicar_identidade(dados, empresa="", agente="", oferta=""):
    """Cópia profunda do nicho com {empresa}/{agente}/{oferta} resolvidos em todos os textos."""
    def passa(valor):
        if isinstance(valor, str):
            return resolver(valor, empresa, agente, oferta)
        if isinstance(valor, list):
            return [passa(v) for v in valor]
        if isinstance(valor, dict):
            return {k: passa(v) for k, v in valor.items()}
        return valor
    return passa(dados)


def contar_pendencias(texto):
    """Quantos [PREENCHER] ainda restam num texto."""
    return str(texto or "").count("[PREENCHER]")


def _cmd_listar():
    for niche, d in carregar_todos().items():
        print("  %-22s %s" % (niche, d.get("titulo", "")))
        print("  %-22s %s" % ("", d.get("descricao", "")))
    return 0


def _cmd_validar():
    falhou = False
    for arquivo, erros in validar_todos().items():
        if erros:
            falhou = True
            erro("%s" % arquivo)
            for e in erros:
                print("      - %s" % e)
        else:
            ok(arquivo)
    return 1 if falhou else 0


def _cmd_mostrar(nome):
    todos = carregar_todos()
    if nome not in todos:
        erro("Nicho '%s' não existe. Disponíveis: %s" % (nome, ", ".join(sorted(todos))))
        return 2
    d = todos[nome]
    print("%s — %s" % (d.get("niche"), d.get("titulo")))
    print("\nTom:\n%s" % d.get("tone", "").strip())
    print("\nPerguntas de qualificação:")
    for p in d["qualification_questions"]:
        print("  %s %s: %s" % ("*" if p["obrigatoria"] else "-", p["key"], p["pergunta"]))
    print("\nDesqualifica quando:")
    for r in d["disqualify_rules"]:
        print("  - %s: %s" % (r["key"], r["regra"]))
    print("\nObjeções tratadas: %s" % ", ".join(o["categoria"] for o in d["objection_playbook"]))
    print("Follow-up (horas): %s" % ", ".join(str(t["apos_horas"]) for t in d["followup_messages"]))
    pend = contar_pendencias(d.get("knowledge"))
    if pend:
        aviso("A base de conhecimento tem %d campo(s) [PREENCHER] para completar." % pend)
    return 0


def main(argv):
    comando = argv[0] if argv else "listar"
    if comando == "listar":
        return _cmd_listar()
    if comando == "validar":
        return _cmd_validar()
    if comando == "mostrar" and len(argv) > 1:
        return _cmd_mostrar(argv[1])
    print("Uso: nichos.py [listar|validar|mostrar <nicho>]")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
