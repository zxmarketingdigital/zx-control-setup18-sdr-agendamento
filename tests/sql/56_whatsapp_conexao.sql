-- Conexão do WhatsApp pelo painel: tabela singleton, RLS, grants e RPC request_whatsapp_command.
\i tests/sql/_helpers.sql

begin;
do $$
declare
  n int;
  r jsonb;
  v_ok boolean;
  v_id uuid;
begin
  insert into auth.users (id, email) values
    ('00000000-0000-0000-0000-0000000000bb', 'dono@demo.invalid'),
    ('00000000-0000-0000-0000-0000000000aa', 'intruso@demo.invalid');
  insert into public.panel_users (user_id, email) values ('00000000-0000-0000-0000-0000000000bb', 'dono@demo.invalid');

  -- linha singleton e RLS
  perform pg_temp.assert_eq((select count(*)::int from public.whatsapp_connection), 1, 'singleton existe');
  perform pg_temp.assert_eq((select state from public.whatsapp_connection), 'desconhecido', 'estado inicial desconhecido');
  perform pg_temp.assert_true((select relrowsecurity from pg_class where oid = 'public.whatsapp_connection'::regclass), 'RLS ligada');
  v_ok := false;
  begin insert into public.whatsapp_connection (id) values (2);
  exception when check_violation then v_ok := true; end;
  perform pg_temp.assert_true(v_ok, 'id = 2 é rejeitado');

  -- grants
  perform pg_temp.assert_true(not has_table_privilege('anon', 'public.whatsapp_connection', 'select'), 'anon sem select');
  perform pg_temp.assert_true(not has_table_privilege('authenticated', 'public.whatsapp_connection', 'update'), 'authenticated sem update');
  perform pg_temp.assert_true(not has_table_privilege('authenticated', 'public.whatsapp_connection', 'insert'), 'authenticated sem insert');
  perform pg_temp.assert_true(has_table_privilege('service_role', 'public.whatsapp_connection', 'update'), 'service_role escreve');
  perform pg_temp.assert_true(not has_function_privilege('anon', 'public.request_whatsapp_command(text)', 'execute'), 'anon sem RPC');

  -- authenticated não lê qr_base64 nem pairing_code direto; as demais colunas, sim
  perform pg_temp.assert_true(not has_column_privilege('authenticated', 'public.whatsapp_connection', 'qr_base64', 'select'), 'authenticated sem select em qr_base64');
  perform pg_temp.assert_true(not has_column_privilege('authenticated', 'public.whatsapp_connection', 'pairing_code', 'select'), 'authenticated sem select em pairing_code');
  perform pg_temp.assert_true(not has_table_privilege('authenticated', 'public.whatsapp_connection', 'select'), 'authenticated sem select de tabela inteira');
  perform pg_temp.assert_true(has_column_privilege('authenticated', 'public.whatsapp_connection', 'state', 'select')
                              and has_column_privilege('authenticated', 'public.whatsapp_connection', 'checked_at', 'select')
                              and has_column_privilege('authenticated', 'public.whatsapp_connection', 'command_status', 'select')
                              and has_column_privilege('authenticated', 'public.whatsapp_connection', 'qr_expires_at', 'select'), 'authenticated lê as colunas não sensíveis');
  perform pg_temp.assert_true(has_column_privilege('service_role', 'public.whatsapp_connection', 'qr_base64', 'select'), 'service_role lê qr_base64');

  -- anon não lê
  set local role anon;
  v_ok := false;
  begin perform count(*) from public.whatsapp_connection;
  exception when insufficient_privilege then v_ok := true; end;
  perform pg_temp.assert_true(v_ok, 'anon bloqueado ao ler');
  reset role;

  -- authenticated fora de panel_users: lê 0 linhas e a RPC recusa com 42501
  perform set_config('request.jwt.claims', '{"role":"authenticated","sub":"00000000-0000-0000-0000-0000000000aa"}', true);
  set local role authenticated;
  select count(*) into n from public.whatsapp_connection;
  perform pg_temp.assert_eq(n, 0, 'não-painel lê 0 linhas');
  v_ok := false;
  begin perform public.request_whatsapp_command('connect');
  exception when insufficient_privilege then v_ok := true; end;
  perform pg_temp.assert_true(v_ok, 'não-painel: RPC dá 42501');
  reset role;
  perform pg_temp.assert_true((select command is null from public.whatsapp_connection), 'RPC barrada não gravou comando');

  -- o motor grava um QR (como service_role, sem RLS)
  update public.whatsapp_connection
     set qr_base64 = 'data:image/png;base64,AAAA', pairing_code = 'ABCD1234', qr_expires_at = now() + interval '40 seconds',
         state = 'close', checked_at = now()
   where id = 1;

  -- whatsapp_status: anon sem permissão; não-painel 42501; service_role lê com QR; painel só com QR válido
  perform pg_temp.assert_true(not has_function_privilege('anon', 'public.whatsapp_status()', 'execute'), 'anon: whatsapp_status barrado (sem execute)');
  perform set_config('request.jwt.claims', '{"role":"authenticated","sub":"00000000-0000-0000-0000-0000000000aa"}', true);
  set local role authenticated;
  v_ok := false;
  begin perform public.whatsapp_status();
  exception when insufficient_privilege then v_ok := true; end;
  perform pg_temp.assert_true(v_ok, 'fora de panel_users: whatsapp_status dá 42501');
  reset role;
  update public.whatsapp_connection set command_status = 'aguardando_leitura';
  perform set_config('request.jwt.claims', '{"role":"service_role"}', true);
  set local role service_role;
  r := public.whatsapp_status();
  reset role;
  perform pg_temp.assert_true(r ->> 'qr_base64' is not null and (r ->> 'qr_valid')::boolean, 'service_role lê o QR válido');
  perform pg_temp.assert_true((r ->> 'qr_remaining_seconds')::numeric between 30 and 40, 'qr_remaining_seconds pelo relógio do banco');
  perform set_config('request.jwt.claims', '{"role":"authenticated","sub":"00000000-0000-0000-0000-0000000000bb"}', true);
  set local role authenticated;
  r := public.whatsapp_status();
  reset role;
  perform pg_temp.assert_true(r ->> 'qr_base64' is not null and (r ->> 'qr_valid')::boolean, 'painel recebe QR válido');
  update public.whatsapp_connection set qr_expires_at = now() - interval '1 second';
  set local role authenticated;
  r := public.whatsapp_status();
  reset role;
  perform pg_temp.assert_true((r ->> 'qr_valid')::boolean is false and r ->> 'qr_base64' is null and r ->> 'pairing_code' is null, 'painel não recebe QR vencido');
  perform pg_temp.assert_eq((r ->> 'qr_remaining_seconds')::numeric, 0::numeric, 'QR vencido: restante 0');
  set local role service_role;
  perform set_config('request.jwt.claims', '{"role":"service_role"}', true);
  r := public.whatsapp_status();
  reset role;
  perform pg_temp.assert_true(r ->> 'qr_base64' is not null, 'service_role ainda vê o QR vencido');
  -- sem claims, mesmo com role de banco service_role, a função recusa (só o claim vale)
  perform set_config('request.jwt.claims', '', true);
  set local role service_role;
  v_ok := false;
  begin perform public.whatsapp_status();
  exception when insufficient_privilege then v_ok := true; end;
  reset role;
  perform pg_temp.assert_true(v_ok, 'service_role sem claim de role: whatsapp_status dá 42501');
  update public.whatsapp_connection set qr_expires_at = now() + interval '40 seconds', command_status = null;

  -- trigger: heartbeat com data futura vira now()
  update public.whatsapp_connection set checked_at = now() + interval '1 day';
  perform pg_temp.assert_true((select checked_at <= now() from public.whatsapp_connection), 'trigger força checked_at = now()');
  update public.whatsapp_connection set checked_at = now() - interval '1 day';
  perform pg_temp.assert_true((select checked_at > now() - interval '1 minute' from public.whatsapp_connection), 'trigger também corrige data passada');

  -- painel: lê, não escreve direto
  perform set_config('request.jwt.claims', '{"role":"authenticated","sub":"00000000-0000-0000-0000-0000000000bb"}', true);
  set local role authenticated;
  select count(*) into n from public.whatsapp_connection;
  perform pg_temp.assert_eq(n, 1, 'painel lê a linha');
  perform state, owner_number, command, command_id, command_status, command_error,
          command_requested_at, checked_at, qr_expires_at, updated_at from public.whatsapp_connection;
  v_ok := false;
  begin perform qr_base64 from public.whatsapp_connection;
  exception when insufficient_privilege then v_ok := true; end;
  perform pg_temp.assert_true(v_ok, 'painel: select direto de qr_base64 dá permission denied');
  v_ok := false;
  begin perform pairing_code from public.whatsapp_connection;
  exception when insufficient_privilege then v_ok := true; end;
  perform pg_temp.assert_true(v_ok, 'painel: select direto de pairing_code dá permission denied');
  v_ok := false;
  begin perform * from public.whatsapp_connection;
  exception when insufficient_privilege then v_ok := true; end;
  perform pg_temp.assert_true(v_ok, 'painel: select * dá permission denied');
  v_ok := false;
  begin update public.whatsapp_connection set qr_base64 = 'data:image/png;base64,HACK';
  exception when insufficient_privilege then v_ok := true; end;
  perform pg_temp.assert_true(v_ok, 'painel não faz UPDATE direto');
  v_ok := false;
  begin delete from public.whatsapp_connection;
  exception when insufficient_privilege then v_ok := true; end;
  perform pg_temp.assert_true(v_ok, 'painel não apaga');

  -- RPC: comando inválido
  r := public.request_whatsapp_command('apagar_tudo');
  perform pg_temp.assert_eq(r ->> 'error', 'comando_invalido', 'comando inválido recusado');
  r := public.request_whatsapp_command(null);
  perform pg_temp.assert_eq(r ->> 'error', 'comando_invalido', 'comando nulo recusado');

  -- o pedido do painel NÃO renova o sinal de vida do motor (checked_at só muda se o UPDATE o muda)
  reset role;
  alter table public.whatsapp_connection disable trigger whatsapp_connection_heartbeat;
  update public.whatsapp_connection set checked_at = now() - interval '1 hour', command_status = null;
  alter table public.whatsapp_connection enable trigger whatsapp_connection_heartbeat;
  perform set_config('request.jwt.claims', '{"role":"authenticated","sub":"00000000-0000-0000-0000-0000000000bb"}', true);
  set local role authenticated;
  r := public.request_whatsapp_command('connect');
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'connect com checked_at antigo aceito: ' || r::text);
  reset role;
  perform pg_temp.assert_true((select checked_at < now() - interval '59 minutes' from public.whatsapp_connection), 'request_whatsapp_command não renova checked_at');
  update public.whatsapp_connection set command_status = null, command_requested_at = null;
  perform set_config('request.jwt.claims', '{"role":"authenticated","sub":"00000000-0000-0000-0000-0000000000bb"}', true);
  set local role authenticated;

  -- RPC: connect aceito e zera o QR existente
  r := public.request_whatsapp_command('connect');
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'connect aceito: ' || r::text);
  v_id := (r ->> 'command_id')::uuid;
  perform pg_temp.assert_true(v_id is not null, 'devolve command_id');
  reset role;
  perform pg_temp.assert_eq((select command from public.whatsapp_connection), 'connect', 'command gravado');
  perform pg_temp.assert_eq((select command_status from public.whatsapp_connection), 'pendente', 'status pendente');
  perform pg_temp.assert_eq((select command_id from public.whatsapp_connection), v_id, 'command_id gravado');
  perform pg_temp.assert_true((select command_requested_at is not null and command_error is null from public.whatsapp_connection), 'requested_at preenchido, erro limpo');
  perform pg_temp.assert_true((select qr_base64 is null and pairing_code is null and qr_expires_at is null from public.whatsapp_connection), 'connect zera o QR antigo');

  -- whatsapp_status: painel lê com idades do relógio do banco; QR gravado antes do connect foi zerado
  perform set_config('request.jwt.claims', '{"role":"authenticated","sub":"00000000-0000-0000-0000-0000000000bb"}', true);
  set local role authenticated;
  r := public.whatsapp_status();
  perform pg_temp.assert_true(r ? 'server_now' and (r ->> 'command_age_seconds')::numeric between 0 and 30, 'status do painel traz command_age_seconds');
  perform pg_temp.assert_true((r ->> 'checked_age_seconds')::numeric >= 0, 'status traz checked_age_seconds');
  perform pg_temp.assert_eq((r ->> 'qr_valid')::boolean, false, 'sem QR: qr_valid false');
  reset role;

  -- repetição em menos de 30 s é recusada (pendente e em_andamento)
  perform set_config('request.jwt.claims', '{"role":"authenticated","sub":"00000000-0000-0000-0000-0000000000bb"}', true);
  set local role authenticated;
  r := public.request_whatsapp_command('connect');
  perform pg_temp.assert_eq(r ->> 'error', 'comando_em_andamento', 'duplo clique recusado (pendente)');
  reset role;
  update public.whatsapp_connection set command_status = 'em_andamento';
  set local role authenticated;
  r := public.request_whatsapp_command('logout');
  perform pg_temp.assert_eq(r ->> 'error', 'comando_em_andamento', 'duplo clique recusado (em_andamento)');
  reset role;
  perform pg_temp.assert_eq((select command_id from public.whatsapp_connection), v_id, 'recusa não troca command_id');

  -- depois de 30 s parado, aceita de novo; e logout zera o QR
  update public.whatsapp_connection set command_requested_at = now() - interval '31 seconds';
  set local role authenticated;
  r := public.request_whatsapp_command('logout');
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'logout aceito após 30 s: ' || r::text);
  reset role;
  perform pg_temp.assert_true((select qr_base64 is null and pairing_code is null and qr_expires_at is null from public.whatsapp_connection), 'logout zera o QR');
  perform pg_temp.assert_true((select command_id is distinct from v_id from public.whatsapp_connection), 'novo command_id a cada pedido');

  -- fora de pendente/em_andamento (ex.: aguardando_leitura) aceita na hora
  update public.whatsapp_connection set command_status = 'aguardando_leitura', command_error = 'x';
  set local role authenticated;
  r := public.request_whatsapp_command('connect');
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'aceita durante aguardando_leitura');
  reset role;
  perform pg_temp.assert_true((select command_error is null from public.whatsapp_connection), 'novo pedido limpa command_error');

  -- check constraints
  v_ok := false;
  begin update public.whatsapp_connection set state = 'qualquer';
  exception when check_violation then v_ok := true; end;
  perform pg_temp.assert_true(v_ok, 'state inválido rejeitado');
  v_ok := false;
  begin update public.whatsapp_connection set command_status = 'qualquer';
  exception when check_violation then v_ok := true; end;
  perform pg_temp.assert_true(v_ok, 'command_status inválido rejeitado');

  -- trigger de updated_at
  update public.whatsapp_connection set updated_at = now() - interval '1 day';
  update public.whatsapp_connection set checked_at = now();
  perform pg_temp.assert_true((select updated_at > now() - interval '1 minute' from public.whatsapp_connection), 'updated_at renovado pelo trigger');
end;
$$;
rollback;

-- migration reaplicada não apaga estado nem duplica policy/trigger
-- (a migration abre e fecha a própria transação; por isso nada de begin/rollback aqui)
update public.whatsapp_connection set state = 'open', owner_number = '5511900000000';
\i supabase/migrations/20261118000002_whatsapp_conexao.sql
\i supabase/migrations/20261118000002_whatsapp_conexao.sql
do $$
begin
  perform pg_temp.assert_eq((select state from public.whatsapp_connection), 'open', 'reaplicar não reseta o estado');
  perform pg_temp.assert_eq((select count(*)::int from public.whatsapp_connection), 1, 'continua singleton');
  perform pg_temp.assert_eq((select count(*)::int from pg_policies where schemaname = 'public' and tablename = 'whatsapp_connection'), 1, 'uma policy só');
  perform pg_temp.assert_eq((select count(*)::int from pg_trigger where tgname = 'whatsapp_connection_updated_at' and not tgisinternal), 1, 'um trigger só');
  perform pg_temp.assert_true(has_function_privilege('authenticated', 'public.request_whatsapp_command(text)', 'execute'), 'grant da RPC refeito');
  perform pg_temp.assert_true(has_function_privilege('authenticated', 'public.whatsapp_status()', 'execute')
                              and not has_function_privilege('anon', 'public.whatsapp_status()', 'execute'), 'grants de whatsapp_status refeitos');
  perform pg_temp.assert_eq((select count(*)::int from pg_trigger where tgname = 'whatsapp_connection_heartbeat' and not tgisinternal), 1, 'um trigger de heartbeat só');
end;
$$;
update public.whatsapp_connection set state = 'desconhecido', owner_number = null;

\echo OK 56_whatsapp_conexao
