-- Rodada 5: marcador do setup, normalização de telefone só para forma inequívoca,
-- estágio 'venda' protegido contra outra call e sale_value preservado.
\i tests/sql/_helpers.sql

-- marcador gravado pela migration
do $$
begin
  perform pg_temp.assert_eq(obj_description('public.agent_config'::regclass, 'pg_class'), 'setup18-sdr', 'marcador setup18-sdr em agent_config');
end;
$$;

-- ---- telefones antigos: JID e só-dígitos viram E.164 (o ambíguo aborta: ver 51_rodada6.sql) ----
alter table public.leads drop constraint leads_phone_e164;
insert into public.leads (id, phone, name) values
  ('00000000-0000-0000-0000-0000000049a1', '5511988880001@s.whatsapp.net', 'jid'),
  ('00000000-0000-0000-0000-0000000049a2', '5511988880002', 'digitos'),
  ('00000000-0000-0000-0000-0000000049a5', '5511988880005:12@s.whatsapp.net', 'jid com dispositivo');
insert into public.outbox (lead_id, phone, body, status) values
  ('00000000-0000-0000-0000-0000000049a1', '+5511988880001', 'ok', 'pendente');

\i supabase/migrations/20261118000001_sdr_agendamento.sql

do $$
begin
  perform pg_temp.assert_eq((select phone from public.leads where id = '00000000-0000-0000-0000-0000000049a1'), '+5511988880001', 'JID vira +E164');
  perform pg_temp.assert_eq((select phone from public.leads where id = '00000000-0000-0000-0000-0000000049a2'), '+5511988880002', '12-15 dígitos com DDI vira +E164');
  perform pg_temp.assert_eq((select phone from public.leads where id = '00000000-0000-0000-0000-0000000049a5'), '+5511988880005', 'JID com sufixo de dispositivo vira +E164');
  perform pg_temp.assert_eq((select status from public.outbox where phone = '+5511988880001'), 'pendente', 'outbox de número E.164 segue pendente');
  perform pg_temp.assert_true((select convalidated from pg_constraint where conname = 'leads_phone_e164'), 'CHECK validado');
end;
$$;
delete from public.outbox where phone = '+5511988880001';
delete from public.leads where id::text like '00000000-0000-0000-0000-0000000049a%';

-- ---- venda: outra call do lead não rebaixa o estágio; sale_value null preserva ----
begin;
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana V');
  l uuid := pg_temp.mk_lead('+5511900004901');
  ap1 uuid;
  ap2 uuid;
  r jsonb;
begin
  insert into auth.users (id, email) values ('00000000-0000-0000-0000-0000000049bb', 'dono49@demo.invalid');
  insert into public.panel_users (user_id, email) values ('00000000-0000-0000-0000-0000000049bb', 'dono49@demo.invalid');
  insert into public.appointments (lead_id, closer_id, starts_at, ends_at)
    values (l, a, pg_temp.at_local(d, '09:00'), pg_temp.at_local(d, '09:30')) returning id into ap1;
  perform set_config('request.jwt.claims', '{"role":"authenticated","sub":"00000000-0000-0000-0000-0000000049bb"}', true);
  set local role authenticated;
  r := public.set_appointment_status(ap1, 'venda', 2000);
  reset role;
  perform pg_temp.assert_eq((select stage from public.leads where id = l), 'venda', 'lead em venda');

  -- call 2 (a venda não a impede) marcada no_show/feito não tira o lead de venda
  insert into public.appointments (lead_id, closer_id, starts_at, ends_at, status)
    values (l, a, pg_temp.at_local(d, '11:00'), pg_temp.at_local(d, '11:30'), 'feito') returning id into ap2;
  set local role authenticated;
  r := public.set_appointment_status(ap2, 'no_show');
  reset role;
  perform pg_temp.assert_eq((select status from public.appointments where id = ap2), 'no_show', 'call 2 virou no_show');
  perform pg_temp.assert_eq((select stage from public.leads where id = l), 'venda', 'no_show em call 2 não tira o lead de venda');
  set local role authenticated;
  r := public.set_appointment_status(ap2, 'feito');
  reset role;
  perform pg_temp.assert_eq((select stage from public.leads where id = l), 'venda', 'feito em call 2 não tira o lead de venda');

  -- p_sale_value null preserva o valor atual
  set local role authenticated;
  r := public.set_appointment_status(ap1, 'venda');
  reset role;
  perform pg_temp.assert_eq((select sale_value from public.appointments where id = ap1), 2000.00, 'sale_value null preserva o valor');
  set local role authenticated;
  r := public.set_appointment_status(ap1, 'venda', 2500);
  reset role;
  perform pg_temp.assert_eq((select sale_value from public.appointments where id = ap1), 2500.00, 'sale_value novo substitui');
end;
$$;
rollback;

\echo OK 49_rodada5
