-- Schema: tabelas, linhas únicas, checks, triggers e funções existem como no contrato.
\i tests/sql/_helpers.sql
begin;

do $$
declare
  t text;
begin
  -- 12 tabelas, todas com RLS ligada
  foreach t in array array['agent_config','schedule_settings','closers','availability_rules','blocks',
    'leads','messages','funnel_events','appointments','objections','outbox','panel_users','public_book_attempts'] loop
    perform pg_temp.assert_true(to_regclass('public.' || t) is not null, 'tabela ' || t || ' existe');
    perform pg_temp.assert_true(
      (select relrowsecurity from pg_class where oid = ('public.' || t)::regclass), 'RLS ligada em ' || t);
  end loop;

  -- linhas únicas nascem prontas, com os defaults do contrato
  perform pg_temp.assert_eq((select count(*)::int from public.agent_config), 1, 'agent_config tem 1 linha');
  perform pg_temp.assert_eq((select count(*)::int from public.schedule_settings), 1, 'schedule_settings tem 1 linha');
  perform pg_temp.assert_eq((select agent_name from public.agent_config), 'Ana', 'nome padrão do agente');
  perform pg_temp.assert_true((select test_only from public.agent_config), 'test_only começa ligado');
  perform pg_temp.assert_eq((select call_minutes from public.schedule_settings), 30, 'call_minutes padrão');
  perform pg_temp.assert_eq((select timezone from public.schedule_settings), 'America/Sao_Paulo', 'fuso padrão');
  perform pg_temp.assert_true(not (select public_enabled from public.schedule_settings), 'página pública começa desativada');
end;
$$;

-- só pode existir uma linha em cada tabela de configuração
do $$
begin
  begin
    insert into public.agent_config (id) values (2);
    raise exception 'FALHOU: agent_config aceitou id=2';
  exception when check_violation then null;
  end;
  begin
    insert into public.schedule_settings (id) values (2);
    raise exception 'FALHOU: schedule_settings aceitou id=2';
  exception when check_violation then null;
  end;
end;
$$;

-- checks de valores inválidos
do $$
begin
  begin
    update public.schedule_settings set slot_step_minutes = 0;
    raise exception 'FALHOU: aceitou slot_step_minutes=0';
  exception when check_violation then null;
  end;
  begin
    update public.agent_config set ai_provider = 'outro';
    raise exception 'FALHOU: aceitou provider desconhecido';
  exception when check_violation then null;
  end;
  begin
    insert into public.leads (phone, stage) values ('+5511900000001', 'inexistente');
    raise exception 'FALHOU: aceitou estágio inválido';
  exception when check_violation then null;
  end;
  begin
    perform pg_temp.mk_rule(pg_temp.mk_closer('X'), 1, '12:00', '09:00');
    raise exception 'FALHOU: aceitou regra com fim antes do início';
  exception when check_violation then null;
  end;
  begin
    insert into public.closers (name, weight) values ('Y', 11);
    raise exception 'FALHOU: aceitou peso 11';
  exception when check_violation then null;
  end;
end;
$$;

-- telefone único
do $$
begin
  perform pg_temp.mk_lead('+5511900000002');
  begin
    perform pg_temp.mk_lead('+5511900000002');
    raise exception 'FALHOU: aceitou telefone repetido';
  exception when unique_violation then null;
  end;
end;
$$;

-- trigger: updated_at muda sozinho
do $$
declare
  v_lead uuid := pg_temp.mk_lead('+5511900000003');
  v_before timestamptz;
begin
  update public.leads set updated_at = now() - interval '1 day' where id = v_lead;
  select updated_at into v_before from public.leads where id = v_lead;
  -- o trigger sobrescreve qualquer valor manual de updated_at
  update public.leads set name = 'Novo nome' where id = v_lead;
  perform pg_temp.assert_true(
    (select updated_at from public.leads where id = v_lead) > now() - interval '1 minute',
    'updated_at atualizado pelo trigger');
end;
$$;

-- trigger: funil registra o nascimento do lead e cada mudança de estágio
do $$
declare
  v_lead uuid := pg_temp.mk_lead('+5511900000004');
begin
  perform pg_temp.assert_eq((select count(*)::int from public.funnel_events where lead_id = v_lead), 1, 'evento de nascimento');
  perform pg_temp.assert_eq((select stage from public.funnel_events where lead_id = v_lead), 'novo', 'nasce em novo');

  update public.leads set stage = 'respondeu' where id = v_lead;
  update public.leads set stage = 'respondeu' where id = v_lead;           -- mesmo estágio: não duplica
  update public.leads set stage = 'qualificado', name = 'Teste' where id = v_lead;
  update public.leads set name = 'Outro nome' where id = v_lead;           -- sem mudança de estágio

  perform pg_temp.assert_eq(
    (select array_agg(stage order by id) from public.funnel_events where lead_id = v_lead),
    array['novo','respondeu','qualificado'], 'sequência de eventos do funil');
end;
$$;

-- trigger: resultado da call atualiza o estágio do lead (cancelado não mexe)
do $$
declare
  v_closer uuid := pg_temp.mk_closer('Closer T');
  v_lead uuid := pg_temp.mk_lead('+5511900000005');
  v_appt uuid;
begin
  insert into public.appointments (lead_id, closer_id, starts_at, ends_at)
  values (v_lead, v_closer, now() + interval '3 days', now() + interval '3 days 30 minutes') returning id into v_appt;
  perform pg_temp.assert_eq((select stage from public.leads where id = v_lead), 'agendado', 'agendamento move o lead para agendado');

  update public.appointments set status = 'confirmado' where id = v_appt;
  perform pg_temp.assert_eq((select stage from public.leads where id = v_lead), 'confirmado', 'confirmado');
  update public.appointments set status = 'feito' where id = v_appt;
  perform pg_temp.assert_eq((select stage from public.leads where id = v_lead), 'compareceu', 'feito vira compareceu');
  update public.appointments set status = 'venda', sale_value = 1234.50 where id = v_appt;
  perform pg_temp.assert_eq((select stage from public.leads where id = v_lead), 'venda', 'venda');
  update public.appointments set status = 'cancelado' where id = v_appt;
  perform pg_temp.assert_eq((select stage from public.leads where id = v_lead), 'venda', 'cancelar uma call já vendida não sobrescreve o resultado');
end;
$$;

-- cancelar a única call ativa devolve o lead a 'qualificado'; havendo outra ativa, nada muda
do $$
declare
  v_closer uuid := pg_temp.mk_closer('Closer C');
  v_lead uuid := pg_temp.mk_lead('+5511900000006');
  v_a1 uuid;
  v_a2 uuid;
begin
  insert into public.appointments (lead_id, closer_id, starts_at, ends_at)
  values (v_lead, v_closer, now() + interval '3 days', now() + interval '3 days 30 minutes') returning id into v_a1;
  update public.appointments set status = 'cancelado' where id = v_a1;
  perform pg_temp.assert_eq((select stage from public.leads where id = v_lead), 'qualificado', 'cancelar a única call ativa volta o lead a qualificado');

  insert into public.appointments (lead_id, closer_id, starts_at, ends_at)
  values (v_lead, v_closer, now() + interval '4 days', now() + interval '4 days 30 minutes') returning id into v_a1;
  -- um lead só pode ter UMA call ativa (índice parcial)
  begin
    insert into public.appointments (lead_id, closer_id, starts_at, ends_at)
    values (v_lead, v_closer, now() + interval '5 days', now() + interval '5 days 30 minutes') returning id into v_a2;
    perform pg_temp.assert_true(false, 'segunda call ativa do mesmo lead deveria ser recusada');
  exception when unique_violation then null;
  end;
  update public.appointments set status = 'cancelado' where id = v_a1;
  perform pg_temp.assert_eq((select stage from public.leads where id = v_lead), 'qualificado', 'cancelar a call ativa volta o lead a qualificado');
  insert into public.appointments (lead_id, closer_id, starts_at, ends_at)
  values (v_lead, v_closer, now() + interval '5 days', now() + interval '5 days 30 minutes') returning id into v_a2;
  perform pg_temp.assert_eq((select stage from public.leads where id = v_lead), 'agendado', 'nova call ativa agenda o lead');

  update public.appointments set status = 'no_show' where id = v_a2;
  perform pg_temp.assert_eq((select stage from public.leads where id = v_lead), 'no_show', 'no_show muda o estágio');
end;
$$;

-- fuso horário inexistente é recusado na hora de salvar
do $$
declare v_ok boolean := false;
begin
  begin
    update public.schedule_settings set timezone = 'Nao/Existe';
  exception when others then v_ok := true;
  end;
  perform pg_temp.assert_true(v_ok, 'timezone inválido é recusado');
  perform pg_temp.assert_eq((select timezone from public.schedule_settings), 'America/Sao_Paulo', 'timezone segue válido');
end;
$$;

-- as funções do contrato existem
do $$
declare
  f text;
begin
  foreach f in array array[
    'available_slots(timestamp with time zone,timestamp with time zone,uuid)',
    'book_slot(uuid,timestamp with time zone,text,uuid,text)',
    'public_slots(text,timestamp with time zone,timestamp with time zone)',
    'public_book(text,text,text,text,timestamp with time zone,text)',
    'dashboard_stats(timestamp with time zone,timestamp with time zone,uuid)',
    'reschedule_slot(uuid,timestamp with time zone,uuid)',
    'is_panel_user()'] loop
    perform pg_temp.assert_true(to_regprocedure('public.' || f) is not null, 'função ' || f);
  end loop;
end;
$$;

-- outbox.client_key é única: o mesmo envio repetido pelo painel é recusado
do $$
declare
  v_ok boolean := false;
begin
  insert into public.outbox (phone, body, client_key) values ('+5511900000401', 'oi', 'k-1');
  begin
    insert into public.outbox (phone, body, client_key) values ('+5511900000401', 'oi', 'k-1');
  exception when unique_violation then v_ok := true;
  end;
  perform pg_temp.assert_true(v_ok, 'client_key duplicado é recusado');
  -- sem chave não conflita (envios do motor)
  insert into public.outbox (phone, body) values ('+5511900000401', 'a');
  insert into public.outbox (phone, body) values ('+5511900000401', 'b');
  perform pg_temp.assert_eq((select public_max_per_hour from public.schedule_settings), 20, 'public_max_per_hour padrão 20');
  perform pg_temp.assert_eq((select public_max_per_ip_day from public.schedule_settings), 3, 'public_max_per_ip_day padrão 3');
end;
$$;

rollback;
\echo OK 10_schema
