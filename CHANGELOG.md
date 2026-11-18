# Changelog

Todas as mudanças relevantes do Setup 18 (Agente SDR de Agendamento) ficam aqui.
O formato segue o [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/) e as versões seguem o [SemVer](https://semver.org/lang/pt-BR/).

## [1.0.0] - 2026-11-18

### Adicionado
- Agente SDR no WhatsApp que qualifica o lead, trata objeções, oferece horários livres e agenda a call com o closer.
- Banco no Supabase do próprio aluno (migration em `supabase/migrations/`): leads, conversas, funil, agendamentos, closers, horários, bloqueios, fila de envio e usuários do painel, com RLS.
- Painel web (login com e-mail e senha) e página pública de agendamento no Cloudflare Pages, com modo demonstração sem banco.
- Motor em Python que roda no computador do aluno (`python -m motor run|once|check|simulate`).
- 5 modelos de nicho: imobiliária, veículos, seguros, saúde e clínicas, serviços e consultoria.
- Instalador guiado em 7 Etapas (`setup/`) e skills `/sdr-instalar` e `/sdr-agendamento`.
- Serviço que liga o motor junto com o computador (launchd, systemd --user e Agendador de Tarefas do Windows).
- Teste guiado antes de liberar a produção e modo teste (só números cadastrados recebem resposta).
- Integração opcional com Google Calendar via Apps Script.
