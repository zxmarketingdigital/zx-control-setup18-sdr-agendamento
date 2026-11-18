-- reschedule_slot: reagendamento atômico (marca o antigo + reserva o novo, ou nada muda).
\i tests/sql/_helpers.sql

begin;
select pg_temp.as_service() as _x \gset
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana A');
  l1 uuid := pg_temp.mk_lead('+5511900000301');
  l2 uuid := pg_temp.mk_lead('+5511900000302');
  r jsonb;
  r2 jsonb;
  ap1 uuid;
  ap2 uuid;
  v_ap public.appointments%rowtype;
begin
  perform pg_temp.mk_rule(a, 1, '09:00', '12:00');
  r := public.book_slot(l1, pg_temp.at_local(d, '09:00'), 'pagina');
  ap1 := (r ->> 'appointment_id')::uuid;
  r := public.book_slot(l2, pg_temp.at_local(d, '11:00'));
  ap2 := (r ->> 'appointment_id')::uuid;

  -- sucesso: antigo vira reagendado, novo nasce ativo com a origem herdada e a nota
  r := public.reschedule_slot(ap1, pg_temp.at_local(d, '10:00'));
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'reschedule ok: ' || r::text);
  perform pg_temp.assert_eq((r ->> 'old_appointment_id')::uuid, ap1, 'devolve old_appointment_id');
  perform pg_temp.assert_eq((r ->> 'starts_at')::timestamptz, pg_temp.at_local(d, '10:00'), 'novo horário devolvido');
  perform pg_temp.assert_eq((select status from public.appointments where id = ap1), 'reagendado', 'antigo marcado reagendado');
  select * into v_ap from public.appointments where id = (r ->> 'appointment_id')::uuid;
  perform pg_temp.assert_eq(v_ap.status, 'agendado', 'novo está agendado');
  perform pg_temp.assert_eq(v_ap.source, 'pagina', 'source herdada');
  perform pg_temp.assert_true(v_ap.notes like 'reagendado de %', 'nota "reagendado de ..."');
  perform pg_temp.assert_eq((select stage from public.leads where id = l1), 'agendado', 'lead volta a agendado');

  -- slot ocupado: devolve erro e o antigo continua INTACTO
  r := public.book_slot(l1, pg_temp.at_local(d, '11:30')) ;   -- lead já tem ativo: só para garantir o estado
  perform pg_temp.assert_eq(r ->> 'error', 'lead_ja_agendado', 'sanidade: lead já agendado');
  r2 := public.reschedule_slot((select id from public.appointments where lead_id = l1 and status = 'agendado'), pg_temp.at_local(d, '11:00'));
  perform pg_temp.assert_eq(r2 ->> 'error', 'slot_indisponivel', 'slot ocupado: slot_indisponivel');
  perform pg_temp.assert_eq((select count(*)::int from public.appointments where lead_id = l1 and status = 'agendado'), 1, 'antigo continua ativo');
  perform pg_temp.assert_eq((select starts_at from public.appointments where lead_id = l1 and status = 'agendado'),
                            pg_temp.at_local(d, '10:00'), 'antigo no mesmo horário');
  perform pg_temp.assert_eq((select count(*)::int from public.appointments where lead_id = l1), 2, 'nenhum agendamento novo criado na falha');
  perform pg_temp.assert_eq((select stage from public.leads where id = l1), 'agendado', 'estágio do lead não mexeu na falha');

  -- fora da agenda
  r2 := public.reschedule_slot(ap2, pg_temp.at_local(d, '15:00'));
  perform pg_temp.assert_eq(r2 ->> 'error', 'fora_da_agenda', 'fora da agenda');
  perform pg_temp.assert_eq((select status from public.appointments where id = ap2), 'agendado', 'antigo intacto (fora da agenda)');

  -- inexistente e inativo
  r2 := public.reschedule_slot(gen_random_uuid(), pg_temp.at_local(d, '10:00'));
  perform pg_temp.assert_eq(r2 ->> 'error', 'agendamento_inexistente', 'inexistente');
  r2 := public.reschedule_slot(ap1, pg_temp.at_local(d, '09:00'));
  perform pg_temp.assert_eq(r2 ->> 'error', 'agendamento_inativo', 'já reagendado = inativo');

  -- mesmo horário do próprio agendamento: o antigo libera o slot, então é permitido
  r := public.reschedule_slot(ap2, pg_temp.at_local(d, '11:00'));
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'reagendar para o mesmo horário: ' || r::text);
end;
$$;

-- quem não é de confiança recebe 42501 e nada muda
do $$
declare
  d date := pg_temp.next_dow(1);
  ap uuid := (select id from public.appointments where status = 'agendado' limit 1);
  v_ok boolean := false;
  n int;
begin
  perform set_config('request.jwt.claims', '{"role":"authenticated","sub":"00000000-0000-0000-0000-0000000000aa"}', true);
  set local role authenticated;
  begin
    perform public.reschedule_slot(ap, pg_temp.at_local(d, '10:30'));
  exception when insufficient_privilege then v_ok := true; end;
  reset role;
  perform pg_temp.assert_true(v_ok, 'não-painel recebe 42501 em reschedule_slot');
  perform pg_temp.assert_eq((select status from public.appointments where id = ap), 'agendado', 'reschedule barrado não alterou nada');
end;
$$;
rollback;

\echo OK 35_reschedule
