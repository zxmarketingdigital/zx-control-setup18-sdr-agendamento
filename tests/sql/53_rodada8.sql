-- Rodada 8: reschedule_slot zera as flags de envio na mesma transação; sem x-forwarded-for o IP
-- desconhecido não entra no teto por IP (valem só os tetos por telefone e global).
\i tests/sql/_helpers.sql

-- ---- remarcar zera as quatro flags (no antigo e no novo) ----
begin;
select pg_temp.as_service() as _x \gset
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana A');
  l1 uuid := pg_temp.mk_lead('+5511900005301');
  r jsonb;
  ap1 uuid;
  v_new uuid;
begin
  perform pg_temp.mk_rule(a, 1, '09:00', '12:00');
  r := public.book_slot(l1, pg_temp.at_local(d, '09:00'));
  ap1 := (r ->> 'appointment_id')::uuid;
  update public.appointments
     set reminder_24h_at = now(), reminder_2h_at = now(), handoff_sent_at = now(), handoff_1h_sent_at = now()
   where id = ap1;

  r := public.reschedule_slot(ap1, pg_temp.at_local(d, '10:00'));
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'reschedule ok: ' || r::text);
  v_new := (r ->> 'appointment_id')::uuid;
  perform pg_temp.assert_eq((select count(*)::int from public.appointments
    where id in (ap1, v_new) and (reminder_24h_at is not null or reminder_2h_at is not null
      or handoff_sent_at is not null or handoff_1h_sent_at is not null)), 0, 'remarcar zera as 4 flags (antigo e novo)');
end;
$$;
rollback;

-- ---- remarcação que falha não zera nada (mesma transação) ----
begin;
select pg_temp.as_service() as _x \gset
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana A');
  l1 uuid := pg_temp.mk_lead('+5511900005311');
  r jsonb;
  ap1 uuid;
begin
  perform pg_temp.mk_rule(a, 1, '09:00', '12:00');
  r := public.book_slot(l1, pg_temp.at_local(d, '09:00'));
  ap1 := (r ->> 'appointment_id')::uuid;
  update public.appointments set reminder_24h_at = now(), handoff_sent_at = now() where id = ap1;
  r := public.reschedule_slot(ap1, pg_temp.at_local(d, '15:00'));
  perform pg_temp.assert_eq(r ->> 'error', 'fora_da_agenda', 'fora da grade');
  perform pg_temp.assert_true((select reminder_24h_at is not null and handoff_sent_at is not null
    from public.appointments where id = ap1), 'falha preserva as flags do antigo');
end;
$$;
rollback;

-- ---- sem x-forwarded-for: IP desconhecido não bate no teto por IP ----
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
    public_max_per_hour = 1000, public_max_per_ip_day = 1, public_max_per_phone_day = 1000;
  perform set_config('request.headers', '', true);
  for n in 1..4 loop
    set local role anon;
    r := public.public_book('demo', 'Ana Souza', 'ana@exemplo.com', '+55119000053' || lpad(n::text, 2, '0'), pg_temp.at_local(d, '15:00'));
    reset role;
    perform pg_temp.assert_true(r ->> 'error' is distinct from 'limite_atingido', 'sem header, tentativa ' || n || ' não bate no teto por IP: ' || r::text);
  end loop;
  perform pg_temp.assert_eq((select count(*)::int from public.public_book_attempts), 4, 'as 4 tentativas foram registradas');
  -- com header o teto por IP continua valendo
  perform set_config('request.headers', '{"x-forwarded-for":"8.8.4.4"}', true);
  for n in 1..2 loop
    set local role anon;
    r := public.public_book('demo', 'Ana Souza', 'ana@exemplo.com', '+55119000054' || lpad(n::text, 2, '0'), pg_temp.at_local(d, '15:00'));
    reset role;
  end loop;
  perform pg_temp.assert_eq(r ->> 'error', 'limite_atingido', 'com header o teto por IP segue valendo');
end;
$$;
rollback;
