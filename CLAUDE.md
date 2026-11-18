# Instruções do Setup 18 — Agente SDR de Agendamento

Este repositório é instalado pelo aluno no próprio computador. Quando o aluno escrever ou disser **INICIAR SETUP**, leia o estado e execute `/sdr-instalar`. Conduza uma Etapa por vez, conferindo o efeito real antes de avançar.

## Regras de atendimento

- Fale sempre em português do Brasil e use a palavra **Etapa**.
- Nunca peça uma chave, token ou senha no chat. Todo segredo (service_role, chave da Evolution, chave de IA, senha do painel) o aluno digita num terminal próprio, sem eco, ou coloca no bloco `"entrada"` de `config/config.json` (arquivo local, fora do Git) ou em variável de ambiente (`SDR_SUPABASE_SERVICE_ROLE_KEY`, `SDR_SUPABASE_DB_PASSWORD`, `SDR_OWNER_PASSWORD`, `SDR_EVOLUTION_API_KEY`, `SDR_AI_API_KEY`). O Claude Code roda sem terminal interativo: script sem valor aborta pedindo flag ou variável, nunca trava esperando.
- Nunca comite nem mostre `config/config.json`, `web/config.js`, `data/` ou `logs/`. Eles já estão no `.gitignore`.
- Não invente informações do negócio do aluno (preços, prazos, políticas, endereço). O que ele não souber vira [PREENCHER] e é completado no painel.
- Fale em "seu computador". O agente roda via Claude Code em Mac, Windows e Linux. Em Windows use `python` no lugar de `python3`.
- O agente envia mensagens reais no WhatsApp do negócio. Antes da Etapa 7 ele só responde os números de teste. Nunca libere a produção sem o aluno pedir.
- Suporte é sempre o da ZX LAB: https://suporte.zxlab.com.br/hub

## Fluxo

Use `python3 setup/estado.py status` no início e depois de cada Etapa. Os scripts rodam a partir da raiz do repositório. Se um comando falhar, explique o que a mensagem significa e mantenha a Etapa em andamento; não marque como concluída só porque o comando terminou.

| Etapa | O que faz | Script |
|---|---|---|
| 1 | Entrevista e nicho | `etapa_config.py modelo` e `validar` |
| 2 | Banco de dados e usuário dono | `etapa_banco.py tudo` |
| 3 | Painel e página pública | `etapa_config.py banco`, `etapa_web.py config` e `deploy` |
| 4 | WhatsApp (Evolution) e IA | `etapa_config.py evolution` e `ia` |
| 5 | Motor como serviço | `servico.py install` |
| 6 | Teste guiado | `teste_guiado.py` |
| 7 | Liberar produção | `liberar_producao.py` |

Leia a skill `skills/sdr-instalar/SKILL.md` antes de conduzir o trabalho. As skills são autocontidas e não dependem de nada fora do repositório.

## Depois da Etapa 7

`/sdr-agendamento` é a skill do dia a dia: ver como o agente está, pausar e retomar, ajustar tom, perguntas e horários, diagnosticar problemas. O motor roda no computador do aluno; se ele desligar, o agente para de responder até o computador voltar.
