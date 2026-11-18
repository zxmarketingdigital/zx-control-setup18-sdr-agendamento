-- =====================================================================
-- Setup 18 · dados de DEMONSTRAÇÃO (100% fictícios)
-- Autoria e suporte: ZX LAB (https://suporte.zxlab.com.br/hub)
--
-- Uso (banco já com a migration aplicada):
--   psql "$DATABASE_URL" --single-transaction -f supabase/seed_demo.sql
--
-- Cria uma empresa de exemplo com 2 closers, agenda de segunda a sexta,
-- 30 leads em todas as etapas do funil, calls marcadas e feitas, vendas,
-- objeções e conversas. Telefones +5511900000xxx não existem.
-- SÓ para banco vazio ou de demonstração: aborta se o agente já foi configurado.
-- Pode ser rodado de novo: antes de inserir, apaga a demonstração anterior
-- (só os leads +5511900000xxx e os closers @demo.invalid).
-- =====================================================================

-- 0a. Trava: este seed é SÓ para banco vazio ou de demonstração. Se o aluno já
-- configurou o agente (empresa preenchida e diferente da demo), aborta sem tocar em nada.
do $$
begin
  if exists (
    select 1 from public.agent_config
    where company is not null and btrim(company) <> '' and company <> 'Studio Aurora'
  ) then
    raise exception 'seed_demo.sql abortado: agent_config já foi configurado (empresa diferente de "Studio Aurora"). Este seed é só para um projeto de demonstração; ele sobrescreveria a configuração real.';
  end if;
end;
$$;

-- 0. Limpa a demonstração anterior ------------------------------------
delete from public.leads where phone like '+5511900000%';
delete from public.closers where email like '%@demo.invalid';

-- 1. Agente e agenda ----------------------------------------------------
update public.agent_config set
  agent_name = 'Ana',
  company = 'Studio Aurora',
  tone = 'Acolhedora, direta e profissional. Frases curtas, sem jargão, no máximo um emoji por mensagem.',
  niche = 'servicos',
  offer = 'Conversa de diagnóstico de 30 minutos com um especialista, sem compromisso.',
  knowledge = E'# Studio Aurora (empresa fictícia de demonstração)\n\n'
    '## O que fazemos\nConsultoria de organização e crescimento para pequenos negócios de serviços.\n\n'
    '## Como funciona\n1. Conversa de diagnóstico (30 min, online).\n2. Plano de ação por escrito.\n3. Acompanhamento mensal opcional.\n\n'
    '## Quem atendemos\nNegócios com 1 a 20 pessoas que querem previsibilidade de clientes.\n\n'
    '## Quem NÃO atendemos\nQuem busca apenas indicação de fornecedor ou ainda não tem negócio aberto.',
  qualification_questions = '[
    {"key":"segmento","pergunta":"Qual é o segmento do seu negócio?","obrigatoria":true},
    {"key":"tamanho","pergunta":"Quantas pessoas trabalham com você hoje?","obrigatoria":true},
    {"key":"orcamento","pergunta":"Você já separou algum valor mensal para investir nisso?","obrigatoria":true},
    {"key":"decisor","pergunta":"A decisão é só sua ou mais alguém participa?","obrigatoria":true}
  ]'::jsonb,
  disqualify_rules = '[
    {"key":"sem_orcamento","regra":"Não tem nenhum valor disponível e não pretende ter nos próximos 3 meses.","mensagem":"Obrigada por me contar! Quando fizer sentido investir, é só me chamar por aqui."},
    {"key":"sem_decisor","regra":"Não decide nem participa da decisão.","mensagem":"Entendi! Se quiser, traga quem decide para a conversa e eu agendo com vocês dois."},
    {"key":"fora_da_regiao","regra":"Atendemos apenas o Brasil.","mensagem":"Por enquanto atendemos só negócios no Brasil. Obrigada pelo interesse!"}
  ]'::jsonb,
  objection_playbook = '[
    {"categoria":"preco","sinais":["caro","sem dinheiro","valor"],"resposta":"Entendo a preocupação. A conversa de diagnóstico não tem custo e você sai com um plano mesmo que decida não seguir."},
    {"categoria":"tempo","sinais":["sem tempo","corrido"],"resposta":"São só 30 minutos e posso encontrar um horário que caiba na sua rotina."},
    {"categoria":"confianca","sinais":["funciona mesmo","garantia"],"resposta":"Faz sentido perguntar. Na conversa você vê exemplos reais de clientes do seu segmento."},
    {"categoria":"decisor","sinais":["preciso falar com","meu sócio"],"resposta":"Sem problema. Podemos marcar com quem decide junto, assim a conversa rende mais."}
  ]'::jsonb,
  followup_messages = '[
    {"apos_horas":24,"texto":"Oi! Passando para saber se ainda faz sentido conversarmos. Posso te mostrar dois horários?"},
    {"apos_horas":72,"texto":"Última mensagem por aqui: se quiser retomar, me chame e eu encontro um horário para você."}
  ]'::jsonb,
  confirm_template = 'Olá {nome}! Sua conversa está confirmada para {data} às {hora}. Qualquer imprevisto, é só me avisar por aqui.',
  reminder_24h_template = 'Oi {nome}! Lembrando da nossa conversa amanhã, {data} às {hora}. Está confirmada?',
  reminder_2h_template = 'Oi {nome}! Daqui a pouco, às {hora}, começa a nossa conversa. Te espero!',
  owner_phone = '+5511900000000',
  test_only = true,
  test_phones = array['+5511900000000'],
  paused = false
where id = 1;

update public.schedule_settings set
  call_minutes = 30,
  buffer_minutes = 10,
  slot_step_minutes = 30,
  min_notice_hours = 2,
  days_ahead = 14,
  timezone = 'America/Sao_Paulo',
  public_slug = 'demo',
  public_enabled = true,
  distribution = 'rodizio'
where id = 1;

-- 2. Closers e disponibilidade -----------------------------------------
insert into public.closers (id, name, whatsapp, email, active, weight, color) values
  ('00000000-0000-4000-8000-0000000000c1', 'Helena Duarte', '+5511900009001', 'helena@demo.invalid', true, 1, '#D97706'),
  ('00000000-0000-4000-8000-0000000000c2', 'Bruno Almeida', '+5511900009002', 'bruno@demo.invalid', true, 1, '#2563EB');

insert into public.availability_rules (closer_id, weekday, start_time, end_time)
select c.id, d, t.s, t.e
from public.closers c
cross join generate_series(1, 5) as d
cross join (values ('09:00'::time, '12:00'::time), ('14:00'::time, '18:00'::time)) as t(s, e)
where c.email like '%@demo.invalid';

-- 3. Leads (tabela de roteiro temporária) --------------------------------
create or replace function pg_temp.next_wd(d date) returns date language sql immutable as $$
  select case extract(dow from d)::int when 6 then d + 2 when 0 then d + 1 else d end
$$;
create or replace function pg_temp.prev_wd(d date) returns date language sql immutable as $$
  select case extract(dow from d)::int when 6 then d - 1 when 0 then d - 2 else d end
$$;
create or replace function pg_temp.path_for(p_stage text) returns text[] language sql immutable as $$
  select case p_stage
    when 'novo' then array['novo']
    when 'respondeu' then array['novo','respondeu']
    when 'desqualificado' then array['novo','respondeu','desqualificado']
    when 'qualificado' then array['novo','respondeu','qualificado']
    when 'horarios_ofertados' then array['novo','respondeu','qualificado','horarios_ofertados']
    when 'agendado' then array['novo','respondeu','qualificado','horarios_ofertados','agendado']
    when 'confirmado' then array['novo','respondeu','qualificado','horarios_ofertados','agendado','confirmado']
    when 'compareceu' then array['novo','respondeu','qualificado','horarios_ofertados','agendado','confirmado','compareceu']
    when 'no_show' then array['novo','respondeu','qualificado','horarios_ofertados','agendado','confirmado','no_show']
    when 'venda' then array['novo','respondeu','qualificado','horarios_ofertados','agendado','confirmado','compareceu','venda']
    when 'perdido' then array['novo','respondeu','qualificado','horarios_ofertados','perdido']
    when 'reagendado' then array['novo','respondeu','qualificado','horarios_ofertados','agendado','reagendado']
  end
$$;

drop table if exists pg_temp.demo_leads;
create temp table demo_leads as
select * from (values
  -- n, nome, etapa, criado há (dias), closer, status da call, dia da call (±dias de hoje), hora, venda, motivo, origem, score
  (1,  'Marina Costa',      'novo',               0, null, null,         null,  null,    null,    null,             'whatsapp',  null),
  (2,  'Bruno Tavares',     'novo',               0, null, null,         null,  null,    null,    null,             'instagram', null),
  (3,  'Larissa Mendes',    'novo',               1, null, null,         null,  null,    null,    null,             'pagina',    null),
  (4,  'Diego Fonseca',     'respondeu',          1, null, null,         null,  null,    null,    null,             'whatsapp',  30),
  (5,  'Carolina Prado',    'respondeu',          2, null, null,         null,  null,    null,    null,             'anuncio',   35),
  (6,  'Eduardo Lima',      'respondeu',          2, null, null,         null,  null,    null,    null,             'indicacao', 40),
  (7,  'Fernanda Rocha',    'desqualificado',     3, null, null,         null,  null,    null,    'sem_orcamento',  'anuncio',   10),
  (8,  'Gustavo Pires',     'desqualificado',     4, null, null,         null,  null,    null,    'fora_da_regiao', 'whatsapp',  5),
  (9,  'Helena Vieira',     'desqualificado',     6, null, null,         null,  null,    null,    'sem_decisor',    'instagram', 20),
  (10, 'Igor Batista',      'qualificado',        2, null, null,         null,  null,    null,    null,             'anuncio',   70),
  (11, 'Juliana Moraes',    'qualificado',        3, null, null,         null,  null,    null,    null,             'whatsapp',  65),
  (12, 'Kleber Nogueira',   'qualificado',        5, null, null,         null,  null,    null,    null,             'indicacao', 75),
  (13, 'Leticia Andrade',   'horarios_ofertados', 1, null, null,         null,  null,    null,    null,             'anuncio',   80),
  (14, 'Mateus Ribeiro',    'horarios_ofertados', 2, null, null,         null,  null,    null,    null,             'instagram', 72),
  (15, 'Natalia Freitas',   'agendado',           4, 1,    'agendado',   1,     '10:00', null,    null,             'anuncio',   85),
  (16, 'Otavio Campos',     'agendado',           5, 2,    'agendado',   1,     '14:30', null,    null,             'whatsapp',  78),
  (17, 'Patricia Gomes',    'agendado',           2, 1,    'agendado',   2,     '09:30', null,    null,             'pagina',    82),
  (18, 'Renato Barros',     'confirmado',         6, 2,    'confirmado', 1,     '16:00', null,    null,             'indicacao', 90),
  (19, 'Sabrina Lopes',     'confirmado',         7, 1,    'confirmado', 3,     '11:00', null,    null,             'anuncio',   88),
  (20, 'Thiago Martins',    'compareceu',         9, 1,    'feito',      -5,    '10:00', null,    null,             'anuncio',   84),
  (21, 'Ursula Teixeira',   'compareceu',        10, 2,    'feito',      -6,    '15:00', null,    null,             'whatsapp',  79),
  (22, 'Vinicius Duarte',   'compareceu',        12, 1,    'feito',      -8,    '09:00', null,    null,             'instagram', 74),
  (23, 'Wesley Moura',      'compareceu',        13, 2,    'feito',      -9,    '16:30', null,    null,             'indicacao', 81),
  (24, 'Yasmin Cardoso',    'no_show',            8, 1,    'no_show',    -4,    '11:00', null,    null,             'anuncio',   77),
  (25, 'Zeca Ferraz',       'no_show',           11, 2,    'no_show',    -7,    '14:00', null,    null,             'whatsapp',  69),
  (26, 'Aline Dantas',      'venda',             14, 1,    'venda',      -10,   '10:30', 2400.00, null,             'anuncio',   92),
  (27, 'Bernardo Siqueira', 'venda',             15, 2,    'venda',      -11,   '15:30', 3900.00, null,             'indicacao', 95),
  (28, 'Cintia Vasconcelos','venda',             18, 1,    'venda',      -14,   '09:30', 1800.00, null,             'whatsapp',  89),
  (29, 'Davi Queiroz',      'perdido',           20, null, null,         null,  null,    null,    null,             'anuncio',   45),
  (30, 'Elisa Brandao',     'reagendado',        10, 2,    'reagendado', -3,    '17:00', null,    null,             'pagina',    80)
) as v(n, nome, etapa, criado_ha, closer, call_status, call_dia, call_hora, venda, motivo, origem, score);

insert into public.leads (
  phone, name, email, source, stage, profile, qualification, score, disqualify_reason,
  takeover_until, closer_id, followup_step, last_inbound_at, last_outbound_at, created_at
)
select
  '+5511900000' || lpad(d.n::text, 3, '0'),
  d.nome,
  lower(replace(split_part(d.nome, ' ', 1), ' ', '')) || '.' || lower(replace(split_part(d.nome, ' ', 2), ' ', '')) || '@example.com',
  d.origem,
  d.etapa,
  case when d.etapa in ('novo') then null
       else (array['Dona de salão com 4 funcionárias', 'Clínica de estética, 2 sócias', 'Escritório de contabilidade, 6 pessoas',
                   'Oficina mecânica, equipe de 8', 'Consultor autônomo', 'Loja de móveis planejados'])[1 + d.n % 6] end,
  case when d.etapa in ('novo', 'respondeu') then '{}'::jsonb
       else jsonb_build_object(
         'segmento', (array['beleza', 'saúde', 'contabilidade', 'automotivo', 'consultoria', 'varejo'])[1 + d.n % 6],
         'tamanho', (array['1-2', '3-5', '6-10', '11-20'])[1 + d.n % 4],
         'orcamento', case when d.motivo = 'sem_orcamento' then 'nenhum' else 'definido' end,
         'decisor', case when d.motivo = 'sem_decisor' then 'não' else 'sim' end) end,
  d.score,
  d.motivo,
  case when d.n = 24 then now() + interval '12 hours' end,     -- dono assumiu esta conversa
  case d.closer when 1 then '00000000-0000-4000-8000-0000000000c1'::uuid
                when 2 then '00000000-0000-4000-8000-0000000000c2'::uuid end,
  case when d.etapa in ('novo', 'respondeu') then 0 when d.etapa = 'perdido' then 2 else 0 end,
  least(now() - interval '2 minutes', now() - d.criado_ha * interval '1 day' - interval '2 hours' + interval '8 minutes'),
  least(now() - interval '1 minute', now() - d.criado_ha * interval '1 day' - interval '2 hours' + interval '10 minutes'),
  now() - d.criado_ha * interval '1 day' - interval '2 hours'
from pg_temp.demo_leads d;

-- 4. Calls ---------------------------------------------------------------
insert into public.appointments (lead_id, closer_id, starts_at, ends_at, status, sale_value, source, notes, created_at)
select
  l.id,
  l.closer_id,
  ((case when d.call_dia >= 0 then pg_temp.next_wd((now() at time zone 'America/Sao_Paulo')::date + d.call_dia)
         else pg_temp.prev_wd((now() at time zone 'America/Sao_Paulo')::date + d.call_dia) end)
     + d.call_hora::time) at time zone 'America/Sao_Paulo',
  ((case when d.call_dia >= 0 then pg_temp.next_wd((now() at time zone 'America/Sao_Paulo')::date + d.call_dia)
         else pg_temp.prev_wd((now() at time zone 'America/Sao_Paulo')::date + d.call_dia) end)
     + d.call_hora::time + interval '30 minutes') at time zone 'America/Sao_Paulo',
  d.call_status,
  d.venda,
  case when d.origem = 'pagina' then 'pagina' else 'agente' end,
  case d.call_status
    when 'feito' then 'Cliente com interesse, pediu proposta por escrito.'
    when 'no_show' then 'Não entrou na chamada. Dono tentou contato.'
    when 'venda' then 'Fechou o plano completo.'
    when 'reagendado' then 'Pediu para remarcar por imprevisto.'
  end,
  l.created_at + interval '3 hours'
from pg_temp.demo_leads d
join public.leads l on l.phone = '+5511900000' || lpad(d.n::text, 3, '0')
where d.call_status is not null;

-- 5. Funil: o trigger registra só a etapa final; refaz o caminho completo ----
delete from public.funnel_events
where lead_id in (select id from public.leads where phone like '+5511900000%');

insert into public.funnel_events (lead_id, stage, created_at)
select l.id, p.stage, least(l.created_at + (p.ord - 1) * interval '7 hours', now())
from pg_temp.demo_leads d
join public.leads l on l.phone = '+5511900000' || lpad(d.n::text, 3, '0')
cross join lateral unnest(pg_temp.path_for(d.etapa)) with ordinality as p(stage, ord);

-- 6. Conversas ------------------------------------------------------------
-- cada mensagem: (ordem, direção, autor, condição de etapa, texto)
insert into public.messages (lead_id, direction, author, body, wa_id, created_at)
select l.id, m.direcao, m.autor, replace(m.texto, '{nome}', split_part(d.nome, ' ', 1)),
       'DEMO-' || d.n || '-' || m.ord,
       l.created_at + m.ord * interval '4 minutes'
from pg_temp.demo_leads d
join public.leads l on l.phone = '+5511900000' || lpad(d.n::text, 3, '0')
join (values
  (1,  'in',  'lead',   'novo',               'Oi! Vi o anúncio de vocês e queria saber mais.'),
  (2,  'out', 'agente', 'novo',               'Olá, {nome}! Eu sou a Ana, do Studio Aurora. Posso te fazer duas perguntas rápidas para entender como ajudar?'),
  (3,  'in',  'lead',   'respondeu',          'Pode sim.'),
  (4,  'out', 'agente', 'respondeu',          'Perfeito! Qual é o segmento do seu negócio e quantas pessoas trabalham com você hoje?'),
  (5,  'in',  'lead',   'qualificado',        'Tenho um negócio de serviços, somos em 4 pessoas e quero atrair mais clientes.'),
  (6,  'out', 'agente', 'qualificado',        'Que bom! Você já separou algum valor mensal para investir nisso e a decisão é só sua?'),
  (7,  'in',  'lead',   'qualificado',        'Já tenho um valor separado e a decisão é minha.'),
  (8,  'out', 'agente', 'horarios_ofertados', 'Ótimo! Tenho amanhã às 10h ou depois de amanhã às 14h30. Qual fica melhor?'),
  (9,  'in',  'lead',   'horarios_ofertados', 'Pode ser o primeiro horário.'),
  (10, 'out', 'agente', 'agendado',           'Combinado! Sua conversa está agendada. Te lembro um dia antes, combinado?'),
  (11, 'out', 'agente', 'desqualificado',     'Obrigada por me contar! Quando fizer sentido investir nisso, é só me chamar por aqui.'),
  (12, 'out', 'dono',   'no_show',            'Oi! Vi que não deu para entrar na chamada. Quer remarcar para outro dia?')
) as m(ord, direcao, autor, etapa_min, texto)
  on  (m.etapa_min = 'novo')
   or (m.etapa_min = 'respondeu'   and 'respondeu' = any (pg_temp.path_for(d.etapa)))
   or (m.etapa_min = 'qualificado' and 'qualificado' = any (pg_temp.path_for(d.etapa)))
   or (m.etapa_min = 'horarios_ofertados' and 'horarios_ofertados' = any (pg_temp.path_for(d.etapa)))
   or (m.etapa_min = 'agendado'    and 'agendado' = any (pg_temp.path_for(d.etapa)))
   or (m.etapa_min = 'desqualificado' and d.etapa = 'desqualificado')
   or (m.etapa_min = 'no_show'     and d.etapa = 'no_show');

-- 7. Objeções --------------------------------------------------------------
insert into public.objections (lead_id, category, excerpt, handled, created_at)
select l.id, o.categoria, o.trecho, o.tratada, l.created_at + interval '30 minutes'
from (values
  (7,  'preco',      'Acho que não cabe no meu orçamento agora.',              false),
  (9,  'decisor',    'Preciso falar com meu sócio antes de marcar qualquer coisa.', true),
  (10, 'preco',      'Quanto custa isso? Estou com o caixa apertado.',         true),
  (11, 'confianca',  'Isso funciona mesmo para o meu tipo de negócio?',        true),
  (12, 'tempo',      'Estou sem tempo essa semana, o salão está lotado.',      true),
  (13, 'confianca',  'Vocês têm algum caso de cliente parecido comigo?',       true),
  (14, 'preco',      'Achei caro para o momento.',                             false),
  (17, 'preco',      'Tem desconto para pagamento à vista?',                   true),
  (18, 'concorrente','Já estou conversando com outra empresa.',                true),
  (20, 'decisor',    'Vou levar para a minha sócia decidir.',                  true),
  (22, 'concorrente','Outra consultoria me passou uma proposta mais barata.',  false),
  (24, 'tempo',      'Minha agenda está uma loucura, desculpa.',               true),
  (25, 'preco',      'Preciso pensar no valor com calma.',                     false),
  (29, 'agora_nao',  'Agora não é o momento, me chama no ano que vem.',        false)
) as o(n, categoria, trecho, tratada)
join public.leads l on l.phone = '+5511900000' || lpad(o.n::text, 3, '0');

-- 8. Fila de envio (um lembrete já enviado e um pendente) -----------------
insert into public.outbox (lead_id, phone, body, status, attempts, wa_key_id, created_by, created_at, sent_at)
select l.id, l.phone, 'Olá, ' || split_part(l.name, ' ', 1) || '! Sua conversa está confirmada. Até lá!',
       'enviado', 1, 'DEMO-KEY-' || l.id, 'pagina', now() - interval '1 day', now() - interval '1 day' + interval '20 seconds'
from public.leads l where l.phone = '+5511900000017';

insert into public.outbox (lead_id, phone, body, status, created_by)
select l.id, l.phone, 'Oi, ' || split_part(l.name, ' ', 1) || '! Posso te ajudar a escolher um horário?', 'pendente', 'painel'
from public.leads l where l.phone = '+5511900000013';

drop table pg_temp.demo_leads;
