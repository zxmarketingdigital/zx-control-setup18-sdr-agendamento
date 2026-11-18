-- Rodada 9: nome gigante é recusado antes de qualquer regexp; lead terminal (venda/compareceu)
-- agenda pela página pública mas não recebe WhatsApp de confirmação.
\i tests/sql/_helpers.sql

-- ---- nome de 5000 caracteres sem espaço: dados_invalidos, nada criado ----
begin;
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana A');
  r jsonb;
begin
  perform pg_temp.mk_rule(a, 1, '09:00', '12:00');
  update public.schedule_settings set public_slug = 'demo', public_enabled = true,
    public_max_per_hour = 1000, public_max_per_ip_day = 1000, public_max_per_phone_day = 1000;
  set local role anon;
  r := public.public_book('demo', repeat('a', 5000), 'ana@exemplo.com', '+5511900005401', pg_temp.at_local(d, '09:00'));
  reset role;
  perform pg_temp.assert_eq(r ->> 'error', 'dados_invalidos', 'nome de 5000 chars: ' || r::text);
  perform pg_temp.assert_eq((select count(*)::int from public.leads where phone = '+5511900005401'), 0, 'nenhum lead criado');
  perform pg_temp.assert_eq((select count(*)::int from public.appointments), 0, 'nenhum appointment criado');
  perform pg_temp.assert_eq((select count(*)::int from public.outbox), 0, 'nada na outbox');
  -- 200 chars ainda passa pelo teto (vira 60 após o saneamento)
  set local role anon;
  r := public.public_book('demo', repeat('a', 200), 'ana@exemplo.com', '+5511900005402', pg_temp.at_local(d, '09:00'));
  reset role;
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'nome de 200 chars é aceito: ' || r::text);
  perform pg_temp.assert_eq((select char_length(name) from public.leads where phone = '+5511900005402'), 60, 'nome cortado em 60');
end;
$$;
rollback;

-- ---- lead terminal agenda sem WhatsApp; lead novo recebe ----
begin;
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana A');
  st text;
  n int := 0;
  r jsonb;
  l uuid;
begin
  perform pg_temp.mk_rule(a, 1, '09:00', '18:00');
  update public.schedule_settings set public_slug = 'demo', public_enabled = true,
    public_max_per_hour = 1000, public_max_per_ip_day = 1000, public_max_per_phone_day = 1000;
  foreach st in array array['venda', 'compareceu', 'desqualificado', 'perdido'] loop
    n := n + 1;
    insert into public.leads (phone, name, stage) values ('+55119000054' || lpad((10 + n)::text, 2, '0'), 'Lead ' || st, st) returning id into l;
    set local role anon;
    r := public.public_book('demo', 'Ana Souza', 'ana@exemplo.com', '+55119000054' || lpad((10 + n)::text, 2, '0'),
                            pg_temp.at_local(d, (lpad((10 + n)::text, 2, '0') || ':00')::time));
    reset role;
    perform pg_temp.assert_true((r ->> 'ok')::boolean, st || ' agenda: ' || r::text);
    perform pg_temp.assert_eq((select count(*)::int from public.outbox where lead_id = l), 0, st || ': nada na outbox');
    perform pg_temp.assert_eq((select stage from public.leads where id = l), st, st || ': mantém o estágio');
    perform pg_temp.assert_eq((select count(*)::int from public.appointments where lead_id = l), 1, st || ': call criada');
  end loop;
  -- lead novo continua recebendo a confirmação
  set local role anon;
  r := public.public_book('demo', 'Beto Lima', 'beto@exemplo.com', '+5511900005450', pg_temp.at_local(d, '16:00'));
  reset role;
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'lead novo agenda: ' || r::text);
  perform pg_temp.assert_eq((select count(*)::int from public.outbox where phone = '+5511900005450'), 1, 'lead novo recebe confirmação');
end;
$$;
rollback;
