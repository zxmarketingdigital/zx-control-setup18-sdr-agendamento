-- A migration roda 2 vezes seguidas sobre um banco já instalado sem falhar e sem apagar dados.
-- (Fora de transação: o próprio arquivo abre e fecha a sua. Limpa o que criou no fim.)
insert into public.leads (id, phone, name) values ('00000000-0000-0000-0000-00000000f001', '+5511900000501', 'Persistente');
update public.agent_config set agent_name = 'Marca';

\i supabase/migrations/20261118000001_sdr_agendamento.sql
\i supabase/migrations/20261118000001_sdr_agendamento.sql

\i tests/sql/_helpers.sql
do $$
begin
  perform pg_temp.assert_eq((select count(*)::int from public.leads where phone = '+5511900000501'), 1, 'reaplicar a migration não apaga leads');
  perform pg_temp.assert_eq((select agent_name from public.agent_config), 'Marca', 'reaplicar não reseta agent_config');
  perform pg_temp.assert_eq((select count(*)::int from public.agent_config), 1, 'agent_config continua com uma linha');
  perform pg_temp.assert_eq((select count(*)::int from pg_policies where schemaname = 'public' and tablename = 'leads' and policyname = 'panel_all'), 1, 'uma policy panel_all em leads');
  perform pg_temp.assert_eq((select count(*)::int from pg_trigger where tgname = 'leads_stage_log' and not tgisinternal), 1, 'um trigger leads_stage_log');
  perform pg_temp.assert_true(has_function_privilege('authenticated', 'public.set_appointment_status(uuid,text,numeric)', 'execute'), 'grants refeitos');
  perform pg_temp.assert_true(not has_function_privilege('anon', 'public.book_slot(uuid,timestamptz,text,uuid,text)', 'execute'), 'anon continua sem book_slot');
end;
$$;
delete from public.leads where id = '00000000-0000-0000-0000-00000000f001';
update public.agent_config set agent_name = 'Ana';

\echo OK 90_migration_idempotente
