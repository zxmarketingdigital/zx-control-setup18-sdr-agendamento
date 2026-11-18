-- Agendamento: book_slot, dupla reserva barrada, rodízio e peso, fluxo de reagendamento.
\i tests/sql/_helpers.sql

-- 1) caminho feliz e erros literais do contrato -----------------------------------------
begin;
select pg_temp.as_service() as _x \gset
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana A');
  l1 uuid := pg_temp.mk_lead('+5511900000021');
  l2 uuid := pg_temp.mk_lead('+5511900000022');
  l3 uuid := pg_temp.mk_lead('+5511900000023');
  r jsonb;
begin
  perform pg_temp.mk_rule(a, 1, '09:00', '12:00');

  -- sucesso
  r := public.book_slot(l1, pg_temp.at_local(d, '09:00'));
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'book_slot ok: ' || r::text);
  perform pg_temp.assert_eq((r ->> 'closer_id')::uuid, a, 'closer devolvido');
  perform pg_temp.assert_eq(r ->> 'closer_name', 'Ana A', 'nome do closer devolvido');
  perform pg_temp.assert_eq((r ->> 'starts_at')::timestamptz, pg_temp.at_local(d, '09:00'), 'starts_at devolvido');
  perform pg_temp.assert_eq((r ->> 'ends_at')::timestamptz, pg_temp.at_local(d, '09:30'), 'ends_at = início + call_minutes');
  perform pg_temp.assert_true((r ->> 'appointment_id') is not null, 'appointment_id devolvido');
  perform pg_temp.assert_eq((select source from public.appointments where id = (r ->> 'appointment_id')::uuid), 'agente', 'origem padrão: agente');
  perform pg_temp.assert_eq((select status from public.appointments where id = (r ->> 'appointment_id')::uuid), 'agendado', 'status padrão');
  perform pg_temp.assert_eq((select stage from public.leads where id = l1), 'agendado', 'lead vai para agendado');
  perform pg_temp.assert_eq((select closer_id from public.leads where id = l1), a, 'lead recebe o closer');
  perform pg_temp.assert_true(exists (select 1 from public.funnel_events where lead_id = l1 and stage = 'agendado'), 'funil registra agendado');

  -- lead já agendado (mesmo pedindo outro horário livre)
  r := public.book_slot(l1, pg_temp.at_local(d, '11:00'));
  perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'lead_ja_agendado'), 'lead_ja_agendado');

  -- lead inexistente
  r := public.book_slot(gen_random_uuid(), pg_temp.at_local(d, '11:00'));
  perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'lead_inexistente'), 'lead_inexistente');

  -- horário tomado (mesmo horário, mesmo único closer)
  r := public.book_slot(l2, pg_temp.at_local(d, '09:00'));
  perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'slot_indisponivel'), 'mesmo horário: slot_indisponivel');
  -- dentro do buffer de 10 min: também indisponível
  r := public.book_slot(l2, pg_temp.at_local(d, '09:30'));
  perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'slot_indisponivel'), 'colado no agendamento (buffer): slot_indisponivel');
  perform pg_temp.assert_eq((select stage from public.leads where id = l2), 'novo', 'falha não muda o lead');
  perform pg_temp.assert_eq((select count(*)::int from public.appointments where lead_id = l2), 0, 'falha não cria agendamento');

  -- bloqueio: slot_indisponivel
  insert into public.blocks (closer_id, starts_at, ends_at) values (a, pg_temp.at_local(d, '10:00'), pg_temp.at_local(d, '10:30'));
  r := public.book_slot(l2, pg_temp.at_local(d, '10:00'));
  perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'slot_indisponivel'), 'bloqueio: slot_indisponivel');

  -- fora da agenda: antes da regra, depois da regra, fora da grade, dia sem regra, passado, além de days_ahead
  foreach r in array array[
    public.book_slot(l2, pg_temp.at_local(d, '08:00')),
    public.book_slot(l2, pg_temp.at_local(d, '12:00')),
    public.book_slot(l2, pg_temp.at_local(d, '10:15')),
    public.book_slot(l2, pg_temp.at_local(d + 1, '10:00')),
    public.book_slot(l2, now() - interval '1 day'),
    public.book_slot(l2, now() + interval '60 days'),
    public.book_slot(l2, null)
  ] loop
    perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'fora_da_agenda'), 'fora_da_agenda: ' || r::text);
  end loop;

  -- antecedência mínima
  update public.schedule_settings set min_notice_hours = 24 * 30, days_ahead = 60;
  r := public.book_slot(l2, pg_temp.at_local(d, '11:00'));
  perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'fora_da_agenda'), 'antes da antecedência mínima: fora_da_agenda');
  update public.schedule_settings set min_notice_hours = 2, days_ahead = 14;

  -- origem e observação informadas
  r := public.book_slot(l3, pg_temp.at_local(d, '11:00'), 'manual', null, 'indicado pelo dono');
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'agendamento manual ok');
  perform pg_temp.assert_eq((select source from public.appointments where id = (r ->> 'appointment_id')::uuid), 'manual', 'origem manual gravada');
  perform pg_temp.assert_eq((select notes from public.appointments where id = (r ->> 'appointment_id')::uuid), 'indicado pelo dono', 'notas gravadas');
end;
$$;
rollback;

-- 2) a constraint de exclusão é a última barreira ----------------------------------------
begin;
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana A');
  b uuid := pg_temp.mk_closer('Bia B');
  l1 uuid := pg_temp.mk_lead('+5511900000031');
  l2 uuid := pg_temp.mk_lead('+5511900000032');
  l3 uuid := pg_temp.mk_lead('+5511900000033');
  l4 uuid := pg_temp.mk_lead('+5511900000034');
begin
  insert into public.appointments (lead_id, closer_id, starts_at, ends_at)
  values (l1, a, pg_temp.at_local(d, '10:00'), pg_temp.at_local(d, '10:30'));

  -- mesmo closer, intervalo sobreposto: barrado pelo banco, sem passar por nenhuma função
  begin
    insert into public.appointments (lead_id, closer_id, starts_at, ends_at)
    values (l2, a, pg_temp.at_local(d, '10:15'), pg_temp.at_local(d, '10:45'));
    raise exception 'FALHOU: constraint deixou passar dupla reserva';
  exception when exclusion_violation then null;
  end;
  -- mesmo horário exato
  begin
    insert into public.appointments (lead_id, closer_id, starts_at, ends_at)
    values (l2, a, pg_temp.at_local(d, '10:00'), pg_temp.at_local(d, '10:30'));
    raise exception 'FALHOU: constraint deixou passar mesmo horário';
  exception when exclusion_violation then null;
  end;
  -- closer diferente no mesmo horário: permitido
  insert into public.appointments (lead_id, closer_id, starts_at, ends_at)
  values (l2, b, pg_temp.at_local(d, '10:00'), pg_temp.at_local(d, '10:30'));
  -- encostado (fim = início do próximo): permitido, o intervalo é [)
  insert into public.appointments (lead_id, closer_id, starts_at, ends_at)
  values (l3, a, pg_temp.at_local(d, '10:30'), pg_temp.at_local(d, '11:00'));
  -- agendamento cancelado não conta
  insert into public.appointments (lead_id, closer_id, starts_at, ends_at, status)
  values (l4, a, pg_temp.at_local(d, '10:00'), pg_temp.at_local(d, '10:30'), 'cancelado');
  -- reativar um cancelado em cima de um ativo: barrado
  begin
    update public.appointments set status = 'agendado'
    where closer_id = a and status = 'cancelado';
    raise exception 'FALHOU: reativar cancelado sobreposto deveria falhar';
  exception when exclusion_violation then null;
  end;
end;
$$;
rollback;

-- 3) com 2 closers: o segundo lead cai no outro closer; o terceiro não tem vaga ---------------
begin;
select pg_temp.as_service() as _x \gset
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana A');
  b uuid := pg_temp.mk_closer('Bia B');
  l1 uuid := pg_temp.mk_lead('+5511900000041');
  l2 uuid := pg_temp.mk_lead('+5511900000042');
  l3 uuid := pg_temp.mk_lead('+5511900000043');
  r1 jsonb; r2 jsonb; r3 jsonb;
begin
  perform pg_temp.mk_rule(a, 1, '09:00', '12:00');
  perform pg_temp.mk_rule(b, 1, '09:00', '12:00');
  r1 := public.book_slot(l1, pg_temp.at_local(d, '09:00'));
  r2 := public.book_slot(l2, pg_temp.at_local(d, '09:00'));
  r3 := public.book_slot(l3, pg_temp.at_local(d, '09:00'));
  perform pg_temp.assert_eq(r1 ->> 'closer_name', 'Ana A', '1º lead: desempate por nome');
  perform pg_temp.assert_eq(r2 ->> 'closer_name', 'Bia B', '2º lead: vai para o outro closer');
  perform pg_temp.assert_eq(r3, jsonb_build_object('ok', false, 'error', 'slot_indisponivel'), '3º lead: sem closer livre');

  -- p_closer explícito: pede a Bia num horário em que só ela está livre... e o closer pedido ocupado
  r1 := public.book_slot(l3, pg_temp.at_local(d, '10:00'), 'agente', b);
  perform pg_temp.assert_eq((r1 ->> 'closer_id')::uuid, b, 'p_closer respeitado');
end;
$$;
rollback;

begin;
select pg_temp.as_service() as _x \gset
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana A');
  b uuid := pg_temp.mk_closer('Bia B');
  l1 uuid := pg_temp.mk_lead('+5511900000051');
  l2 uuid := pg_temp.mk_lead('+5511900000052');
  r jsonb;
begin
  perform pg_temp.mk_rule(a, 1, '09:00', '12:00');
  perform pg_temp.mk_rule(b, 1, '09:00', '12:00');
  -- a Ana está ocupada às 09:00; pedir a Ana nesse horário falha, mesmo com a Bia livre
  perform public.book_slot(l1, pg_temp.at_local(d, '09:00'), 'agente', a);
  r := public.book_slot(l2, pg_temp.at_local(d, '09:00'), 'agente', a);
  perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'slot_indisponivel'), 'closer pedido ocupado: slot_indisponivel');
  -- closer pedido que não atende naquele dia: fora_da_agenda
  r := public.book_slot(l2, pg_temp.at_local(d + 1, '09:00'), 'agente', b);
  perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'fora_da_agenda'), 'closer sem regra naquele dia: fora_da_agenda');
end;
$$;
rollback;

-- 4) rodízio e peso -------------------------------------------------------------------------------
begin;
select pg_temp.as_service() as _x \gset
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana A', 3);
  b uuid := pg_temp.mk_closer('Bia B', 1);
  h text[] := array['09:00','09:30','10:00','10:30','11:00','11:30','14:00','14:30'];
  l uuid;
  r jsonb;
  seq text := '';
begin
  perform pg_temp.mk_rule(a, 1, '09:00', '18:00');
  perform pg_temp.mk_rule(b, 1, '09:00', '18:00');
  update public.schedule_settings set buffer_minutes = 0;   -- horários distintos, sem interferência

  -- rodízio: pesos 3 e 1 são ignorados, alterna A, B, A, B...
  for i in 1..8 loop
    l := pg_temp.mk_lead('+55119000001' || lpad(i::text, 2, '0'));
    r := public.book_slot(l, pg_temp.at_local(d, h[i]::time));
    perform pg_temp.assert_true((r ->> 'ok')::boolean, 'rodízio ok ' || i);
    seq := seq || left(r ->> 'closer_name', 1);
  end loop;
  perform pg_temp.assert_eq(seq, 'ABABABAB', 'rodízio alterna');

  -- peso 3:1: de 8 agendamentos, a Ana (peso 3) fica com 6 e a Bia com 2
  delete from public.appointments;
  update public.schedule_settings set distribution = 'peso';
  seq := '';
  for i in 1..8 loop
    l := pg_temp.mk_lead('+55119000002' || lpad(i::text, 2, '0'));
    r := public.book_slot(l, pg_temp.at_local(d, h[i]::time));
    seq := seq || left(r ->> 'closer_name', 1);
  end loop;
  perform pg_temp.assert_eq(seq, 'ABAAABAA', 'peso 3:1 distribui proporcionalmente');
  perform pg_temp.assert_eq((select count(*)::int from public.appointments where closer_id = a), 6, 'Ana com 6');
  perform pg_temp.assert_eq((select count(*)::int from public.appointments where closer_id = b), 2, 'Bia com 2');
end;
$$;
rollback;

-- 5) reagendar: antigo vira reagendado, o lead agenda de novo --------------------------------------
begin;
select pg_temp.as_service() as _x \gset
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana A');
  l uuid := pg_temp.mk_lead('+5511900000061');
  r jsonb;
  v_old uuid;
begin
  perform pg_temp.mk_rule(a, 1, '09:00', '12:00');
  r := public.book_slot(l, pg_temp.at_local(d, '09:00'));
  v_old := (r ->> 'appointment_id')::uuid;

  update public.appointments set status = 'reagendado' where id = v_old;
  perform pg_temp.assert_eq((select stage from public.leads where id = l), 'reagendado', 'lead fica reagendado');
  -- o horário antigo volta a ficar livre
  perform pg_temp.assert_eq(
    (select count(*)::int from public.available_slots(pg_temp.at_local(d, '09:00'), pg_temp.at_local(d, '09:30'))), 1, 'horário antigo livre');

  r := public.book_slot(l, pg_temp.at_local(d, '11:00'));
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'novo agendamento após reagendar');
  perform pg_temp.assert_eq((select stage from public.leads where id = l), 'agendado', 'lead volta para agendado');
  perform pg_temp.assert_eq((select count(*)::int from public.appointments where lead_id = l), 2, 'histórico guarda os dois');

  -- uma call que já passou não impede um novo agendamento
  update public.appointments set status = 'feito' where lead_id = l;
  r := public.book_slot(l, pg_temp.at_local(d, '09:30'));
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'feito não bloqueia novo agendamento: ' || r::text);
end;
$$;
rollback;

-- 6) agendar com a call já feita no passado não bloqueia novo agendamento ----------------------------
begin;
select pg_temp.as_service() as _x \gset
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana A');
  l uuid := pg_temp.mk_lead('+5511900000071');
  r jsonb;
begin
  perform pg_temp.mk_rule(a, 1, '09:00', '12:00');
  -- ativa mas já terminada (passado): não conta como "ativo futuro"
  insert into public.appointments (lead_id, closer_id, starts_at, ends_at, status)
  values (l, a, now() - interval '3 days', now() - interval '3 days' + interval '30 minutes', 'agendado');
  r := public.book_slot(l, pg_temp.at_local(d, '09:00'));
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'call antiga não conta como agendamento ativo futuro: ' || r::text);
  perform pg_temp.assert_eq((select status from public.appointments where lead_id = l and starts_at < now()), 'reagendado', 'call antiga não marcada é substituída (reagendado)');
  perform pg_temp.assert_eq((select stage from public.leads where id = l), 'agendado', 'lead fica agendado');
end;
$$;
rollback;

\echo OK 30_book
