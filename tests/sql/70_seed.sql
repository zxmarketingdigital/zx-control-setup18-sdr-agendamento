-- Seed de demonstração: carrega limpo, pode rodar duas vezes e produz um painel coerente.
\i tests/sql/_helpers.sql

begin;
\i supabase/seed_demo.sql

create temp table seed_primeira as
select (select count(*) from public.leads) leads,
       (select count(*) from public.appointments) appts,
       (select count(*) from public.messages) msgs,
       (select count(*) from public.objections) objs,
       (select count(*) from public.closers) closers;

-- segunda execução: a demonstração anterior é substituída, não duplicada
\i supabase/seed_demo.sql

do $$
declare
  p record;
  r jsonb;
  s public.schedule_settings%rowtype;
begin
  select * into p from seed_primeira;
  perform pg_temp.assert_eq((select count(*) from public.leads), p.leads, 'seed idempotente: leads');
  perform pg_temp.assert_eq((select count(*) from public.appointments), p.appts, 'seed idempotente: agendamentos');
  perform pg_temp.assert_eq((select count(*) from public.messages), p.msgs, 'seed idempotente: mensagens');
  perform pg_temp.assert_eq((select count(*) from public.objections), p.objs, 'seed idempotente: objeções');
  perform pg_temp.assert_eq((select count(*) from public.closers), p.closers, 'seed idempotente: closers');

  perform pg_temp.assert_eq((select count(*)::int from public.closers), 2, '2 closers de exemplo');
  perform pg_temp.assert_true((select bool_and(email like '%@demo.invalid') from public.closers), 'closers com e-mail fictício');
  perform pg_temp.assert_true((select bool_and(phone ~ '^\+5511900000[0-9]{3}$') from public.leads), 'telefones fictícios +5511900000xxx');
  perform pg_temp.assert_true((select count(*) from public.leads) between 28 and 32, 'cerca de 30 leads');
  perform pg_temp.assert_eq((select count(distinct weekday)::int from public.availability_rules), 5, 'agenda de segunda a sexta');
  perform pg_temp.assert_eq((select count(*)::int from public.availability_rules where start_time = '09:00' and end_time = '12:00'), 10, 'turno da manhã 9h–12h (2 closers x 5 dias)');
  perform pg_temp.assert_eq((select count(*)::int from public.availability_rules where start_time = '14:00' and end_time = '18:00'), 10, 'turno da tarde 14h–18h');
  perform pg_temp.assert_true((select count(distinct stage) from public.leads) >= 8, 'leads espalhados pelas etapas do funil');
  perform pg_temp.assert_true((select count(*) from public.appointments) > 0, 'há agendamentos');
  perform pg_temp.assert_true((select count(*) from public.objections) > 0, 'há objeções');
  perform pg_temp.assert_true((select count(*) from public.messages) > 0, 'há mensagens');
  perform pg_temp.assert_true((select count(*) from public.appointments where status = 'venda') > 0, 'há vendas');

  select * into s from public.schedule_settings where id = 1;
  perform pg_temp.assert_eq(s.public_slug, 'demo', 'slug público demo');
  perform pg_temp.assert_true(s.public_enabled, 'página pública ligada na demo');
  perform pg_temp.assert_true((select test_only from public.agent_config), 'demo mantém o modo teste ligado');

  -- o painel da demo mostra números
  r := public.dashboard_stats(now() - interval '60 days', now() + interval '1 day');
  perform pg_temp.assert_true((r ->> 'leads')::int > 0, 'dashboard da demo tem leads');
  perform pg_temp.assert_true((r ->> 'agendados')::int > 0, 'dashboard da demo tem agendamentos');
  perform pg_temp.assert_true((r ->> 'vendas')::int > 0, 'dashboard da demo tem vendas');
  -- e a agenda da demo tem horários livres
  perform pg_temp.assert_true((select count(*) from public.available_slots(now(), now() + interval '14 days')) > 0, 'demo tem horários livres');
end;
$$;
rollback;

\echo OK 70_seed
