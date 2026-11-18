-- RLS e permissões: anon não lê nada, authenticated fora de panel_users não lê, dentro lê e escreve.
\i tests/sql/_helpers.sql

begin;
do $$
declare
  d date := pg_temp.next_dow(1);
  t text;
  tabelas text[] := array['agent_config', 'schedule_settings', 'closers', 'availability_rules', 'blocks',
    'leads', 'messages', 'funnel_events', 'appointments', 'objections', 'outbox', 'panel_users'];
  a uuid := pg_temp.mk_closer('Ana A');
  l uuid := pg_temp.mk_lead('+5511900000201');
  l2 uuid := pg_temp.mk_lead('+5511900000203');
  n int;
  r jsonb;
  v_ok boolean;
begin
  perform pg_temp.mk_rule(a, 1, '09:00', '12:00');
  insert into public.blocks (closer_id, starts_at, ends_at, reason) values (a, now() + interval '30 days', now() + interval '31 days', 'folga');
  insert into public.messages (lead_id, direction, author, body) values (l, 'in', 'lead', 'oi');
  insert into public.objections (lead_id, category, excerpt) values (l, 'preco', 'tá caro');
  insert into public.outbox (lead_id, phone, body) values (l, '+5511900000201', 'oi');
  insert into public.appointments (lead_id, closer_id, starts_at, ends_at)
    values (l, a, pg_temp.at_local(d, '09:00'), pg_temp.at_local(d, '09:30'));
  insert into auth.users (id, email) values
    ('00000000-0000-0000-0000-0000000000bb', 'dono@demo.invalid'),
    ('00000000-0000-0000-0000-0000000000cc', 'impostor@demo.invalid');
  insert into public.panel_users (user_id, email) values ('00000000-0000-0000-0000-0000000000bb', 'dono@demo.invalid');

  -- RLS ligada em todas as tabelas
  foreach t in array tabelas loop
    perform pg_temp.assert_true((select relrowsecurity from pg_class where oid = ('public.' || t)::regclass), 'RLS ligada em ' || t);
  end loop;

  -- 1) anon: sem privilégio nenhum nas tabelas
  foreach t in array tabelas loop
    perform pg_temp.assert_true(not has_table_privilege('anon', 'public.' || t, 'select'), 'anon sem select em ' || t);
    perform pg_temp.assert_true(not has_table_privilege('anon', 'public.' || t, 'insert'), 'anon sem insert em ' || t);
    perform pg_temp.assert_true(not has_table_privilege('anon', 'public.' || t, 'update'), 'anon sem update em ' || t);
    perform pg_temp.assert_true(not has_table_privilege('anon', 'public.' || t, 'delete'), 'anon sem delete em ' || t);
  end loop;
  -- e de fato não consegue ler
  set local role anon;
  foreach t in array tabelas loop
    v_ok := false;
    begin
      execute format('select count(*) from public.%I', t) into n;
    exception when insufficient_privilege then v_ok := true;
    end;
    perform pg_temp.assert_true(v_ok, 'anon bloqueado ao ler ' || t);
  end loop;
  reset role;

  -- 2) authenticated FORA de panel_users: lê zero linhas e não escreve
  perform set_config('request.jwt.claims', '{"role":"authenticated","email":"intruso@demo.invalid","sub":"00000000-0000-0000-0000-0000000000aa"}', true);
  set local role authenticated;
  foreach t in array tabelas loop
    execute format('select count(*) from public.%I', t) into n;
    perform pg_temp.assert_eq(n, 0, 'não-painel enxerga 0 linhas em ' || t);
  end loop;
  perform pg_temp.assert_eq(public.is_panel_user(), false, 'não-painel: is_panel_user false');
  -- escrita barrada pela RLS
  v_ok := false;
  begin insert into public.leads (phone) values ('+5511900000299');
  exception when insufficient_privilege then v_ok := true; end;
  perform pg_temp.assert_true(v_ok, 'não-painel não insere lead');
  v_ok := false;
  begin insert into public.panel_users (user_id, email) values ('00000000-0000-0000-0000-0000000000cc', 'intruso@demo.invalid');
  exception when insufficient_privilege then v_ok := true; end;
  perform pg_temp.assert_true(v_ok, 'não-painel não se cadastra em panel_users');
  update public.leads set name = 'hack';
  get diagnostics n = row_count;
  perform pg_temp.assert_eq(n, 0, 'não-painel não atualiza lead');
  delete from public.appointments;
  get diagnostics n = row_count;
  perform pg_temp.assert_eq(n, 0, 'não-painel não apaga agendamento');
  update public.agent_config set agent_name = 'hack';
  get diagnostics n = row_count;
  perform pg_temp.assert_eq(n, 0, 'não-painel não altera agent_config');
  -- RPCs que gravam: recusam quem não é de confiança
  -- lead SEM agendamento e horário livre: só o guard impede (sem ele a chamada daria ok=true)
  v_ok := false;
  begin r := public.book_slot(l2, pg_temp.at_local(d, '10:00'));
  exception when insufficient_privilege then v_ok := true; end;
  perform pg_temp.assert_true(v_ok, 'não-painel não agenda por book_slot (42501)');
  -- dashboard por RLS: zeros
  r := public.dashboard_stats(now() - interval '30 days', now() + interval '1 day');
  perform pg_temp.assert_eq((r ->> 'leads')::int, 0, 'não-painel vê funil zerado');
  reset role;

  -- 2b) usuário autenticado com o MESMO e-mail do dono no claim, mas outro user_id: não lê nada
  --     (o e-mail do JWT não prova posse; vale o ID do usuário em panel_users)
  perform set_config('request.jwt.claims', '{"role":"authenticated","email":"dono@demo.invalid","sub":"00000000-0000-0000-0000-0000000000cc"}', true);
  set local role authenticated;
  perform pg_temp.assert_eq(public.is_panel_user(), false, 'e-mail igual ao do dono, outro user_id: is_panel_user false');
  foreach t in array tabelas loop
    execute format('select count(*) from public.%I', t) into n;
    perform pg_temp.assert_eq(n, 0, 'impostor (mesmo e-mail, outro id) enxerga 0 linhas em ' || t);
  end loop;
  reset role;

  perform pg_temp.assert_eq((select count(*)::int from public.leads), 2, 'nada foi gravado pelo intruso');
  perform pg_temp.assert_eq((select count(*)::int from public.appointments where lead_id = l2), 0, 'book_slot barrado não gravou agendamento');
  perform pg_temp.assert_eq((select agent_name from public.agent_config), 'Ana', 'agent_config intacta');

  -- 3) authenticated DENTRO de panel_users (e-mail com caixa diferente): lê e escreve
  perform set_config('request.jwt.claims', '{"role":"authenticated","email":"dono@demo.invalid","sub":"00000000-0000-0000-0000-0000000000bb"}', true);
  set local role authenticated;
  perform pg_temp.assert_eq(public.is_panel_user(), true, 'painel: is_panel_user true (user_id em panel_users)');
  foreach t in array array['agent_config', 'schedule_settings', 'closers', 'availability_rules', 'blocks',
                           'leads', 'messages', 'objections', 'outbox', 'appointments', 'panel_users'] loop
    execute format('select count(*) from public.%I', t) into n;
    perform pg_temp.assert_true(n >= 1, 'painel enxerga ' || t);
  end loop;
  -- o painel lê panel_users mas não concede nem revoga acesso (escrita só pelo service_role)
  v_ok := false;
  begin insert into public.panel_users (user_id, email) values ('00000000-0000-0000-0000-0000000000cc', 'x@demo.invalid');
  exception when insufficient_privilege then v_ok := true; end;
  perform pg_temp.assert_true(v_ok, 'painel não insere em panel_users');
  v_ok := false;
  begin delete from public.panel_users;
  exception when insufficient_privilege then v_ok := true; end;
  perform pg_temp.assert_true(v_ok, 'painel não apaga de panel_users');
  insert into public.leads (phone, name) values ('+5511900000202', 'Novo pelo painel');
  update public.leads set name = 'Editado' where phone = '+5511900000202';
  insert into public.outbox (phone, body) values ('+5511900000202', 'mensagem manual do dono');
  update public.agent_config set agent_name = 'Bia';
  update public.leads set takeover_until = now() + interval '24 hours' where phone = '+5511900000201';
  delete from public.leads where phone = '+5511900000202';
  get diagnostics n = row_count;
  perform pg_temp.assert_eq(n, 1, 'painel apaga lead');
  -- RPCs do painel
  perform pg_temp.assert_true((select count(*) from public.available_slots(now(), now() + interval '14 days')) > 0, 'painel chama available_slots');
  r := public.book_slot(l, pg_temp.at_local(d, '11:00'));
  perform pg_temp.assert_eq(r ->> 'error', 'lead_ja_agendado', 'painel chama book_slot (lead já agendado)');
  r := public.dashboard_stats(now() - interval '30 days', now() + interval '1 day');
  perform pg_temp.assert_true((r ->> 'leads')::int >= 1, 'painel vê o funil');
  reset role;
  perform pg_temp.assert_eq((select agent_name from public.agent_config), 'Bia', 'painel alterou agent_config');

  -- 4) service_role (motor): vê tudo e agenda
  perform set_config('request.jwt.claims', '{"role":"service_role"}', true);
  set local role service_role;
  select count(*) into n from public.leads;
  perform pg_temp.assert_true(n >= 1, 'service_role lê leads');
  insert into public.messages (lead_id, direction, author, body) values (l, 'out', 'agente', 'olá');
  r := public.book_slot(l, pg_temp.at_local(d, '11:00'));
  perform pg_temp.assert_eq(r ->> 'error', 'lead_ja_agendado', 'service_role chama book_slot');
  reset role;

  -- 4b) public_book_attempts é interna: ninguém além do service_role/dono da função
  perform pg_temp.assert_true(not has_table_privilege('anon', 'public.public_book_attempts', 'select'), 'anon sem select em public_book_attempts');
  perform pg_temp.assert_true(not has_table_privilege('authenticated', 'public.public_book_attempts', 'select'), 'authenticated sem select em public_book_attempts');
  perform pg_temp.assert_true(not has_table_privilege('authenticated', 'public.public_book_attempts', 'insert'), 'authenticated sem insert em public_book_attempts');
  perform pg_temp.assert_true((select relrowsecurity from pg_class where oid = 'public.public_book_attempts'::regclass), 'RLS ligada em public_book_attempts');

  -- 5) grants das funções: cada uma só para quem deve
  perform pg_temp.assert_true(not has_function_privilege('authenticated', 'public._book_core(uuid,timestamptz,text,uuid,text,boolean)', 'execute'), '_book_core interna');
  perform pg_temp.assert_true(not has_function_privilege('authenticated', 'public._is_trusted_caller()', 'execute'), '_is_trusted_caller interna');
  perform pg_temp.assert_true(not has_function_privilege('anon', 'public.set_updated_at()', 'execute'), 'trigger fn sem execute para anon');
  perform pg_temp.assert_true(has_function_privilege('authenticated', 'public.book_slot(uuid,timestamptz,text,uuid,text)', 'execute'), 'authenticated executa book_slot');
  perform pg_temp.assert_true(has_function_privilege('authenticated', 'public.reschedule_slot(uuid,timestamptz,uuid)', 'execute'), 'authenticated executa reschedule_slot');
  perform pg_temp.assert_true(has_function_privilege('service_role', 'public.reschedule_slot(uuid,timestamptz,uuid)', 'execute'), 'service_role executa reschedule_slot');
  perform pg_temp.assert_true(not has_function_privilege('anon', 'public.reschedule_slot(uuid,timestamptz,uuid)', 'execute'), 'anon NÃO executa reschedule_slot');
  perform pg_temp.assert_true(has_function_privilege('service_role', 'public.dashboard_stats(timestamptz,timestamptz,uuid)', 'execute'), 'service_role executa dashboard_stats');
  perform pg_temp.assert_true(has_function_privilege('authenticated', 'public.public_slots(text,timestamptz,timestamptz)', 'execute'), 'authenticated executa public_slots');
  perform pg_temp.assert_true(not has_function_privilege('public', 'public.book_slot(uuid,timestamptz,text,uuid,text)', 'execute'), 'PUBLIC sem execute em book_slot');
  perform pg_temp.assert_true(not has_table_privilege('public', 'public.leads', 'select'), 'PUBLIC sem select em leads');

  -- 6) funções security definer fixam search_path
  perform pg_temp.assert_true(not exists (
    select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.prosecdef
      and not exists (select 1 from unnest(coalesce(p.proconfig, '{}')) c where c like 'search_path=%')
  ), 'toda função security definer tem search_path fixo');
end;
$$;
rollback;

\echo OK 50_rls
