-- set_appointment_status: resultado da call em uma transação (appointment + lead + funil).
\i tests/sql/_helpers.sql

begin;
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana A');
  l uuid := pg_temp.mk_lead('+5511900000401');
  ap uuid;
  r jsonb;
  v_ok boolean;
  n_antes int;
begin
  insert into auth.users (id, email) values
    ('00000000-0000-0000-0000-0000000000bb', 'dono@demo.invalid'),
    ('00000000-0000-0000-0000-0000000000cc', 'impostor@demo.invalid');
  insert into public.panel_users (user_id, email) values ('00000000-0000-0000-0000-0000000000bb', 'dono@demo.invalid');
  insert into public.appointments (lead_id, closer_id, starts_at, ends_at)
    values (l, a, pg_temp.at_local(d, '09:00'), pg_temp.at_local(d, '09:30')) returning id into ap;

  -- quem não é do painel recebe 42501 e nada muda
  perform set_config('request.jwt.claims', '{"role":"authenticated","sub":"00000000-0000-0000-0000-0000000000cc"}', true);
  set local role authenticated;
  v_ok := false;
  begin r := public.set_appointment_status(ap, 'feito');
  exception when insufficient_privilege then v_ok := true; end;
  reset role;
  perform pg_temp.assert_true(v_ok, 'não-painel recebe 42501 em set_appointment_status');
  perform pg_temp.assert_eq((select status from public.appointments where id = ap), 'agendado', 'não-painel não altera o agendamento');
  -- service_role sem estar em panel_users também é barrado (a função é do painel)
  perform pg_temp.as_service();
  v_ok := false;
  begin r := public.set_appointment_status(ap, 'feito');
  exception when insufficient_privilege then v_ok := true; end;
  perform pg_temp.assert_true(v_ok, 'service_role fora do painel recebe 42501');
  perform pg_temp.assert_true(not has_function_privilege('anon', 'public.set_appointment_status(uuid,text,numeric)', 'execute'), 'anon NÃO executa set_appointment_status');
  perform pg_temp.assert_true(has_function_privilege('authenticated', 'public.set_appointment_status(uuid,text,numeric)', 'execute'), 'authenticated executa set_appointment_status');

  -- painel: confirmado
  perform set_config('request.jwt.claims', '{"role":"authenticated","sub":"00000000-0000-0000-0000-0000000000bb"}', true);
  set local role authenticated;
  r := public.set_appointment_status(ap, 'confirmado');
  reset role;
  perform pg_temp.assert_eq(r, jsonb_build_object('ok', true, 'appointment_id', ap, 'status', 'confirmado', 'lead_stage', 'confirmado'), 'confirmado: retorno');
  perform pg_temp.assert_eq((select status from public.appointments where id = ap), 'confirmado', 'appointment atualizado');
  perform pg_temp.assert_eq((select stage from public.leads where id = l), 'confirmado', 'lead atualizado');
  perform pg_temp.assert_eq((select count(*)::int from public.funnel_events where lead_id = l and stage = 'confirmado'), 1, 'funil registrou confirmado');

  -- venda grava valor, estágio e funil juntos
  set local role authenticated;
  r := public.set_appointment_status(ap, 'venda', 1500.50);
  reset role;
  perform pg_temp.assert_eq(r ->> 'lead_stage', 'venda', 'venda: estágio no retorno');
  perform pg_temp.assert_eq((select sale_value from public.appointments where id = ap), 1500.50, 'valor da venda gravado');
  perform pg_temp.assert_eq((select stage from public.leads where id = l), 'venda', 'lead em venda');
  perform pg_temp.assert_eq((select count(*)::int from public.funnel_events where lead_id = l and stage = 'venda'), 1, 'funil registrou venda');

  -- lead em venda só sai de venda por ação explícita no painel de leads (rodada 5):
  -- mudar a própria call de venda para feito/no_show não rebaixa o estágio.
  set local role authenticated;
  r := public.set_appointment_status(ap, 'feito');
  reset role;
  perform pg_temp.assert_eq(r ->> 'lead_stage', 'venda', 'lead em venda continua em venda ao marcar feito');
  set local role authenticated;
  r := public.set_appointment_status(ap, 'no_show');
  reset role;
  perform pg_temp.assert_eq(r ->> 'lead_stage', 'venda', 'lead em venda continua em venda ao marcar no_show');
  -- lead sem venda: feito -> compareceu, no_show -> no_show
  update public.leads set stage = 'confirmado' where id = l;
  set local role authenticated;
  r := public.set_appointment_status(ap, 'feito');
  reset role;
  perform pg_temp.assert_eq(r ->> 'lead_stage', 'compareceu', 'feito vira compareceu');
  set local role authenticated;
  r := public.set_appointment_status(ap, 'no_show');
  reset role;
  perform pg_temp.assert_eq(r ->> 'lead_stage', 'no_show', 'no_show vira no_show');

  -- erros
  set local role authenticated;
  perform pg_temp.assert_eq(public.set_appointment_status(ap, 'qualquer'), jsonb_build_object('ok', false, 'error', 'status_invalido'), 'status inválido');
  perform pg_temp.assert_eq(public.set_appointment_status(gen_random_uuid(), 'feito'), jsonb_build_object('ok', false, 'error', 'agendamento_inexistente'), 'agendamento inexistente');
  reset role;
  perform pg_temp.assert_eq((select status from public.appointments where id = ap), 'no_show', 'erros não alteram nada');
end;
$$;
rollback;

\echo OK 47_set_status
