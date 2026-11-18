---
name: sdr-instalar
description: "Instalador guiado do Setup 18, o Agente SDR de Agendamento. Use quando o aluno disser INICIAR SETUP, instalar o SDR, continuar a instalação ou mostrar o status. Conduz as Etapas 1 a 7: entrevista, banco, painel, WhatsApp e IA, serviço, teste e produção."
model: claude-sonnet-5-5
effort: medium
---

# /sdr-instalar — Instalação do Agente SDR de Agendamento

Você conduz uma instalação com o aluno. Faça uma etapa por vez, confirme o resultado de cada comando antes de seguir e nunca peça para o aluno colar segredos no chat.

Em Windows, troque `python3` por `python` em todos os comandos.

## Regras que valem sempre

- Chaves e senhas (service_role, chave da Evolution, chave de IA, senha do painel) nunca vão para o chat, nunca para argumento de comando e nunca são repetidas na sua resposta.
- O Claude Code roda os comandos SEM terminal interativo: um script que pergunta algo aborta com a mensagem "entrada interativa indisponível". Por isso todo valor tem caminho não interativo, e você escolhe com o aluno um destes:
  1. **Terminal do aluno (preferido para segredos):** peça para ele abrir um terminal próprio, fora do chat, na pasta do projeto, e rodar o comando da etapa. Ele digita o segredo sem eco e volta ao chat.
  2. **Arquivo local:** o aluno edita `config/config.json` (já ignorado pelo Git, só no computador dele) e adiciona um bloco `"entrada"` com as variáveis da etapa, por exemplo `"entrada": {"SDR_SUPABASE_SERVICE_ROLE_KEY": "...", "SDR_SUPABASE_DB_PASSWORD": "...", "SDR_OWNER_PASSWORD": "...", "SDR_AI_API_KEY": "...", "SDR_EVOLUTION_API_KEY": "..."}`. Os scripts leem dali, ou da variável de ambiente de mesmo nome. Depois da instalação, o aluno apaga o bloco `entrada`.
  3. **Flags para o que não é segredo:** `--supabase-url`, `--anon-key`, `--email`, `--instance`, `--projeto`, `--telefone` (ou as variáveis `SDR_SUPABASE_URL`, `SDR_SUPABASE_ANON_KEY`, `SDR_OWNER_EMAIL`, `SDR_EVOLUTION_URL`, `SDR_EVOLUTION_INSTANCE`, `SDR_PAGES_PROJETO`, `SDR_TEST_PHONE`).
  Peça ao aluno o valor apenas para ele colocar no arquivo ou no terminal dele, nunca para colar no chat.
- Os arquivos `config/config.json`, `web/config.js`, `data/` e `logs/` ficam só no computador do aluno e não vão para o Git.
- Fale em "seu computador", nunca em um sistema operacional específico. Tudo roda via Claude Code em Mac, Windows e Linux.
- O motor do agente roda no computador do aluno. Se o computador desligar, o agente para de responder até ligar de novo. Diga isso uma vez, na Etapa 5.
- Suporte é sempre o da ZX LAB: https://suporte.zxlab.com.br/hub
- Um comando falhou? Leia a mensagem, explique em uma frase o que ela significa e rode o comando de novo depois de corrigir. Não pule a etapa.

## Estado

Comece sempre por aqui e mostre a etapa atual:

```bash
python3 setup/estado.py status
```

Se o aluno quer só continuar, rode `python3 setup/estado.py proxima` e comece pela etapa devolvida.

Em Windows, se `python3` não existir, use `python` (ou `py`). Os scripts usam UTF-8 e resolvem sozinhos os comandos `.cmd` (supabase, wrangler, npx).

Antes da Etapa 1 rode a conferência do computador:

```bash
python3 setup/check_prerequisites.py
```

Se faltar PyYAML, instale com `python3 -m pip install pyyaml` e rode a conferência de novo. Falta Git, Node ou Supabase CLI: recomende instalar a Supabase CLI (sem ela o instalador usa `npx --yes supabase@2.90.0`, versão fixa e testada, nunca a última); explique o link de instalação que o script mostrar e peça para abrir um novo terminal depois.

## Etapa 1 — Entrevista e nicho

Objetivo: gravar `data/entrevista.json`.

1. Mostre as opções de nicho e peça a escolha:

   ```bash
   python3 setup/nichos.py listar
   ```

   Opções: imobiliaria, veiculos, seguros, saude-clinica, servicos-consultoria. O aluno pode ver um nicho inteiro com `python3 setup/nichos.py mostrar <nicho>`.
2. Gere o modelo e preencha com as respostas do aluno, uma pergunta de cada vez:

   ```bash
   python3 setup/etapa_config.py modelo
   ```

   Perguntas, nesta ordem:
   - Nome do negócio e a oferta em uma frase.
   - Nome do agente (a "pessoa" que atende no WhatsApp) e se quer algum ajuste de tom.
   - WhatsApp (sempre em formato internacional com `+`, por exemplo `+5511999990000`; nunca sem o `+`) e e-mail do dono.
   - Quem atende as calls: nome, WhatsApp, e-mail (opcional), peso (1 a 10, usado na distribuição) e os dias e horários de atendimento de cada closer. Pode ser uma pessoa só.
   - Duração da call, intervalo entre calls, antecedência mínima, quantos dias à frente mostrar e fuso horário.
   - Se quer a página pública de agendamento e qual endereço usar (padrão `agendar`).
   - Números de teste (o seu e de quem vai ajudar a testar). Só eles recebem resposta até a Etapa 7.
   - Provedor de IA: gemini, openai ou anthropic.
   - Informações do negócio que o agente deve saber (endereço, serviços, regras). O que o aluno não souber agora fica como [PREENCHER] e é completado no painel.
   O arquivo gerado por `modelo` traz valores de exemplo (empresa, oferta, "Closer 1", os números `+5511999990000`/`+5511999990001` e `voce@exemplo.com`). O `validar` e o `banco` recusam esses valores e dizem qual campo trocar: nunca avance com eles, porque os números de exemplo podem ser de terceiros.
3. Valide:

   ```bash
   python3 setup/etapa_config.py validar
   ```

   Se listar problemas, corrija o JSON e valide de novo. Mostre o resumo ao aluno e peça confirmação antes de seguir.

## Etapa 2 — Banco de dados e usuário dono

O aluno precisa de um projeto Supabase (gratuito) criado em supabase.com. Peça para ele ter à mão: Project URL, chave `anon`, chave `service_role` e a senha do banco que ele definiu ao criar o projeto.

```bash
python3 setup/etapa_banco.py tudo
```

**Use um projeto Supabase NOVO e dedicado a este setup:** a migration recusa rodar (com mensagem clara: "este projeto já tem tabela X que não é do Setup 18; use um projeto Supabase novo") se o projeto já tiver qualquer tabela do setup (`leads`, `messages`, `appointments`, `outbox`, `panel_users` etc.) sem ser deste setup. Não tente contornar: peça para o aluno criar outro projeto. Reexecutar no mesmo projeto do setup é seguro. O instalador testa a conexão ANTES de gravar as chaves; se der 401, nada é gravado e o aluno roda de novo com `--trocar-chaves`.

**Telefone sempre com DDI:** dono, closers, números de teste e a página pública usam o formato internacional com `+` (ex.: `+5511999990000`). Número sem DDI é recusado, nunca "completado" por inferência. O instalador também confere o papel de cada chave: a `anon`/`publishable` vai para o painel público e a `service_role`/`secret` nunca; chaves trocadas ou de formato desconhecido são recusadas.

**Atenção antes de migrar (versão com dados):** o banco precisa ter TODOS os telefones de leads com DDI (`+5511999990000`). Se existir lead com telefone sem DDI, a migration aborta listando os ids; corrija esses telefones com `+55...` e rode de novo. **A chave `anon`/`publishable` é a única que vai para o painel e a página pública, nunca a `service_role`:** o painel recusa a configuração se a chave colada for `service_role` ou `sb_secret_`.

**Migration destrutiva:** antes do `db push`, o instalador lista as migrations pendentes e lê o SQL delas. Se alguma tiver `DROP TABLE`, `DROP SCHEMA`, `ALTER TABLE ... DROP [COLUMN]`, `TRUNCATE` ou `DELETE FROM` sem `WHERE` (ou com `WHERE` que vale para todas as linhas: `true`, `1=1`, `'a'='a'`, `x=x`, `not false`), ele sai com erro e não aplica nada. A leitura inclui comentários e textos do SQL; `DELETE FROM` só conta como protegido se o `WHERE` estiver no mesmo nível (um `WHERE` fora de uma CTE não vale); arquivo que não dá para ler também bloqueia; quando a CLI cita só algumas pendentes, o guard lê todas as migrations a partir da mais antiga citada (um `DROP` só num comentário também para: é falso positivo e a flag libera); `DROP CONSTRAINT` não conta; se a CLI diz que o banco está em dia, nada é bloqueado nem aplicado. Oriente o aluno a fazer backup, conferir o SQL e só então rodar de novo com `--confirmar-destrutivo`. A instalação inicial (só a migration do setup) não precisa da flag.

**Troca de dono:** `dono --email novo@...` apaga de `panel_users` TODAS as linhas com id diferente do novo (consulta no banco, não depende do estado local), mostra quem perdeu o acesso e só então grava o novo dono. Se a revogação falhar, sai com erro e não grava nada: rode de novo. A conta antiga no Supabase Auth continua e pode ser apagada no painel do Supabase.

O comando guarda as credenciais, aplica a migration (`supabase link` + `db push`), confere as 13 tabelas e cria o usuário dono pela API de administração do Auth do Supabase (e-mail já confirmado), liberando o acesso ao painel pelo id desse usuário. Ele usa o e-mail e a senha do painel (mínimo de 10 caracteres). Se já existir uma conta com esse e-mail, ela NUNCA é reaproveitada em silêncio (outra pessoa poderia tê-la cadastrado antes): o comando exige uma senha nova (`SDR_OWNER_PASSWORD` ou terminal próprio, nunca no chat), redefine a senha pela API de administração e só então libera o painel. Sem a senha, sai com erro e não libera nada. Peça ao aluno uma senha nova; `--redefinir-senha` continua aceita por compatibilidade, mas já não é necessária.

Como o Claude Code não tem terminal interativo, use o caminho 1 ou 2 das regras acima para os segredos (`SDR_SUPABASE_SERVICE_ROLE_KEY`, `SDR_SUPABASE_DB_PASSWORD`, `SDR_OWNER_PASSWORD`) e as flags para o resto:

```bash
python3 setup/etapa_banco.py tudo --supabase-url https://<codigo>.supabase.co --anon-key <chave-anon> --email <email-do-dono>
```

Se a Supabase CLI pedir login, oriente `supabase login` no terminal. Se o `db push` não funcionar, o script mostra o plano B (colar no SQL Editor do Supabase), só com as migrations pendentes (na instalação inicial sem lista de aplicadas, TODAS as migrations da pasta, em ordem, uma Run por arquivo). Arquivo com SQL destrutivo só aparece com `--confirmar-destrutivo`; num banco que já tem dados e sem lista de aplicadas, o plano B não manda colar nada e pede para usar a Supabase CLI. Nunca peça para colar arquivo já aplicado. Depois rode (o `verificar` confere as 13 tabelas E as sete funções `book_slot`, `reschedule_slot`, `public_slots`, `public_book`, `set_appointment_status`, `request_whatsapp_command` e `whatsapp_status` no OpenAPI do banco; faltou alguma, sai com erro, não marca o banco como pronto e o plano B lista só a migration que cria a função ausente, nunca todas; se o OpenAPI não puder ser lido (rede, HTTP 5xx), sai com erro sem mexer no estado e sem plano B, é só repetir; se acabou de aplicar, espere alguns segundos e repita):

```bash
python3 setup/etapa_banco.py verificar
python3 setup/etapa_banco.py dono
```

## Etapa 3 — Painel e página pública

1. Grave a configuração do agente no banco (nome, tom, perguntas, objeções, mensagens, closers, horários):

   ```bash
   python3 setup/etapa_config.py banco
   ```

   Rodar de novo avisa antes de sobrescrever o que o aluno já editou no painel (o provedor de IA escolhido na Etapa 4 é preservado). Se o banco tiver closer ativo que não está na entrevista (por exemplo, troca de nome), o comando para com erro e não grava closers; para desativar os que sobraram use `--desativar-fora`: `python3 setup/etapa_config.py banco --sobrescrever --desativar-fora`. Para regravar só uma parte: `--partes agenda --sobrescrever`. Regravar não reativa closer que o dono desativou nem reabre a página pública que ele desligou no painel, e a troca dos horários de uma closer grava as faixas novas antes de apagar as antigas (se a rede cair no meio, o horário antigo continua valendo; se só o apagamento das antigas falhar, as faixas novas são desfeitas e nada muda; é só rodar de novo).
2. Para o aluno ver o painel sem banco, antes de publicar, existe a demonstração:

   ```bash
   python3 setup/etapa_web.py demo
   python3 setup/etapa_web.py local
   ```

   Depois volte ao modo real com `python3 setup/etapa_web.py config`.
3. Publique no Cloudflare Pages. O aluno precisa de uma conta Cloudflare (gratuita) e do login do Wrangler (`wrangler login`):

   ```bash
   python3 setup/etapa_web.py config
   python3 setup/etapa_web.py deploy
   ```

   O deploy mostra o link da página pública já com o endereço escolhido (formato `<url>/agendar/<endereço>`); é esse link completo que o aluno compartilha. Sem o endereço no final a página não abre. O teste local (`etapa_web.py local`) também serve `/agendar/<endereço>`.

   **Limites da página pública (anti-abuso):** como qualquer pessoa pode abri-la, ela tem dois limites que o aluno ajusta no painel, em Disponibilidade: no máximo 20 tentativas por hora no total, 3 por dia por endereço de IP e 3 por dia por telefone (`public_max_per_phone_day`), valores padrão. Acima disso a página mostra uma mensagem pedindo para tentar mais tarde (`limite_atingido`). O nome digitado é limpo (sem links, até 60 caracteres) antes de entrar nas mensagens.

   **Risco aceito:** a página avisa "já existe um horário marcado" quando o telefone informado já tem agendamento. Isso revela apenas que aquele número tem horário marcado e nada além; mantivemos de propósito para evitar agendamento duplicado. Se o aluno não aceita esse risco, ele pode desativar a página pública no painel.

   **Pedido de não receber mensagens (`PARAR`, opt-out):** se o lead já tem call marcada, a call é mantida (agendado/confirmado), o repasse ao closer e os alertas ao dono continuam; só o envio AO LEAD é bloqueado. O agente avisa o dono que o lead pediu para não receber mensagens mas tem call marcada. Explique isso ao aluno.

   **Risco aceito (sem OTP):** a página pública não confirma por código que quem digita o telefone é o dono dele; os limites (por IP, por telefone e globais) reduzem, mas não eliminam, o abuso. O limite por IP é aproximado (usa o último valor de `x-forwarded-for` do proxy do Supabase; sem o cabeçalho o IP não entra no limite e só valem os limites por telefone e global), então Cloudflare Turnstile ou regras de WAF na borda são a proteção real: recomende configurar se a página for divulgada abertamente.
4. Peça para o aluno configurar a URL no Supabase (Authentication > URL Configuration), como o script mostra, e entrar no painel com o e-mail e a senha do dono.

## Etapa 4 — WhatsApp (Evolution API) e IA

O aluno precisa de uma Evolution API rodando (a URL e a chave dela). A instância é o número de WhatsApp do agente: use um número dedicado ao atendimento.

```bash
python3 setup/etapa_config.py evolution --criar
python3 setup/etapa_config.py ia
```

O primeiro comando cria a instância se não existir e salva o QR code em `data/qrcode-whatsapp.png` (só o seu usuário lê o arquivo, e ele é apagado quando o WhatsApp conecta); o aluno lê em WhatsApp > Aparelhos conectados. O segundo usa a chave do provedor de IA escolhido (sem `--provider`, vale o provedor que já está no banco, depois o do `config.json`; a entrevista só vale na primeira configuração): grava o provedor no banco primeiro e relê; só depois guarda provedor e chave em `config/config.json`. Se o banco recusar, nada muda no config e o comando sai com erro (rode de novo). Sem Supabase configurado ainda, grava só no config e avisa que o banco não foi atualizado, com o comando para rodar depois. As chaves (`SDR_EVOLUTION_API_KEY`, `SDR_AI_API_KEY`) seguem o caminho 1 ou 2 das regras; URL e instância podem ir por `--url` e `--instance`.

**Depois da instalação não é preciso voltar aqui para reconectar:** se o WhatsApp cair ou o aluno quiser trocar de número, ele usa o painel, menu **WhatsApp** (Reconectar e ler o QR Code, ou Trocar número). O painel só faz o pedido e o motor, que roda no computador do aluno, executa; por isso o motor precisa estar ligado (Etapa 5). A tabela e a função dessa tela vêm da migration `20261118000002_whatsapp_conexao.sql`, aplicada por `etapa_banco.py migrar`/`tudo` e conferida por `verificar` (tabela `whatsapp_connection` e função `request_whatsapp_command`).

Opcional, para agendar também no Google Calendar: o aluno publica o Apps Script do repositório (se existir) e coloca `google_calendar.apps_script_url` e `google_calendar.secret` no `config/config.json`. Sem isso o agente funciona normalmente, só sem criar evento no Google.

Confirme tudo com:

```bash
python3 -m motor check
```

## Etapa 5 — Motor como serviço

```bash
python3 setup/servico.py install
python3 setup/servico.py status
```

O script só confirma "serviço instalado" depois de ver o motor rodando (trava `data/motor.lock`); se disser que o motor não subiu, rode `python3 setup/servico.py logs` e resolva antes de seguir. Explique: o motor liga junto com o computador e reinicia sozinho se cair. Para acompanhar: `python3 setup/servico.py logs`. Para parar: `python3 setup/servico.py stop`. Para remover: `python3 setup/servico.py uninstall`. Se `stop` ou `uninstall` disser que não conseguiu identificar o processo do motor (PID mostrado), o motor pode continuar respondendo leads (também vale quando o sistema não informa de qual pasta o processo roda: o instalador só encerra o motor que provar ser desta instalação): a trava não é removida e o comando sai com erro. Peça ao aluno para encerrar esse PID pelo Gerenciador de Tarefas/Monitor de Atividade e rodar o comando de novo. Sem trava legível, `stop`/`uninstall` procuram o processo python do motor (`motor run`) e o encerram; se a varredura falhar, o comando sai com erro e pede conferir pelo Gerenciador de Tarefas/Monitor de Atividade. Só confirme "motor parado" quando o comando terminar sem erro. No Windows o instalador grava, quando o motor sobe (`install`, `start`, `restart`), a identidade dele em `data/motor.lock.json` (PID, hora de criação e pasta da instalação); `stop`, `restart` e `uninstall` usam esse registro para provar o dono e derrubar a árvore do motor (`taskkill /T /F`) antes de encerrar a tarefa, mesmo com o `cmd.exe` pai já fechado. Sem o registro (motor iniciado por fora) vale o comportamento seguro de sempre: sem prova de dono nada é encerrado.

## Etapa 6 — Teste guiado

```bash
python3 setup/teste_guiado.py --whatsapp
```

Confere o modo teste, o motor, uma conversa simulada, os horários da página pública e, com `--whatsapp`, uma conversa real: o aluno manda "oi" de um número de teste e o agente responde (só conta mensagem enviada depois do início do passo). O teste só é aprovado se todos os passos rodarem na mesma execução; `--passos` parcial não libera a produção. O teste grava `teste_ok=false` ao começar e só volta a `true` se todos os passos passarem; interromper (Ctrl+C) deixa a produção bloqueada. No modo teste, o resumo para o closer sai com o prefixo `[TESTE]` e só chega ao WhatsApp do closer se o número dele estiver nos números de teste; senão vai para o telefone do dono (o closer é equipe interna). Trocar o banco (`etapa_config.py banco`), a Evolution ou a IA (`etapa_config.py ia`) depois do teste grava `teste_ok=false` ANTES da primeira escrita (mesmo que o comando falhe no meio) e avisa: rode o teste guiado de novo antes de liberar a produção. Trocar a URL ou as chaves do Supabase (`etapa_banco.py credenciais`) também zera `teste_ok` e `banco_migrado`: o projeto novo precisa de migração e de teste próprios. A Supabase CLI e o wrangler só são usados se estiverem numa pasta absoluta do PATH fora do repositório; um executável na pasta atual é ignorado. Se o aluno não vai usar a página pública, rode com `--sem-pagina-publica`; caso contrário a página precisa estar ativa. Peça para ele conversar de verdade: responder as perguntas, escolher um horário e conferir se o agendamento apareceu no painel.

No passo `--whatsapp` só vale uma mensagem de saída do próprio agente (registrada em `messages` como `out`/`agente`) posterior à mensagem do aluno; avisos do dono ou saídas antigas não contam. Perguntas de qualificação de mesmo prefixo longo recebem chaves únicas (`_2`, `_3`...), e `key` precisa ser texto.

Também peça para testar três situações e conferir o comportamento: uma objeção de preço, um pedido de "PARAR" (o agente encerra e não manda mais nada) e o botão Assumir do painel (o agente se cala e o dono responde).

## Etapa 7 — Liberar produção

Antes de liberar, o aluno completa no painel os campos [PREENCHER] da base de conhecimento. O agente só afirma o que está escrito ali.

```bash
python3 setup/liberar_producao.py
```

O script recusa liberar se o teste não passou ou se ainda há [PREENCHER] em qualquer campo do agente (conhecimento, objeções, regras de desqualificação, mensagens), e diz onde estão. Depois de liberar, o agente responde qualquer pessoa que chamar. Para voltar ao modo teste: `python3 setup/liberar_producao.py --voltar-ao-teste`.

Termine mostrando `python3 setup/estado.py status` e explicando o dia a dia: use `/sdr-agendamento` para operar.
