-- Rodada 4: reagendar mantém estágio final, telefone E.164 no banco,
-- um agendamento ativo por lead (inclusive ao reativar pelo painel).
\i tests/sql/_helpers.sql

begin;
select pg_temp.as_service() as _x \gset
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana A');
  lv uuid := pg_temp.mk_lead('+5511900000601');
  lp uuid := pg_temp.mk_lead('+5511900000602');
  ld uuid := pg_temp.mk_lead('+5511900000603');
  r jsonb;
  ap uuid;
  v_ok boolean;
begin
  perform pg_temp.mk_rule(a, 1, '09:00', '13:00');

  -- reagendar lead em 'venda' e em 'perdido' mantém o estágio
  update public.leads set stage = 'venda' where id = lv;
  update public.leads set stage = 'perdido' where id = lp;
  update public.leads set stage = 'desqualificado' where id = ld;
  r := public.book_slot(lv, pg_temp.at_local(d, '09:00'));
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'book em venda: ' || r::text);
  r := public.book_slot(lp, pg_temp.at_local(d, '10:00'));
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'book em perdido: ' || r::text);
  r := public.book_slot(ld, pg_temp.at_local(d, '11:00'));
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'book em desqualificado: ' || r::text);

  r := public.reschedule_slot((select id from public.appointments where lead_id = lv and status = 'agendado'), pg_temp.at_local(d, '12:00'));
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'reagenda venda: ' || r::text);
  perform pg_temp.assert_eq((select stage from public.leads where id = lv), 'venda', 'reagendar mantém venda');
  r := public.reschedule_slot((select id from public.appointments where lead_id = lp and status = 'agendado'), pg_temp.at_local(d, '09:00'));
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'reagenda perdido: ' || r::text);
  perform pg_temp.assert_eq((select stage from public.leads where id = lp), 'perdido', 'reagendar mantém perdido');
  r := public.reschedule_slot((select id from public.appointments where lead_id = ld and status = 'agendado'), pg_temp.at_local(d, '10:00'));
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'reagenda desqualificado: ' || r::text);
  perform pg_temp.assert_eq((select stage from public.leads where id = ld), 'desqualificado', 'reagendar mantém desqualificado');
  -- e a falha de reagendar também não mexe
  r := public.reschedule_slot((select id from public.appointments where lead_id = lv and status = 'agendado'), pg_temp.at_local(d, '09:00'));
  perform pg_temp.assert_eq(r ->> 'error', 'slot_indisponivel', 'slot ocupado');
  perform pg_temp.assert_eq((select stage from public.leads where id = lv), 'venda', 'falha mantém venda');

  -- telefone sem '+' é recusado pelo CHECK E.164
  v_ok := false;
  begin
    insert into public.leads (phone) values ('5511900000699');
  exception when check_violation then v_ok := true; end;
  perform pg_temp.assert_true(v_ok, 'phone sem + recusado pelo CHECK');
  v_ok := false;
  begin
    insert into public.leads (phone) values ('5511900000699@s.whatsapp.net');
  exception when check_violation then v_ok := true; end;
  perform pg_temp.assert_true(v_ok, 'JID cru recusado pelo CHECK');
  v_ok := false;
  begin
    update public.leads set phone = '11900000601' where id = lv;
  exception when check_violation then v_ok := true; end;
  perform pg_temp.assert_true(v_ok, 'update para phone sem + recusado');
end;
$$;

-- segundo agendamento ativo do mesmo lead, via set_appointment_status, é recusado
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := (select id from public.closers where name = 'Ana A');
  l uuid := pg_temp.mk_lead('+5511900000604');
  r jsonb;
  apa uuid;
  apb uuid;
begin
  insert into auth.users (id, email) values ('00000000-0000-0000-0000-0000000000dd', 'dono4@demo.invalid');
  insert into public.panel_users (user_id, email) values ('00000000-0000-0000-0000-0000000000dd', 'dono4@demo.invalid');
  r := public.book_slot(l, pg_temp.at_local(d, '11:00'));
  apa := (r ->> 'appointment_id')::uuid;
  perform set_config('request.jwt.claims', '{"role":"authenticated","sub":"00000000-0000-0000-0000-0000000000dd"}', true);
  set local role authenticated;
  r := public.set_appointment_status(apa, 'cancelado');
  reset role;
  perform pg_temp.assert_eq(r ->> 'ok', 'true', 'cancela a call A');
  perform pg_temp.as_service();
  r := public.book_slot(l, pg_temp.at_local(d, '11:00'));
  apb := (r ->> 'appointment_id')::uuid;
  perform pg_temp.assert_true(apb is not null, 'call B agendada: ' || r::text);
  perform set_config('request.jwt.claims', '{"role":"authenticated","sub":"00000000-0000-0000-0000-0000000000dd"}', true);
  set local role authenticated;
  r := public.set_appointment_status(apa, 'confirmado');
  reset role;
  perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'lead_ja_agendado'), 'reativar A com B ativa: lead_ja_agendado');
  perform pg_temp.assert_eq((select status from public.appointments where id = apa), 'cancelado', 'A continua cancelada');
  perform pg_temp.assert_eq((select count(*)::int from public.appointments where lead_id = l and status in ('agendado', 'confirmado')), 1, 'um ativo só');
  -- o índice parcial é a última barreira, mesmo fora das RPCs
  begin
    insert into public.appointments (lead_id, closer_id, starts_at, ends_at)
      values (l, a, pg_temp.at_local(d, '13:00') - interval '30 minutes', pg_temp.at_local(d, '13:00'));
    perform pg_temp.assert_true(false, 'índice parcial deveria barrar o 2º ativo');
  exception when unique_violation then null; end;
end;
$$;
rollback;

\echo OK 48_rodada4
