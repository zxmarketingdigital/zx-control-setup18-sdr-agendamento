-- =====================================================================
-- Setup 18 · Agente SDR de Agendamento · conexão do WhatsApp pelo painel
-- Autoria e suporte: ZX LAB (https://suporte.zxlab.com.br/hub)
--
-- O painel roda no navegador e não conhece a chave da Evolution (ela vive só no
-- computador do aluno). Então o painel PEDE (RPC request_whatsapp_command) e o
-- MOTOR executa e grava aqui o estado e o QR Code. A tabela tem uma linha só.
-- Idempotente: pode ser aplicada mais de uma vez.
-- =====================================================================

begin;

create table if not exists public.whatsapp_connection (
  id smallint primary key default 1 check (id = 1),
  state text not null default 'desconhecido'
    check (state in ('open', 'close', 'connecting', 'desconhecido')),
  owner_number text,
  qr_base64 text,
  pairing_code text,
  qr_expires_at timestamptz,
  command text check (command in ('connect', 'logout')),
  command_id uuid,
  command_status text
    check (command_status in ('pendente', 'em_andamento', 'aguardando_leitura', 'concluido', 'expirado', 'erro')),
  command_error text,
  command_requested_at timestamptz,
  checked_at timestamptz,
  updated_at timestamptz not null default now()
);

insert into public.whatsapp_connection (id) values (1) on conflict do nothing;

drop trigger if exists whatsapp_connection_updated_at on public.whatsapp_connection;
create trigger whatsapp_connection_updated_at before update on public.whatsapp_connection
  for each row execute function public.set_updated_at();

-- Heartbeat sempre no relógio do BANCO: o motor manda checked_at, mas o valor gravado é now(),
-- para nunca comparar relógio do computador do aluno com o do navegador.
create or replace function public.whatsapp_connection_heartbeat_clock()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $$
begin
  if new.checked_at is distinct from old.checked_at then
    new.checked_at := now();
  end if;
  return new;
end;
$$;

drop trigger if exists whatsapp_connection_heartbeat on public.whatsapp_connection;
create trigger whatsapp_connection_heartbeat before update on public.whatsapp_connection
  for each row execute function public.whatsapp_connection_heartbeat_clock();

-- RLS: o painel só LÊ (e só quem está em panel_users). Quem escreve é o motor (service_role)
-- e a RPC abaixo; não existe policy de insert/update/delete para authenticated nem anon.
alter table public.whatsapp_connection enable row level security;
drop policy if exists panel_read on public.whatsapp_connection;
create policy panel_read on public.whatsapp_connection for select to authenticated
  using ((select public.is_panel_user()));

-- GRANTs explícitos (o Supabase não dá privilégio automático a tabela nova).
revoke all on public.whatsapp_connection from public, anon, authenticated;
-- O painel NÃO lê qr_base64 nem pairing_code direto na tabela (fora do gate de validade):
-- só pelas colunas abaixo e, para o QR, pela RPC whatsapp_status.
grant select (id, state, owner_number, command, command_id, command_status, command_error,
              command_requested_at, checked_at, qr_expires_at, updated_at)
  on public.whatsapp_connection to authenticated;
grant select, insert, update, delete on public.whatsapp_connection to service_role;

-- Pedido do painel ao motor. security definer: grava mesmo sem policy de escrita,
-- mas só para quem está em panel_users.
create or replace function public.request_whatsapp_command(p_command text)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_row public.whatsapp_connection%rowtype;
  v_id uuid;
begin
  if not public.is_panel_user() then
    raise exception 'sem_permissao' using errcode = '42501';
  end if;
  if p_command is null or p_command not in ('connect', 'logout') then
    return jsonb_build_object('ok', false, 'error', 'comando_invalido');
  end if;

  insert into public.whatsapp_connection (id) values (1) on conflict do nothing;
  select * into v_row from public.whatsapp_connection where id = 1 for update;

  if v_row.command_status in ('pendente', 'em_andamento')
     and v_row.command_requested_at is not null
     and v_row.command_requested_at > now() - interval '30 seconds' then
    return jsonb_build_object('ok', false, 'error', 'comando_em_andamento');
  end if;

  v_id := gen_random_uuid();
  update public.whatsapp_connection
     set command = p_command,
         command_id = v_id,
         command_status = 'pendente',
         command_error = null,
         command_requested_at = now(),
         qr_base64 = null,
         pairing_code = null,
         qr_expires_at = null
   where id = 1;

  return jsonb_build_object('ok', true, 'command_id', v_id);
end;
$$;

revoke all on function public.request_whatsapp_command(text) from public, anon;
grant execute on function public.request_whatsapp_command(text) to authenticated, service_role;

-- Leitura do estado com TODAS as idades e a validade do QR calculadas no relógio do banco.
-- Painel (panel_users) e motor (service_role) usam esta função; o navegador não compara
-- datas com o próprio relógio. Painel não recebe QR vencido. Stable: não cria a linha
-- (ela nasce na migration); se faltar, devolve estado 'desconhecido'.
create or replace function public.whatsapp_status()
returns jsonb
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
declare
  v_row public.whatsapp_connection%rowtype;
  v_service boolean;
  v_out jsonb;
  v_valid boolean;
begin
  v_service := coalesce(nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role', '') = 'service_role';
  if not (v_service or public.is_panel_user()) then
    raise exception 'sem_permissao' using errcode = '42501';
  end if;

  select * into v_row from public.whatsapp_connection where id = 1;
  if not found then
    return jsonb_build_object(
      'id', 1, 'state', 'desconhecido', 'server_now', now(),
      'checked_age_seconds', null, 'command_age_seconds', null,
      'qr_valid', false, 'qr_remaining_seconds', null);
  end if;

  v_valid := v_row.qr_base64 is not null
             and v_row.qr_expires_at is not null
             and v_row.qr_expires_at > now()
             and v_row.command_status = 'aguardando_leitura';
  v_out := to_jsonb(v_row) || jsonb_build_object(
    'server_now', now(),
    'checked_age_seconds', extract(epoch from now() - v_row.checked_at),
    'command_age_seconds', extract(epoch from now() - v_row.command_requested_at),
    'qr_valid', v_valid,
    'qr_remaining_seconds', case when v_row.qr_expires_at is null then null
                                 else greatest(0, extract(epoch from v_row.qr_expires_at - now())) end);
  if not v_service and not v_valid then
    v_out := v_out || jsonb_build_object('qr_base64', null, 'pairing_code', null);
  end if;
  return v_out;
end;
$$;

revoke all on function public.whatsapp_status() from public, anon;
grant execute on function public.whatsapp_status() to authenticated, service_role;

commit;
