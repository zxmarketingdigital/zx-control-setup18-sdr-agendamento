---
name: sdr-agendamento
description: "Operação do dia a dia do Agente SDR de Agendamento. Use quando o aluno quiser ver como o agente está, pausar ou retomar, ajustar nicho, tom, perguntas, objeções ou horários, entender uma conversa, agendar manualmente, rodar o motor ou resolver um problema do agente."
model: claude-sonnet-5-5
effort: medium
---

# /sdr-agendamento — Operar o Agente SDR

Use depois que a instalação (`/sdr-instalar`) terminou. Comece sempre pelo estado:

```bash
python3 setup/estado.py status
python3 setup/operar.py status
```

Em Windows, troque `python3` por `python`.

O Claude Code roda os comandos sem terminal interativo. Se um script precisar de senha ou chave, ele aborta pedindo a variável de ambiente (por exemplo `SDR_OWNER_PASSWORD`, `SDR_AI_API_KEY`) ou o bloco `"entrada"` de `config/config.json` (arquivo local, fora do Git). Peça ao aluno para rodar o comando num terminal próprio, fora do chat, ou colocar o valor nesse arquivo; nunca peça a chave no chat.

## Como o agente funciona

O lead chama no WhatsApp. O motor (que roda no computador do aluno) recebe pela Evolution API, a IA conversa com o tom e a base de conhecimento configurados, qualifica com as perguntas do nicho, trata objeções, oferece horários livres dos closers e agenda. Tudo fica no Supabase do aluno e aparece no painel.

- Quem conversa nunca recebe preço, promessa ou dado que não esteja na base de conhecimento.
- Pedido de "PARAR" encerra a conversa e o lead não recebe mais nada.
- Lead desqualificado recebe uma despedida educada e não é agendado.
- Confirmação, lembretes de 24h e 2h e o resumo para o closer saem sozinhos. No modo teste o resumo leva o prefixo `[TESTE]` e só vai ao closer se o número dele estiver nos números de teste; senão vai ao dono.
- Telefones sempre em formato internacional com `+`. O limite por IP da página pública é aproximado; Turnstile/WAF na borda é a proteção real.
- Se a IA falhar, o lead recebe uma mensagem neutra e o dono é avisado.

## Tarefas comuns

| O aluno quer | O que fazer |
|---|---|
| Ver como está | `python3 setup/operar.py status` e o painel (visão geral, funil, conversas e agenda) |
| Pausar o agente | `python3 setup/operar.py pausar` (ou o botão no painel). Retomar: `python3 setup/operar.py retomar` |
| Responder pessoalmente a um lead | No painel, abra a conversa e use Assumir. O agente se cala por 24h; Devolver reativa |
| Mudar tom, perguntas, objeções, mensagens ou base de conhecimento | Painel, tela de configuração do agente. Vale na hora |
| Mudar horários, duração, intervalo ou closers | Painel, tela de agenda. Para regravar a configuração inicial: `python3 setup/etapa_config.py banco --partes agenda --sobrescrever` |
| Trocar de nicho | Edite `nichos/` ou escolha outro na entrevista (`data/entrevista.json`) e rode `python3 setup/etapa_config.py banco --partes agente --sobrescrever`. Isso substitui o que foi editado no painel, avise antes |
| Completar a base de conhecimento | Painel, tela de configuração do agente. Procure por [PREENCHER] |
| O WhatsApp caiu ou quer trocar o número | Painel > WhatsApp > Reconectar (ler QR Code) ou Trocar número. Veja a seção abaixo |
| Ver o log do motor | `python3 setup/servico.py logs` |
| Reiniciar o motor | `python3 setup/servico.py restart` |
| Voltar ao modo teste | `python3 setup/liberar_producao.py --voltar-ao-teste` |
| Testar uma conversa sem enviar WhatsApp | `python3 -m motor simulate --phone +5511999990000 --text "oi"` |

## WhatsApp caiu ou quer trocar o número

Depois da instalação o aluno não precisa voltar ao instalador. No painel, abra **WhatsApp**:

- **Reconectar (ler QR Code):** pede um QR novo para o mesmo número. No celular: WhatsApp > Aparelhos conectados > Conectar aparelho, e aponte para o QR da tela. O QR é renovado sozinho por alguns minutos.
- **Trocar número:** desconecta o número atual (o painel pede confirmação) e mostra o QR para ler com o WhatsApp do número novo.

O painel só faz o pedido; quem fala com a Evolution é o motor, que roda no computador do aluno. Por isso o motor precisa estar ligado (`python3 setup/servico.py status`); se ele estiver parado, a tela avisa "O motor não está respondendo" e os botões ficam desabilitados. Se a tela WhatsApp não existir ou der erro de tabela, o banco ainda não tem a migration nova: `python3 setup/etapa_banco.py migrar`.

Alternativa pelo instalador, se o painel não estiver acessível: `python3 setup/etapa_config.py evolution` (gera o QR em `data/qrcode-whatsapp.png`).

## Quando algo não funciona

Siga esta ordem e pare quando achar a causa:

1. `python3 -m motor check` mostra qual peça não responde (Supabase, Evolution ou IA).
2. `python3 setup/servico.py status` e `python3 setup/servico.py logs` mostram se o motor está rodando e o último erro.
3. O agente não responde um lead: confira se ele está em modo teste (só os números de teste recebem) com `python3 setup/operar.py status`, se está pausado, e se o lead pediu PARAR.
4. Mensagem do dono ou do painel não saiu: no painel, a fila de envio mostra o erro; no painel, tela WhatsApp, confira se a instância está conectada e reconecte se precisar.
5. O computador ficou desligado: o motor retoma sozinho ao ligar e responde o que ficou pendente.
6. Continua sem resolver: suporte da ZX LAB em https://suporte.zxlab.com.br/hub, com o trecho do log (sem chaves).

## Cuidados

- Nunca peça nem mostre chaves, senhas ou o conteúdo de `config/config.json`.
- Antes de qualquer mudança que regrave a configuração no banco, avise que ela substitui edições feitas no painel.
- O agente fala em nome do negócio do aluno. Mudanças de tom, oferta ou promessas são decisão do dono.
