# Masterclass — Agente SDR de Agendamento

Aula de lançamento do Setup 18.

- **Vídeo:** [PREENCHER: link da gravação, publicado também na área de membros]
- **Apresentação:** [PREENCHER: link da apresentação]
- **Demonstração:** o painel e a página de agendamento têm um modo demonstração, sem login e sem banco. Veja como abrir em "Rodar a demonstração" abaixo.

## Capítulos

| Assunto | O que você vê |
|---|---|
| Por que o primeiro contato decide a venda | O custo de responder tarde e de agendar na mão |
| O que o Setup entrega | Agente, painel, página de agendamento, lembretes e follow-up |
| Demonstração do painel | Visão geral, funil, conversas, agenda e fila de envio |
| Uma conversa de ponta a ponta | Do "oi" até o horário marcado com o closer |
| Objeções e desqualificação | Como o agente responde "está caro" e "vou pensar" e quando encerra com educação |
| Os nichos | Imobiliária, veículos, seguros, saúde e clínicas, serviços e consultoria |
| Assumir a conversa | O dono entra na conversa e o agente se cala |
| Anti-no-show | Confirmação, lembrete de 24h e de 2h, aviso ao closer |
| A instalação em 7 Etapas | A entrevista e o que cada Etapa configura |
| Modo teste e produção | Como testar sem risco e quando liberar |
| Agência | Uma pasta e um Supabase por cliente, operados do seu computador |
| Operação diária | Pausar, ajustar o tom, completar a base de conhecimento |

## Como acompanhar

1. Assista à demonstração para entender o fluxo completo.
2. Clone o repositório e digite `INICIAR SETUP` no Claude Code (ver [README](README.md)).
3. Faça a entrevista da Etapa 1 com calma: ela define o que o agente fala pelo seu negócio.
4. Teste com um número seu antes de liberar a produção.

## Rodar a demonstração (sem banco)

```bash
python3 setup/etapa_web.py demo
python3 setup/etapa_web.py local
```

Depois acesse `http://localhost:8788`. Para voltar ao modo real, rode `python3 setup/etapa_web.py config`.

## Para quem instala para clientes

- Use uma cópia do repositório, um projeto Supabase e uma instância de WhatsApp por cliente.
- O modo teste deixa você validar o fluxo com o cliente antes de liberar.
- O cliente é o responsável pelas mensagens enviadas em nome do negócio dele e pelos dados dos leads (LGPD). Combine isso por escrito.
