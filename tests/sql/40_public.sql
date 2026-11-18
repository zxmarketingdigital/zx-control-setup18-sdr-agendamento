-- Página pública: public_slots e public_book chamados como anon (o papel do navegador).
\i tests/sql/_helpers.sql

begin;
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana A');
  b uuid := pg_temp.mk_closer('Bia B');
  r jsonb;
  n int;
  v_body text;
  v_lead public.leads%rowtype;
  v_ap public.appointments%rowtype;
  v_slots timestamptz[];
  v_old uuid;
begin
  perform pg_temp.mk_rule(a, 1, '09:00', '12:00');
  perform pg_temp.mk_rule(b, 1, '09:00', '12:00');

  -- 1) página desativada (estado inicial): nada aparece, nada grava
  set local role anon;
  select count(*) into n from public.public_slots('demo', pg_temp.at_local(d, '00:00'), pg_temp.at_local(d + 1, '00:00'));
  perform pg_temp.assert_eq(n, 0, 'página desativada não lista horários');
  r := public.public_book('demo', 'Ana Souza', 'ana@exemplo.com', '+5511900000101', pg_temp.at_local(d, '09:00'));
  perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'pagina_desativada'), 'página desativada recusa');
  reset role;

  update public.schedule_settings set public_slug = 'demo', public_enabled = true, public_max_per_ip_day = 1000;

  -- 2) slug errado
  set local role anon;
  select count(*) into n from public.public_slots('outro', pg_temp.at_local(d, '00:00'), pg_temp.at_local(d + 1, '00:00'));
  perform pg_temp.assert_eq(n, 0, 'slug errado não lista horários');
  r := public.public_book('outro', 'Ana Souza', 'ana@exemplo.com', '+5511900000101', pg_temp.at_local(d, '09:00'));
  perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'pagina_desativada'), 'slug errado recusa');

  -- 3) horários distintos, sem expor closer (2 closers no mesmo horário = 1 linha)
  select array_agg(s.starts_at order by s.starts_at) into v_slots
  from public.public_slots('demo', pg_temp.at_local(d, '00:00'), pg_temp.at_local(d + 1, '00:00')) s;
  perform pg_temp.assert_eq(coalesce(array_length(v_slots, 1), 0), 6, 'seis horários distintos de 30 min entre 9h e 12h');
  perform pg_temp.assert_eq(v_slots[1], pg_temp.at_local(d, '09:00'), 'primeiro horário às 9h locais');
  reset role;

  -- 4) honeypot: finge sucesso e não grava nada
  set local role anon;
  r := public.public_book('demo', 'Robô Teste', 'robo@exemplo.com', '+5511900000102', pg_temp.at_local(d, '09:00'), 'http://spam');
  reset role;
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'honeypot responde ok');
  perform pg_temp.assert_eq((select count(*)::int from public.leads), 0, 'honeypot não cria lead');
  perform pg_temp.assert_eq((select count(*)::int from public.appointments), 0, 'honeypot não cria agendamento');
  perform pg_temp.assert_eq((select count(*)::int from public.outbox), 0, 'honeypot não cria outbox');

  -- 5) dados inválidos
  set local role anon;
  foreach r in array array[
    public.public_book('demo', 'Ana Souza', 'ana@exemplo.com', '11999999999', pg_temp.at_local(d, '09:00')),       -- sem +
    public.public_book('demo', 'Ana Souza', 'ana@exemplo.com', '+0511999999999', pg_temp.at_local(d, '09:00')),    -- começa com 0
    public.public_book('demo', 'Ana Souza', 'ana@exemplo.com', '+55119', pg_temp.at_local(d, '09:00')),            -- curto
    public.public_book('demo', 'Ana Souza', 'ana@exemplo.com', '+5511999999999999', pg_temp.at_local(d, '09:00')), -- longo
    public.public_book('demo', 'Ana Souza', 'ana@exemplo.com', '+55 11 abcde 9999', pg_temp.at_local(d, '09:00')), -- letras
    public.public_book('demo', 'Ana Souza', 'ana@exemplo.com', null, pg_temp.at_local(d, '09:00')),
    public.public_book('demo', 'A', 'ana@exemplo.com', '+5511900000103', pg_temp.at_local(d, '09:00')),            -- nome curto
    public.public_book('demo', null, 'ana@exemplo.com', '+5511900000103', pg_temp.at_local(d, '09:00')),
    public.public_book('demo', 'Ana Souza', 'ana@exemplo.com', '+5511900000103', null)
  ] loop
    perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'dados_invalidos'), 'dados_invalidos: ' || r::text);
  end loop;
  foreach r in array array[
    public.public_book('demo', 'Ana Souza', 'sem-arroba', '+5511900000103', pg_temp.at_local(d, '09:00')),
    public.public_book('demo', 'Ana Souza', '', '+5511900000103', pg_temp.at_local(d, '09:00'))
  ] loop
    perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'email_invalido'), 'email_invalido: ' || r::text);
  end loop;
  reset role;
  perform pg_temp.assert_eq((select count(*)::int from public.leads), 0, 'dado inválido não cria lead');

  -- 6) caminho feliz (telefone com máscara é normalizado para E.164)
  set local role anon;
  r := public.public_book('demo', 'Ana Souza', 'ana@exemplo.com', '+55 (11) 90000-0104', pg_temp.at_local(d, '09:00'));
  reset role;
  perform pg_temp.assert_eq(r, jsonb_build_object('ok', true, 'starts_at', pg_temp.at_local(d, '09:00')), 'agendamento pela página');
  select * into v_lead from public.leads where phone = '+5511900000104';
  perform pg_temp.assert_true(v_lead.id is not null, 'lead criado com telefone E.164');
  perform pg_temp.assert_eq(v_lead.source, 'pagina', 'origem do lead: pagina');
  perform pg_temp.assert_eq(v_lead.email, 'ana@exemplo.com', 'e-mail gravado');
  perform pg_temp.assert_eq(v_lead.stage, 'agendado', 'lead agendado');
  select * into v_ap from public.appointments where lead_id = v_lead.id;
  perform pg_temp.assert_eq(v_ap.source, 'pagina', 'agendamento com origem pagina');
  perform pg_temp.assert_eq(v_ap.starts_at, pg_temp.at_local(d, '09:00'), 'horário gravado');
  select body into v_body from public.outbox where lead_id = v_lead.id and created_by = 'pagina' and phone = '+5511900000104';
  perform pg_temp.assert_eq(v_body,
    'Olá! Sua conversa está confirmada para ' || to_char(d, 'DD/MM/YYYY') || ' às 09:00.',
    'outbox com a confirmação neutra (sem o nome digitado na página)');
  perform pg_temp.assert_eq((select status from public.outbox where lead_id = v_lead.id), 'pendente', 'outbox nasce pendente');

  -- 7) número internacional + empresa configurada: confirmação neutra com o nome da empresa
  update public.agent_config set company = 'Studio Aurora';
  set local role anon;
  r := public.public_book('demo', 'João Pedro Lima', 'jp@exemplo.com', '+351912345678', pg_temp.at_local(d, '10:00'));
  reset role;
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'número internacional (Portugal) aceito');
  perform pg_temp.assert_eq((select body from public.outbox where phone = '+351912345678'),
    'Olá! Sua conversa com Studio Aurora está confirmada para ' || to_char(d, 'DD/MM/YYYY') || ' às 10:00.',
    'confirmação com o nome da empresa, sem {nome} da página');
  update public.agent_config set company = null;

  -- 8) mesmo telefone de novo: já agendado, e nada novo é gravado
  select count(*)::int into n from public.appointments;
  set local role anon;
  r := public.public_book('demo', 'Ana Souza', 'ana@exemplo.com', '+5511900000104', pg_temp.at_local(d, '11:00'));
  reset role;
  perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'ja_agendado'), 'um agendamento ativo por telefone');
  perform pg_temp.assert_eq((select count(*)::int from public.appointments), n, 'ja_agendado não cria agendamento');
  perform pg_temp.assert_eq((select count(*)::int from public.outbox where phone = '+5511900000104'), 1, 'ja_agendado não cria outbox');

  -- 9) horário ocupado (os 2 closers já têm 9h e 10h? só 1 cada): lead novo no mesmo horário cai no outro closer
  set local role anon;
  r := public.public_book('demo', 'Carla Dias', 'carla@exemplo.com', '+5511900000105', pg_temp.at_local(d, '09:00'));
  reset role;
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'segundo lead às 9h vai para o outro closer');
  -- terceiro lead às 9h: ninguém livre; o lead não pode ficar criado
  set local role anon;
  r := public.public_book('demo', 'Davi Melo', 'davi@exemplo.com', '+5511900000106', pg_temp.at_local(d, '09:00'));
  reset role;
  perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'slot_indisponivel'), 'sem closer livre: slot_indisponivel');
  perform pg_temp.assert_eq((select count(*)::int from public.leads where phone = '+5511900000106'), 0, 'falha não deixa lead órfão');
  perform pg_temp.assert_eq((select count(*)::int from public.outbox where phone = '+5511900000106'), 0, 'falha não deixa outbox');
  -- horário fora da agenda
  set local role anon;
  r := public.public_book('demo', 'Davi Melo', 'davi@exemplo.com', '+5511900000106', pg_temp.at_local(d, '15:00'));
  reset role;
  perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'slot_indisponivel'), 'fora da agenda: slot_indisponivel');

  -- 10) o horário que ficou tomado some da lista pública
  set local role anon;
  select count(*) into n from public.public_slots('demo', pg_temp.at_local(d, '09:00'), pg_temp.at_local(d, '09:30'));
  reset role;
  perform pg_temp.assert_eq(n, 0, 'horário sem closer livre some da lista pública');

  -- 11) lead que já existia (veio do WhatsApp) é reaproveitado: origem e nome preservados
  insert into public.leads (phone, name, email, source) values ('+5511900000107', 'Elisa', 'orig@exemplo.com', 'whatsapp');
  set local role anon;
  r := public.public_book('demo', 'Elisa Prado', 'elisa@exemplo.com', '+5511900000107', pg_temp.at_local(d, '11:00'));
  reset role;
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'lead existente agenda pela página');
  perform pg_temp.assert_eq((select count(*)::int from public.leads where phone = '+5511900000107'), 1, 'não duplica lead');
  perform pg_temp.assert_eq((select source from public.leads where phone = '+5511900000107'), 'whatsapp', 'origem original preservada');
  perform pg_temp.assert_eq((select name from public.leads where phone = '+5511900000107'), 'Elisa', 'nome existente não é sobrescrito pela página');
  perform pg_temp.assert_eq((select email from public.leads where phone = '+5511900000107'), 'orig@exemplo.com', 'e-mail existente não é sobrescrito pela página');
  -- closer extra com a tarde livre (não interfere nos horários das manhãs acima)
  perform pg_temp.mk_rule(pg_temp.mk_closer('Cris C'), 1, '13:00', '15:00');
  -- lead existente SEM e-mail: a página preenche o vazio
  insert into public.leads (phone, name, source) values ('+5511900000109', 'Gil', 'whatsapp');
  set local role anon;
  r := public.public_book('demo', 'Gilberto Nunes', 'gil@exemplo.com', '+5511900000109', pg_temp.at_local(d, '13:00'));
  reset role;
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'lead sem e-mail agenda: ' || r::text);
  perform pg_temp.assert_eq((select name from public.leads where phone = '+5511900000109'), 'Gil', 'nome existente preservado');
  perform pg_temp.assert_eq((select email from public.leads where phone = '+5511900000109'), 'gil@exemplo.com', 'e-mail vazio é preenchido');

  -- 11b) lead com opt_out: a página recusa (indisponivel), não agenda, não enfileira e mantém o opt_out
  insert into public.leads (phone, name, source, opt_out) values ('+5511900000110', 'Hugo', 'whatsapp', true);
  set local role anon;
  r := public.public_book('demo', 'Hugo Lopes', 'hugo@exemplo.com', '+5511900000110', pg_temp.at_local(d, '14:00'));
  reset role;
  perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'indisponivel'), 'lead opt_out: página recusa');
  perform pg_temp.assert_eq((select count(*)::int from public.appointments a join public.leads l on l.id = a.lead_id where l.phone = '+5511900000110'), 0, 'opt_out: nenhum agendamento');
  perform pg_temp.assert_eq((select count(*)::int from public.outbox where phone = '+5511900000110'), 0, 'opt_out: nenhuma mensagem enfileirada');
  perform pg_temp.assert_eq((select opt_out from public.leads where phone = '+5511900000110'), true, 'opt_out continua ligado');

  -- 12) call antiga já feita não impede agendar de novo
  insert into public.leads (phone, name) values ('+5511900000108', 'Fábio') returning id into v_old;
  insert into public.appointments (lead_id, closer_id, starts_at, ends_at, status)
  values (v_old, a, now() - interval '5 days', now() - interval '5 days' + interval '30 minutes', 'feito');
  set local role anon;
  r := public.public_book('demo', 'Fábio Reis', 'fabio@exemplo.com', '+5511900000108', pg_temp.at_local(d, '11:30'));
  reset role;
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'call antiga feita não bloqueia novo agendamento: ' || r::text);

  -- 12b) abuso: nome saneado, estágio protegido, limites por hora e por IP (closers novos, tarde livre)
  perform pg_temp.mk_rule(pg_temp.mk_closer('Dora D'), 1, '16:00', '19:00');
  perform pg_temp.mk_rule(pg_temp.mk_closer('Eli E'), 1, '16:00', '19:00');

  -- nome com URL/domínio vira só o nome; confirmação não leva link
  set local role anon;
  r := public.public_book('demo', E'Ivo https://golpe.exemplo/x\nwww.mal.com', 'ivo@exemplo.com', '+5511900000111', pg_temp.at_local(d, '16:00'));
  reset role;
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'nome com URL: agenda ' || r::text);
  perform pg_temp.assert_eq((select name from public.leads where phone = '+5511900000111'), 'Ivo', 'nome saneado: sem URL');
  perform pg_temp.assert_true((select body from public.outbox where phone = '+5511900000111') !~* 'http|www|golpe', 'confirmação sem link');
  -- nome só de URL fica vazio depois de sanear: dados_invalidos
  set local role anon;
  r := public.public_book('demo', 'https://golpe.exemplo', 'x@exemplo.com', '+5511900000118', pg_temp.at_local(d, '17:00'));
  reset role;
  perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'dados_invalidos'), 'nome só URL é inválido');
  perform pg_temp.assert_eq((select count(*)::int from public.leads where phone = '+5511900000118'), 0, 'nome só URL não cria lead');
  -- nome longo é cortado em 60
  set local role anon;
  r := public.public_book('demo', repeat('Bia ', 40), 'bia@exemplo.com', '+5511900000112', pg_temp.at_local(d, '16:00'));
  reset role;
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'nome longo: agenda ' || r::text);
  perform pg_temp.assert_true(char_length((select name from public.leads where phone = '+5511900000112')) <= 60, 'nome cortado em 60 caracteres');

  -- lead que já comprou: a página não muda estágio nem closer
  insert into public.leads (phone, name, source, stage, closer_id) values ('+5511900000116', 'Vera', 'whatsapp', 'venda', a);
  set local role anon;
  r := public.public_book('demo', 'Vera Lima', 'vera@exemplo.com', '+5511900000116', pg_temp.at_local(d, '17:00'));
  reset role;
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'lead com venda agenda pela página: ' || r::text);
  perform pg_temp.assert_eq((select stage from public.leads where phone = '+5511900000116'), 'venda', 'estágio venda preservado');
  perform pg_temp.assert_eq((select closer_id from public.leads where phone = '+5511900000116'), a, 'closer do lead preservado');
  perform pg_temp.assert_eq((select count(*)::int from public.funnel_events f join public.leads l on l.id = f.lead_id
                             where l.phone = '+5511900000116' and f.stage = 'agendado'), 0, 'funil não registra agendado para quem já comprou');
  perform pg_temp.assert_eq((select count(*)::int from public.appointments ap join public.leads l on l.id = ap.lead_id
                             where l.phone = '+5511900000116' and ap.status = 'agendado'), 1, 'agendamento foi criado');

  -- limite total por hora
  update public.schedule_settings set public_max_per_hour = 1;
  set local role anon;
  r := public.public_book('demo', 'Jaci Melo', 'jaci@exemplo.com', '+5511900000113', pg_temp.at_local(d, '18:00'));
  reset role;
  perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'limite_atingido'), 'limite por hora atingido');
  perform pg_temp.assert_eq((select count(*)::int from public.leads where phone = '+5511900000113'), 0, 'limite por hora não cria lead');
  update public.schedule_settings set public_max_per_hour = 20;

  -- limite por IP/dia: o IP é o ÚLTIMO valor de x-forwarded-for (o que o proxy anexou)
  update public.schedule_settings set public_max_per_ip_day = 1;
  perform set_config('request.headers', '{"x-forwarded-for":"203.0.113.9"}', true);
  set local role anon;
  r := public.public_book('demo', 'Kaue Reis', 'kaue@exemplo.com', '+5511900000114', pg_temp.at_local(d, '17:00'));
  reset role;
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'primeiro agendamento do IP passa: ' || r::text);
  perform pg_temp.assert_eq((select count(*)::int from public.public_book_attempts where ip = '203.0.113.9'), 1, 'tentativa registrada com o IP');
  set local role anon;
  r := public.public_book('demo', 'Lia Rocha', 'lia@exemplo.com', '+5511900000115', pg_temp.at_local(d, '18:00'));
  reset role;
  perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'limite_atingido'), 'limite por IP atingido');
  -- IP antes do último valor da lista não vale: o cliente escreve essa parte
  perform set_config('request.headers', '{"x-forwarded-for":"203.0.113.9, 198.51.100.7"}', true);
  set local role anon;
  r := public.public_book('demo', 'Lia Rocha', 'lia@exemplo.com', '+5511900000115', pg_temp.at_local(d, '18:00'));
  reset role;
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'IP novo (último da lista) passa: ' || r::text);
  perform pg_temp.assert_eq((select count(*)::int from public.leads where phone = '+5511900000115'), 1, 'lead do outro IP criado');
  perform set_config('request.headers', '', true);
  update public.schedule_settings set public_max_per_ip_day = 1000;

  -- 13) anon só tem as duas portas da página pública.
  -- (has_function_privilege em vez de chamar e capturar o erro: capturar "permission denied
  -- for function" depois de set role derruba o Postgres 17.6 do CLI com segfault.)
  perform pg_temp.assert_true(has_function_privilege('anon', 'public.public_slots(text,timestamptz,timestamptz)', 'execute'), 'anon executa public_slots');
  perform pg_temp.assert_true(has_function_privilege('anon', 'public.public_book(text,text,text,text,timestamptz,text)', 'execute'), 'anon executa public_book');
  perform pg_temp.assert_true(not has_function_privilege('anon', 'public.book_slot(uuid,timestamptz,text,uuid,text)', 'execute'), 'anon NÃO executa book_slot');
  perform pg_temp.assert_true(not has_function_privilege('anon', 'public.available_slots(timestamptz,timestamptz,uuid)', 'execute'), 'anon NÃO executa available_slots');
  perform pg_temp.assert_true(not has_function_privilege('anon', 'public.dashboard_stats(timestamptz,timestamptz,uuid)', 'execute'), 'anon NÃO executa dashboard_stats');
  perform pg_temp.assert_true(not has_function_privilege('anon', 'public._book_core(uuid,timestamptz,text,uuid,text,boolean)', 'execute'), 'anon NÃO executa _book_core');
  perform pg_temp.assert_true(not has_function_privilege('anon', 'public.is_panel_user()', 'execute'), 'anon NÃO executa is_panel_user');
end;
$$;
rollback;

\echo OK 40_public
