#!/usr/bin/env python3
"""Etapa 5: instala o motor do SDR como serviço que liga sozinho com o seu computador.

Uso:
  python3 setup/servico.py [--dry-run] install     cria e liga o serviço
  python3 setup/servico.py [--dry-run] uninstall   desliga e remove o serviço
  python3 setup/servico.py [--dry-run] start|stop|restart|status
  python3 setup/servico.py logs [--linhas 50]      mostra o fim do log do motor

Sistemas: macOS (launchd), Linux (systemd --user) e Windows (Agendador de Tarefas).
Com --dry-run nada é criado nem executado: o script só mostra o que faria.
"""
import argparse
import os
import platform
import re
import shutil
import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _comum import (  # noqa: E402
    CONFIG_PATH, DATA_DIR, LOG_DIR, REPO_ROOT, _timeout_env, aviso, cfg, erro, gravar_atomico, gravar_json, ler_json, info, load_config, ok, python_cmd,
    rodar, salvar_estado, titulo,
)

NOME = "setup18-sdr-agendamento"
LABEL_MAC = "com.setup18.sdr-agendamento"
UNIT_LINUX = "setup18-sdr-agendamento.service"
TAREFA_WIN = "Setup18-SDR-Agendamento"
LOG_FILE = LOG_DIR / "motor.log"
LOCK_FILE = DATA_DIR / "motor.lock"
# Identidade do motor gravada pelo instalador quando ele sobe (Windows): PID + hora de criação + pasta da instalação.
LOCK_ID_FILE = DATA_DIR / "motor.lock.json"
# Quanto esperar o motor aparecer depois de instalar (segundos). Override: ZX_SDR_SERVICE_WAIT.
ESPERA_SUBIR = _timeout_env("ZX_SDR_SERVICE_WAIT", 30)


def sistema():
    return platform.system()


# ------------------------------------------------------------------ conteúdos (funções puras)

def plist_mac(python, repo, log):
    return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>%(label)s</string>
  <key>ProgramArguments</key>
  <array>
    <string>%(python)s</string>
    <string>-m</string>
    <string>motor</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key><string>%(repo)s</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>%(log)s</string>
  <key>StandardErrorPath</key><string>%(log)s</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
""" % {"label": LABEL_MAC, "python": escape(str(python)), "repo": escape(str(repo)), "log": escape(str(log))}


def unit_linux(python, repo, log):
    return """[Unit]
Description=Setup 18 - Agente SDR de Agendamento (motor)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%(repo)s
ExecStart="%(python)s" -m motor run
Restart=always
RestartSec=15
Environment=PYTHONUNBUFFERED=1
StandardOutput=append:%(log)s
StandardError=append:%(log)s

[Install]
WantedBy=default.target
""" % {"python": str(python).replace("\\", "\\\\").replace('"', '\\"'), "repo": repo, "log": log}


def xml_tarefa_windows(python, repo, log):
    """Definição da tarefa do Agendador: sem limite de execução (PT0S), reinício em falha e início no logon."""
    argumentos = '/c ""%s" -m motor run >> "%s" 2>&1"' % (python, log)
    return """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Setup 18 - Agente SDR de Agendamento (motor)</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>cmd.exe</Command>
      <Arguments>%(args)s</Arguments>
      <WorkingDirectory>%(repo)s</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
""" % {"args": escape(argumentos), "repo": escape(str(repo))}


def motor_subiu(desde, lock=None, espera=None, dormir=time.sleep):
    """True se o motor DESTA instalação está rodando segundo data/motor.lock. Espera até `espera` segundos.

    A prova é a identidade do PID gravado (identidade_do_pid == 'motor'), não o mtime da trava: um motor que já estava de
    pé (a unit/tarefa não o reinicia) não regrava a trava, e mesmo assim está rodando. Identidade 'desconhecido' (sem como
    provar) só vale se a trava foi gravada depois de `desde` (epoch), como antes. 'outro' e 'morto' nunca valem."""
    lock = Path(lock or LOCK_FILE)
    limite = time.monotonic() + (ESPERA_SUBIR if espera is None else espera)
    while True:
        try:
            texto = lock.read_text(encoding="utf-8", errors="replace").strip()
            if texto.isdigit() and int(texto) > 0:
                quem = identidade_do_pid(int(texto))
                if quem == "motor" or (quem == "desconhecido" and lock.stat().st_mtime >= desde - 1):
                    return True
        except OSError:
            pass
        if time.monotonic() >= limite:
            return False
        dormir(2)


def ler_pid_motor(lock=None):
    """PID gravado em data/motor.lock pelo motor, ou None se não há trava legível."""
    try:
        texto = Path(lock or LOCK_FILE).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return int(texto) if texto.isdigit() and int(texto) > 0 else None


def _linha_de_comando(pid):
    """Linha de comando do processo `pid` (minúscula), ou None se não deu para ler."""
    if sistema() == "Windows":
        codigo, saida = rodar(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                               "(Get-CimInstance Win32_Process -Filter \"ProcessId=%d\").CommandLine" % pid], timeout=30)
    else:
        codigo, saida = rodar(["ps", "-o", "command=", "-p", str(pid)], timeout=30)
    return saida.lower() if codigo == 0 and saida.strip() else None


_TOKEN = re.compile(r"\"[^\"]*\"|'[^']*'|\S+")
_PYTHON_EXE = re.compile(r"^\s*[\"']?(?:.*[\\/])?pythonw?[\d.]*(?:\.exe)?[\"']?(?=\s|$)(.*)$", re.S)


_OPCOES_COM_VALOR = ("-x", "-w", "--check-hash-based-pycs")


def _tokens(texto):
    return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t for t in _TOKEN.findall(texto)]


def _e_linha_do_motor(linha):
    """True só se o ARGV é o entrypoint do motor: o executável é python e o programa é o módulo `motor` (-m motor /
    -m motor.cli) ou o script motor/cli.py | motor/__main__.py, seguido do subcomando `run` como argumento separado. Um
    processo que só MENCIONA 'motor run' dentro de um argumento (luna-review --contexto "... motor run ...") não é motor."""
    l = (linha or "").strip().lower()
    if "get-ciminstance" in l:
        return False
    m = _PYTHON_EXE.match(l)
    if not m:
        return False
    argv = _tokens(m.group(1))
    i = 0
    while i < len(argv) and argv[i].startswith("-") and argv[i] != "-m":
        # -X e -W (a linha vem em minúsculas, então também -x/-w) recebem o valor como argumento separado
        i += 2 if argv[i] in _OPCOES_COM_VALOR else 1
    if i >= len(argv):
        return False
    if argv[i] == "-m":
        programa_ok = i + 1 < len(argv) and argv[i + 1] in ("motor", "motor.cli", "motor.__main__")
        resto = argv[i + 2:]
    else:
        alvo = argv[i].replace("\\", "/")
        programa_ok = re.search(r"(^|/)motor/(cli(\.py)?|__main__\.py)$", alvo) is not None
        resto = argv[i + 1:]
    return programa_ok and bool(resto) and resto[0] == "run"


def _norm(caminho):
    return str(caminho).lower().replace("\\", "/").rstrip("/")


def _raizes_do_repo():
    raizes = {_norm(REPO_ROOT)}
    try:
        raizes.add(_norm(Path(REPO_ROOT).resolve()))
    except OSError:
        pass
    return {r for r in raizes if r}


def _cwd_do_processo(pid):
    """Diretório de trabalho do processo, ou None quando o sistema não informa (Windows, lsof ausente...)."""
    if sistema() == "Linux":
        try:
            return os.readlink("/proc/%d/cwd" % pid)
        except OSError:
            pass
    if sistema() in ("Darwin", "Linux"):
        codigo, saida = rodar(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], timeout=30)
        if codigo == 0:
            for l in saida.splitlines():
                if l.startswith("n") and len(l) > 1:
                    return l[1:]
    return None


def _linha_do_pai_windows(pid):
    """Linha de comando do processo pai (no Windows o cmd.exe da tarefa carrega o caminho do log desta instalação)."""
    codigo, saida = rodar(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                           "$p=(Get-CimInstance Win32_Process -Filter \"ProcessId=%d\").ParentProcessId; "
                           "(Get-CimInstance Win32_Process -Filter \"ProcessId=$p\").CommandLine" % pid], timeout=30)
    return saida.lower() if codigo == 0 and saida.strip() else None


def _criacao_do_processo_windows(pid):
    """Hora de criação do processo (ISO, UTC) via Get-CimInstance, ou None se não deu para ler."""
    codigo, saida = rodar(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                           "(Get-CimInstance Win32_Process -Filter \"ProcessId=%d\").CreationDate"
                           ".ToUniversalTime().ToString('o')" % pid], timeout=30)
    texto = saida.strip() if codigo == 0 else ""
    return texto or None


def registrar_identidade_motor(pid=None):
    """Windows: guarda PID + hora de criação + REPO_ROOT do motor em data/motor.lock.json logo que ele sobe, para provar
    o dono depois sem depender do cmd.exe pai (que o `schtasks /End` encerra). Devolve True se gravou."""
    if sistema() != "Windows":
        return False
    pid = pid or ler_pid_motor()
    if pid is None:
        return False
    criacao = _criacao_do_processo_windows(pid)
    if not criacao:
        return False
    try:
        gravar_json(LOCK_ID_FILE, {"pid": pid, "criacao": criacao, "repo": str(REPO_ROOT)}, 0o600)
    except OSError:
        return False
    return True


def _dono_pela_identidade_gravada(pid):
    """True: o PID é o motor gravado por esta instalação (mesmo PID e mesma hora de criação). False: comprovadamente
    outro processo (hora de criação diferente = PID reaproveitado, ou pasta de outra instalação). None: sem registro
    ou sem como ler a hora de criação (fail-closed)."""
    reg = ler_json(LOCK_ID_FILE)
    if not isinstance(reg, dict) or reg.get("pid") != pid or not reg.get("criacao") or not reg.get("repo"):
        return None
    if _norm(reg["repo"]) not in _raizes_do_repo():
        return False
    atual = _criacao_do_processo_windows(pid)
    if atual is None:
        return None
    return atual == reg["criacao"]


def _dentro_do_repo(texto, raizes):
    """True se `texto` (linha de comando) tem um token de caminho IGUAL a uma raiz ou dentro dela (raiz + '/'), com
    fronteira de diretório nos dois lados: 'c:/users/ana/setup18-2/logs' NÃO é de 'c:/users/ana/setup18'."""
    t = _norm(texto)
    for r in raizes:
        if re.search(r"(?<![^\s\"'=,;(])" + re.escape(r) + r"(?=$|/|[\s\"'=,;)])", t):
            return True
    return False


def _dono_do_processo(pid, linha):
    """True se o processo do motor (`linha`) pertence a ESTA instalação (REPO_ROOT); False se comprovadamente é de outra
    pasta; None se o sistema não informou (fail-closed: quem chama não mata nem dá por parado)."""
    raizes = _raizes_do_repo()
    if _dentro_do_repo(linha, raizes):
        return True
    cwd = _cwd_do_processo(pid)
    if cwd is not None:
        c = _norm(cwd)
        return any(c == r or c.startswith(r + "/") for r in raizes)
    if sistema() == "Windows":
        prova = _dono_pela_identidade_gravada(pid)
        if prova is not None:
            return prova
        pai = _linha_do_pai_windows(pid)
        if pai and _dentro_do_repo(pai, raizes):
            return True
    return None


def _classificar_motor(pid, linha):
    """'motor' (é o motor desta instalação), 'outro' (motor de outra pasta) ou 'desconhecido' (sem como provar de quem é)."""
    dono = _dono_do_processo(pid, linha)
    return "motor" if dono is True else ("outro" if dono is False else "desconhecido")


def identidade_do_pid(pid):
    """'motor' (o PID é o motor), 'outro' (existe e é comprovadamente outro programa), 'morto' (comprovadamente
    sem processo) ou 'desconhecido' (a consulta falhou, estourou o tempo ou a linha de comando não foi legível).

    Desconhecido nunca vira 'outro' nem 'morto': o motor pode estar vivo e respondendo leads."""
    if sistema() == "Windows":
        codigo, saida = rodar(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"], timeout=30)
        if codigo != 0:
            return "desconhecido"
        if ('"%d"' % pid) not in saida:
            return "morto"
        if "python" not in saida.lower():
            return "outro"
    else:
        codigo, saida = rodar(["ps", "-p", str(pid), "-o", "pid="], timeout=30)
        if codigo == 1 and not saida.strip():
            return "morto"  # ps sai 1 sem saída quando o PID não existe
        if codigo != 0 or not saida.strip():
            return "desconhecido"
    linha = _linha_de_comando(pid)
    if linha is None:
        return "desconhecido"
    if not _e_linha_do_motor(linha):
        return "outro"
    return _classificar_motor(pid, linha)


def pid_do_motor_vivo(pid):
    """True se o PID é o motor OU se não deu para saber (desconhecido conta como vivo). Nunca acusa nem mata
    outro processo que reaproveitou o PID, mas também nunca dá o motor por parado sem prova."""
    return identidade_do_pid(pid) in ("motor", "desconhecido")


def _varrer_completo():
    """([PIDs do motor DESTA instalação], [PIDs sem prova de dono], [(PID, pasta) de motor de OUTRA pasta], ok).
    ok=False: a varredura falhou."""
    if sistema() == "Windows":
        codigo, saida = rodar(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                               "Get-CimInstance Win32_Process | ForEach-Object { '{0} {1}' -f $_.ProcessId, $_.CommandLine }"],
                              timeout=60)
    else:
        codigo, saida = rodar(["ps", "-ax", "-o", "pid=,command="], timeout=60)
    if codigo != 0:
        return [], [], [], False
    proprios, incertos, outros = [], [], []
    for linha in saida.splitlines():
        partes = linha.strip().split(None, 1)
        if len(partes) == 2 and partes[0].isdigit() and int(partes[0]) != os.getpid() and _e_linha_do_motor(partes[1]):
            pid = int(partes[0])
            quem = _classificar_motor(pid, partes[1].lower())
            if quem == "motor":
                proprios.append(pid)
            elif quem == "desconhecido":
                incertos.append(pid)
            else:
                outros.append((pid, _cwd_do_processo(pid) or "pasta não identificada"))
    return proprios, incertos, outros, True


def _varrer_detalhe():
    """([PIDs do motor DESTA instalação], [PIDs de motor sem prova de dono], ok). ok=False: a varredura falhou."""
    proprios, incertos, _, feito = _varrer_completo()
    return proprios, incertos, feito


def varrer_motores():
    """([PIDs do motor desta instalação], ok). ok=False quando a varredura falhou ou sobrou motor sem prova de dono
    (não vale como 'nenhum')."""
    proprios, incertos, feito = _varrer_detalhe()
    return proprios, feito and not incertos


def _encerrar_pid(ex, pid, matar_arvore, dormir):
    if sistema() == "Windows":
        ex.cmd(["taskkill", "/T", "/F", "/PID", str(pid)], tolerar=True)
        return
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(5):
        dormir(1)
        if pid not in varrer_motores()[0]:
            return
    try:
        os.kill(pid, signal.SIGKILL)
    except (OSError, AttributeError):
        pass


def parar_sem_trava(ex, matar_arvore, dormir):
    """Sem trava legível não existe 'parado' sem prova: procura o processo do motor pela linha de comando, encerra e reconfere.
    0 = nenhum motor vivo comprovado; 1 = ainda vivo ou a varredura falhou."""
    pids, incertos, feito = _varrer_detalhe()
    como = "pelo Gerenciador de Tarefas (procure processos python.exe com 'motor run')" if sistema() == "Windows" \
        else "pelo Monitor de Atividade (procure processos python com 'motor run')"
    if not feito:
        erro("Não há trava legível em %s e não consegui varrer os processos para confirmar que o motor parou. Ele pode "
             "continuar respondendo seus leads. Confira %s e encerre-o, depois rode o comando de novo." % (LOCK_FILE, como))
        return 1
    for pid in pids:
        info("Trava ausente: encerrando o motor encontrado (PID %d)." % pid)
        _encerrar_pid(ex, pid, matar_arvore, dormir)
    if pids:
        restantes, incertos, feito = _varrer_detalhe()
        if not feito or restantes:
            erro("O motor (PID %s) continua rodando ou não consegui confirmar que parou. Encerre-o %s e rode o comando de novo."
                 % (", ".join(map(str, restantes)) or "?", como))
            return 1
    if incertos:
        erro("Achei processo(s) 'motor run' (PID %s) e não consegui provar que são desta instalação (%s): o sistema não "
             "informou a pasta de onde rodam. Não encerrei nada. Se forem o seu SDR, encerre-os %s e rode o comando de novo."
             % (", ".join(map(str, incertos)), REPO_ROOT, como))
        return 1
    return 0


def garantir_motor_parado(ex, matar_arvore=False, espera=10, dormir=None):
    """Depois de parar o serviço, confere pelo PID de data/motor.lock que o motor morreu. 0 = parado.

    No Windows o Agendador só encerra o cmd.exe da tarefa; o python.exe filho continuaria respondendo leads,
    por isso `matar_arvore` roda `taskkill /T /F` no PID da trava. Motor ainda vivo ou PID não identificado = erro (código 1).
    """
    dormir = dormir or time.sleep  # resolvido na chamada (o default fixo ignorava o time.sleep trocado)
    if ex.dry_run:
        if matar_arvore:
            print("  [simulação] taskkill /T /F /PID <pid de %s>" % LOCK_FILE)
        return 0
    pid = ler_pid_motor()
    if pid is None:
        return parar_sem_trava(ex, matar_arvore, dormir)
    quem = identidade_do_pid(pid)
    if quem == "desconhecido":
        erro("Não consegui identificar o processo de PID %d (o do motor, segundo %s), nem provar que ele roda nesta instalação. O motor pode continuar respondendo "
             "seus leads. Não removi a trava e não encerrei nada. Encerre o processo python pelo Gerenciador de Tarefas/"
             "Monitor de Atividade (PID %d) e rode o comando de novo." % (pid, LOCK_FILE, pid))
        return 1
    if quem == "outro":
        # Trava obsoleta: o PID foi reaproveitado por outro programa (comprovado). Não mata nada; só remove a trava.
        try:
            Path(LOCK_FILE).unlink()
        except OSError:
            pass
        aviso("A trava %s apontava para o PID %d, que agora é outro programa. Removi a trava e não encerrei nada." % (LOCK_FILE, pid))
        return parar_sem_trava(ex, matar_arvore, dormir)
    if quem != "motor":
        return 0
    if matar_arvore:
        ex.cmd(["taskkill", "/T", "/F", "/PID", str(pid)], tolerar=True)
    limite = time.monotonic() + espera
    while pid_do_motor_vivo(pid):
        if time.monotonic() >= limite:
            erro("O serviço foi parado, mas o motor (PID %d) continua rodando (ou não deu para confirmar que parou) e "
                 "responderia seus leads. Encerre-o pelo Gerenciador de Tarefas/Monitor de Atividade (processo python) "
                 "e rode o comando de novo." % pid)
            return 1
        dormir(1)
    return 0


def encerrar_dono_da_trava(ex):
    """Windows: ANTES do `schtasks /End` (que só encerra o cmd.exe da tarefa), derruba a árvore do motor cujo PID está na
    trava, desde que a identidade seja comprovada. Sem prova nada é encerrado (garantir_motor_parado segue fail-closed)."""
    if ex.dry_run:
        print("  [simulação] taskkill /T /F /PID <pid de %s> (se a identidade for comprovada)" % LOCK_FILE)
        return
    pid = ler_pid_motor()
    if pid is not None and identidade_do_pid(pid) == "motor":
        ex.cmd(["taskkill", "/T", "/F", "/PID", str(pid)], tolerar=True)


def _tarefa_win(ex, acao, tolerar=True):
    """Windows: `schtasks /Change /DISABLE|/ENABLE`. Com a tarefa desabilitada o RestartOnFailure do Agendador não
    religa o motor depois de um stop. Devolve o código do comando."""
    flag = "/DISABLE" if acao == "disable" else "/ENABLE"
    return ex.cmd(["schtasks", "/Change", "/TN", TAREFA_WIN, flag], tolerar=tolerar)[0]


def _tarefa_desabilitada(saida):
    """True se a saída do `schtasks /Query /V /FO LIST` mostra a tarefa desabilitada (inglês ou português)."""
    for linha in (saida or "").splitlines():
        chave, _, valor = linha.partition(":")
        k, v = chave.strip().lower(), valor.strip().lower()
        if k in ("scheduled task state", "estado da tarefa agendada") and v.startswith(("disabled", "desabilitad", "desativad")):
            return True
    return False


def _parar_windows(ex):
    """Windows: DISABLE → /End → derruba a árvore do dono da trava → confere que o motor morreu. Devolve (disable_ok,
    resultado_de_garantir_motor_parado)."""
    disable_ok = _tarefa_win(ex, "disable") == 0
    ex.cmd(["schtasks", "/End", "/TN", TAREFA_WIN], tolerar=True)
    encerrar_dono_da_trava(ex)
    return disable_ok, garantir_motor_parado(ex, matar_arvore=True)


def _registrar_apos_subir(inicio):
    """Windows: espera o motor assumir a trava e grava a identidade dele (só o instalador pode, motor/ não é tocado).
    Devolve (motor_subiu, identidade_registrada). A mensagem de cada falha é separada: motor parado não é problema de registro."""
    if not motor_subiu(inicio):
        return False, False
    if not registrar_identidade_motor():
        aviso("O motor subiu, mas não consegui registrar a identidade dele (%s). Parar/remover o serviço ainda funciona, mas "
              "pode pedir que você encerre o processo python manualmente." % LOCK_ID_FILE)
        return True, False
    return True, True


def _subir_windows(inicio):
    """Depois do schtasks /Run: 0 se o motor apareceu rodando, 1 (com erro claro) se não."""
    subiu, _ = _registrar_apos_subir(inicio)
    if not subiu:
        erro("O motor não subiu: a tarefa foi disparada, mas o motor não apareceu rodando. Veja logs/motor.log "
             "(python3 setup/servico.py logs) ou rode python3 -m motor check para achar o problema.")
        return 1
    return 0


MSG_VARREDURA_FALHOU = ("Não consegui conferir se já existe outro motor rodando neste computador. Encerre o motor antigo "
                        "(python setup/servico.py stop na pasta antiga) e rode de novo.")


def _checar_motor_alheio(ex):
    """Antes de alterar qualquer coisa: recusa (1) se há processo 'motor run' de OUTRA pasta ou sem prova de dono (fail-closed).
    Dois motores no mesmo Supabase/Evolution respondem o mesmo lead duas vezes. 0 = pode seguir."""
    if ex.dry_run:
        print("  [simulação] varreria os processos 'motor run': de outra pasta, sem prova de dono ou varredura que falha = recusa a instalação")
        return 0
    _, incertos, outros, feito = _varrer_completo()
    if not feito:
        erro(MSG_VARREDURA_FALHOU)
        return 1
    if not outros and not incertos:
        return 0
    linhas = ["PID %d (pasta: %s)" % (pid, pasta) for pid, pasta in outros]
    linhas += ["PID %d (não consegui provar de qual pasta ele roda)" % pid for pid in incertos]
    erro("Há outro motor do SDR rodando: %s. Dois motores no mesmo banco e no mesmo WhatsApp respondem o mesmo lead duas vezes. "
         "Nada foi instalado nem alterado. Pare aquele motor primeiro (rode 'python3 setup/servico.py stop' na pasta antiga ou "
         "encerre o processo python pelo Monitor de Atividade/Gerenciador de Tarefas) e rode a instalação de novo." % "; ".join(linhas))
    return 1


def _encerrar_motores_proprios(ex, dormir=None):
    """Depois de parar o serviço, encerra qualquer 'motor run' comprovadamente DESTA instalação (o aluno pode ter deixado um
    'python -m motor run' aberto) e confirma que morreu antes de subir o serviço. 0 = nenhum sobrou."""
    dormir = dormir or time.sleep
    if ex.dry_run:
        print("  [simulação] encerraria os processos 'motor run' desta instalação que não são o serviço")
        return 0
    pids, incertos, outros, feito = _varrer_completo()
    if not feito:
        erro(MSG_VARREDURA_FALHOU)
        return 1
    if outros or incertos:
        erro("Apareceu outro motor rodando (PID %s) enquanto eu preparava a instalação. Não subi o serviço; encerre-o e rode de novo."
             % ", ".join(str(x) for x in [p for p, _ in outros] + incertos))
        return 1
    for pid in pids:
        info("Encerrando o motor desta instalação que estava rodando fora do serviço (PID %d)." % pid)
        _encerrar_pid(ex, pid, True, dormir)
    if pids:
        restantes, incertos, outros, feito = _varrer_completo()
        if not feito or restantes or incertos or outros:
            erro("O motor (PID %s) continua rodando. Encerre-o pelo Monitor de Atividade/Gerenciador de Tarefas e rode a "
                 "instalação de novo." % (", ".join(str(x) for x in restantes + incertos + [p for p, _ in outros]) or "?"))
            return 1
    return 0


def pid_do_servico():
    """(pid, legivel): PID que o launchd/systemd informa para o serviço. legivel=False quando o comando falhou ou não
    deu para interpretar (aí quem chama cai na checagem pela trava). pid=None com legivel=True: serviço sem processo."""
    so = sistema()
    if so == "Darwin":
        codigo, saida = rodar(["launchctl", "print", "gui/%s/%s" % (_uid(), LABEL_MAC)], timeout=30)
        if codigo != 0:
            return None, False
        m = re.search(r"^\s*pid = (\d+)\s*$", saida, re.M)
        return (int(m.group(1)) if m else None), True
    if so == "Linux":
        codigo, saida = rodar(["systemctl", "--user", "show", "-p", "MainPID", UNIT_LINUX], timeout=30)
        m = re.search(r"^MainPID=(\d+)\s*$", saida or "", re.M)
        if codigo != 0 or not m:
            return None, False
        return (int(m.group(1)) or None), True
    return None, False


def servico_assumiu_o_motor(dormir=time.sleep, espera=None):
    """Darwin/Linux: True se o PID da trava é o PID do serviço (senão o motor que responde leads é outro, avulso). Leitura do
    PID do serviço impossível = aviso e segue com a checagem pela trava (não bloqueia). Espera até `espera` s."""
    limite = time.monotonic() + (ESPERA_SUBIR if espera is None else espera)
    while True:
        servico_pid, legivel = pid_do_servico()
        if not legivel:
            aviso("Não consegui ler o PID do serviço no sistema; confirmei o motor só pela trava (%s)." % LOCK_FILE)
            return True
        trava = ler_pid_motor()
        if servico_pid is not None and trava == servico_pid:
            return True
        if time.monotonic() >= limite:
            erro("O serviço não assumiu o motor; há outro motor rodando com PID %s (o serviço está com PID %s). Encerre aquele "
                 "processo (Monitor de Atividade/terminal onde rodou 'python -m motor run') e rode a instalação de novo."
                 % (trava if trava is not None else "?", servico_pid if servico_pid is not None else "nenhum"))
            return False
        dormir(2)


def caminho_plist():
    return Path.home() / "Library" / "LaunchAgents" / (LABEL_MAC + ".plist")


def caminho_unit():
    return Path.home() / ".config" / "systemd" / "user" / UNIT_LINUX


# ------------------------------------------------------------------ execução

class Executor:
    """Roda comandos ou, com dry-run, só mostra."""

    def __init__(self, dry_run):
        self.dry_run = dry_run

    def cmd(self, comando, tolerar=False):
        texto = " ".join(str(c) for c in comando)
        if self.dry_run:
            print("  [simulação] %s" % texto)
            return 0, ""
        codigo, saida = rodar(comando, timeout=60)
        if codigo != 0 and not tolerar:
            erro("Falhou: %s\n      %s" % (texto, " ".join(saida.split())[:300]))
        return codigo, saida

    def arquivo(self, caminho, conteudo):
        if self.dry_run:
            print("  [simulação] gravaria %s:" % caminho)
            for linha in conteudo.splitlines():
                print("      " + linha)
            return
        gravar_atomico(caminho, conteudo, 0o644)
        ok("Arquivo criado: %s" % caminho)

    def remover(self, caminho):
        if self.dry_run:
            print("  [simulação] removeria %s" % caminho)
            return
        try:
            Path(caminho).unlink()
            ok("Arquivo removido: %s" % caminho)
        except FileNotFoundError:
            pass
        except OSError as exc:
            aviso("Não consegui remover %s: %s" % (caminho, exc))


def _uid():
    return str(os.getuid()) if hasattr(os, "getuid") else ""


def preparar_instalacao(dry_run):
    """Confere o que o motor precisa para ligar. Em simulação só avisa."""
    problemas = []
    if not CONFIG_PATH.exists():
        problemas.append("config/config.json não existe (faça as Etapas 2 e 4)")
    else:
        config = load_config()
        for caminho in (("supabase", "url"), ("supabase", "service_role_key"), ("evolution", "api_key"), ("evolution", "instance")):
            if not cfg(config, *caminho):
                problemas.append("config.json sem %s" % ".".join(caminho))
    if not (REPO_ROOT / "motor").is_dir():
        problemas.append("a pasta motor/ não está no repositório (git pull)")
    if problemas:
        for p in problemas:
            (aviso if dry_run else erro)(p)
        if not dry_run:
            return False
    if not dry_run:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    return True


def instalar(ex):
    inicio = time.time()
    so = sistema()
    python = python_cmd()
    if not preparar_instalacao(ex.dry_run):
        return 1
    titulo("Instalando o serviço (%s)" % so)
    if so in ("Darwin", "Linux", "Windows") and _checar_motor_alheio(ex):
        return 1
    if so == "Darwin":
        ex.arquivo(caminho_plist(), plist_mac(python, REPO_ROOT, LOG_FILE))
        ex.cmd(["launchctl", "bootout", "gui/%s/%s" % (_uid(), LABEL_MAC)], tolerar=True)
        if _encerrar_motores_proprios(ex):
            return 1
        # `stop` grava `launchctl disable`, que persiste: sem reabilitar, o bootstrap não carrega o serviço.
        ex.cmd(["launchctl", "enable", "gui/%s/%s" % (_uid(), LABEL_MAC)], tolerar=True)
        codigo, _ = ex.cmd(["launchctl", "bootstrap", "gui/%s" % _uid(), str(caminho_plist())])
        if codigo != 0:
            return 1
        ex.cmd(["launchctl", "kickstart", "-k", "gui/%s/%s" % (_uid(), LABEL_MAC)], tolerar=True)
    elif so == "Linux":
        ex.arquivo(caminho_unit(), unit_linux(python, REPO_ROOT, LOG_FILE))
        # `enable --now` não reinicia unit que já está ativa: o motor antigo seguiria rodando com a definição velha.
        ja_ativa = ex.cmd(["systemctl", "--user", "is-active", UNIT_LINUX], tolerar=True)[0] == 0
        if ja_ativa:
            ex.cmd(["systemctl", "--user", "stop", UNIT_LINUX], tolerar=True)
        if _encerrar_motores_proprios(ex):
            return 1
        comandos = [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", UNIT_LINUX]]
        if ja_ativa:
            comandos.append(["systemctl", "--user", "restart", UNIT_LINUX])
        for comando in comandos:
            if ex.cmd(comando)[0] != 0:
                return 1
        info("Para o serviço continuar ligado sem você logado: sudo loginctl enable-linger $USER")
    elif so == "Windows":
        xml = xml_tarefa_windows(python, REPO_ROOT, LOG_FILE)
        arquivo_xml = LOG_DIR / "tarefa-windows.xml"
        if ex.dry_run:
            print("  [simulação] gravaria %s (UTF-16):" % arquivo_xml)
            for linha in xml.splitlines():
                print("      " + linha)
        else:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            arquivo_xml.write_text(xml, encoding="utf-16")
        codigo, _ = ex.cmd(["schtasks", "/Create", "/TN", TAREFA_WIN, "/XML", str(arquivo_xml), "/F"])
        if codigo != 0:
            return 1
        # Reinstalação sobre tarefa desabilitada (stop anterior): a tarefa precisa estar habilitada para o /Run valer.
        _tarefa_win(ex, "enable")
        # Com a tarefa em IgnoreNew, /Run é ignorado se o motor já roda: encerrar a instância (/End) ANTES de derrubar a
        # árvore dele (identidade comprovada), para a definição nova entrar em vigor.
        ex.cmd(["schtasks", "/End", "/TN", TAREFA_WIN], tolerar=True)
        encerrar_dono_da_trava(ex)
        if _encerrar_motores_proprios(ex):
            return 1
        ex.cmd(["schtasks", "/Run", "/TN", TAREFA_WIN], tolerar=True)
        if not ex.dry_run:
            _registrar_apos_subir(inicio)
    else:
        erro("Sistema não suportado: %s. Rode manualmente: python -m motor run" % so)
        return 1
    if not ex.dry_run:
        info("Conferindo se o motor subiu (até %ds)..." % ESPERA_SUBIR)
        if not motor_subiu(inicio):
            salvar_estado({"servico_instalado": False})
            erro("O serviço foi criado, mas o motor não apareceu rodando. Veja o log: python3 setup/servico.py logs "
                 "(ou rode python3 -m motor check para achar o problema).")
            return 1
        if so in ("Darwin", "Linux") and not servico_assumiu_o_motor():
            salvar_estado({"servico_instalado": False})
            return 1
        salvar_estado({"servico_instalado": True})
        ok("Serviço instalado e motor rodando. Log em %s" % LOG_FILE)
    return 0


def _conferir_removido(ex, so, tentativa_ok):
    """Depois de remover: lista o que AINDA existe (vazio = sumiu de verdade). `tentativa_ok` diz se os comandos de
    remoção terminaram com sucesso; falha de comando só é aceita quando a conferência prova que o serviço não existe."""
    restos = []
    if so == "Darwin":
        if ex.cmd(["launchctl", "list", LABEL_MAC], tolerar=True)[0] == 0:
            restos.append("o serviço %s ainda está carregado no launchd" % LABEL_MAC)
        if caminho_plist().exists():
            restos.append("o arquivo %s ainda existe" % caminho_plist())
    elif so == "Linux":
        if ex.cmd(["systemctl", "--user", "is-enabled", UNIT_LINUX], tolerar=True)[0] == 0:
            restos.append("a unit %s ainda está habilitada" % UNIT_LINUX)
        if caminho_unit().exists():
            restos.append("o arquivo %s ainda existe" % caminho_unit())
    elif so == "Windows":
        if ex.cmd(["schtasks", "/Query", "/TN", TAREFA_WIN], tolerar=True)[0] == 0:
            restos.append("a tarefa %s ainda existe no Agendador de Tarefas" % TAREFA_WIN)
    return restos


def _instrucao_manual(so):
    if so == "Darwin":
        return "No Terminal: launchctl bootout gui/$(id -u)/%s && rm %s" % (LABEL_MAC, caminho_plist())
    if so == "Linux":
        return "No terminal: systemctl --user disable --now %s && rm %s && systemctl --user daemon-reload" % (UNIT_LINUX, caminho_unit())
    return "Num PowerShell como administrador: schtasks /Delete /TN %s /F  (ou apague a tarefa no Agendador de Tarefas)" % TAREFA_WIN


def remover(ex):
    so = sistema()
    titulo("Removendo o serviço (%s)" % so)
    falhou = False
    if so == "Darwin":
        falhou |= ex.cmd(["launchctl", "bootout", "gui/%s/%s" % (_uid(), LABEL_MAC)], tolerar=True)[0] != 0
        if garantir_motor_parado(ex):
            return 1
        ex.remover(caminho_plist())
    elif so == "Linux":
        falhou |= ex.cmd(["systemctl", "--user", "disable", "--now", UNIT_LINUX], tolerar=True)[0] != 0
        if garantir_motor_parado(ex):
            return 1
        ex.remover(caminho_unit())
        ex.cmd(["systemctl", "--user", "daemon-reload"], tolerar=True)
    elif so == "Windows":
        _, parado = _parar_windows(ex)
        if parado:
            return 1
        falhou |= ex.cmd(["schtasks", "/Delete", "/TN", TAREFA_WIN, "/F"], tolerar=True)[0] != 0
    else:
        erro("Sistema não suportado: %s" % so)
        return 1
    if ex.dry_run:
        return 0
    # Não basta o comando "ter rodado": o serviço só conta como removido se a conferência confirma que sumiu.
    restos = _conferir_removido(ex, so, not falhou)
    if restos:
        erro("O serviço NÃO foi removido: %s. Ele pode voltar a ligar sozinho e responder seus leads." % "; ".join(restos))
        info(_instrucao_manual(so))
        return 1
    if falhou:
        info("O comando de remoção reportou falha, mas conferi e o serviço não existe mais.")
    ok("Serviço removido e conferido")
    salvar_estado({"servico_instalado": False})
    return 0


def _subir_unico(ex, so):
    """start/restart: mesma guarda do install. Recusa (1) motor de OUTRA pasta ou sem prova de dono; depois do serviço já
    parado, encerra o 'motor run' desta instalação que não é o serviço. 0 = pode subir."""
    return 1 if _checar_motor_alheio(ex) else 0


def _controlar_mac(ex, acao):
    alvo = "gui/%s/%s" % (_uid(), LABEL_MAC)
    dominio = "gui/%s" % _uid()
    if acao == "stop":
        codigo, saida = ex.cmd(["launchctl", "bootout", alvo], tolerar=True)
        # Descarregar não basta: o launchd recarrega ~/Library/LaunchAgents no próximo login. `disable` persiste o stop.
        disable_ok = ex.cmd(["launchctl", "disable", alvo], tolerar=True)[0] == 0
        if ex.dry_run:
            return 0
        # bootout pode falhar por já estar parado: o que vale é conferir se o serviço ainda está carregado.
        ainda, _ = ex.cmd(["launchctl", "print", alvo], tolerar=True)
        if ainda == 0:
            erro("Não consegui parar o serviço (launchctl bootout código %s): %s" % (codigo, " ".join(saida.split())[:300]))
            return 1
        parado = garantir_motor_parado(ex)
        if not disable_ok:
            erro("O serviço parou, mas não consegui desativá-lo (launchctl disable): ele pode voltar no próximo login e "
                 "responder seus leads. Rode no Terminal: launchctl disable %s" % alvo)
            return 1
        return parado
    if acao in ("start", "restart"):
        if _subir_unico(ex, "Darwin"):
            return 1
        ex.cmd(["launchctl", "enable", alvo], tolerar=True)
        ex.cmd(["launchctl", "bootout", alvo], tolerar=True)
        if _encerrar_motores_proprios(ex):
            return 1
        if ex.cmd(["launchctl", "bootstrap", dominio, str(caminho_plist())])[0] != 0:
            return 1
        return ex.cmd(["launchctl", "kickstart", "-k", alvo])[0]
    return ex.cmd(["launchctl", "print", alvo], tolerar=True)[0]


def _controlar_linux(ex, acao):
    if acao == "status":
        codigo = ex.cmd(["systemctl", "--user", "status", UNIT_LINUX, "--no-pager"], tolerar=True)[0]
        if not ex.dry_run and caminho_unit().exists() and ex.cmd(["systemctl", "--user", "is-enabled", UNIT_LINUX], tolerar=True)[0] != 0:
            aviso("Serviço parado (desativado pelo stop). Rode: python3 setup/servico.py start")
        return codigo
    if acao == "stop":
        # disable --now: para E impede o motor de voltar no próximo login/reboot.
        codigo = ex.cmd(["systemctl", "--user", "disable", "--now", UNIT_LINUX])[0]
        if codigo == 0:
            return garantir_motor_parado(ex)
        return codigo
    if _subir_unico(ex, "Linux"):
        return 1
    ex.cmd(["systemctl", "--user", "stop", UNIT_LINUX], tolerar=True)
    if _encerrar_motores_proprios(ex):
        return 1
    return ex.cmd(["systemctl", "--user", "enable", "--now", UNIT_LINUX])[0]


def controlar(ex, acao):
    so = sistema()
    if so == "Darwin":
        return _controlar_mac(ex, acao)
    if so == "Linux":
        return _controlar_linux(ex, acao)
    if so == "Windows":
        if acao == "start":
            if _subir_unico(ex, so):
                return 1
            inicio = time.time()
            _tarefa_win(ex, "enable")
            ex.cmd(["schtasks", "/End", "/TN", TAREFA_WIN], tolerar=True)
            encerrar_dono_da_trava(ex)
            if _encerrar_motores_proprios(ex):
                return 1
            codigo = ex.cmd(["schtasks", "/Run", "/TN", TAREFA_WIN])[0]
            if codigo == 0 and not ex.dry_run:
                return _subir_windows(inicio)
            return codigo
        if acao == "stop":
            # Desabilita a tarefa para o RestartOnFailure não religar o motor; /End falha se já estava parada e o que
            # vale é o motor ter morrido de fato.
            disable_ok, parado = _parar_windows(ex)
            if not disable_ok and not ex.dry_run:
                erro("Não consegui desativar a tarefa %s (schtasks /Change /DISABLE): o Agendador pode religar o motor "
                     "sozinho em cerca de 1 minuto. Rode num PowerShell como administrador: schtasks /Change /TN %s /DISABLE"
                     % (TAREFA_WIN, TAREFA_WIN))
                return 1
            return parado
        if acao == "restart":
            if _subir_unico(ex, so):
                return 1
            _, parado = _parar_windows(ex)
            if parado:
                return 1
            _tarefa_win(ex, "enable")
            if _encerrar_motores_proprios(ex):
                return 1
            inicio = time.time()
            codigo = ex.cmd(["schtasks", "/Run", "/TN", TAREFA_WIN])[0]
            if codigo == 0 and not ex.dry_run:
                return _subir_windows(inicio)
            return codigo
        codigo, saida = ex.cmd(["schtasks", "/Query", "/TN", TAREFA_WIN, "/V", "/FO", "LIST"], tolerar=True)
        if codigo == 0 and _tarefa_desabilitada(saida):
            aviso("Serviço parado (desativado pelo stop). Rode: python3 setup/servico.py start")
        return codigo
    erro("Sistema não suportado: %s" % so)
    return 1


def status(ex):
    """Mostra se o serviço está carregado e devolve 0 se sim."""
    if ex.dry_run:
        return controlar(ex, "status")
    so = sistema()
    if so == "Darwin":
        codigo, saida = rodar(["launchctl", "print", "gui/%s/%s" % (_uid(), LABEL_MAC)], timeout=30)
        rodando = codigo == 0 and "state = running" in saida
        if codigo != 0 and caminho_plist().exists():
            aviso("Serviço parado (desativado pelo stop). Rode: python3 setup/servico.py start")
            return 1
        (ok if rodando else aviso)("Serviço %s" % ("rodando" if rodando else ("carregado, mas parado" if codigo == 0 else "não instalado")))
        return 0 if codigo == 0 else 1
    codigo = controlar(ex, "status")
    return 0 if codigo == 0 else 1


def logs(linhas):
    if not LOG_FILE.exists():
        aviso("Ainda não há log em %s (o serviço nunca rodou)." % LOG_FILE)
        return 1
    try:
        conteudo = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        erro("Não consegui ler o log: %s" % exc)
        return 1
    print("\n".join(conteudo[-max(1, linhas):]))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--dry-run", action="store_true", help="só mostra o que faria")
    sub = p.add_subparsers(dest="cmd", required=True)
    for nome in ("install", "uninstall", "start", "stop", "restart", "status"):
        sub.add_parser(nome)
    s = sub.add_parser("logs")
    s.add_argument("--linhas", type=int, default=50)
    args = p.parse_args(argv)

    ex = Executor(args.dry_run)
    if args.cmd == "install":
        return instalar(ex)
    if args.cmd == "uninstall":
        return remover(ex)
    if args.cmd == "status":
        return status(ex)
    if args.cmd == "logs":
        return logs(args.linhas)
    return controlar(ex, args.cmd)


if __name__ == "__main__":
    sys.exit(main())
