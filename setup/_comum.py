#!/usr/bin/env python3
"""Funções compartilhadas do instalador do Setup 18 (Agente SDR de Agendamento).

Só biblioteca padrão (PyYAML aparece apenas em nichos.py). Todo script de setup/
importa daqui; nenhum reimplementa leitura de config, chamada HTTP ou cores.

Arquivos do aluno (todos fora do git, ver .gitignore):
  config/config.json  segredos e endpoints usados pelo motor
  web/config.js       URL e chave anon do Supabase para o painel
  data/entrevista.json  respostas da entrevista (Etapa 1)
  data/estado.json    marcadores que não dá para deduzir do banco (URL do painel, teste guiado)
"""
import base64
import getpass
import http.client
import json
import ntpath
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def _forcar_utf8():
    """Console do Windows (cp1252) não imprime emoji nem acento: força UTF-8 sem lançar erro."""
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


_forcar_utf8()

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"
CONFIG_PATH = CONFIG_DIR / "config.json"
CONFIG_EXEMPLO = CONFIG_DIR / "config.exemplo.json"
WEB_DIR = REPO_ROOT / "web"
WEB_CONFIG_JS = WEB_DIR / "config.js"
DATA_DIR = REPO_ROOT / "data"
ENTREVISTA_PATH = DATA_DIR / "entrevista.json"
ESTADO_PATH = DATA_DIR / "estado.json"
LOG_DIR = REPO_ROOT / "logs"
MIGRATIONS_DIR = REPO_ROOT / "supabase" / "migrations"
NICHOS_DIR = REPO_ROOT / "nichos"
SUPORTE_URL = "https://suporte.zxlab.com.br/hub"

E164 = re.compile(r"^\+[1-9][0-9]{7,14}$")


def _timeout_env(nome, default):
    """Lê um timeout (segundos) de variável de ambiente; valor inválido, zero ou negativo cai no default."""
    try:
        valor = int(os.environ.get(nome, ""))
        return valor if valor > 0 else default
    except ValueError:
        return default


# Chamada HTTP curta (REST do Supabase, Evolution). Override: ZX_SDR_HTTP_TIMEOUT.
HTTP_TIMEOUT = _timeout_env("ZX_SDR_HTTP_TIMEOUT", 30)
# Comando externo longo (supabase db push, wrangler deploy). Override: ZX_SDR_CMD_TIMEOUT.
CMD_TIMEOUT = _timeout_env("ZX_SDR_CMD_TIMEOUT", 600)


# ---------------------------------------------------------------- saída colorida

def _cor_ativa():
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(codigo, texto):
    return "\033[%sm%s\033[0m" % (codigo, texto) if _cor_ativa() else texto


def verde(t):
    return _c("32", t)


def amarelo(t):
    return _c("33", t)


def vermelho(t):
    return _c("31", t)


def ambar(t):
    return _c("38;5;172", t)


def cinza(t):
    return _c("90", t)


def negrito(t):
    return _c("1", t)


def ok(msg):
    print("  ✅ %s" % msg)


def aviso(msg):
    print("  ⚠️  %s" % msg)


def erro(msg):
    print("  ❌ %s" % msg)


def info(msg):
    print("  → %s" % msg)


def titulo(msg):
    print("\n%s" % negrito(ambar(msg)))


def sair(codigo, msg=None):
    if msg:
        (erro if codigo else ok)(msg)
    sys.exit(codigo)


def mascarar(valor):
    if not valor:
        return "(vazio)"
    v = str(valor)
    return v[:4] + "…" + v[-4:] if len(v) > 12 else "****"


# ---------------------------------------------------------------- arquivos

def gravar_atomico(path, texto, modo=0o600):
    """Grava texto em path de forma atômica. Segredos ficam com permissão 600."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix or ".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(texto)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(str(path), modo)
    except OSError:
        pass


def ler_json(path, default=None):
    """Lê um JSON; ausente ou inválido devolve default (nunca lança)."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {} if default is None else default


def gravar_json(path, dados, modo=0o600):
    gravar_atomico(path, json.dumps(dados, ensure_ascii=False, indent=2) + "\n", modo)


def _mesclar(base, novo):
    """Mescla `novo` em `base` recursivamente; o que não está em `novo` é preservado."""
    for chave, valor in novo.items():
        if isinstance(valor, dict) and isinstance(base.get(chave), dict):
            _mesclar(base[chave], valor)
        else:
            base[chave] = valor
    return base


CONFIG_PADRAO = {
    "supabase": {"url": "", "service_role_key": ""},
    "evolution": {"url": "http://localhost:8080", "api_key": "", "instance": ""},
    "ai": {"provider": "gemini", "api_key": "", "model": ""},
    "google_calendar": {"apps_script_url": "", "secret": ""},
    "motor": {"poll_seconds": 15, "jobs_every_seconds": 60, "max_msgs_per_minute": 8},
}


def load_config():
    """config/config.json do aluno; ausente ou inválido devolve {}."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        aviso("Não consegui ler %s (JSON inválido). Nada foi alterado nele." % CONFIG_PATH)
        return {}


def save_config(config):
    gravar_json(CONFIG_PATH, config, 0o600)


def _arquivo_invalido(path):
    """True se o arquivo existe, tem conteúdo e não é um JSON válido."""
    try:
        texto = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if not texto.strip():
        return False
    try:
        json.loads(texto)
    except ValueError:
        return True
    return False


def atualizar_config(novo):
    """Mescla `novo` no config.json (partindo do exemplo ou do padrão se ainda não existir).

    Se o arquivo existe mas o JSON está inválido, aborta sem gravar nada: sobrescrever apagaria
    as chaves que o aluno já configurou."""
    if _arquivo_invalido(CONFIG_PATH):
        sair(1, "O arquivo %s está com JSON inválido (vírgula sobrando ou aspas faltando?). "
                "Não gravei nada para não perder suas chaves. Corrija o arquivo (ou apague-o para recomeçar) e rode de novo." % CONFIG_PATH)
    atual = load_config()
    if not atual:
        atual = ler_json(CONFIG_EXEMPLO) if CONFIG_EXEMPLO.exists() else {}
        if not atual:
            atual = json.loads(json.dumps(CONFIG_PADRAO))
    _mesclar(atual, novo)
    save_config(atual)
    return atual


def cfg(config, *caminho, **kw):
    """cfg(config, 'supabase', 'url', default='') -> valor ou default se ausente/vazio."""
    default = kw.get("default")
    atual = config
    for parte in caminho:
        if isinstance(atual, dict) and parte in atual:
            atual = atual[parte]
        else:
            return default
    return default if atual in (None, "") else atual


def load_estado():
    return ler_json(ESTADO_PATH)


def salvar_estado(dados):
    if _arquivo_invalido(ESTADO_PATH):
        # O estado é só um registro do progresso: guarda a cópia ilegível e recomeça dele.
        try:
            os.replace(str(ESTADO_PATH), str(ESTADO_PATH) + ".invalido")
        except OSError:
            pass
        aviso("%s estava ilegível; guardei uma cópia em %s.invalido e recomecei o registro." % (ESTADO_PATH, ESTADO_PATH))
    atual = load_estado()
    atual.update(dados)
    gravar_json(ESTADO_PATH, atual, 0o600)
    return atual


# ---------------------------------------------------------------- entrada do aluno

def sem_terminal():
    return not sys.stdin.isatty()


def entrada_externa(env):
    """Valor passado sem terminal: variável de ambiente `env` ou config/config.json > "entrada" > env (arquivo gitignored)."""
    if not env:
        return ""
    valor = os.environ.get(env)
    if valor and valor.strip():
        return valor.strip()
    entrada = load_config().get("entrada") if CONFIG_PATH.exists() else None
    if isinstance(entrada, dict) and str(entrada.get(env) or "").strip():
        return str(entrada[env]).strip()
    return ""


def _abortar_sem_entrada(pergunta, env):
    como = "passe o valor por flag ou pela variável de ambiente %s" % env if env else "passe o valor por flag ou variável de ambiente"
    sair(2, "%s: entrada interativa indisponível (sem terminal). Rode no seu terminal ou %s "
            "(ou coloque em config/config.json > \"entrada\")." % (pergunta, como))


def ask(pergunta, default=None, env=None):
    """Pergunta ao aluno. Sem terminal ou EOF: usa env/default, senão aborta (nunca repete em loop)."""
    externo = entrada_externa(env)
    if externo:
        return externo
    sufixo = " [%s]" % default if default else ""
    if sem_terminal():
        if default is not None:
            return default
        _abortar_sem_entrada(pergunta, env)
    while True:
        try:
            valor = input("%s%s: " % (pergunta, sufixo))
        except EOFError:
            valor = ""
            if default is None:
                _abortar_sem_entrada(pergunta, env)
        valor = valor.strip()
        if valor:
            return valor
        if default is not None:
            return default
        print("  (campo obrigatório)")


def segredo(pergunta, env=None):
    """Lê um segredo sem eco. Aceita variável de ambiente (`env`) ou config.json > entrada; sem terminal aborta."""
    externo = entrada_externa(env)
    if externo:
        return externo
    if sem_terminal():
        _abortar_sem_entrada(pergunta, env)
    while True:
        try:
            valor = getpass.getpass("%s: " % pergunta).strip()
        except EOFError:
            _abortar_sem_entrada(pergunta, env)
        if valor:
            return valor
        print("  (campo obrigatório)")


def confirmar(pergunta, padrao=False):
    """Sim/não. Sem terminal ou EOF devolve False (nunca confirma sozinho); use as flags --sim dos scripts."""
    if sem_terminal():
        return False
    sufixo = " [S/n]" if padrao else " [s/N]"
    try:
        resp = input("%s%s: " % (pergunta, sufixo)).strip().lower()
    except EOFError:
        return False
    if not resp:
        return padrao
    return resp in ("s", "sim", "y", "yes")


def telefone_valido(valor):
    return bool(E164.match(str(valor or "")))


def normalizar_telefone(valor):
    """Tira espaço, traço e parênteses. Não inventa DDI: sem '+' o número é devolvido como está."""
    return re.sub(r"[\s().-]", "", str(valor or ""))


def slugificar(texto):
    import unicodedata
    base = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-").lower()
    return base[:40] or "meu-negocio"


# ---------------------------------------------------------------- HTTP

class _SemRedirectOutroHost(urllib.request.HTTPRedirectHandler):
    """Nunca segue redirect para outro host: apikey/Authorization não podem ir para um terceiro."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        origem = urllib.parse.urlsplit(req.full_url)
        newurl = urllib.parse.urljoin(req.full_url, newurl)
        destino = urllib.parse.urlsplit(newurl)
        mesmo = (origem.scheme, origem.hostname, origem.port or {"http": 80, "https": 443}.get(origem.scheme)) == \
                (destino.scheme, destino.hostname, destino.port or {"http": 80, "https": 443}.get(destino.scheme))
        if not mesmo:
            return None  # sem nova requisição: o 3xx vira HTTPError e http_json devolve o status
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SemRedirectOutroHost())


def http_json(metodo, url, headers=None, corpo=None, timeout=None):
    """Devolve (status, corpo). Nunca lança por HTTP 4xx/5xx. Status 0 = falha de rede (corpo traz a mensagem)."""
    dados = None
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    if corpo is not None:
        dados = json.dumps(corpo).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=dados, headers=hdrs, method=metodo)
    try:
        with _OPENER.open(req, timeout=timeout or HTTP_TIMEOUT) as resp:
            texto = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        texto = e.read().decode("utf-8", "replace")
        status = e.code
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
        return 0, str(e)
    try:
        return status, json.loads(texto) if texto else None
    except ValueError:
        return status, texto


def supabase_rest(config, metodo, caminho, corpo=None, extra_headers=None, timeout=None):
    """Chama o PostgREST do projeto do aluno com a service_role. `caminho` começa com '/', ex.: '/leads?select=id'."""
    base = str(cfg(config, "supabase", "url", default="")).rstrip("/")
    chave = cfg(config, "supabase", "service_role_key", default="")
    if not base or not chave:
        return 0, "Supabase não configurado em config/config.json (rode a Etapa 2)."
    headers = {"apikey": chave, "Authorization": "Bearer " + chave, "Prefer": "return=representation"}
    if extra_headers:
        headers.update(extra_headers)
    return http_json(metodo, base + "/rest/v1" + caminho, headers=headers, corpo=corpo, timeout=timeout)


def papel_da_chave(chave):
    """Papel de uma chave Supabase: 'anon', 'service_role' ou None (formato não reconhecido).

    Formato novo: prefixo sb_publishable_ (anon) / sb_secret_ (service_role). Formato JWT: lê o campo
    'role' do payload (base64url), sem verificar assinatura: serve só para não trocar uma chave pela outra."""
    chave = (chave or "").strip()
    if chave.startswith("sb_publishable_"):
        return "anon"
    if chave.startswith("sb_secret_"):
        return "service_role"
    partes = chave.split(".")
    if len(partes) != 3:
        return None
    try:
        bruto = partes[1] + "=" * (-len(partes[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(bruto.encode("ascii")).decode("utf-8"))
    except Exception:
        return None
    papel = payload.get("role") if isinstance(payload, dict) else None
    return papel if papel in ("anon", "service_role") else None


def checar_papeis(service, anon):
    """Devolve a mensagem de erro se as chaves não tiverem o papel certo, ou None se estiver tudo certo."""
    if service is not None and papel_da_chave(service) != "service_role":
        return ("A chave que você colou como service_role não é uma chave service_role (formato não reconhecido "
                "ou papel diferente). No Supabase, Project Settings > API, copie o campo 'service_role' (ou 'secret').")
    if anon is not None and papel_da_chave(anon) != "anon":
        return ("A chave que você colou como anon não é uma chave anon/publishable (formato não reconhecido "
                "ou papel diferente). No Supabase, Project Settings > API, copie o campo 'anon' (ou 'publishable'). "
                "Nunca coloque a service_role aqui: ela daria acesso total ao banco a quem abrir o painel.")
    return None


def q(valor):
    """Escapa um valor para query string do PostgREST (telefone E.164 tem '+')."""
    return urllib.parse.quote(str(valor), safe="")


# ---------------------------------------------------------------- comandos externos

def python_cmd():
    """Interpretador que roda este instalador (o mesmo que roda o motor)."""
    return sys.executable or shutil.which("python3") or shutil.which("python") or "python3"


def _eh_windows():
    return os.name == "nt"


# Utilitários do próprio Windows: caminho absoluto em %SystemRoot%\System32, nunca pela busca do PATH/diretório atual
# (o repositório é público e uma pasta com taskkill.bat ou powershell.exe plantado seria executada com a sessão do aluno).
_WIN_SISTEMA = {
    "taskkill": ("System32", "taskkill.exe"),
    "tasklist": ("System32", "tasklist.exe"),
    "schtasks": ("System32", "schtasks.exe"),
    "powershell": ("System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
}


def resolver_exe(nome):
    """Caminho completo do executável. Nunca resolve pelo diretório atual: no Windows, utilitários do sistema vão por
    caminho absoluto em %SystemRoot%\\System32 e o resto só por pastas absolutas do PATH (_which_seguro)."""
    nome = str(nome)
    if "/" in nome or "\\" in nome or os.path.isabs(nome) or ntpath.isabs(nome):
        return nome  # já é um caminho explícito (sys.executable, resultado de achar_cli)
    if _eh_windows():
        base = nome.lower()
        if base.endswith(".exe"):
            base = base[:-4]
        if base in _WIN_SISTEMA:
            raiz = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or "C:\\Windows"
            return ntpath.join(raiz, *_WIN_SISTEMA[base])
        achado = _which_seguro(nome)
        # Não achou: nunca devolver o nome nu (o Windows procuraria no diretório atual); caminho inexistente falha limpo.
        return achado or ntpath.join(os.environ.get("SystemRoot") or "C:\\Windows", "System32", "__nao_encontrado__", nome)
    return _which_seguro(nome) or nome


# Versão fixa da CLI baixada pelo npx quando ela não está instalada (a mesma testada com este instalador). A CLI do
# Supabase roda com a senha do banco e o token da conta no ambiente: nunca baixar "a última publicada".
CLI_VERSOES_NPX = {"supabase": "2.90.0"}


def _which_seguro(nome):
    """shutil.which só nas pastas do PATH (sem o diretório atual, que no Windows vence a busca) e que não fiquem
    dentro do repositório: o repositório é público e pode trazer um supabase.cmd/.exe plantado."""
    pastas = []
    for p in os.environ.get("PATH", "").split(os.pathsep):
        p = p.strip().strip('"')
        if not p or p == "." or not (os.path.isabs(p) or ntpath.isabs(p)):
            continue  # vazio, '.' ou relativo = diretório atual: nunca confiável
        pastas.append(p)
    if not pastas:
        return None
    achado = shutil.which(nome, path=os.pathsep.join(pastas))
    if not achado:
        return None
    if not (os.path.isabs(achado) or ntpath.isabs(achado)):
        return None
    if not os.path.isabs(achado):
        return achado  # caminho de unidade Windows (C:\...) lido em outro SO: não há como resolver contra o repositório
    try:
        real = Path(achado).resolve()
        # Só o repositório é suspeito (o cwd dentro dele também cai aqui). Ancestrais do cwd, como o home de quem
        # roda o instalador de lá, NÃO são suspeitos: uma CLI legítima em ~/.nvm/... precisa ser aceita.
        proibidos = [REPO_ROOT.resolve()]
        cwd = Path.cwd().resolve()
    except OSError:
        return None
    for raiz in proibidos:
        if real == raiz or raiz in real.parents:
            return None
    # Binário plantado direto no cwd (que o PATH aponta por caminho absoluto) segue recusado quando o cwd é o próprio
    # repositório ou está dentro dele; fora dele só o filho direto do cwd é suspeito (não um ~/.nvm/... sob o home).
    if real.parent == cwd:
        return None
    return achado


def achar_cli(nome):
    """Devolve [caminho] se `nome` está no PATH (nunca no cwd/repositório), senão [npx, --yes, nome[@versão fixa]]
    se npx existe, senão None."""
    achado = _which_seguro(nome)
    if achado:
        return [achado]
    npx = _which_seguro("npx")
    if npx:
        versao = CLI_VERSOES_NPX.get(nome)
        return [npx, "--yes", "%s@%s" % (nome, versao) if versao else nome]
    return None


def _resolver_comando(comando):
    """Troca o primeiro item pelo caminho completo (Windows não roda .cmd pelo nome nu)."""
    if isinstance(comando, (list, tuple)) and comando:
        return [resolver_exe(str(comando[0]))] + [str(c) for c in comando[1:]]
    return comando


def rodar(comando, timeout=None, env=None, cwd=None, entrada=None):
    """Executa um comando e devolve (codigo, saida). Nunca lança. Código -1 = não executou/estourou o tempo."""
    try:
        proc = subprocess.run(
            _resolver_comando(comando),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout or CMD_TIMEOUT,
            env=env,
            cwd=str(cwd) if cwd else None,
            input=entrada,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return -1, "o comando passou do tempo limite (%ss)" % (timeout or CMD_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, str(exc)
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


# ---------------------------------------------------------------- etapas

ETAPAS = [
    ("1", "Entrevista e nicho"),
    ("2", "Banco de dados e usuário dono"),
    ("3", "Painel e página pública"),
    ("4", "WhatsApp (Evolution) e IA"),
    ("5", "Motor como serviço"),
    ("6", "Teste guiado"),
    ("7", "Liberar produção"),
]


__all__ = [n for n in dir() if not n.startswith("__")]
