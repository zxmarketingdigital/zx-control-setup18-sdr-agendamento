-- dashboard_stats: contagens do funil, taxas, vendas, objeções e desqualificação.
\i tests/sql/_helpers.sql

begin;
do $$
declare
  a uuid := pg_temp.mk_closer('Ana A');
  b uuid := pg_temp.mk_closer('Bia B');
  l uuid[] := '{}';
  v uuid;
  i int;
  r jsonb;
  t0 timestamptz := now() - interval '10 days';
  t1 timestamptz := now() + interval '10 days';
  f jsonb;
  e jsonb;
begin
  -- período vazio: tudo zerado, funil completo na ordem certa, sem divisão por zero
  r := public.dashboard_stats(t0, t1);
  perform pg_temp.assert_eq((r ->> 'leads')::int, 0, 'vazio: leads 0');
  perform pg_temp.assert_eq((r ->> 'taxa_qualificacao')::numeric, null::numeric, 'vazio: taxa_qualificacao null (divisão por zero)');
  perform pg_temp.assert_eq((r ->> 'taxa_agendamento')::numeric, null::numeric, 'vazio: taxa_agendamento null (divisão por zero)');
  perform pg_temp.assert_eq((r ->> 'taxa_comparecimento')::numeric, null::numeric, 'vazio: taxa_comparecimento null (divisão por zero)');
  perform pg_temp.assert_eq((r ->> 'valor_vendas')::numeric, 0::numeric, 'vazio: valor_vendas 0');
  perform pg_temp.assert_eq(jsonb_array_length(r -> 'funil'), 12, 'funil tem as 12 etapas');
  perform pg_temp.assert_eq(r -> 'objecoes', '[]'::jsonb, 'vazio: objeções []');
  perform pg_temp.assert_eq(r -> 'desqualificacao', '[]'::jsonb, 'vazio: desqualificação []');
  perform pg_temp.assert_eq(
    (select string_agg(x ->> 'stage', ',' order by ord) from jsonb_array_elements(r -> 'funil') with ordinality as q(x, ord)),
    'novo,respondeu,qualificado,desqualificado,horarios_ofertados,agendado,confirmado,compareceu,no_show,reagendado,venda,perdido',
    'ordem das etapas do funil');

  -- 10 leads
  for i in 1..10 loop
    l := l || pg_temp.mk_lead('+55119000003' || lpad(i::text, 2, '0'));
  end loop;
  -- L1 só novo. L2 respondeu.
  update public.leads set stage = 'respondeu' where id = l[2];
  -- L3 qualificado (passou por respondeu)
  update public.leads set stage = 'respondeu' where id = l[3];
  update public.leads set stage = 'qualificado' where id = l[3];
  -- L4 e L5 desqualificados (um com motivo, outro sem)
  update public.leads set stage = 'respondeu' where id in (l[4], l[5]);
  update public.leads set stage = 'desqualificado', disqualify_reason = 'sem_orcamento' where id = l[4];
  update public.leads set stage = 'desqualificado' where id = l[5];
  -- L6..L10 chegam até horários ofertados
  for i in 6..10 loop
    update public.leads set stage = 'respondeu' where id = l[i];
    update public.leads set stage = 'qualificado' where id = l[i];
    update public.leads set stage = 'horarios_ofertados' where id = l[i];
  end loop;
  update public.leads set closer_id = b where id in (l[6], l[7]);
  update public.leads set closer_id = a where id in (l[8], l[9], l[10]);

  -- agendamentos: L6 confirmado (Bia), L7 feito (Bia), L8 no-show, L9 e L10 vendas (Ana)
  insert into public.appointments (lead_id, closer_id, starts_at, ends_at, status)
    values (l[6], b, now() + interval '2 days', now() + interval '2 days 30 minutes', 'confirmado');
  insert into public.appointments (lead_id, closer_id, starts_at, ends_at, status)
    values (l[7], b, now() - interval '3 days', now() - interval '3 days' + interval '30 minutes', 'feito');
  insert into public.appointments (lead_id, closer_id, starts_at, ends_at, status)
    values (l[8], a, now() - interval '4 days', now() - interval '4 days' + interval '30 minutes', 'no_show');
  insert into public.appointments (lead_id, closer_id, starts_at, ends_at, status, sale_value)
    values (l[9], a, now() - interval '5 days', now() - interval '5 days' + interval '30 minutes', 'venda', 1500.50);
  insert into public.appointments (lead_id, closer_id, starts_at, ends_at, status, sale_value)
    values (l[10], a, now() - interval '6 days', now() - interval '6 days' + interval '30 minutes', 'venda', 500);
  -- um agendamento cancelado do L2 conta como lead que agendou, mas não muda estágio nem resultado
  insert into public.appointments (lead_id, closer_id, starts_at, ends_at, status)
    values (l[2], a, now() + interval '3 days', now() + interval '3 days 30 minutes', 'cancelado');

  -- objeções
  insert into public.objections (lead_id, category, excerpt, handled) values
    (l[3], 'preco', 'tá caro', true), (l[3], 'preco', 'não tenho esse valor', false), (l[6], 'tempo', 'sem tempo agora', false);

  r := public.dashboard_stats(t0, t1);
  perform pg_temp.assert_eq((r ->> 'leads')::int, 10, 'leads');
  perform pg_temp.assert_eq((r ->> 'respondeu')::int, 9, 'respondeu (todos menos o L1)');
  perform pg_temp.assert_eq((r ->> 'qualificados')::int, 6, 'qualificados (L3 e L6..L10; desqualificados fora)');
  perform pg_temp.assert_eq((r ->> 'desqualificados')::int, 2, 'desqualificados');
  perform pg_temp.assert_eq((r ->> 'agendados')::int, 6, 'agendados = leads com agendamento (inclui o cancelado)');
  perform pg_temp.assert_eq((r ->> 'comparecimentos')::int, 3, 'comparecimentos = feito + venda');
  perform pg_temp.assert_eq((r ->> 'no_shows')::int, 1, 'no_shows');
  perform pg_temp.assert_eq((r ->> 'vendas')::int, 2, 'vendas');
  perform pg_temp.assert_eq((r ->> 'valor_vendas')::numeric, 2000.50::numeric, 'valor das vendas');
  perform pg_temp.assert_eq((r ->> 'taxa_qualificacao')::numeric, 0.6::numeric, 'taxa de qualificação 6/10 (fração)');
  perform pg_temp.assert_eq((r ->> 'taxa_agendamento')::numeric, 0.857::numeric, 'taxa de agendamento = agendados / (qualificados UNIÃO agendados) = 6/7 (L2 agendou sem qualificar; fração)');
  perform pg_temp.assert_eq((r ->> 'taxa_comparecimento')::numeric, 0.75::numeric, 'taxa de comparecimento 3/(3+1) (fração)');

  -- funil
  f := r -> 'funil';
  perform pg_temp.assert_eq((select (x ->> 'count')::int from jsonb_array_elements(f) x where x ->> 'stage' = 'novo'), 10, 'funil novo');
  perform pg_temp.assert_eq((select (x ->> 'count')::int from jsonb_array_elements(f) x where x ->> 'stage' = 'respondeu'), 9, 'funil respondeu');
  perform pg_temp.assert_eq((select (x ->> 'count')::int from jsonb_array_elements(f) x where x ->> 'stage' = 'qualificado'), 6, 'funil qualificado');
  perform pg_temp.assert_eq((select (x ->> 'count')::int from jsonb_array_elements(f) x where x ->> 'stage' = 'horarios_ofertados'), 5, 'funil horários ofertados');
  perform pg_temp.assert_eq((select (x ->> 'count')::int from jsonb_array_elements(f) x where x ->> 'stage' = 'confirmado'), 1, 'funil confirmado');
  perform pg_temp.assert_eq((select (x ->> 'count')::int from jsonb_array_elements(f) x where x ->> 'stage' = 'venda'), 2, 'funil venda');
  perform pg_temp.assert_eq((select (x ->> 'count')::int from jsonb_array_elements(f) x where x ->> 'stage' = 'perdido'), 0, 'funil perdido');

  -- objeções (ordem: mais frequente primeiro) e motivos de desqualificação
  e := r -> 'objecoes';
  perform pg_temp.assert_eq(e -> 0, '{"category":"preco","count":2,"tratadas":1}'::jsonb, 'objeção mais frequente');
  perform pg_temp.assert_eq(e -> 1, '{"category":"tempo","count":1,"tratadas":0}'::jsonb, 'segunda objeção');
  e := r -> 'desqualificacao';
  perform pg_temp.assert_eq(jsonb_array_length(e), 2, 'dois motivos de desqualificação');
  perform pg_temp.assert_true(e @> '[{"motivo":"sem_orcamento","count":1},{"motivo":"nao_informado","count":1}]'::jsonb, 'motivos de desqualificação (vazio vira nao_informado)');

  -- filtro por closer: leads do closer (coorte) e calls do closer (resultado)
  r := public.dashboard_stats(t0, t1, b);
  perform pg_temp.assert_eq((r ->> 'leads')::int, 2, 'closer Bia: leads');
  perform pg_temp.assert_eq((r ->> 'agendados')::int, 2, 'closer Bia: agendados');
  perform pg_temp.assert_eq((r ->> 'comparecimentos')::int, 1, 'closer Bia: comparecimentos');
  perform pg_temp.assert_eq((r ->> 'no_shows')::int, 0, 'closer Bia: no_shows');
  perform pg_temp.assert_eq((r ->> 'vendas')::int, 0, 'closer Bia: vendas');
  r := public.dashboard_stats(t0, t1, a);
  perform pg_temp.assert_eq((r ->> 'leads')::int, 3, 'closer Ana: leads');
  perform pg_temp.assert_eq((r ->> 'vendas')::int, 2, 'closer Ana: vendas');
  perform pg_temp.assert_eq((r ->> 'valor_vendas')::numeric, 2000.50::numeric, 'closer Ana: valor');

  -- 0.5 é 0.5 (fração), não 50
  declare c uuid := pg_temp.mk_closer('Cris C'); m1 uuid; m2 uuid;
  begin
    m1 := pg_temp.mk_lead('+5511900000391'); m2 := pg_temp.mk_lead('+5511900000392');
    update public.leads set closer_id = c where id in (m1, m2);
    insert into public.appointments (lead_id, closer_id, starts_at, ends_at, status)
      values (m1, c, now() - interval '2 days', now() - interval '2 days' + interval '30 minutes', 'feito'),
             (m2, c, now() - interval '1 day', now() - interval '1 day' + interval '30 minutes', 'no_show');
    r := public.dashboard_stats(t0, t1, c);
    perform pg_temp.assert_eq((r ->> 'taxa_comparecimento')::numeric, 0.5::numeric, 'comparecimento 1/2 = 0.5, não 50');
    perform pg_temp.assert_eq((r ->> 'taxa_qualificacao')::numeric, 1::numeric, 'taxa_qualificacao 2/2 = 1, não 100');
    perform pg_temp.assert_eq((r ->> 'taxa_agendamento')::numeric, 1::numeric, 'taxa_agendamento 2/2 (base = qualificados união agendados) = 1');
  end;

  -- período: coorte fora da janela não entra; limite final é exclusivo
  r := public.dashboard_stats(now() - interval '60 days', now() - interval '30 days');
  perform pg_temp.assert_eq((r ->> 'leads')::int, 0, 'janela antiga: sem leads');
  perform pg_temp.assert_eq((r ->> 'vendas')::int, 0, 'janela antiga: sem vendas');
  r := public.dashboard_stats(now() - interval '5 days 12 hours', now() + interval '10 days');
  perform pg_temp.assert_eq((r ->> 'vendas')::int, 1, 'janela curta pega só a venda de 5 dias atrás');
  perform pg_temp.assert_eq((r ->> 'valor_vendas')::numeric, 1500.50::numeric, 'valor na janela curta');
end;
$$;
rollback;

\echo OK 60_dashboard
