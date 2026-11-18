-- Rodada 7: taxa_agendamento sempre em 0..1 (agendamento sem qualificação prévia), teto global
-- da página pública conta só reservas efetivas, reschedule_slot move o closer do lead.
\i tests/sql/_helpers.sql

-- ---- taxa_agendamento: denominador = qualificados UNIÃO agendados ----
begin;
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana A');
  l1 uuid;
  l2 uuid;
  l3 uuid;
  r jsonb;
begin
  perform pg_temp.as_service();
  perform pg_temp.mk_rule(a, 1, '09:00', '12:00');
  l1 := pg_temp.mk_lead('+5511900005201');
  update public.leads set stage = 'respondeu' where id = l1;
  update public.leads set stage = 'qualificado' where id = l1;
  insert into public.leads (phone, name, stage) values ('+5511900005202', 'Desq 1', 'desqualificado'), ('+5511900005203', 'Desq 2', 'desqualificado');
  select id into l2 from public.leads where phone = '+5511900005202';
  select id into l3 from public.leads where phone = '+5511900005203';
  perform pg_temp.assert_true((public.book_slot(l1, pg_temp.at_local(d, '09:00')) ->> 'ok')::boolean, 'agenda l1');
  perform pg_temp.assert_true((public.book_slot(l2, pg_temp.at_local(d, '10:00')) ->> 'ok')::boolean, 'agenda l2 desqualificado');
  perform pg_temp.assert_true((public.book_slot(l3, pg_temp.at_local(d, '11:00')) ->> 'ok')::boolean, 'agenda l3 desqualificado');
  perform pg_temp.assert_eq((select count(*)::int from public.funnel_events where lead_id in (l2, l3) and stage in ('qualificado', 'agendado')), 0, 'sanidade: l2 e l3 sem evento qualificante');

  r := public.dashboard_stats(now() - interval '1 day', now() + interval '1 day');
  perform pg_temp.assert_eq((r ->> 'agendados')::int, 3, 'agendados = 3');
  perform pg_temp.assert_eq((r ->> 'qualificados')::int, 1, 'qualificados segue = 1 (KPI não muda)');
  perform pg_temp.assert_true((r ->> 'taxa_agendamento')::numeric <= 1, 'taxa_agendamento <= 1: ' || (r ->> 'taxa_agendamento'));
  perform pg_temp.assert_eq((r ->> 'taxa_agendamento')::numeric, 1.000::numeric, 'taxa_agendamento 3/3');
end;
$$;
rollback;

-- ---- teto global por hora: só reservas efetivas contam ----
begin;
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana A');
  r jsonb;
  n int;
begin
  perform pg_temp.mk_rule(a, 1, '09:00', '12:00');
  update public.schedule_settings set public_slug = 'demo', public_enabled = true,
    public_max_per_hour = 2, public_max_per_ip_day = 1000, public_max_per_phone_day = 1000;

  -- 5 tentativas recusadas (horário fora da grade) NÃO derrubam a página
  for n in 1..5 loop
    set local role anon;
    r := public.public_book('demo', 'Ana Souza', 'ana@exemplo.com', '+55119000052' || lpad(n::text, 2, '0'), pg_temp.at_local(d, '15:00'));
    reset role;
    perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'slot_indisponivel'), 'tentativa recusada ' || n);
  end loop;
  perform pg_temp.assert_eq((select count(*)::int from public.public_book_attempts), 5, 'tentativas ficam registradas');
  perform pg_temp.assert_eq((select count(*)::int from public.public_book_attempts where ok), 0, 'nenhuma efetiva');

  -- 2 reservas efetivas enchem o teto de 2; a 3ª é limite_atingido
  set local role anon;
  r := public.public_book('demo', 'Beto Lima', 'beto@exemplo.com', '+5511900005301', pg_temp.at_local(d, '09:00'));
  reset role;
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'reserva 1 passa mesmo após recusas: ' || r::text);
  set local role anon;
  r := public.public_book('demo', 'Caio Dias', 'caio@exemplo.com', '+5511900005302', pg_temp.at_local(d, '10:00'));
  reset role;
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'reserva 2 passa: ' || r::text);
  perform pg_temp.assert_eq((select count(*)::int from public.public_book_attempts where ok), 2, 'só as 2 reservas efetivas marcadas');
  set local role anon;
  r := public.public_book('demo', 'Dora Reis', 'dora@exemplo.com', '+5511900005303', pg_temp.at_local(d, '11:00'));
  reset role;
  perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'limite_atingido'), 'teto global de reservas efetivas atingido');
end;
$$;
rollback;

-- ---- reschedule_slot move leads.closer_id ----
begin;
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana A');
  b uuid := pg_temp.mk_closer('Bia B');
  l1 uuid;
  l2 uuid;
  r jsonb;
begin
  perform pg_temp.as_service();
  perform pg_temp.mk_rule(a, 1, '09:00', '10:00');
  perform pg_temp.mk_rule(b, 1, '11:00', '12:00');
  -- lead em estágio final (compareceu) com closer A
  insert into public.leads (phone, name, stage, closer_id) values ('+5511900005401', 'Terminal', 'compareceu', a);
  select id into l1 from public.leads where phone = '+5511900005401';
  r := public.book_slot(l1, pg_temp.at_local(d, '09:00'));
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'agenda com A: ' || r::text);
  perform pg_temp.assert_eq((select closer_id from public.leads where id = l1), a, 'lead com A');
  r := public.reschedule_slot((r ->> 'appointment_id')::uuid, pg_temp.at_local(d, '11:00'));
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'reagenda: ' || r::text);
  perform pg_temp.assert_eq((r ->> 'closer_id')::uuid, b, 'call nova com B');
  perform pg_temp.assert_eq((select closer_id from public.leads where id = l1), b, 'leads.closer_id acompanha o reagendamento');
  perform pg_temp.assert_eq((select stage from public.leads where id = l1), 'compareceu', 'estágio final preservado');
end;
$$;
rollback;

\echo OK 52_rodada7
