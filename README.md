# Setup 18 — Agente SDR de Agendamento

Um agente de IA que atende no WhatsApp do seu negócio, qualifica o contato, trata as objeções mais comuns e **agenda a conversa com o seu closer**, com lembretes para reduzir o não comparecimento. Você acompanha tudo em um painel e pode assumir qualquer conversa quando quiser.

Funciona para qualquer negócio que vende em uma conversa: imobiliária, veículos, seguros, saúde e clínicas, serviços e consultoria. Cada um já vem com um modelo de nicho pronto para você ajustar.

## O que você recebe

- **Agente no WhatsApp:** responde na hora, com o tom do seu negócio, usando só o que você escreveu na base de conhecimento.
- **Qualificação e objeções:** perguntas do nicho, regras para desqualificar com educação e respostas para as objeções mais comuns.
- **Agenda inteligente:** horários livres por closer, duração, intervalo e antecedência mínima. Distribuição por rodízio ou por peso.
- **Página pública de agendamento:** o lead também pode marcar sozinho, sem conversar. Tem limites contra abuso (por hora no total e por IP por dia, ajustáveis no painel) e, por decisão de projeto, avisa quando o telefone informado já tem horário marcado; isso revela só que existe um horário marcado, nada mais.
  - **Limites:** no máximo `public_max_per_phone_day` (padrão 3) tentativas por telefone por dia, além do limite por IP por dia. As tentativas contam mesmo quando não resultam em agendamento. O teto global por hora (`public_max_per_hour`, padrão 20) conta só reservas efetivadas, para tentativas recusadas não derrubarem a página; quem encher a agenda com reservas reais só é contido por Turnstile/WAF na borda.
  - **Risco aceito:** a página pública não tem verificação por código (OTP), então não dá para provar que quem digita um número é o dono dele. Os limites reduzem o abuso, mas não eliminam. O limite por IP é aproximado (usa o último endereço de `x-forwarded-for` que o proxy do Supabase acrescenta; sem o cabeçalho (IP desconhecido), a reserva não entra no limite por IP e só os limites por telefone e global protegem a página), então a proteção real é Cloudflare Turnstile ou regras de WAF na borda, na frente da página: recomendamos configurar se ela for divulgada abertamente.
  - **Telefone:** sempre em formato internacional com `+` (por exemplo `+5511999990000`), no painel, na página pública e nos comandos de teste. O sistema guarda no formato canônico com `+`.
  - **Modo teste e repasse ao closer:** com o modo teste ligado, o resumo para o closer sai com o prefixo `[TESTE]` e só vai para o WhatsApp do closer se o número dele estiver nos números de teste; senão vai para o telefone do dono. O closer é equipe interna do negócio, não um lead.
  - **Migration:** use um projeto Supabase NOVO e dedicado ao Setup 18. A migration recusa rodar num projeto que já tenha qualquer tabela do setup (`leads`, `messages`, `appointments`, `outbox`, `panel_users` etc.) sem ser deste setup, para nunca misturar com dados de outro sistema.
  - **Telefone com DDI:** todo telefone (lead, closer, dono, testes) vale só com código do país, como `+5511999990000`. Número local sem DDI não é adivinhado.
  - **Pedido de não receber mensagens (`PARAR`):** se o lead já tem call marcada, a call é mantida e o repasse ao closer continua, mas o agente não escreve mais para ele; o dono é avisado de que o lead pediu para sair e ainda tem horário marcado.
- **Painel com login:** visão geral, funil, conversas, agenda, configuração do agente e fila de envio. Botão para assumir a conversa.
- **Lembretes e follow-up:** confirmação, lembrete de 24h e 2h, e 3 toques de acompanhamento dentro de um horário comercial.
- **Seus dados no seu Supabase:** nada fica em servidor de terceiros além dos serviços que você mesmo conecta.
- **Google Calendar (opcional)** por Apps Script.

## Como funciona

```text
Lead no WhatsApp  <->  Evolution API  <->  Motor (Python, no seu computador)
                                              |
                                              v
                                   Supabase (seu projeto)
                                              |
                          Painel e página /agendar (Cloudflare Pages)
```

O **motor** roda no seu computador e precisa estar ligado para o agente responder. A Etapa 5 instala o motor como serviço para ele ligar junto com o computador.

## Pré-requisitos

- Python 3.10 ou mais recente, Git, Node.js (com `npx`) e Claude Code.
- Conta no Supabase (plano gratuito serve para começar) e a Supabase CLI instalada (recomendado). Sem ela, o instalador usa `npx` com a versão fixa `supabase@2.90.0` (a testada com este instalador), nunca a última publicada.
- Conta no Cloudflare (gratuita) e o Wrangler.
- Uma Evolution API funcionando e um número de WhatsApp dedicado ao atendimento.
- Chave de API de um provedor de IA: Gemini, OpenAI ou Anthropic.
- PyYAML (`python3 -m pip install pyyaml`). O instalador avisa se faltar.

Para conferir tudo de uma vez:

```bash
python3 setup/check_prerequisites.py
```

Em Windows, use `python` no lugar de `python3`.

## Instalação guiada

```bash
gh repo clone zxmarketingdigital/zx-control-setup18-sdr-agendamento && cd zx-control-setup18-sdr-agendamento && claude
```

No Claude Code, digite:

```text
INICIAR SETUP
```

O comando abre `/sdr-instalar`, mostra o estado e conduz as 7 Etapas na ordem.

## As Etapas

1. **Entrevista e nicho:** você responde sobre o negócio, os closers e a agenda.
2. **Banco de dados e usuário dono:** cria as tabelas no seu Supabase e o seu login. Se já existir uma conta com o seu e-mail, o instalador não a reaproveita em silêncio: exige uma senha nova (`SDR_OWNER_PASSWORD`) e a redefine.
3. **Painel e página pública:** grava a configuração do agente e publica no Cloudflare Pages.
4. **WhatsApp e IA:** conecta a Evolution API (QR code) e a chave de IA. Depois da instalação, se o WhatsApp cair ou você quiser trocar de número, reconecte pelo painel, no menu **WhatsApp** (o motor precisa estar ligado).
5. **Motor como serviço:** liga o motor junto com o computador.
6. **Teste guiado:** conversa simulada e conversa real com um número de teste.
7. **Liberar produção:** o agente passa a responder qualquer lead.

Para ver em que Etapa você está a qualquer momento:

```bash
python3 setup/estado.py status
```

## Modo teste e segurança

Até a Etapa 7, o agente **só responde os números de teste** cadastrados. Lead que pede para parar (`PARAR`) não recebe mais mensagem (se tinha call marcada, ela é mantida e você é avisado). Você pode pausar o agente a qualquer momento (`python3 setup/operar.py pausar`) ou assumir uma conversa pelo painel.

**Atualizando um banco que já tem dados:** antes de aplicar migrations novas, o instalador lista o que vai rodar. Se houver operação que apaga dados (`DROP TABLE`, `DROP SCHEMA`, `ALTER TABLE ... DROP [COLUMN]`, `TRUNCATE`, `DELETE` ou `UPDATE` sem `WHERE`, ou com `WHERE` que não olha nenhuma coluna, como `WHERE true`, `WHERE 1=1` ou `WHERE 1 > 0`), ele para; o plano B pelo SQL Editor segue a mesma regra e só lista migrations pendentes; faça backup, leia o SQL e rode de novo com `--confirmar-destrutivo`. A análise também lê comentários e textos do SQL: um `DROP` só dentro de comentário é falso positivo e a mesma flag libera. Se a Supabase CLI disser que o banco já está em dia, nada é aplicado e nada é bloqueado. Os telefones de leads já gravados precisam ter DDI (`+55...`) antes de migrar; se algum não tiver, a migration aborta e lista os ids para você corrigir. Nas chaves do Supabase, o painel e a página pública usam só a `anon`/`publishable`, nunca a `service_role`.

Chaves e senhas ficam apenas no seu computador (`config/config.json`, permissão restrita) e nunca vão para o Git. O painel usa somente a chave pública `anon`; o acesso aos dados exige login.

## Nichos

Os modelos ficam em `nichos/*.yaml`: imobiliária, veículos, seguros, saúde e clínicas, serviços e consultoria. Cada um traz tom, perguntas de qualificação, regras de desqualificação, objeções, mensagens de acompanhamento e lembretes. Onde estiver `[PREENCHER]` na base de conhecimento, você completa com as informações reais do seu negócio no painel.

```bash
python3 setup/nichos.py listar
python3 setup/nichos.py validar
```

## Dia a dia

Use `/sdr-agendamento` no Claude Code para ver como o agente está, pausar, ajustar e resolver problemas. Comandos úteis:

```bash
python3 setup/operar.py status
python3 setup/servico.py status
python3 setup/servico.py logs
python3 -m motor check
```

`uninstall` só diz que removeu depois de conferir que o serviço sumiu (launchd, systemd ou Agendador de Tarefas); se ainda existir, sai com erro e mostra o comando manual.

Se `servico.py stop` ou `uninstall` avisar que não conseguiu identificar o processo do motor, ele pode continuar rodando (o instalador só encerra um processo que prove ser o motor desta pasta; se o sistema não informar a pasta, ele não mata nada e avisa): encerre o PID mostrado pelo Gerenciador de Tarefas/Monitor de Atividade e rode o comando de novo.

## Estrutura

| Pasta | Conteúdo |
|---|---|
| `setup/` | Instalador e utilitários em Python |
| `skills/` | `/sdr-instalar` e `/sdr-agendamento` |
| `nichos/` | Modelos de nicho |
| `motor/` | Motor do agente |
| `web/` | Painel e página pública |
| `supabase/` | Migrations do banco |
| `docs/` | Plano e desenho do projeto |

## Suporte

Suporte da ZX LAB: https://suporte.zxlab.com.br/hub

Licença de uso em [LICENSE.md](LICENSE.md).
