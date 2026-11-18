-- =====================================================================
-- Setup 18 · Agente SDR de Agendamento · schema do banco (Supabase do aluno)
-- Autoria e suporte: ZX LAB (https://suporte.zxlab.com.br/hub)
--
-- O que este arquivo cria:
--   tabelas, constraint de exclusão (sem dupla reserva), triggers,
--   funções (RPC) de horários e agendamento, RLS e GRANTs explícitos.
--
-- Segurança em uma frase: tudo é negado por padrão. O painel só enxerga
-- dados quando o ID do usuário logado está em panel_users; a página pública
-- (anon) só executa public_slots e public_book; o motor usa service_role.
-- =====================================================================

begin;

-- Preflight: este arquivo cria tabelas e funções com nomes comuns (leads, messages, blocks,
-- set_updated_at...). Em um projeto Supabase que já tem qualquer um desses nomes de OUTRO app,
-- abortar antes de mexer em qualquer coisa é mais seguro do que misturar dados, mudar RLS ou
-- revogar grants de um app que já usa o banco. O marcador `setup18-sdr` (comentário de
-- public.agent_config, gravado no fim desta migration) diz que o projeto já é deste setup:
-- só então reexecutar é permitido (todo o arquivo é idempotente).
do $$
declare
  v_alheios text;
  v_marcador text;
begin
  select obj_description(to_regclass('public.agent_config'), 'pg_class') into v_marcador;
  if v_marcador is distinct from 'setup18-sdr' then
    select string_agg(x, ', ' order by x) into v_alheios from (
      select t as x
      from unnest(array['agent_config', 'schedule_settings', 'closers', 'availability_rules', 'blocks',
                        'leads', 'messages', 'funnel_events', 'appointments', 'objections', 'outbox',
                        'panel_users', 'public_book_attempts']) as t
      where to_regclass('public.' || t) is not null
      union all
      select 'função ' || p.proname
      from pg_proc p
      where p.pronamespace = 'public'::regnamespace
        and p.proname in ('set_updated_at', 'log_lead_stage', 'sync_lead_stage_from_appointment', 'is_panel_user',
                          '_is_trusted_caller', '_slot_grid', 'available_slots', '_book_core', 'book_slot',
                          'reschedule_slot', 'public_slots', 'public_book', 'set_appointment_status', 'dashboard_stats')
    ) q;
    if v_alheios is not null then
      raise exception 'Este projeto Supabase já tem objetos com os mesmos nomes do Setup 18 (%) e não é um projeto deste setup. Use um projeto Supabase novo para este setup.', v_alheios;
    end if;
  end if;
end;
$$;

create extension if not exists btree_gist with schema extensions;
create extension if not exists pgcrypto with schema extensions;

-- ---------------------------------------------------------------------
-- Tabelas
-- ---------------------------------------------------------------------

-- Linha única: quem é o agente, como fala e o que sabe.
create table if not exists public.agent_config (
  id int primary key default 1 check (id = 1),
  agent_name text not null default 'Ana',
  company text,
  tone text,
  niche text,
  offer text,
  knowledge text,                                              -- markdown
  qualification_questions jsonb not null default '[]',         -- [{"key","pergunta","obrigatoria"}]
  disqualify_rules jsonb not null default '[]',                -- [{"key","regra","mensagem"}]
  objection_playbook jsonb not null default '[]',              -- [{"categoria","sinais":[],"resposta"}]
  followup_messages jsonb not null default '[]',               -- [{"apos_horas","texto"}]
  confirm_template text not null default 'Olá {nome}! Sua conversa está confirmada para {data} às {hora}.',
  reminder_24h_template text,
  reminder_2h_template text,
  owner_phone text,
  test_only boolean not null default true,
  test_phones text[] not null default '{}',
  ai_provider text not null default 'gemini' check (ai_provider in ('gemini', 'openai', 'anthropic')),
  paused boolean not null default false,
  followup_window_start time default '08:00',
  followup_window_end time default '20:00',
  updated_at timestamptz default now()
);

-- Linha única: regras da agenda. Os checks evitam divisão por zero na geração de horários.
create table if not exists public.schedule_settings (
  id int primary key default 1 check (id = 1),
  call_minutes int not null default 30 check (call_minutes > 0),
  buffer_minutes int not null default 10 check (buffer_minutes >= 0),
  slot_step_minutes int not null default 30 check (slot_step_minutes > 0),
  min_notice_hours int not null default 2 check (min_notice_hours >= 0),
  days_ahead int not null default 14 check (days_ahead >= 1),
  -- o check recusa fuso inexistente na hora de salvar (senão toda consulta de horários falharia)
  timezone text not null default 'America/Sao_Paulo' check ((now() at time zone timezone) is not null),
  public_slug text unique,
  public_enabled boolean not null default false,
  distribution text not null default 'rodizio' check (distribution in ('rodizio', 'peso')),
  -- Proteção da página pública (anon): tetos de TENTATIVAS por hora (total), por IP/dia e por telefone/dia.
  public_max_per_hour int not null default 20 check (public_max_per_hour >= 1),
  public_max_per_ip_day int not null default 3 check (public_max_per_ip_day >= 1),
  public_max_per_phone_day int not null default 3 check (public_max_per_phone_day >= 1),
  updated_at timestamptz default now()
);

create table if not exists public.closers (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  whatsapp text,
  email text,
  active boolean not null default true,
  weight int not null default 1 check (weight between 1 and 10),
  color text default '#D97706',
  created_at timestamptz default now()
);

create table if not exists public.availability_rules (
  id uuid primary key default gen_random_uuid(),
  closer_id uuid not null references public.closers (id) on delete cascade,
  weekday smallint not null check (weekday between 0 and 6),   -- 0 = domingo
  start_time time not null,
  end_time time not null,
  check (start_time < end_time)
);

create table if not exists public.blocks (
  id uuid primary key default gen_random_uuid(),
  closer_id uuid references public.closers (id) on delete cascade,  -- null = todos os closers
  starts_at timestamptz not null,
  ends_at timestamptz not null,
  reason text,
  check (starts_at < ends_at)
);

create table if not exists public.leads (
  id uuid primary key default gen_random_uuid(),
  phone text not null unique,
  name text,
  email text,
  source text not null default 'whatsapp',
  stage text not null default 'novo' check (stage in (
    'novo', 'respondeu', 'qualificado', 'desqualificado', 'horarios_ofertados',
    'agendado', 'confirmado', 'compareceu', 'no_show', 'reagendado', 'venda', 'perdido')),
  profile text,
  qualification jsonb not null default '{}',
  score int,
  disqualify_reason text,
  takeover_until timestamptz,
  closer_id uuid references public.closers (id) on delete set null,
  opt_out boolean not null default false,
  followup_step int not null default 0,
  last_inbound_at timestamptz,
  last_outbound_at timestamptz,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create table if not exists public.messages (
  id bigserial primary key,
  lead_id uuid not null references public.leads (id) on delete cascade,
  direction text not null check (direction in ('in', 'out')),
  author text not null check (author in ('agente', 'dono', 'lead', 'sistema')),
  body text,
  media_type text,
  wa_id text unique,
  created_at timestamptz default now()
);

create table if not exists public.funnel_events (
  id bigserial primary key,
  lead_id uuid not null references public.leads (id) on delete cascade,
  stage text not null,
  created_at timestamptz default now()
);

create table if not exists public.appointments (
  id uuid primary key default gen_random_uuid(),
  lead_id uuid not null references public.leads (id) on delete cascade,
  closer_id uuid not null references public.closers (id),
  starts_at timestamptz not null,
  ends_at timestamptz not null,
  status text not null default 'agendado'
    check (status in ('agendado', 'confirmado', 'feito', 'no_show', 'reagendado', 'cancelado', 'venda')),
  sale_value numeric(12, 2),
  source text not null default 'agente' check (source in ('agente', 'pagina', 'manual')),
  google_event_id text,
  notes text,
  reminder_24h_at timestamptz,
  reminder_2h_at timestamptz,
  handoff_sent_at timestamptz,
  handoff_1h_sent_at timestamptz,
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  check (starts_at < ends_at),
  -- Última barreira contra dupla reserva: o mesmo closer não pode ter dois
  -- agendamentos ATIVOS (agendado/confirmado) em intervalos que se sobrepõem.
  exclude using gist (closer_id with =, tstzrange(starts_at, ends_at, '[)') with &&)
    where (status in ('agendado', 'confirmado'))
);

create table if not exists public.objections (
  id bigserial primary key,
  lead_id uuid not null references public.leads (id) on delete cascade,
  category text not null,
  excerpt text,
  handled boolean not null default false,
  created_at timestamptz default now()
);

create table if not exists public.outbox (
  id bigserial primary key,
  lead_id uuid references public.leads (id) on delete set null,
  phone text not null,
  body text not null,
  status text not null default 'pendente' check (status in ('pendente', 'enviando', 'enviado', 'erro')),
  attempts int not null default 0,
  last_error text,
  wa_key_id text,
  created_by text not null default 'painel',
  -- chave de idempotência gerada pelo cliente (painel): o mesmo envio repetido não duplica.
  client_key text unique,
  created_at timestamptz default now(),
  sent_at timestamptz
);

-- Quem pode usar o painel, por ID do usuário no Auth (e-mail do JWT não prova posse:
-- com "Confirm email" desligado alguém pode se cadastrar com o e-mail do dono).
-- Cadastro aberto no Auth não dá acesso a nada. Escrita só pelo service_role (instalador).
create table if not exists public.panel_users (
  user_id uuid primary key references auth.users (id) on delete cascade,
  email text,
  created_at timestamptz default now()
);

-- TENTATIVAS da página pública (sucesso ou não): limites por hora, por IP e por telefone.
-- Interna: sem acesso para anon/authenticated; só public_book (security definer) escreve.
create table if not exists public.public_book_attempts (
  id bigserial primary key,
  ip text,
  phone text,
  ok boolean not null default false,   -- true só quando a reserva foi efetivada (o teto global conta só estas)
  created_at timestamptz not null default now()
);
-- bancos que já tinham a versão anterior da tabela / das configurações
alter table public.public_book_attempts add column if not exists phone text;
alter table public.public_book_attempts add column if not exists ok boolean not null default false;
alter table public.schedule_settings
  add column if not exists public_max_per_phone_day int not null default 3 check (public_max_per_phone_day >= 1);
create index if not exists public_book_attempts_created_idx on public.public_book_attempts (created_at);
create index if not exists public_book_attempts_ip_idx on public.public_book_attempts (ip, created_at);
create index if not exists public_book_attempts_phone_idx on public.public_book_attempts (phone, created_at);

-- Telefone canônico: E.164 COM '+' em leads.phone (o motor normaliza o JID da Evolution antes
-- de gravar/buscar; a página pública e o painel já mandam com '+'). Linhas antigas só são
-- normalizadas quando a forma é INEQUÍVOCA: JID (...@s.whatsapp.net / @c.us) ou só dígitos com
-- 12 a 15 dígitos (DDI incluso, como o motor sempre gravou). Número local ('(11) 99999-9999')
-- NUNCA é inferido: se sobrar qualquer lead fora do E.164, a migration ABORTA listando os ids
-- (fail-closed). Assim public_book e o motor sempre casam por igualdade exata em E.164.
do $$
declare
  v_dup text;
  v_pend text;
begin
  create temp table _s18_phone_norm on commit drop as
  select l.id,
         '+' || regexp_replace(regexp_replace(l.phone, '[:@].*$', ''), '[^0-9]', '', 'g') as novo
  from public.leads l
  where l.phone !~ '^\+[1-9][0-9]{7,14}$'
    and (
      (l.phone ~ '^[0-9]+(:[0-9]+)?@(s\.whatsapp\.net|c\.us)$'
         and regexp_replace(l.phone, '[:@].*$', '') ~ '^[1-9][0-9]{7,14}$')
      or l.phone ~ '^[1-9][0-9]{11,14}$');

  -- colisão: duas linhas antigas com o mesmo número normalizado, ou uma que já existe canônica
  select string_agg(x.ids, ' | ' order by x.ids) into v_dup from (
    select novo || ': ' || string_agg(id::text, ', ' order by id::text) as ids
    from (
      select n.id, n.novo from _s18_phone_norm n
      union all
      select o.id, o.phone from public.leads o
      where o.phone ~ '^\+[1-9][0-9]{7,14}$' and o.phone in (select novo from _s18_phone_norm)
    ) u
    group by novo having count(*) > 1
  ) x;
  if v_dup is not null then
    raise exception 'Telefones duplicados após normalizar para E.164 (número: ids dos leads): %. Una ou apague os leads repetidos e rode a migration de novo.', v_dup;
  end if;

  update public.leads l set phone = n.novo from _s18_phone_norm n where n.id = l.id;

  -- ambíguo restante (número local etc.): aborta, nada é inferido
  select string_agg(l.id::text, ', ' order by l.id::text) into v_pend
  from public.leads l where l.phone !~ '^\+[1-9][0-9]{7,14}$';
  if v_pend is not null then
    raise exception 'leads com telefone fora do padrão E.164 (ids: %): corrija esses telefones com DDI (+55...) e rode de novo.', v_pend;
  end if;

  -- outbox: mesma regra inequívoca (JID / 12-15 dígitos) antes do sweep
  update public.outbox o
  set phone = '+' || regexp_replace(regexp_replace(o.phone, '[:@].*$', ''), '[^0-9]', '', 'g')
  where o.phone !~ '^\+[1-9][0-9]{7,14}$'
    and (
      (o.phone ~ '^[0-9]+(:[0-9]+)?@(s\.whatsapp\.net|c\.us)$'
         and regexp_replace(o.phone, '[:@].*$', '') ~ '^[1-9][0-9]{7,14}$')
      or o.phone ~ '^[1-9][0-9]{11,14}$');

  -- só vai para erro a linha SEM wa_key_id que continua fora do E.164 (nunca rebaixa a que já tem)
  update public.outbox o
  set status = 'erro', attempts = greatest(o.attempts, 99),
      last_error = 'telefone_invalido: corrija o número do lead antes de reenviar'
  where o.status in ('pendente', 'enviando') and o.wa_key_id is null
    and o.phone !~ '^\+[1-9][0-9]{7,14}$';
end;
$$;
alter table public.leads drop constraint if exists leads_phone_e164;
-- validado: a normalização acima garante que nenhuma linha antiga viola
alter table public.leads add constraint leads_phone_e164 check (phone ~ '^\+[1-9][0-9]{7,14}$');

-- Um agendamento ativo por lead (a última barreira, além da checagem em _book_core).
create unique index if not exists appointments_one_active_per_lead
  on public.appointments (lead_id) where status in ('agendado', 'confirmado');

-- Índices de apoio (consultas do painel, do motor e dos horários)
create index if not exists leads_stage_idx on public.leads (stage);
create index if not exists leads_created_at_idx on public.leads (created_at);
create index if not exists messages_lead_created_idx on public.messages (lead_id, created_at);
create index if not exists funnel_events_lead_idx on public.funnel_events (lead_id);
create index if not exists funnel_events_created_idx on public.funnel_events (created_at);
create index if not exists appointments_closer_starts_idx on public.appointments (closer_id, starts_at);
create index if not exists appointments_lead_idx on public.appointments (lead_id);
create index if not exists appointments_status_starts_idx on public.appointments (status, starts_at);
create index if not exists objections_created_idx on public.objections (created_at);
create index if not exists outbox_status_idx on public.outbox (status, created_at);
create index if not exists availability_rules_closer_idx on public.availability_rules (closer_id, weekday);
create index if not exists blocks_range_idx on public.blocks (starts_at, ends_at);

-- Linhas únicas já nascem criadas: o painel só atualiza.
insert into public.agent_config (id) values (1) on conflict do nothing;
insert into public.schedule_settings (id) values (1) on conflict do nothing;

-- ---------------------------------------------------------------------
-- Triggers
-- ---------------------------------------------------------------------

create or replace function public.set_updated_at()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists agent_config_updated_at on public.agent_config;
create trigger agent_config_updated_at before update on public.agent_config
  for each row execute function public.set_updated_at();
drop trigger if exists schedule_settings_updated_at on public.schedule_settings;
create trigger schedule_settings_updated_at before update on public.schedule_settings
  for each row execute function public.set_updated_at();
drop trigger if exists leads_updated_at on public.leads;
create trigger leads_updated_at before update on public.leads
  for each row execute function public.set_updated_at();
drop trigger if exists appointments_updated_at on public.appointments;
create trigger appointments_updated_at before update on public.appointments
  for each row execute function public.set_updated_at();

-- Mudou o estágio do lead (ou o lead nasceu): registra no funil.
create or replace function public.log_lead_stage()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if tg_op = 'INSERT' then
    insert into public.funnel_events (lead_id, stage, created_at)
    values (new.id, new.stage, coalesce(new.created_at, now()));
  elsif new.stage is distinct from old.stage then
    insert into public.funnel_events (lead_id, stage) values (new.id, new.stage);
  end if;
  return new;
end;
$$;

drop trigger if exists leads_stage_log on public.leads;
create trigger leads_stage_log after insert or update of stage on public.leads
  for each row execute function public.log_lead_stage();

-- O resultado da call, marcado no painel ou pelo motor, atualiza o estágio do lead.
-- Cancelar a call NÃO significa lead perdido: se o lead não tem outra call ativa
-- (agendado/confirmado) e ainda está num estágio de "call marcada", volta a 'qualificado'.
-- Estágios de resultado (compareceu, no_show, venda...) nunca são sobrescritos pelo cancelamento.
create or replace function public.sync_lead_stage_from_appointment()
returns trigger
language plpgsql
set search_path = public
as $$
declare
  v_stage text;
begin
  if tg_op = 'UPDATE' and new.status is not distinct from old.status then
    return new;
  end if;
  -- public_book protege o estágio de um lead que já comprou/compareceu (decisão 7).
  if coalesce(current_setting('setup18.keep_stage_lead', true), '') = new.lead_id::text then
    return new;
  end if;
  -- lead em 'venda' só sai de venda por ação explícita no painel de leads: a mudança de status
  -- de OUTRA call dele (no_show, feito...) não o rebaixa. Só marcar a própria call como venda mantém.
  if new.status <> 'cancelado' and exists (
       select 1 from public.leads where id = new.lead_id and stage = 'venda') then
    return new;
  end if;
  v_stage := case new.status
    when 'agendado'   then 'agendado'
    when 'confirmado' then 'confirmado'
    when 'feito'      then 'compareceu'
    when 'no_show'    then 'no_show'
    when 'reagendado' then 'reagendado'
    when 'venda'      then 'venda'
    else null
  end;
  if new.status = 'cancelado' then
    if not exists (
      select 1 from public.appointments x
      where x.lead_id = new.lead_id and x.id <> new.id and x.status in ('agendado', 'confirmado')
    ) then
      update public.leads set stage = 'qualificado'
      where id = new.lead_id and stage in ('agendado', 'confirmado', 'reagendado', 'horarios_ofertados');
    end if;
    return new;
  end if;
  if v_stage is not null then
    update public.leads set stage = v_stage where id = new.lead_id and stage is distinct from v_stage;
  end if;
  return new;
end;
$$;

drop trigger if exists appointments_sync_lead_stage on public.appointments;
create trigger appointments_sync_lead_stage after insert or update of status on public.appointments
  for each row execute function public.sync_lead_stage_from_appointment();

-- ---------------------------------------------------------------------
-- Acesso: quem é o dono do painel e quem é de confiança
-- ---------------------------------------------------------------------

-- security definer de propósito: a policy da própria panel_users chama esta
-- função, e sem isso haveria recursão infinita de RLS.
create or replace function public.is_panel_user()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1 from public.panel_users p
    where p.user_id = auth.uid()
  );
$$;

-- Chamador de confiança das funções security definer que gravam dados:
-- o motor (service_role) ou o dono do painel (user_id em panel_users).
create or replace function public._is_trusted_caller()
returns boolean
language sql
stable
set search_path = public
as $$
  select coalesce(auth.role(), '') = 'service_role' or public.is_panel_user();
$$;

-- ---------------------------------------------------------------------
-- Horários disponíveis
-- ---------------------------------------------------------------------

-- Grade BRUTA: só as regras de disponibilidade (dia da semana + horários no
-- fuso do negócio), sem descontar bloqueios, agendamentos nem antecedência.
-- Interna: usada por available_slots e pelo book_slot (para separar
-- "fora da agenda" de "horário ocupado").
create or replace function public._slot_grid(p_from timestamptz, p_to timestamptz, p_closer uuid default null)
returns table (starts_at timestamptz, ends_at timestamptz, closer_id uuid)
language sql
stable
set search_path = public
as $$
  with s as (
    select * from public.schedule_settings where id = 1
  ),
  rng as (
    select (p_from at time zone s.timezone)::date as d0,
           (least(p_to, p_from + interval '366 days') at time zone s.timezone)::date as d1
    from s
  ),
  days as (
    select rng.d0 + k as day
    from rng, generate_series(0, rng.d1 - rng.d0) as k
  ),
  cand as (
    select r.closer_id,
           ((days.day + r.start_time) + (n.i * s.slot_step_minutes) * interval '1 minute')
             at time zone s.timezone as st
    from s
    cross join days
    join public.availability_rules r on r.weekday = extract(dow from days.day)::int
    join public.closers c on c.id = r.closer_id and c.active and (p_closer is null or c.id = p_closer)
    cross join lateral generate_series(
      0,
      floor((extract(epoch from (r.end_time - r.start_time)) / 60 - s.call_minutes) / s.slot_step_minutes)::int
    ) as n(i)
  )
  select distinct
         cand.st as starts_at,
         cand.st + s.call_minutes * interval '1 minute' as ends_at,
         cand.closer_id
  from cand
  cross join s
  where cand.st >= p_from
    and cand.st + s.call_minutes * interval '1 minute' <= p_to
  order by 1, 3;
$$;

-- Horários livres por closer. security invoker: o painel só enxerga se estiver
-- em panel_users (RLS); o motor usa service_role.
create or replace function public.available_slots(p_from timestamptz, p_to timestamptz, p_closer uuid default null)
returns table (starts_at timestamptz, ends_at timestamptz, closer_id uuid)
language sql
stable
set search_path = public
as $$
  with s as (
    select * from public.schedule_settings where id = 1
  )
  select g.starts_at, g.ends_at, g.closer_id
  from s
  cross join lateral public._slot_grid(
    greatest(p_from, now()),
    least(p_to, now() + s.days_ahead * interval '1 day' + s.call_minutes * interval '1 minute'),
    p_closer
  ) g
  where g.starts_at >= now() + s.min_notice_hours * interval '1 hour'
    and g.starts_at <= now() + s.days_ahead * interval '1 day'
    and not exists (
      select 1 from public.blocks b
      where (b.closer_id is null or b.closer_id = g.closer_id)
        and b.starts_at < g.ends_at
        and b.ends_at > g.starts_at
    )
    and not exists (
      select 1 from public.appointments a
      where a.closer_id = g.closer_id
        and a.status in ('agendado', 'confirmado')
        and a.starts_at - s.buffer_minutes * interval '1 minute' < g.ends_at
        and a.ends_at + s.buffer_minutes * interval '1 minute' > g.starts_at
    )
  order by g.starts_at, g.closer_id;
$$;

-- ---------------------------------------------------------------------
-- Agendamento
-- ---------------------------------------------------------------------

-- Núcleo do agendamento, compartilhado por book_slot (motor/painel) e
-- public_book (página pública). Interna: ninguém recebe execute.
-- Serializa todas as reservas com um lock e, mesmo assim, deixa a constraint
-- de exclusão como última barreira.
-- p_keep_closer: só a página pública pede para preservar o closer que o lead já tinha (lead em
-- estágio final). book_slot e reschedule_slot passam false: o lead segue o closer da call nova.
drop function if exists public._book_core(uuid, timestamptz, text, uuid, text);
create or replace function public._book_core(
  p_lead uuid,
  p_starts_at timestamptz,
  p_source text,
  p_closer uuid,
  p_notes text,
  p_keep_closer boolean default false
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  s public.schedule_settings%rowtype;
  v_end timestamptz;
  v_closer uuid;
  v_cname text;
  v_id uuid;
  v_keep boolean;
  v_stage text;
begin
  perform pg_advisory_xact_lock(hashtextextended('setup18_sdr_book', 0));

  if not exists (select 1 from public.leads where id = p_lead) then
    return jsonb_build_object('ok', false, 'error', 'lead_inexistente');
  end if;

  select * into s from public.schedule_settings where id = 1;
  if not found or p_starts_at is null then
    return jsonb_build_object('ok', false, 'error', 'fora_da_agenda');
  end if;

  if exists (
    select 1 from public.appointments a
    where a.lead_id = p_lead and a.status in ('agendado', 'confirmado') and a.ends_at > now()
  ) then
    return jsonb_build_object('ok', false, 'error', 'lead_ja_agendado');
  end if;

  v_end := p_starts_at + s.call_minutes * interval '1 minute';

  -- fora da agenda: não cai em nenhuma regra de disponibilidade ou fora da
  -- janela (antecedência mínima / dias à frente).
  if p_starts_at < now() + s.min_notice_hours * interval '1 hour'
     or p_starts_at > now() + s.days_ahead * interval '1 day'
     or not exists (
       select 1 from public._slot_grid(p_starts_at, v_end, p_closer) g where g.starts_at = p_starts_at
     ) then
    return jsonb_build_object('ok', false, 'error', 'fora_da_agenda');
  end if;

  -- escolhe o closer entre os livres naquele horário: o menos acionado nos
  -- últimos 30 dias (no rodízio todos pesam 1; em "peso" vale closers.weight).
  select c.id, c.name
    into v_closer, v_cname
  from public.available_slots(p_starts_at, v_end, p_closer) a
  join public.closers c on c.id = a.closer_id
  where a.starts_at = p_starts_at
  order by
    (select count(*) from public.appointments x
      where x.closer_id = c.id and x.created_at >= now() - interval '30 days')::numeric
      / (case when s.distribution = 'peso' then c.weight else 1 end),
    c.name,
    c.id
  limit 1;

  if v_closer is null then
    return jsonb_build_object('ok', false, 'error', 'slot_indisponivel');
  end if;

  -- O estágio só é promovido a 'agendado' a partir de estágios NÃO terminais. Lead que já
  -- comprou/compareceu ou foi desqualificado/perdido mantém o estágio (a call é criada mesmo assim).
  -- public_book também pode marcar o lead (setup18.keep_stage_lead) pelo mesmo motivo.
  select stage into v_stage from public.leads where id = p_lead;
  v_keep := v_stage in ('venda', 'compareceu', 'desqualificado', 'perdido')
            or coalesce(current_setting('setup18.keep_stage_lead', true), '') = p_lead::text;
  if v_keep then
    perform set_config('setup18.keep_stage_lead', p_lead::text, true);  -- o trigger do agendamento respeita
  end if;

  begin
    -- call ativa que já terminou e ninguém marcou (o índice único não deixa coexistir com a nova):
    -- a nova a substitui, ficando 'reagendado'. Dentro do bloco: se a reserva falhar, volta tudo.
    update public.appointments set status = 'reagendado',
      reminder_24h_at = null, reminder_2h_at = null, handoff_sent_at = null, handoff_1h_sent_at = null
    where lead_id = p_lead and status in ('agendado', 'confirmado') and ends_at <= now();
    insert into public.appointments (lead_id, closer_id, starts_at, ends_at, source, notes)
    values (p_lead, v_closer, p_starts_at, v_end, p_source, p_notes)
    returning id into v_id;
  exception
    when exclusion_violation then
      perform set_config('setup18.keep_stage_lead', '', true);
      return jsonb_build_object('ok', false, 'error', 'slot_indisponivel');
    when unique_violation then
      perform set_config('setup18.keep_stage_lead', '', true);
      return jsonb_build_object('ok', false, 'error', 'lead_ja_agendado');
  end;
  perform set_config('setup18.keep_stage_lead', '', true);

  if v_keep then
    if p_keep_closer then
      update public.leads set closer_id = coalesce(closer_id, v_closer) where id = p_lead;
    else
      update public.leads set closer_id = v_closer where id = p_lead;
    end if;
  else
    update public.leads set stage = 'agendado', closer_id = v_closer where id = p_lead;
  end if;

  return jsonb_build_object(
    'ok', true,
    'appointment_id', v_id,
    'closer_id', v_closer,
    'closer_name', v_cname,
    'starts_at', p_starts_at,
    'ends_at', v_end
  );
end;
$$;

create or replace function public.book_slot(
  p_lead uuid,
  p_starts_at timestamptz,
  p_source text default 'agente',
  p_closer uuid default null,
  p_notes text default null
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
begin
  -- security definer ignora RLS, então a porta de entrada confere quem chamou.
  if not public._is_trusted_caller() then
    raise exception 'sem permissao para agendar' using errcode = '42501';
  end if;
  return public._book_core(p_lead, p_starts_at, p_source, p_closer, p_notes);
end;
$$;

-- Reagendamento ATÔMICO: marca o antigo como 'reagendado' e reserva o novo na mesma
-- transação. Se o novo falhar, nada muda (o antigo continua como estava).
create or replace function public.reschedule_slot(
  p_appointment uuid,
  p_starts_at timestamptz,
  p_closer uuid default null
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  a public.appointments%rowtype;
  v_tz text;
  v_res jsonb;
  v_err text;
  v_stage text;
begin
  if not public._is_trusted_caller() then
    raise exception 'sem permissao para reagendar' using errcode = '42501';
  end if;

  select * into a from public.appointments where id = p_appointment for update;
  if not found then
    return jsonb_build_object('ok', false, 'error', 'agendamento_inexistente');
  end if;
  if a.status not in ('agendado', 'confirmado') then
    return jsonb_build_object('ok', false, 'error', 'agendamento_inativo');
  end if;

  select timezone into v_tz from public.schedule_settings where id = 1;

  -- Lead em estágio final (venda, compareceu, desqualificado, perdido) mantém o estágio: o trigger
  -- do agendamento e o _book_core respeitam setup18.keep_stage_lead (ler o estágio ANTES de mexer).
  select stage into v_stage from public.leads where id = a.lead_id;
  if v_stage in ('venda', 'compareceu', 'desqualificado', 'perdido') then
    perform set_config('setup18.keep_stage_lead', a.lead_id::text, true);
  end if;

  begin
    update public.appointments set status = 'reagendado' where id = a.id;
    v_res := public._book_core(
      a.lead_id, p_starts_at, a.source, p_closer,
      'reagendado de ' || to_char(a.starts_at at time zone coalesce(v_tz, 'America/Sao_Paulo'), 'DD/MM/YYYY HH24:MI'));
    if (v_res ->> 'ok')::boolean is not true then
      raise exception using errcode = 'SD001', message = coalesce(v_res ->> 'error', 'slot_indisponivel');
    end if;
  exception when sqlstate 'SD001' then
    -- o rollback do bloco devolve o antigo ao estado original
    get stacked diagnostics v_err = message_text;
    perform set_config('setup18.keep_stage_lead', '', true);
    return jsonb_build_object('ok', false, 'error', case v_err
      when 'fora_da_agenda' then 'fora_da_agenda'
      else 'slot_indisponivel'
    end);
  end;
  perform set_config('setup18.keep_stage_lead', '', true);

  -- Flags de envio ligadas ao horário (lembretes 24h/2h, repasse, repasse de 1h) zeradas na MESMA
  -- transação da troca: o horário novo recebe lembretes e repasse como se fosse a primeira vez.
  update public.appointments
     set reminder_24h_at = null, reminder_2h_at = null, handoff_sent_at = null, handoff_1h_sent_at = null
   where id in (a.id, (v_res ->> 'appointment_id')::uuid);

  return v_res || jsonb_build_object('old_appointment_id', a.id);
end;
$$;

-- ---------------------------------------------------------------------
-- Página pública (anon)
-- ---------------------------------------------------------------------

-- Horários distintos, sem expor closer. Slug errado ou página desativada: vazio.
create or replace function public.public_slots(p_slug text, p_from timestamptz, p_to timestamptz)
returns table (starts_at timestamptz, ends_at timestamptz)
language plpgsql
stable
security definer
set search_path = public
as $$
#variable_conflict use_column
begin
  if not exists (
    select 1 from public.schedule_settings s
    where s.id = 1 and s.public_enabled and s.public_slug is not null and s.public_slug = p_slug
  ) then
    return;
  end if;
  return query
    select distinct a.starts_at, a.ends_at
    from public.available_slots(p_from, p_to) a
    order by 1;
end;
$$;

create or replace function public.public_book(
  p_slug text,
  p_name text,
  p_email text,
  p_whatsapp text,
  p_starts_at timestamptz,
  p_hp text default ''
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  s public.schedule_settings%rowtype;
  -- nome saneado: sem URL/domínio/http/www, sem quebra de linha nem chaves, no máximo 60 caracteres
  -- (padrão genérico, não lista de TLD: qualquer ponto seguido de 2+ letras, "://" ou "www")
  v_name text;
  v_email text := btrim(coalesce(p_email, ''));
  v_phone text := regexp_replace(btrim(coalesce(p_whatsapp, '')), '[\s().-]', '', 'g');
  v_lead uuid;
  v_res jsonb;
  v_err text;
  v_local timestamp;
  v_company text;
  v_hdr json;
  v_xff text[];
  v_ip text;
  v_ip_conhecido boolean;
  v_prev_stage text;
  v_prev_closer uuid;
  v_prev_optout boolean;
  v_silent boolean := false;
  v_attempt bigint;
begin
  -- honeypot: robô preencheu o campo escondido. Finge sucesso e não grava nada.
  if coalesce(p_hp, '') <> '' then
    return jsonb_build_object('ok', true, 'starts_at', p_starts_at);
  end if;

  select * into s from public.schedule_settings where id = 1;
  if not found or not s.public_enabled or s.public_slug is null or s.public_slug is distinct from p_slug then
    return jsonb_build_object('ok', false, 'error', 'pagina_desativada');
  end if;

  -- Teto de tamanho ANTES de qualquer regexp: nome gigante sem espaços não gasta CPU do banco.
  if char_length(coalesce(p_name, '')) > 200 then
    return jsonb_build_object('ok', false, 'error', 'dados_invalidos');
  end if;
  v_name := left(btrim(regexp_replace(regexp_replace(regexp_replace(
                    coalesce(p_name, ''),
                    '\S*://\S*|https?:\S*|\mwww\S*|\S*\.[[:alpha:]]{2,}\S*', ' ', 'gi'),
                    '[{}<>]', ' ', 'g'),
                    '\s+', ' ', 'g')), 60);

  if p_starts_at is null
     or char_length(v_name) not between 2 and 60
     or v_phone !~ '^\+[1-9][0-9]{7,14}$' then
    return jsonb_build_object('ok', false, 'error', 'dados_invalidos');
  end if;

  -- e-mail: conjunto seguro de caracteres (sem < > aspas parênteses barra), até 254
  if char_length(v_email) > 254
     or v_email !~ '^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$' then
    return jsonb_build_object('ok', false, 'error', 'email_invalido');
  end if;

  -- IP de quem chamou (PostgREST expõe os headers). NÃO se confia em cf-connecting-ip: quem chama
  -- o endpoint direto, com a anon key, escolhe o valor que quiser. Vale o ÚLTIMO valor de
  -- x-forwarded-for (o que o proxy do Supabase anexou), nunca os anteriores (o cliente escreve).
  -- Sem o header, a tentativa fica registrada como 'desconhecido' mas não conta no teto por IP. O limite por IP é APROXIMADO: a proteção real contra
  -- abuso é Turnstile/WAF na borda (ver README).
  begin
    v_hdr := nullif(current_setting('request.headers', true), '')::json;
  exception when others then
    v_hdr := null;
  end;
  v_xff := string_to_array(coalesce(v_hdr ->> 'x-forwarded-for', ''), ',');
  v_ip := left(coalesce(
            nullif(btrim(case when coalesce(array_length(v_xff, 1), 0) > 0 then v_xff[array_length(v_xff, 1)] end), ''),
            'desconhecido'), 64);
  -- IP desconhecido (sem o header) NÃO entra no teto por IP: seria um balde global que um só
  -- visitante esgota para todos. Sem header valem só o teto por telefone e o global.
  v_ip_conhecido := nullif(btrim(case when coalesce(array_length(v_xff, 1), 0) > 0 then v_xff[array_length(v_xff, 1)] end), '') is not null;

  -- Limites contra abuso (a agenda é um recurso finito). Contam TENTATIVAS, não só sucessos.
  -- Serializa para a contagem não furar.
  perform pg_advisory_xact_lock(hashtextextended('setup18_sdr_public_limit', 0));
  delete from public.public_book_attempts where created_at < now() - interval '2 days';
  -- Teto GLOBAL por hora: conta só reservas EFETIVAS (ok = true). Tentativa recusada (horário
  -- ocupado, dados ruins) não derruba a página para os outros visitantes. Por IP e por telefone
  -- continuam contando TENTATIVAS. Contra quem enche a agenda com reservas reais, o contorno é
  -- Turnstile/WAF na borda (ver README).
  if (select count(*) from public.public_book_attempts
       where ok and created_at > now() - interval '1 hour') >= s.public_max_per_hour
     or (v_ip_conhecido and (select count(*) from public.public_book_attempts
          where ip = v_ip and created_at > now() - interval '1 day') >= s.public_max_per_ip_day)
     or (select count(*) from public.public_book_attempts
          where phone = v_phone and created_at > now() - interval '1 day') >= s.public_max_per_phone_day then
    return jsonb_build_object('ok', false, 'error', 'limite_atingido');
  end if;

  -- Registra a tentativa ANTES de tentar reservar (fora do bloco abaixo: uma reserva que falha
  -- não pode apagar o registro, senão o atacante tenta de graça).
  insert into public.public_book_attempts (ip, phone) values (v_ip, v_phone)
  returning id into v_attempt;

  select l.stage, l.closer_id, l.opt_out into v_prev_stage, v_prev_closer, v_prev_optout
  from public.leads l where l.phone = v_phone;

  -- Quem pediu para não receber mensagens não é agendado nem tocado pela página pública.
  if coalesce(v_prev_optout, false) then
    return jsonb_build_object('ok', false, 'error', 'indisponivel');
  end if;
  -- Lead em estágio terminal (venda, compareceu, desqualificado, perdido): a call é agendada, mas sem
  -- WhatsApp de confirmação e sem mudar o estágio.
  v_silent := coalesce(v_prev_stage in ('venda', 'compareceu', 'desqualificado', 'perdido'), false);

  begin
    -- Cria o lead (ou reaproveita pelo telefone). Lead existente: a página só PREENCHE
    -- nome/e-mail que estejam vazios, nunca sobrescreve (quem sabe só o telefone não
    -- pode trocar os dados de um lead que já existe).
    insert into public.leads (phone, name, email, source)
    values (v_phone, v_name, v_email, 'pagina')
    on conflict (phone) do update
      set name = coalesce(nullif(public.leads.name, ''), excluded.name),
          email = coalesce(nullif(public.leads.email, ''), excluded.email)
    returning id into v_lead;

    -- Lead em estágio terminal não volta para 'agendado' pela página pública.
    if v_prev_stage in ('venda', 'compareceu', 'desqualificado', 'perdido') then
      perform set_config('setup18.keep_stage_lead', v_lead::text, true);
    end if;

    v_res := public._book_core(v_lead, p_starts_at, 'pagina', null, null, true);

    perform set_config('setup18.keep_stage_lead', '', true);

    if (v_res ->> 'ok')::boolean is not true then
      -- desfaz o lead criado nesta tentativa: página pública não suja o banco
      raise exception using errcode = 'SD001', message = coalesce(v_res ->> 'error', 'slot_indisponivel');
    end if;

    -- closer_id de lead existente nunca é trocado pela página.
    if v_prev_closer is not null then
      update public.leads set closer_id = v_prev_closer where id = v_lead and closer_id is distinct from v_prev_closer;
    end if;

    select nullif(btrim(company), '') into v_company from public.agent_config where id = 1;
    v_local := p_starts_at at time zone s.timezone;

    -- A confirmação NUNCA leva texto livre digitado na página (nome, e-mail): quem sabe só o
    -- telefone de outra pessoa não pode fazer o número do dono dizer o que quiser. Texto neutro
    -- com o nome da empresa (configurado pelo dono) + data e hora. test_only é conferido pelo
    -- motor no envio.
    if not v_silent then
      insert into public.outbox (lead_id, phone, body, created_by)
      values (
        v_lead,
        v_phone,
        'Olá! Sua conversa'
          || case when v_company is not null then ' com ' || left(v_company, 80) else '' end
          || ' está confirmada para ' || to_char(v_local, 'DD/MM/YYYY') || ' às ' || to_char(v_local, 'HH24:MI') || '.',
        'pagina'
      );
    end if;
  exception when sqlstate 'SD001' then
    get stacked diagnostics v_err = message_text;
    return jsonb_build_object('ok', false, 'error', case v_err
      when 'lead_ja_agendado' then 'ja_agendado'
      else 'slot_indisponivel'
    end);
  end;

  update public.public_book_attempts set ok = true where id = v_attempt;
  return jsonb_build_object('ok', true, 'starts_at', p_starts_at);
end;
$$;

-- ---------------------------------------------------------------------
-- Painel: resultado da call em UMA transação
-- ---------------------------------------------------------------------
-- Atualiza o status do agendamento (e o valor da venda), o estágio do lead e o funil juntos:
-- o estágio e o evento de funil saem dos triggers de appointments/leads, na mesma transação.
-- Mapa status -> estágio: agendado->agendado, confirmado->confirmado, feito->compareceu,
-- no_show->no_show, venda->venda, reagendado->reagendado. 'cancelado' segue a regra do trigger
-- (volta a 'qualificado' se não há outra call ativa; nunca marca o lead como perdido).
-- security invoker + is_panel_user(): só o dono do painel (a RLS também vale).
create or replace function public.set_appointment_status(
  p_appointment uuid,
  p_status text,
  p_sale_value numeric default null
)
returns jsonb
language plpgsql
set search_path = public
as $$
declare
  a public.appointments%rowtype;
  v_stage text;
begin
  if not public.is_panel_user() then
    raise exception 'sem permissao para alterar agendamento' using errcode = '42501';
  end if;
  if p_status is null or p_status not in ('agendado', 'confirmado', 'feito', 'no_show', 'reagendado', 'cancelado', 'venda') then
    return jsonb_build_object('ok', false, 'error', 'status_invalido');
  end if;

  select * into a from public.appointments where id = p_appointment for update;
  if not found then
    return jsonb_build_object('ok', false, 'error', 'agendamento_inexistente');
  end if;

  -- reativar uma call antiga (cancelada, no_show, feita...) não pode criar um 2º agendamento ativo do lead
  if p_status in ('agendado', 'confirmado') and a.status not in ('agendado', 'confirmado')
     and exists (
       select 1 from public.appointments x
       where x.lead_id = a.lead_id and x.id <> a.id and x.status in ('agendado', 'confirmado')) then
    return jsonb_build_object('ok', false, 'error', 'lead_ja_agendado');
  end if;

  begin
    update public.appointments
    set status = p_status,
        sale_value = case when p_status = 'venda' then coalesce(p_sale_value, sale_value) else sale_value end
    where id = a.id;
  exception
    when exclusion_violation then
      -- reativar uma call antiga que hoje colide com outra do mesmo closer
      return jsonb_build_object('ok', false, 'error', 'slot_indisponivel');
    when unique_violation then
      return jsonb_build_object('ok', false, 'error', 'lead_ja_agendado');
  end;

  select stage into v_stage from public.leads where id = a.lead_id;
  return jsonb_build_object('ok', true, 'appointment_id', a.id, 'status', p_status, 'lead_stage', v_stage);
end;
$$;

-- ---------------------------------------------------------------------
-- Painel: números do funil
-- ---------------------------------------------------------------------
-- Leitura do funil por COORTE: leads criados no período [p_from, p_to) e até
-- onde cada um chegou. Resultado das calls (comparecimentos, no_shows, vendas,
-- valor) por data da call (starts_at) dentro do período.
-- Taxas são FRAÇÃO (0 a 1, três casas), nunca pontos percentuais (o painel multiplica por 100):
--   taxa_qualificacao = qualificados / leads
--   taxa_agendamento  = agendados / (leads com evento qualificante UNIÃO leads agendados): mesmo conjunto,
--                       então a taxa fica sempre em 0..1 (agendar pela página não exige qualificação prévia)
--   taxa_comparecimento = comparecimentos / (comparecimentos + no_shows)
-- Divisão por zero devolve null (o painel mostra "—").
-- Contagens do funil são de quem "chegou" na etapa, inclusive quem passou dela.
-- Atenção: 'desqualificados' (KPI) conta quem JÁ passou por desqualificado (evento de funil);
-- a lista 'desqualificacao' conta o estágio ATUAL. Um lead desqualificado e depois
-- reativado entra no KPI e não entra na lista de motivos: a diferença é intencional.
-- security invoker: respeita a RLS (quem não é do painel recebe zeros).
create or replace function public.dashboard_stats(p_from timestamptz, p_to timestamptz, p_closer uuid default null)
returns jsonb
language plpgsql
stable
set search_path = public
as $$
declare
  v_leads int;
  v_resp int;
  v_qual int;
  v_desq int;
  v_ofert int;
  v_conf int;
  v_reag int;
  v_perd int;
  v_agend int;
  v_base int;
  v_comp int;
  v_nos int;
  v_vendas int;
  v_valor numeric;
  v_obj jsonb;
  v_desqs jsonb;
begin
  select count(*) into v_leads
  from public.leads l
  where l.created_at >= p_from and l.created_at < p_to
    and (p_closer is null or l.closer_id = p_closer);

  select
    count(distinct e.lead_id) filter (where e.stage = any (array[
      'respondeu', 'qualificado', 'desqualificado', 'horarios_ofertados', 'agendado',
      'confirmado', 'compareceu', 'no_show', 'reagendado', 'venda'])),
    count(distinct e.lead_id) filter (where e.stage = any (array[
      'qualificado', 'horarios_ofertados', 'agendado', 'confirmado', 'compareceu',
      'no_show', 'reagendado', 'venda'])),
    count(distinct e.lead_id) filter (where e.stage = 'desqualificado'),
    count(distinct e.lead_id) filter (where e.stage = any (array[
      'horarios_ofertados', 'agendado', 'confirmado', 'compareceu', 'no_show',
      'reagendado', 'venda'])),
    count(distinct e.lead_id) filter (where e.stage = 'confirmado'),
    count(distinct e.lead_id) filter (where e.stage = 'reagendado'),
    count(distinct e.lead_id) filter (where e.stage = 'perdido')
  into v_resp, v_qual, v_desq, v_ofert, v_conf, v_reag, v_perd
  from public.funnel_events e
  join public.leads l on l.id = e.lead_id
  where l.created_at >= p_from and l.created_at < p_to
    and (p_closer is null or l.closer_id = p_closer);

  select count(distinct a.lead_id) into v_agend
  from public.appointments a
  join public.leads l on l.id = a.lead_id
  where l.created_at >= p_from and l.created_at < p_to
    and (p_closer is null or l.closer_id = p_closer);

  -- base da taxa de agendamento: quem tem evento qualificante OU agendou (o numerador está contido nela)
  select count(*) into v_base
  from public.leads l
  where l.created_at >= p_from and l.created_at < p_to
    and (p_closer is null or l.closer_id = p_closer)
    and (
      exists (select 1 from public.appointments a where a.lead_id = l.id)
      or exists (select 1 from public.funnel_events e where e.lead_id = l.id and e.stage = any (array[
        'qualificado', 'horarios_ofertados', 'agendado', 'confirmado', 'compareceu',
        'no_show', 'reagendado', 'venda']))
    );

  select
    count(*) filter (where a.status in ('feito', 'venda')),
    count(*) filter (where a.status = 'no_show'),
    count(*) filter (where a.status = 'venda'),
    coalesce(sum(a.sale_value) filter (where a.status = 'venda'), 0)
  into v_comp, v_nos, v_vendas, v_valor
  from public.appointments a
  where a.starts_at >= p_from and a.starts_at < p_to
    and (p_closer is null or a.closer_id = p_closer);

  select coalesce(jsonb_agg(jsonb_build_object('category', t.category, 'count', t.n, 'tratadas', t.tratadas)
                            order by t.n desc, t.category), '[]'::jsonb)
  into v_obj
  from (
    select o.category, count(*)::int as n, (count(*) filter (where o.handled))::int as tratadas
    from public.objections o
    join public.leads l on l.id = o.lead_id
    where o.created_at >= p_from and o.created_at < p_to
      and (p_closer is null or l.closer_id = p_closer)
    group by o.category
  ) t;

  select coalesce(jsonb_agg(jsonb_build_object('motivo', t.motivo, 'count', t.n)
                            order by t.n desc, t.motivo), '[]'::jsonb)
  into v_desqs
  from (
    select coalesce(nullif(l.disqualify_reason, ''), 'nao_informado') as motivo, count(*)::int as n
    from public.leads l
    where l.stage = 'desqualificado'
      and l.created_at >= p_from and l.created_at < p_to
      and (p_closer is null or l.closer_id = p_closer)
    group by 1
  ) t;

  return jsonb_build_object(
    'leads', v_leads,
    'respondeu', v_resp,
    'qualificados', v_qual,
    'desqualificados', v_desq,
    'agendados', v_agend,
    'comparecimentos', v_comp,
    'no_shows', v_nos,
    'vendas', v_vendas,
    'valor_vendas', v_valor,
    'taxa_qualificacao', round(v_qual::numeric / nullif(v_leads, 0), 3),
    'taxa_agendamento', round(v_agend::numeric / nullif(v_base, 0), 3),
    'taxa_comparecimento', round(v_comp::numeric / nullif(v_comp + v_nos, 0), 3),
    'funil', jsonb_build_array(
      jsonb_build_object('stage', 'novo', 'count', v_leads),
      jsonb_build_object('stage', 'respondeu', 'count', v_resp),
      jsonb_build_object('stage', 'qualificado', 'count', v_qual),
      jsonb_build_object('stage', 'desqualificado', 'count', v_desq),
      jsonb_build_object('stage', 'horarios_ofertados', 'count', v_ofert),
      jsonb_build_object('stage', 'agendado', 'count', v_agend),
      jsonb_build_object('stage', 'confirmado', 'count', v_conf),
      jsonb_build_object('stage', 'compareceu', 'count', v_comp),
      jsonb_build_object('stage', 'no_show', 'count', v_nos),
      jsonb_build_object('stage', 'reagendado', 'count', v_reag),
      jsonb_build_object('stage', 'venda', 'count', v_vendas),
      jsonb_build_object('stage', 'perdido', 'count', v_perd)
    ),
    'objecoes', v_obj,
    'desqualificacao', v_desqs
  );
end;
$$;

-- ---------------------------------------------------------------------
-- RLS: tudo fechado; só quem está em panel_users abre
-- ---------------------------------------------------------------------

do $$
declare
  t text;
begin
  foreach t in array array[
    'agent_config', 'schedule_settings', 'closers', 'availability_rules', 'blocks',
    'leads', 'messages', 'funnel_events', 'appointments', 'objections', 'outbox'
  ] loop
    execute format('alter table public.%I enable row level security', t);
    execute format('drop policy if exists panel_all on public.%I', t);
    execute format(
      'create policy panel_all on public.%I for all to authenticated '
      'using ((select public.is_panel_user())) with check ((select public.is_panel_user()))', t);
  end loop;
end;
$$;

-- panel_users: o painel só LÊ (quem entra no painel é decidido pelo service_role/instalador).
alter table public.panel_users enable row level security;
drop policy if exists panel_read on public.panel_users;
create policy panel_read on public.panel_users for select to authenticated
  using ((select public.is_panel_user()));
-- Tentativas da página pública: RLS ligada e nenhuma policy (só o dono da função enxerga).
alter table public.public_book_attempts enable row level security;

-- ---------------------------------------------------------------------
-- GRANTs explícitos (nada de confiar em privilégio automático)
-- ---------------------------------------------------------------------

-- Só os objetos DESTE setup (o projeto Supabase pode ter outras tabelas e funções;
-- nada de grant ou revoke em massa no schema inteiro).
do $$
declare
  t text;
  q text;
  f text;
begin
  foreach t in array array[
    'agent_config', 'schedule_settings', 'closers', 'availability_rules', 'blocks',
    'leads', 'messages', 'funnel_events', 'appointments', 'objections', 'outbox'
  ] loop
    execute format('revoke all on table public.%I from anon, authenticated, public', t);
    execute format('grant all on table public.%I to service_role', t);
    execute format('grant select, insert, update, delete on table public.%I to authenticated', t);
  end loop;

  -- panel_users: authenticated só lê. public_book_attempts: ninguém além do service_role.
  revoke all on table public.panel_users from anon, authenticated, public;
  grant all on table public.panel_users to service_role;
  grant select on table public.panel_users to authenticated;
  revoke all on table public.public_book_attempts from anon, authenticated, public;
  grant all on table public.public_book_attempts to service_role;
  revoke all on sequence public.public_book_attempts_id_seq from anon, authenticated, public;
  grant all on sequence public.public_book_attempts_id_seq to service_role;

  -- Sequences (bigserial) das tabelas do setup.
  foreach t in array array['messages', 'funnel_events', 'objections', 'outbox'] loop
    q := pg_get_serial_sequence('public.' || t, 'id');
    execute format('revoke all on sequence %s from anon, authenticated, public', q);
    execute format('grant all on sequence %s to service_role', q);
    execute format('grant usage, select on sequence %s to authenticated', q);
  end loop;

  -- Funções: revoga o execute padrão (PUBLIC) de cada uma, por assinatura.
  foreach f in array array[
    'public.set_updated_at()', 'public.log_lead_stage()', 'public.sync_lead_stage_from_appointment()',
    'public.is_panel_user()', 'public._is_trusted_caller()',
    'public._slot_grid(timestamptz,timestamptz,uuid)', 'public.available_slots(timestamptz,timestamptz,uuid)',
    'public._book_core(uuid,timestamptz,text,uuid,text,boolean)', 'public.book_slot(uuid,timestamptz,text,uuid,text)',
    'public.reschedule_slot(uuid,timestamptz,uuid)',
    'public.public_slots(text,timestamptz,timestamptz)',
    'public.public_book(text,text,text,text,timestamptz,text)',
    'public.dashboard_stats(timestamptz,timestamptz,uuid)',
    'public.set_appointment_status(uuid,text,numeric)'
  ] loop
    execute format('revoke execute on function %s from public, anon, authenticated', f);
  end loop;
end;
$$;

grant usage on schema public to anon, authenticated, service_role;

-- is_panel_user: a policy roda como o chamador, então ele precisa do execute.
grant execute on function public.is_panel_user() to authenticated, service_role;
-- grade bruta: invoker + RLS, chamada por available_slots (painel/motor).
grant execute on function public._slot_grid(timestamptz, timestamptz, uuid) to authenticated, service_role;
grant execute on function public.available_slots(timestamptz, timestamptz, uuid) to authenticated, service_role;
grant execute on function public.book_slot(uuid, timestamptz, text, uuid, text) to authenticated, service_role;
grant execute on function public.reschedule_slot(uuid, timestamptz, uuid) to authenticated, service_role;
grant execute on function public.dashboard_stats(timestamptz, timestamptz, uuid) to authenticated, service_role;
-- resultado da call pelo painel (a própria função exige is_panel_user):
grant execute on function public.set_appointment_status(uuid, text, numeric) to authenticated;
-- única porta da página pública:
grant execute on function public.public_slots(text, timestamptz, timestamptz) to anon, authenticated, service_role;
grant execute on function public.public_book(text, text, text, text, timestamptz, text) to anon, authenticated, service_role;
-- _book_core, _is_trusted_caller e as funções de trigger: sem grant (uso interno).

-- Conversas ao vivo no painel (Realtime respeita a RLS acima). Uma tabela por vez: reexecutar
-- o arquivo não falha por tabela que já está na publicação.
do $$
declare
  t text;
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
    foreach t in array array['messages', 'leads', 'appointments'] loop
      begin
        execute format('alter publication supabase_realtime add table public.%I', t);
      exception when duplicate_object then
        null;
      end;
    end loop;
  end if;
end;
$$;

-- Marcador: este projeto é do Setup 18 (o preflight libera a reexecução da migration).
comment on table public.agent_config is 'setup18-sdr';

commit;
