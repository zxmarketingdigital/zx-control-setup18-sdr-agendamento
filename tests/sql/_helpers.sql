-- Ajudantes dos testes de banco. Cada teste faz \i deste arquivo no começo.
-- Tudo vive em pg_temp: some quando a sessão termina e não suja o banco.

create function pg_temp.assert_true(p_cond boolean, p_msg text) returns void
language plpgsql as $$
begin
  if p_cond is not true then
    raise exception 'FALHOU: %', p_msg;
  end if;
end;
$$;

create function pg_temp.assert_eq(p_got anyelement, p_want anyelement, p_msg text) returns void
language plpgsql as $$
begin
  if p_got is distinct from p_want then
    raise exception 'FALHOU: % (obtido=%, esperado=%)', p_msg, p_got, p_want;
  end if;
end;
$$;

-- Próximo dia da semana (0=domingo) pelo menos p_min dias à frente, no fuso p_tz.
create function pg_temp.next_dow(p_dow int, p_min int default 3, p_tz text default 'America/Sao_Paulo')
returns date language sql stable as $$
  select d::date from generate_series(
    (now() at time zone p_tz)::date + p_min, (now() at time zone p_tz)::date + p_min + 6, interval '1 day') d
  where extract(dow from d)::int = p_dow
  limit 1
$$;

-- Dia local + hora local -> timestamptz.
create function pg_temp.at_local(p_day date, p_time time, p_tz text default 'America/Sao_Paulo')
returns timestamptz language sql immutable as $$
  select (p_day + p_time) at time zone p_tz
$$;

create function pg_temp.mk_closer(p_name text, p_weight int default 1, p_active boolean default true)
returns uuid language plpgsql as $$
declare v uuid;
begin
  insert into public.closers (name, weight, active) values (p_name, p_weight, p_active) returning id into v;
  return v;
end;
$$;

create function pg_temp.mk_rule(p_closer uuid, p_dow int, p_from time, p_to time) returns void
language sql as $$
  insert into public.availability_rules (closer_id, weekday, start_time, end_time) values (p_closer, p_dow, p_from, p_to)
$$;

create function pg_temp.mk_lead(p_phone text) returns uuid
language plpgsql as $$
declare v uuid;
begin
  insert into public.leads (phone, name) values (p_phone, 'Lead ' || p_phone) returning id into v;
  return v;
end;
$$;

-- Faz as próximas chamadas valerem como o motor (service_role) na checagem de book_slot.
create function pg_temp.as_service() returns void language sql as $$
  select set_config('request.jwt.claims', '{"role":"service_role"}', true)
$$;
