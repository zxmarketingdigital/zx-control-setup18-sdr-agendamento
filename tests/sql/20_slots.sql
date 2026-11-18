-- Horários: duração, intervalo, antecedência, bloqueios, buffer, fuso, dias à frente.
\i tests/sql/_helpers.sql

-- todos os slots de um dia local (00:00 até 24:00 no fuso informado)
create function pg_temp.slots_on(p_day date, p_closer uuid default null, p_tz text default 'America/Sao_Paulo')
returns table (starts_at timestamptz, ends_at timestamptz, closer_id uuid)
language sql stable as $$
  select * from public.available_slots(
    pg_temp.at_local(p_day, '00:00', p_tz), pg_temp.at_local(p_day + 1, '00:00', p_tz), p_closer)
$$;

-- 1) duração, intervalo e janela da regra ------------------------------------
begin;
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana A');
  n int;
begin
  perform pg_temp.mk_rule(a, 1, '09:00', '12:00');

  select count(*) into n from pg_temp.slots_on(d);
  perform pg_temp.assert_eq(n, 6, '09-12 com call 30 e passo 30 gera 6 slots');
  perform pg_temp.assert_eq((select min(starts_at) from pg_temp.slots_on(d)), pg_temp.at_local(d, '09:00'), 'primeiro slot às 09:00 local');
  perform pg_temp.assert_eq((select max(starts_at) from pg_temp.slots_on(d)), pg_temp.at_local(d, '11:30'), 'último slot às 11:30 local');
  perform pg_temp.assert_true(
    (select bool_and(ends_at - starts_at = interval '30 minutes') from pg_temp.slots_on(d)), 'duração de 30 min');
  perform pg_temp.assert_true(
    (select bool_and(closer_id = a) from pg_temp.slots_on(d)), 'slot traz o closer');

  -- outro dia da semana não tem regra
  perform pg_temp.assert_eq((select count(*)::int from pg_temp.slots_on(d + 1)), 0, 'terça sem regra: zero slots');

  -- chamada de 45 min com passo de 30: o último slot tem que caber inteiro na janela
  update public.schedule_settings set call_minutes = 45, slot_step_minutes = 30;
  perform pg_temp.assert_eq((select count(*)::int from pg_temp.slots_on(d)), 5, 'call 45 passo 30: 5 slots (09:00 a 11:00)');
  perform pg_temp.assert_eq((select max(starts_at) from pg_temp.slots_on(d)), pg_temp.at_local(d, '11:00'), 'último slot de 45 min às 11:00');
  perform pg_temp.assert_eq((select max(ends_at) from pg_temp.slots_on(d)), pg_temp.at_local(d, '11:45'), 'termina às 11:45');

  -- passo de 60 min
  update public.schedule_settings set call_minutes = 30, slot_step_minutes = 60;
  perform pg_temp.assert_eq((select count(*)::int from pg_temp.slots_on(d)), 3, 'passo 60: 09, 10, 11');

  -- janela menor que a duração da call: nenhum slot
  update public.schedule_settings set call_minutes = 30, slot_step_minutes = 30;
  delete from public.availability_rules;
  perform pg_temp.mk_rule(a, 1, '09:00', '09:20');
  perform pg_temp.assert_eq((select count(*)::int from pg_temp.slots_on(d)), 0, 'janela de 20 min não comporta call de 30');

  -- janela exata: 1 slot
  delete from public.availability_rules;
  perform pg_temp.mk_rule(a, 1, '09:00', '09:30');
  perform pg_temp.assert_eq((select count(*)::int from pg_temp.slots_on(d)), 1, 'janela exata de 30 min: 1 slot');
end;
$$;
rollback;

-- 2) várias regras, vários closers, filtro por closer, closer inativo -------------
begin;
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana A');
  b uuid := pg_temp.mk_closer('Bia B');
  c uuid := pg_temp.mk_closer('Caio C', 1, false);
begin
  perform pg_temp.mk_rule(a, 1, '09:00', '12:00');
  perform pg_temp.mk_rule(a, 1, '14:00', '16:00');   -- manhã e tarde
  perform pg_temp.mk_rule(b, 1, '10:00', '11:00');
  perform pg_temp.mk_rule(c, 1, '09:00', '18:00');   -- inativo: nunca aparece

  perform pg_temp.assert_eq((select count(*)::int from pg_temp.slots_on(d, a)), 10, 'A: 6 pela manhã + 4 à tarde');
  perform pg_temp.assert_eq((select count(*)::int from pg_temp.slots_on(d, b)), 2, 'B: 2 slots');
  perform pg_temp.assert_eq((select count(*)::int from pg_temp.slots_on(d, c)), 0, 'closer inativo não oferece horário');
  perform pg_temp.assert_eq((select count(*)::int from pg_temp.slots_on(d)), 12, 'sem filtro: A + B');
  -- ordenado por horário e depois closer
  perform pg_temp.assert_true(
    (select array_agg(t.k order by t.rn) = array_agg(t.k order by t.st, t.cl)
       from (select starts_at as st, closer_id as cl, starts_at::text || closer_id::text as k,
                    row_number() over () as rn from pg_temp.slots_on(d)) t),
    'resultado ordenado por starts_at, closer_id');
  -- mesmo horário de dois closers aparece duas vezes (10:00 de A e de B)
  perform pg_temp.assert_eq(
    (select count(*)::int from pg_temp.slots_on(d) where starts_at = pg_temp.at_local(d, '10:00')), 2, '10:00 livre para A e B');

  -- regras sobrepostas do mesmo closer não duplicam o horário
  perform pg_temp.mk_rule(a, 1, '09:00', '10:00');
  perform pg_temp.assert_eq((select count(*)::int from pg_temp.slots_on(d, a)), 10, 'regra sobreposta não duplica slot');
end;
$$;
rollback;

-- 3) recorte pelo intervalo pedido ----------------------------------------------
begin;
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana A');
begin
  perform pg_temp.mk_rule(a, 1, '09:00', '12:00');
  -- o slot precisa caber inteiro no intervalo pedido
  perform pg_temp.assert_eq(
    (select count(*)::int from public.available_slots(pg_temp.at_local(d, '09:00'), pg_temp.at_local(d, '10:00'))),
    2, '[09:00,10:00] traz 09:00 e 09:30');
  perform pg_temp.assert_eq(
    (select count(*)::int from public.available_slots(pg_temp.at_local(d, '09:10'), pg_temp.at_local(d, '10:20'))),
    1, '[09:10,10:20] traz só 09:30 (10:00 termina depois)');
  perform pg_temp.assert_eq(
    (select count(*)::int from public.available_slots(pg_temp.at_local(d, '10:00'), pg_temp.at_local(d, '09:00'))),
    0, 'intervalo invertido: vazio');
end;
$$;
rollback;

-- 4) antecedência mínima ------------------------------------------------------------
begin;
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana A');
  v_hours int;
begin
  perform pg_temp.mk_rule(a, 1, '09:00', '12:00');
  -- antecedência que faz o corte cair entre 09:00 (exclusivo) e 10:00 (inclusivo) do dia D
  v_hours := floor(extract(epoch from (pg_temp.at_local(d, '10:00') - now())) / 3600)::int;
  update public.schedule_settings set min_notice_hours = v_hours, days_ahead = 30;

  perform pg_temp.assert_true(
    (select count(*) from pg_temp.slots_on(d) where starts_at < now() + v_hours * interval '1 hour') = 0,
    'nenhum slot antes da antecedência mínima');
  perform pg_temp.assert_eq((select count(*)::int from pg_temp.slots_on(d) where starts_at = pg_temp.at_local(d, '09:00')), 0,
    '09:00 cortado pela antecedência');
  perform pg_temp.assert_eq((select count(*)::int from pg_temp.slots_on(d) where starts_at = pg_temp.at_local(d, '10:00')), 1,
    '10:00 ainda livre');
  perform pg_temp.assert_true((select count(*) from pg_temp.slots_on(d)) between 1 and 5, 'sobraram alguns slots');

  -- antecedência enorme: nada
  update public.schedule_settings set min_notice_hours = 24 * 40;
  perform pg_temp.assert_eq((select count(*)::int from pg_temp.slots_on(d)), 0, 'antecedência de 40 dias zera tudo');
end;
$$;
rollback;

-- 5) dias à frente ---------------------------------------------------------------------
begin;
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana A');
begin
  -- regra todos os dias da semana
  for i in 0..6 loop perform pg_temp.mk_rule(a, i, '09:00', '17:00'); end loop;

  update public.schedule_settings set days_ahead = 1;
  perform pg_temp.assert_eq((select count(*)::int from pg_temp.slots_on(d)), 0, 'dia além de days_ahead não tem slot');

  update public.schedule_settings set days_ahead = 14;
  perform pg_temp.assert_true((select count(*) from pg_temp.slots_on(d)) > 0, 'dentro de days_ahead tem slot');
  perform pg_temp.assert_true(
    (select max(starts_at) from public.available_slots(now(), now() + interval '90 days')) <= now() + interval '14 days',
    'nunca oferece horário além de days_ahead');
  perform pg_temp.assert_true(
    (select min(starts_at) from public.available_slots(now() - interval '5 days', now() + interval '90 days')) >= now() + interval '2 hours',
    'nunca oferece horário no passado');
end;
$$;
rollback;

-- 6) bloqueios --------------------------------------------------------------------------
begin;
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana A');
  b uuid := pg_temp.mk_closer('Bia B');
begin
  perform pg_temp.mk_rule(a, 1, '09:00', '12:00');
  perform pg_temp.mk_rule(b, 1, '09:00', '12:00');

  -- bloqueio só do closer A, 10:00 a 10:30
  insert into public.blocks (closer_id, starts_at, ends_at, reason)
  values (a, pg_temp.at_local(d, '10:00'), pg_temp.at_local(d, '10:30'), 'consulta');
  perform pg_temp.assert_eq((select count(*)::int from pg_temp.slots_on(d, a) where starts_at = pg_temp.at_local(d, '10:00')), 0, 'A bloqueado às 10:00');
  perform pg_temp.assert_eq((select count(*)::int from pg_temp.slots_on(d, a) where starts_at = pg_temp.at_local(d, '09:30')), 1, 'A livre às 09:30');
  perform pg_temp.assert_eq((select count(*)::int from pg_temp.slots_on(d, a) where starts_at = pg_temp.at_local(d, '10:30')), 1, 'A livre às 10:30 (bloqueio termina às 10:30)');
  perform pg_temp.assert_eq((select count(*)::int from pg_temp.slots_on(d, b) where starts_at = pg_temp.at_local(d, '10:00')), 1, 'B não é afetado');

  -- bloqueio que pega metade de dois slots derruba os dois
  delete from public.blocks;
  insert into public.blocks (closer_id, starts_at, ends_at)
  values (a, pg_temp.at_local(d, '10:15'), pg_temp.at_local(d, '10:45'));
  perform pg_temp.assert_eq((select count(*)::int from pg_temp.slots_on(d, a) where starts_at in
    (pg_temp.at_local(d, '10:00'), pg_temp.at_local(d, '10:30'))), 0, 'bloqueio parcial derruba os dois slots tocados');
  perform pg_temp.assert_eq((select count(*)::int from pg_temp.slots_on(d, a)), 4, 'sobram 4 slots de A');

  -- bloqueio sem closer (feriado) vale para todos
  delete from public.blocks;
  insert into public.blocks (closer_id, starts_at, ends_at, reason)
  values (null, pg_temp.at_local(d, '00:00'), pg_temp.at_local(d + 1, '00:00'), 'feriado');
  perform pg_temp.assert_eq((select count(*)::int from pg_temp.slots_on(d)), 0, 'feriado zera o dia de todos os closers');
end;
$$;
rollback;

-- 7) intervalo entre calls (buffer) e agendamentos ---------------------------------------
begin;
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana A');
  l uuid := pg_temp.mk_lead('+5511900000010');
  v_appt uuid;
begin
  perform pg_temp.mk_rule(a, 1, '09:00', '12:00');
  insert into public.appointments (lead_id, closer_id, starts_at, ends_at)
  values (l, a, pg_temp.at_local(d, '10:00'), pg_temp.at_local(d, '10:30')) returning id into v_appt;

  -- buffer de 10 min: o agendamento ocupa 09:50 a 10:40, então 09:30 e 10:30 caem junto com 10:00
  perform pg_temp.assert_eq(
    (select array_agg(to_char(starts_at at time zone 'America/Sao_Paulo', 'HH24:MI') order by starts_at) from pg_temp.slots_on(d)),
    array['09:00','11:00','11:30'], 'com buffer 10: sobram 09:00, 11:00 e 11:30');

  -- sem buffer: só o horário exato fica ocupado
  update public.schedule_settings set buffer_minutes = 0;
  perform pg_temp.assert_eq(
    (select array_agg(to_char(starts_at at time zone 'America/Sao_Paulo', 'HH24:MI') order by starts_at) from pg_temp.slots_on(d)),
    array['09:00','09:30','10:30','11:00','11:30'], 'sem buffer: só 10:00 ocupado');

  -- agendamento cancelado ou reagendado libera o horário
  update public.schedule_settings set buffer_minutes = 10;
  update public.appointments set status = 'cancelado' where id = v_appt;
  perform pg_temp.assert_eq((select count(*)::int from pg_temp.slots_on(d)), 6, 'cancelado libera os 6 slots');
  update public.appointments set status = 'reagendado' where id = v_appt;
  perform pg_temp.assert_eq((select count(*)::int from pg_temp.slots_on(d)), 6, 'reagendado libera os 6 slots');
  update public.appointments set status = 'confirmado' where id = v_appt;
  perform pg_temp.assert_eq((select count(*)::int from pg_temp.slots_on(d)), 3, 'confirmado também ocupa');
end;
$$;
rollback;

-- 8) fuso do negócio ----------------------------------------------------------------------
begin;
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana A');
begin
  -- horário de Brasília (UTC-3): 09:00 local = 12:00 UTC
  perform pg_temp.mk_rule(a, 1, '09:00', '10:00');
  perform pg_temp.assert_eq(
    (select min(starts_at) at time zone 'UTC' from pg_temp.slots_on(d)),
    (d + time '12:00')::timestamp, '09:00 de Brasília é 12:00 UTC');
end;
$$;
rollback;

begin;
do $$
declare
  d date := pg_temp.next_dow(1, 3, 'Asia/Tokyo');
  a uuid := pg_temp.mk_closer('Ana A');
  n int;
begin
  -- Tóquio (UTC+9): segunda 07:00 local cai no DOMINGO à noite em UTC. A regra vale pelo dia local.
  update public.schedule_settings set timezone = 'Asia/Tokyo', days_ahead = 30;
  perform pg_temp.mk_rule(a, 1, '07:00', '09:00');
  select count(*) into n from pg_temp.slots_on(d, null, 'Asia/Tokyo');
  perform pg_temp.assert_eq(n, 4, 'Tóquio: 07:00 a 09:00 gera 4 slots no dia local');
  perform pg_temp.assert_eq(
    (select min(starts_at) at time zone 'UTC' from pg_temp.slots_on(d, null, 'Asia/Tokyo')),
    ((d - 1) + time '22:00')::timestamp, '07:00 de Tóquio é 22:00 UTC do dia anterior');
  -- o dia UTC anterior (domingo) NÃO ganha slots por causa do fuso
  perform pg_temp.assert_eq(
    (select count(*)::int from public.available_slots(
       pg_temp.at_local(d - 1, '00:00', 'Asia/Tokyo'), pg_temp.at_local(d, '00:00', 'Asia/Tokyo'))),
    0, 'domingo local de Tóquio sem regra: zero slots');
end;
$$;
rollback;

\echo OK 20_slots
