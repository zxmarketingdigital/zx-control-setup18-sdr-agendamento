#!/usr/bin/env python3
"""Etapa 2: banco de dados e usuário dono.

Subcomandos:
  credenciais   guarda URL, chave service_role e chave anon do seu Supabase em config/config.json
  migrar        aplica supabase/migrations/*.sql (supabase link + db push; se falhar, mostra o caminho pelo SQL Editor)
  verificar     confere se as 13 tabelas e as 7 funções existem e respondem
  dono          cria o usuário dono no Supabase Auth (admin API, e-mail já confirmado) e o libera em panel_users pelo id
  tudo          credenciais, migrar, verificar e dono, nessa ordem

Segredos são lidos sem eco ou das variáveis SDR_SUPABASE_SERVICE_KEY, SDR_SUPABASE_ANON_KEY,
SDR_SUPABASE_DB_PASSWORD, SDR_SUPABASE_ACCESS_TOKEN e SDR_OWNER_PASSWORD. Nunca vão para a linha de comando.
"""
import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _comum import (  # noqa: E402
    checar_papeis,
    ENTREVISTA_PATH, MIGRATIONS_DIR, REPO_ROOT, aviso, ask, atualizar_config, achar_cli, cfg, confirmar,
    entrada_externa, erro, http_json, info, ler_json, load_config, load_estado, mascarar, ok, q, rodar, salvar_estado, segredo,
    supabase_rest, titulo,
)

TABELAS = [
    "agent_config", "schedule_settings", "closers", "availability_rules", "blocks", "leads",
    "messages", "funnel_events", "appointments", "objections", "outbox", "panel_users", "whatsapp_connection",
]
RPCS = ["book_slot", "reschedule_slot", "public_slots", "public_book", "set_appointment_status",
        "request_whatsapp_command", "whatsapp_status"]
_URL = re.compile(r"^https://([a-z0-9]{15,30})\.supabase\.co/?$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SENHA_MINIMA = 10


def ref_da_url(url):
    m = _URL.match(str(url or "").strip())
    return m.group(1) if m else None


_NOME_MIGRATION = re.compile(r"(\d{8,}_[A-Za-z0-9_\-]+\.sql)")


_ALTER_TABLE = re.compile(r'\balter\s+table\s+(?:only\s+)?(?:if\s+exists\s+)?(?:"[^"]+"|[\w.]+)(?:\s*\*)?\s*')


def _virgulas_de_topo(texto):
    """Divide por vírgulas fora de parênteses (ações de um mesmo ALTER TABLE)."""
    partes, nivel, atual = [], 0, []
    for ch in texto:
        if ch == "(":
            nivel += 1
        elif ch == ")":
            nivel = max(0, nivel - 1)
        if ch == "," and nivel == 0:
            partes.append("".join(atual))
            atual = []
        else:
            atual.append(ch)
    partes.append("".join(atual))
    return partes


def _achados_destrutivos(texto):
    achados = []
    for comando in texto.split(";"):
        c = " ".join(comando.lower().split())
        if re.search(r"\bdrop\s+table\b", c):
            achados.append("DROP TABLE")
        if re.search(r"\bdrop\s+schema\b", c):
            achados.append("DROP SCHEMA")
        # Dentro do ALTER TABLE (até o ';'), qualquer DROP que não seja CONSTRAINT/DEFAULT/NOT NULL, seja qual for o
        # formato do identificador (aspas, schema, ONLY, IF EXISTS).
        m = re.search(r"\balter\s+table\b", c)
        if m and re.search(r"\bdrop\s+(?!constraint\b|default\b|not\s+null\b)", c[m.end():]):
            achados.append("DROP COLUMN")
        if re.search(r"\btruncate\b", c):
            achados.append("TRUNCATE")
        for m in re.finditer(r"\bdelete\s+from\b", c):
            if not _delete_tem_where(c, m.end()):
                achados.append("DELETE FROM sem WHERE")  # sem filtro efetivo (WHERE trivial conta como sem)
        # UPDATE <tabela> [alias] SET ...: sem WHERE (ou WHERE trivial) reescreve todas as linhas. "do update set" e
        # "for update" não casam (não há tabela antes do SET).
        for m in re.finditer(r"\bupdate\s+(?:only\s+)?[\w.\"]+(?:\s+(?:as\s+)?(?!set\b)[\w\"]+)?\s+set\b", c):
            if not _delete_tem_where(c, m.end()):
                achados.append("UPDATE sem WHERE")
    return achados


_WHERE = re.compile(r"where\b")


def _parens_casam(s):
    """True se o '(' do começo de `s` fecha exatamente no último caractere."""
    nivel = 0
    for i, ch in enumerate(s):
        if ch == "(":
            nivel += 1
        elif ch == ")":
            nivel -= 1
            if nivel == 0:
                return i == len(s) - 1
    return False


def _sem_parens_externos(s):
    s = s.strip()
    while s.startswith("(") and s.endswith(")") and _parens_casam(s):
        s = s[1:-1].strip()
    return s


def _partir_no_topo(s, conectivo):
    """Divide `s` por ' and ' / ' or ' fora de parênteses."""
    partes, ultimo = [], 0
    for m in re.finditer(r"\s%s\s" % conectivo, s):
        if s[:m.start()].count("(") - s[:m.start()].count(")") == 0:
            partes.append(s[ultimo:m.start()])
            ultimo = m.end()
    partes.append(s[ultimo:])
    return partes


_PALAVRAS_SEM_COLUNA = {
    "not", "is", "true", "false", "null", "unknown", "and", "or", "distinct", "from", "like", "ilike", "between", "in",
    "cast", "as", "int", "integer", "bigint", "smallint", "text", "numeric", "decimal", "boolean", "bool", "varchar",
    "char", "float", "real", "double", "precision", "date", "timestamp", "timestamptz", "interval", "uuid", "similar", "to",
}


def _sem_identificador(s):
    """True se a condição só tem literais, operadores, casts, NOT, IS TRUE/FALSE e parênteses: não referencia coluna
    nem função, então vale igual para todas as linhas."""
    t = re.sub(r"'(?:[^']|'')*'", " ", s)          # literais de texto
    t = re.sub(r"::\s*[a-z_][a-z_0-9]*(\s*\(\s*\d+(\s*,\s*\d+)?\s*\))?(\[\])?", " ", t)  # casts ::int, ::numeric(10,2)
    if '"' in t:
        return False                                  # identificador entre aspas é coluna
    palavras = re.findall(r"[a-z_][a-z_0-9]*", t)
    return all(p in _PALAVRAS_SEM_COLUNA for p in palavras)


def _condicao_trivial(cond):
    """True se a condição do WHERE vale para todas as linhas ou não referencia nenhuma coluna: true, not false, 1 > 0,
    NOT (1 = 0), 1 = 1::int, true IS TRUE, x=x (o mesmo identificador dos dois lados), e combinações com AND (todas
    triviais) ou OR (alguma trivial)."""
    s = _sem_parens_externos(cond)
    if not s:
        return False
    if _sem_identificador(s):
        return True
    ors = _partir_no_topo(s, "or")
    if len(ors) > 1:
        return any(_condicao_trivial(p) for p in ors)
    ands = _partir_no_topo(s, "and")
    if len(ands) > 1:
        return all(_condicao_trivial(p) for p in ands)
    if s == "true":
        return True
    if s.startswith("not ") and _sem_parens_externos(s[4:]) == "false":
        return True
    m = re.fullmatch(r"(.+?)\s*(>=|<=|=|is not distinct from|ilike|like)\s*(.+)", s)
    if m:
        esq, dir_ = _sem_parens_externos(m.group(1)), _sem_parens_externos(m.group(3))
        return bool(esq) and esq == dir_
    return False


def _condicao_do_where(c, inicio):
    """Texto da condição do WHERE que começa em `inicio`: vai até o RETURNING do mesmo nível, o ')' que fecha o
    parêntese do DELETE ou o fim do comando."""
    nivel, i = 0, inicio
    while i < len(c):
        ch = c[i]
        if ch == "(":
            nivel += 1
        elif ch == ")":
            if nivel == 0:
                break
            nivel -= 1
        elif nivel == 0 and c.startswith("returning", i) and (i == 0 or not (c[i - 1].isalnum() or c[i - 1] == "_")):
            break
        i += 1
    return c[inicio:i].strip()


def _delete_tem_where(c, inicio):
    """True se há WHERE com filtro efetivo no MESMO nível de parênteses do DELETE (a partir de `inicio`). WHERE em
    subconsulta (nível mais fundo) ou depois do fechamento do parêntese que contém o DELETE (CTE) não conta, e WHERE
    trivial (true, 1=1, 'a'='a', x=x, not false) também não: apaga tudo do mesmo jeito."""
    nivel = 0
    i = inicio
    while i < len(c):
        ch = c[i]
        if ch == "(":
            nivel += 1
        elif ch == ")":
            nivel -= 1
            if nivel < 0:
                return False  # o parêntese que continha o DELETE fechou sem WHERE
        elif nivel == 0 and _WHERE.match(c, i) and (i == 0 or not (c[i - 1].isalnum() or c[i - 1] == "_")):
            return not _condicao_trivial(_condicao_do_where(c, i + len("where")))
        i += 1
    return False


def sql_destrutivo(sql):
    """Trechos do SQL que apagam dados: DROP TABLE, DROP SCHEMA, ALTER TABLE ... DROP [COLUMN] (DROP CONSTRAINT/
    DEFAULT/NOT NULL não contam), TRUNCATE e DELETE FROM sem WHERE. Analisa o SQL sem comentários E o SQL bruto
    (um '--' dentro de uma string não pode esconder um comando): falso positivo em comentário exige --confirmar-destrutivo."""
    sem_comentarios = re.sub(r"/\*.*?\*/", " ", re.sub(r"--[^\n]*", " ", sql), flags=re.S)
    return sorted(set(_achados_destrutivos(sem_comentarios)) | set(_achados_destrutivos(sql)))


def perigos_das(arquivos):
    """{nome: [operações destrutivas]} dos arquivos .sql dados."""
    perigos = {}
    for a in arquivos:
        try:
            achados = sql_destrutivo(a.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            # Fail-closed: arquivo que não dá para ler não pode ser dado como seguro.
            achados = ["não consegui ler o arquivo (%s): confira a permissão e rode de novo" % (exc.strerror or exc.__class__.__name__)]
        if achados:
            perigos[a.name] = achados
    return perigos


def recusar_destrutivas(perigos):
    for nome, achados in perigos.items():
        erro("A migration %s contém operação destrutiva (%s)." % (nome, ", ".join(achados)))
    erro("Nada foi aplicado. Faça backup do banco, leia o SQL e rode de novo com --confirmar-destrutivo se quiser mesmo aplicar. "
         "(A análise também olha comentários e textos: se o termo aparece só num comentário, é falso positivo e a mesma flag libera.)")


def pendentes_do_push(saida_dry_run):
    """Migrations locais que o `db push --dry-run` diz que vai aplicar. Saída ilegível = todas (fail-closed)."""
    locais = {a.name: a for a in migracoes()}
    nomes = [n for n in _NOME_MIGRATION.findall(str(saida_dry_run or "")) if n in locais]
    if not nomes:
        return list(locais.values())
    menor = min(_versao(n) for n in nomes)
    return [a for a in migracoes() if _versao(a.name) >= menor]


def _versao(nome):
    m = re.match(r"(\d+)", nome)
    return int(m.group(1)) if m else 0


def pendentes_ou_none(codigo, saida):
    """Migrations pendentes quando o `db push --dry-run` deu uma resposta que se entende; None quando não se sabe."""
    if codigo != 0:
        return None
    locais = {a.name: a for a in migracoes()}
    nomes = [n for n in _NOME_MIGRATION.findall(str(saida or "")) if n in locais]
    if nomes:
        # Lista parcial (formato, quebra de linha, truncamento): o `db push` aplica tudo que falta, então o guard olha
        # TODAS as locais a partir da menor versão citada, não só as citadas.
        menor = min(_versao(n) for n in nomes)
        return [a for a in migracoes() if _versao(a.name) >= menor]
    return [] if "up to date" in str(saida or "").lower() else None


def migracao_do_setup():
    """A migration do setup (*_sdr_agendamento.sql); sem ela, a primeira da pasta."""
    todas = migracoes()
    for a in todas:
        if a.name.endswith("_sdr_agendamento.sql"):
            return a
    return todas[0] if todas else None


def migracoes():
    return sorted(MIGRATIONS_DIR.glob("*.sql")) if MIGRATIONS_DIR.is_dir() else []


# ------------------------------------------------------------------ credenciais

def cmd_credenciais(args):
    config = load_config()
    url = args.url or cfg(config, "supabase", "url") or ask("URL do projeto Supabase (ex.: https://abcdefghijklmnop.supabase.co)", env="SDR_SUPABASE_URL")
    url = url.strip().rstrip("/")
    ref = ref_da_url(url)
    if not ref:
        erro("A URL precisa ser do formato https://<código>.supabase.co (Project Settings > API > Project URL).")
        return 1
    service = cfg(config, "supabase", "service_role_key")
    if not service or args.trocar_chaves:
        service = (entrada_externa("SDR_SUPABASE_SERVICE_ROLE_KEY") or segredo("Chave service_role (Project Settings > API, campo 'service_role' ou 'secret')", "SDR_SUPABASE_SERVICE_KEY"))
    anon = cfg(config, "supabase", "anon_key")
    if not anon or args.trocar_chaves:
        anon = args.anon_key or ask("Chave anon public (Project Settings > API, campo 'anon' ou 'publishable')", env="SDR_SUPABASE_ANON_KEY")
    if args.dry_run:
        titulo("Simulação (nada foi gravado nem enviado)")
        print("  guardaria URL %s e as chaves (service_role %s, anon %s) em config/config.json" % (url, mascarar(service), mascarar(anon)))
        return 0
    if service == anon:
        erro("A chave service_role e a chave anon são iguais. Copie cada uma do campo certo.")
        return 1
    problema = checar_papeis(service, anon)
    if problema:
        erro(problema)
        return 1
    # Testa a conexão com as chaves candidatas ANTES de gravar: chave errada não fica salva.
    candidato = {"supabase": {"url": url, "service_role_key": service, "anon_key": anon, "project_ref": ref}}
    st, corpo = supabase_rest(candidato, "GET", "/")
    if st == 0:
        erro("Não consegui falar com o Supabase: %s" % corpo)
        return 1
    if st in (401, 403):
        erro("O Supabase recusou a chave service_role (HTTP %s). Nada foi gravado. Confira se a URL e a chave são do MESMO projeto "
             "e rode de novo com --trocar-chaves." % st)
        return 1
    if st != 200:
        erro("O Supabase respondeu HTTP %s em vez de 200. Nada foi gravado. Confira a URL e se o projeto está ativo (não pausado) e tente de novo." % st)
        return 1
    # A anon também é testada ANTES de gravar: a de outro projeto do mesmo Supabase passa pelo papel do JWT, mas o
    # gateway deste projeto a recusa (chave de outro projeto = 401). Usa /auth/v1/settings, que é público para qualquer
    # chave válida do projeto; a raiz /rest/v1/ (OpenAPI) pode exigir chave secreta e recusaria uma anon legítima.
    st_anon, corpo_anon = http_json("GET", url + "/auth/v1/settings", headers={"apikey": anon})
    if st_anon == 0:
        erro("Não consegui testar a chave anon no Supabase: %s" % corpo_anon)
        return 1
    if st_anon in (401, 403):
        erro("A chave anon não é deste projeto (HTTP %s). Nada foi gravado. Copie a chave 'anon' / 'publishable' do MESMO "
             "projeto da URL (Project Settings > API) e rode de novo com --trocar-chaves." % st_anon)
        return 1
    if st_anon != 200:
        erro("O Supabase respondeu HTTP %s ao testar a chave anon em vez de 200. Nada foi gravado. Confira a chave e tente de novo." % st_anon)
        return 1
    antigo = (config.get("supabase") or {}) if isinstance(config.get("supabase"), dict) else {}
    mudou = any(antigo.get(k) != candidato["supabase"][k] for k in ("url", "service_role_key", "anon_key"))
    if mudou:
        # Outro projeto/outras chaves: o teste guiado e a migração do projeto anterior não valem para este.
        salvar_estado({"teste_ok": False, "banco_migrado": False})
    atualizar_config(candidato)
    ok("Credenciais gravadas em config/config.json (service_role %s, anon %s)" % (mascarar(service), mascarar(anon)))
    ok("Conexão com o Supabase funcionando")
    return 0


# ------------------------------------------------------------------ migração

def instrucoes_sql_editor(pendentes=None, confirmar_destrutivo=False):
    """Plano B pelo SQL Editor. Só lista migrations PENDENTES (`pendentes` conhecido) ou, sem essa informação, TODAS as
    migrations locais, em ordem, na instalação inicial. Arquivo com SQL destrutivo só aparece com --confirmar-destrutivo.
    Devolve True se imprimiu o passo a passo."""
    titulo("Plano B: aplicar a migration pelo SQL Editor do Supabase")
    if not migracoes():
        erro("Não encontrei supabase/migrations/*.sql. Atualize o repositório com 'git pull'.")
        return False
    if pendentes is None:
        if load_estado().get("banco_migrado"):
            aviso("Não sei quais migrations já foram aplicadas e não vou mandar colar arquivo que pode já estar no banco. "
                  "Rode 'python3 setup/etapa_banco.py migrar' com a Supabase CLI para aplicar só as pendentes.")
            return False
        # Sem saber o que já foi aplicado (instalação inicial), todas as migrations locais, em ordem: só a do setup
        # deixaria o banco sem as RPCs/ajustes das migrations seguintes.
        pendentes = list(migracoes())
    if not pendentes:
        info("Nenhuma migration pendente: não há nada para colar no SQL Editor.")
        return False
    perigos = perigos_das(pendentes)
    if perigos and not confirmar_destrutivo:
        recusar_destrutivas(perigos)
        return False
    print("  1. Abra o painel do Supabase > SQL Editor > New query.")
    for n, a in enumerate(pendentes, 1):
        print("  2.%d. (%d de %d, nesta ordem) Abra o arquivo %s no seu editor, copie TODO o conteúdo, cole no SQL Editor e clique em Run." % (n, n, len(pendentes), a))
    print("  3. Cada Run deve terminar com 'Success. No rows returned'. Só passe para o arquivo seguinte depois do anterior.")
    print("  4. Volte aqui e rode: python3 setup/etapa_banco.py verificar")
    return True


def cmd_migrar(args):
    config = load_config()
    ref = cfg(config, "supabase", "project_ref") or ref_da_url(cfg(config, "supabase", "url"))
    arquivos = migracoes()
    if not arquivos:
        erro("Não encontrei supabase/migrations/*.sql. Atualize o repositório com 'git pull'.")
        return 1
    if not ref:
        erro("Rode primeiro: python3 setup/etapa_banco.py credenciais")
        return 1
    cli = achar_cli("supabase")
    if not cli:
        aviso("Não encontrei a Supabase CLI nem o npx.")
        instrucoes_sql_editor(None, getattr(args, "confirmar_destrutivo", False))
        return 1
    if args.dry_run:
        titulo("Simulação (nada foi executado)")
        print("  %s link --project-ref %s" % (" ".join(cli), ref))
        print("  %s db push   (confirmação enviada pela entrada padrão)" % " ".join(cli))
        for a in arquivos:
            print("  migration: %s" % a.name)
        return 0

    senha = entrada_externa("SDR_SUPABASE_DB_PASSWORD") or os.environ.get("SUPABASE_DB_PASSWORD")
    if not senha:
        senha = segredo("Senha do banco do Supabase (Project Settings > Database; a que você criou no projeto)", "SDR_SUPABASE_DB_PASSWORD")
    env = dict(os.environ)
    env["SUPABASE_DB_PASSWORD"] = senha
    token = entrada_externa("SDR_SUPABASE_ACCESS_TOKEN") or os.environ.get("SUPABASE_ACCESS_TOKEN")
    if token:
        env["SUPABASE_ACCESS_TOKEN"] = token

    info("Vinculando ao projeto %s ..." % ref)
    cod, saida = rodar(cli + ["link", "--project-ref", ref], env=env, cwd=REPO_ROOT)
    if cod != 0:
        erro("supabase link falhou: %s" % _resumo(saida, senha))
        info("Se a CLI pediu login, rode 'supabase login' no terminal (ou defina SDR_SUPABASE_ACCESS_TOKEN) e tente de novo.")
        instrucoes_sql_editor(None, getattr(args, "confirmar_destrutivo", False))
        return 1
    ok("Projeto vinculado")

    cod, saida = rodar(cli + ["db", "push", "--dry-run"], env=env, cwd=REPO_ROOT)
    conhecidas = pendentes_ou_none(cod, saida)
    # Lista vazia (a CLI disse "up to date") = nada a aplicar; None (incerteza) = fail-closed, todas as locais.
    pend = conhecidas if conhecidas is not None else list(arquivos)
    info("Migrations que serão aplicadas: %s" % (", ".join(a.name for a in pend) or "nenhuma"))
    perigos = perigos_das(pend)
    if perigos and not getattr(args, "confirmar_destrutivo", False):
        recusar_destrutivas(perigos)
        return 1
    if conhecidas == []:
        ok("Banco já está em dia: nenhuma migration pendente")
        salvar_estado({"banco_migrado": False})  # só vira True quando as tabelas respondem (cmd_verificar)
        return cmd_verificar(args)

    # Há migration a aplicar: o schema vai mudar, então o teste guiado e a marca de "banco pronto" anteriores deixam de valer
    # ANTES do push (se ele falhar no meio, o banco fica parcial e nada pode continuar marcado como pronto).
    salvar_estado({"teste_ok": False, "banco_migrado": False})
    info("Aplicando a migration (db push) ...")
    cod, saida = rodar(cli + ["db", "push"], env=env, cwd=REPO_ROOT, entrada="y\n")
    if cod != 0:
        erro("supabase db push falhou: %s" % _resumo(saida, senha))
        instrucoes_sql_editor(conhecidas, getattr(args, "confirmar_destrutivo", False))
        return 1
    ok("Migration aplicada")
    salvar_estado({"banco_migrado": False})  # só vira True quando as tabelas respondem (cmd_verificar)
    return cmd_verificar(args)


def _resumo(saida, segredo_txt=""):
    texto = " ".join(str(saida).split())
    if segredo_txt:
        texto = texto.replace(segredo_txt, "****")
    return texto[:400] or "sem detalhes"


# ------------------------------------------------------------------ verificação

def _tabela_ausente(status, corpo):
    """True só quando o PostgREST diz que a relação não existe: 404, ou corpo de erro com PGRST205/42P01/'does not exist'/
    'Could not find the table'. Qualquer outra falha (401, 403, 408, 429, 5xx, rede) é falha de leitura, não tabela ausente."""
    if status == 404:
        return True
    if status in (401, 403, 408, 429) or status == 0 or status >= 500:
        return False
    if isinstance(corpo, dict):
        if str(corpo.get("code", "")) in ("PGRST205", "42P01"):
            return True
        texto = " ".join(str(corpo.get(k, "")) for k in ("message", "details", "hint"))
    else:
        texto = str(corpo or "")
    texto = texto.lower()
    return "does not exist" in texto or "could not find the table" in texto


def cmd_verificar(args):
    config = load_config()
    if not cfg(config, "supabase", "url") or not cfg(config, "supabase", "service_role_key"):
        erro("Rode primeiro: python3 setup/etapa_banco.py credenciais")
        return 1
    faltando = []
    for tabela in TABELAS:
        st, corpo = supabase_rest(config, "GET", "/%s?select=*&limit=1" % tabela)
        if st == 200:
            ok("Tabela %s" % tabela)
        elif st == 0:
            erro("Sem conexão com o Supabase: %s" % corpo)
            return 1
        elif _tabela_ausente(st, corpo):
            faltando.append(tabela)
            erro("Tabela %s não respondeu (HTTP %s)" % (tabela, st))
        else:
            # 401/403/408/429/5xx ou corpo que não diz "relação inexistente": não sei se a tabela existe. Nada de mexer
            # no estado nem de mandar colar migration (que pode já estar aplicada).
            erro("Não consegui ler a tabela %s (HTTP %s). Nada foi alterado. Confira a chave service_role em config/config.json "
                 "ou tente de novo em instantes: python3 setup/etapa_banco.py verificar" % (tabela, st))
            return 1
    if faltando:
        aviso("Faltam %d tabela(s). A migration provavelmente não foi aplicada." % len(faltando))
        salvar_estado({"banco_migrado": False})  # o banco acusou tabela ausente: o estado antigo (True) não vale mais
        pendentes = _migracoes_das_tabelas(faltando)
        if pendentes:
            instrucoes_sql_editor(pendentes, getattr(args, "confirmar_destrutivo", False))
        else:
            aviso("Não sei qual migration cria essas tabelas e não vou mandar colar todas. Rode "
                  "'python3 setup/etapa_banco.py migrar' com a Supabase CLI para aplicar só as pendentes.")
        return 1
    ausentes, falha_leitura = _ler_rpcs(config)
    if falha_leitura:
        # Não sei o que está no banco: nada de mexer no estado nem de mandar colar migration (pode já estar aplicada).
        erro("Não consegui ler a lista de funções do banco (%s). Nada foi alterado. Tente de novo em instantes: "
             "python3 setup/etapa_banco.py verificar" % falha_leitura)
        return 1
    if ausentes:
        erro("As tabelas existem, mas faltam funções (RPC) no banco: %s. Alguma migration ainda não foi aplicada." % ", ".join(ausentes))
        info("Se você acabou de aplicar a migration, aguarde alguns segundos (o Supabase recarrega o esquema) e rode 'verificar' de novo.")
        pendentes = _migracoes_das_rpcs(ausentes)
        if pendentes:
            instrucoes_sql_editor(pendentes, getattr(args, "confirmar_destrutivo", False))
        else:
            aviso("Não sei qual migration cria essas funções e não vou mandar colar todas. Rode "
                  "'python3 setup/etapa_banco.py migrar' com a Supabase CLI para aplicar só as pendentes.")
        salvar_estado({"banco_migrado": False})  # só depois de confirmar que a RPC de fato está ausente
        return 1
    salvar_estado({"banco_migrado": True})
    ok("Banco pronto: as %d tabelas e as %d funções respondem" % (len(TABELAS), len(RPCS)))
    return 0


def _ler_rpcs(config):
    """(ausentes, falha). Funções esperadas que NÃO aparecem no OpenAPI do PostgREST (GET /rest/v1/); `falha` é o motivo
    quando o OpenAPI não pôde ser lido (nesse caso `ausentes` é vazio e NADA se conclui sobre o banco). Nunca chama a RPC."""
    st, corpo = supabase_rest(config, "GET", "/")
    paths = corpo.get("paths") if isinstance(corpo, dict) else None
    if st != 200 or not isinstance(paths, dict):
        return [], "HTTP %s" % st
    return ["/rpc/" + r for r in RPCS if "/rpc/" + r not in paths], None


def _rpcs_ausentes(config):
    ausentes, falha = _ler_rpcs(config)
    return ["não consegui ler a lista de funções do banco (%s)" % falha] if falha else ausentes


def _migracoes_das_tabelas(faltando):
    """Só as migrations locais que criam alguma das tabelas faltantes (nunca todas)."""
    achadas = []
    for arq in migracoes():
        try:
            sql = arq.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(re.search(r"create\s+table\s+(?:if\s+not\s+exists\s+)?(?:\"?public\"?\.)?\"?%s\"?[\s(]" % re.escape(t), sql, re.I)
               for t in faltando):
            achadas.append(arq)
    return achadas


def _migracoes_das_rpcs(ausentes):
    """Só as migrations locais que criam alguma das funções ausentes (nunca todas)."""
    nomes = [a.rsplit("/", 1)[-1] for a in ausentes]
    achadas = []
    for arq in migracoes():
        try:
            sql = arq.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(re.search(r"function\s+(?:\"?public\"?\.)?\"?%s\"?\s*\(" % re.escape(n), sql, re.I) for n in nomes):
            achadas.append(arq)
    return achadas


# ------------------------------------------------------------------ dono

def _email_dono(args):
    if args.email:
        return args.email.strip().lower()
    dados = ler_json(ENTREVISTA_PATH)
    sugerido = (dados.get("dono") or {}).get("email") if isinstance(dados, dict) else None
    return ask("E-mail do dono (vai ser o login do painel)", sugerido or None, env="SDR_OWNER_EMAIL").strip().lower()


def _senha_dono():
    senha = segredo("Senha do painel (mínimo %d caracteres)" % SENHA_MINIMA, "SDR_OWNER_PASSWORD")
    if len(senha) < SENHA_MINIMA:
        erro("A senha precisa ter pelo menos %d caracteres." % SENHA_MINIMA)
        return None
    if not entrada_externa("SDR_OWNER_PASSWORD"):
        if segredo("Repita a senha") != senha:
            erro("As senhas não são iguais.")
            return None
    return senha


def _auth_headers(config):
    chave = cfg(config, "supabase", "service_role_key", default="")
    return {"apikey": chave, "Authorization": "Bearer " + chave}


def _achar_usuario(config, email):
    base = cfg(config, "supabase", "url").rstrip("/")
    for pagina in range(1, 11):
        st, corpo = http_json("GET", "%s/auth/v1/admin/users?page=%d&per_page=200" % (base, pagina), headers=_auth_headers(config))
        if st != 200 or not isinstance(corpo, dict):
            return None
        usuarios = corpo.get("users") or []
        for u in usuarios:
            if str(u.get("email", "")).lower() == email:
                return u
        if len(usuarios) < 200:
            return None
    return None


def cmd_dono(args):
    config = load_config()
    if not cfg(config, "supabase", "url") or not cfg(config, "supabase", "service_role_key"):
        erro("Rode primeiro: python3 setup/etapa_banco.py credenciais")
        return 1
    email = _email_dono(args)
    if not _EMAIL.match(email):
        erro("E-mail inválido.")
        return 1
    if args.dry_run:
        titulo("Simulação (nada foi criado)")
        print("  criaria (ou conferiria) o usuário %s no Supabase Auth e o liberaria em panel_users (pelo id do usuário)" % email)
        return 0
    base = cfg(config, "supabase", "url").rstrip("/")

    existente = _achar_usuario(config, email)
    user_id = existente.get("id") if existente else None
    # Conta existente nunca é reaproveitada em silêncio: quem já cadastrou o e-mail pode não ser o dono.
    # A senha é exigida (flag/variável, nunca chat) e redefinida pela admin API, provando posse.
    if existente:
        info("Já existe uma conta com o e-mail %s no Supabase Auth. Por segurança, ela só é liberada com uma senha nova que você define agora." % email)
    senha = _senha_dono()
    if not senha:
        return 1
    if existente:
        st, corpo = http_json("PUT", "%s/auth/v1/admin/users/%s" % (base, q(existente["id"])),
                              headers=_auth_headers(config), corpo={"password": senha, "email_confirm": True})
        if st != 200:
            erro("Não consegui redefinir a senha (HTTP %s): %s" % (st, str(corpo)[:200]))
            return 1
        ok("Senha de %s redefinida" % email)
    else:
        st, corpo = http_json("POST", base + "/auth/v1/admin/users", headers=_auth_headers(config),
                              corpo={"email": email, "password": senha, "email_confirm": True})
        if st not in (200, 201) or not isinstance(corpo, dict) or not corpo.get("id"):
            erro("Não consegui criar o usuário (HTTP %s): %s" % (st, str(corpo)[:200]))
            return 1
        user_id = corpo["id"]
        ok("Usuário %s criado no Supabase Auth" % email)

    if not user_id:
        erro("Não consegui descobrir o id do usuário %s no Supabase Auth." % email)
        return 1
    # panel_users é por user_id (o e-mail sozinho não prova que a conta foi confirmada).
    st, corpo = supabase_rest(config, "POST", "/panel_users?on_conflict=user_id", [{"user_id": user_id, "email": email}],
                              extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
    if st not in (200, 201, 204):
        erro("Não consegui liberar %s em panel_users (HTTP %s): %s" % (email, st, str(corpo)[:200]))
        info("Confira se a migration foi aplicada: python3 setup/etapa_banco.py verificar")
        return 1
    ok("%s liberado para entrar no painel" % email)
    # Revoga por CONSULTA, não pelo estado (estado.json pode faltar ou estar corrompido). panel_users não tem coluna de
    # papel: todo user_id diferente do novo dono perde o acesso. Só depois de conferir é que o novo dono é gravado.
    st, corpo = supabase_rest(config, "DELETE", "/panel_users?user_id=neq.%s" % q(user_id))
    if st not in (200, 204):
        erro("Não consegui revogar o acesso dos outros usuários em panel_users (HTTP %s): %s" % (st, str(corpo)[:200]))
        erro("O novo dono (%s) JÁ FOI liberado em panel_users, mas o dono anterior pode ainda entrar no painel. O estado do instalador "
             "não foi atualizado: rode 'dono' de novo para concluir a troca, ou apague as outras linhas de panel_users no Supabase "
             "(Table Editor). Para desfazer, apague a linha de %s." % (email, email))
        return 1
    st2, restantes = supabase_rest(config, "GET", "/panel_users?select=user_id,email&user_id=neq.%s" % q(user_id))
    if st2 != 200 or not isinstance(restantes, list) or restantes:
        erro("Não consegui confirmar que o dono anterior foi removido de panel_users (HTTP %s). O novo dono (%s) JÁ FOI liberado em "
             "panel_users e o estado do instalador não foi atualizado: rode 'dono' de novo." % (st2, email))
        return 1
    saiu = [str(r.get("email") or r.get("user_id")) for r in corpo if isinstance(r, dict)] if isinstance(corpo, list) else []
    if saiu:
        aviso("Perderam o acesso ao painel: %s. A conta deles no Supabase Auth continua existindo; remova-a no painel do Supabase "
              "se quiser." % ", ".join(saiu))
    salvar_estado({"dono_email": email, "dono_user_id": user_id})
    return 0


def cmd_tudo(args):
    """credenciais, migrar e dono em sequência; --dry-run vale para as três e a cadeia para no primeiro código != 0."""
    for etapa in (cmd_credenciais, cmd_migrar, cmd_dono):
        codigo = etapa(args)
        if codigo != 0:
            return codigo
        if args.dry_run and etapa is cmd_credenciais and not cfg(load_config(), "supabase", "project_ref"):
            info("Simulação: as próximas etapas (migrar, dono) dependem das credenciais gravadas; rode sem --dry-run para executá-las.")
            return 0
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def comuns(s):
        s.add_argument("--url", "--supabase-url", dest="url")
        s.add_argument("--anon-key", default="", help="a chave anon é pública; prefira SDR_SUPABASE_ANON_KEY ou a pergunta interativa")
        s.add_argument("--trocar-chaves", action="store_true")
        s.add_argument("--dry-run", action="store_true")
        s.add_argument("--confirmar-destrutivo", action="store_true",
                       help="aplica migration pendente que apaga dados (DROP TABLE/COLUMN, TRUNCATE, DELETE sem WHERE); faça backup antes")
        s.add_argument("--email")
        s.add_argument("--redefinir-senha", action="store_true", help="mantida por compatibilidade: conta existente sempre exige senha nova")

    for nome, fn in (("credenciais", cmd_credenciais), ("migrar", cmd_migrar), ("verificar", cmd_verificar),
                     ("dono", cmd_dono), ("tudo", cmd_tudo)):
        s = sub.add_parser(nome)
        comuns(s)
        s.set_defaults(fn=fn)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
