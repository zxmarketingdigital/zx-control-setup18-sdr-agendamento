-- Página pública: abuso. Tentativas (não só sucessos) contam; IP vem do proxy confiável;
-- a confirmação nunca leva texto livre do lead; leads terminais/opt_out não são tocados.
\i tests/sql/_helpers.sql

begin;
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana A');
  b uuid := pg_temp.mk_closer('Bia B');
  r jsonb;
  n int;
begin
  perform pg_temp.mk_rule(a, 1, '09:00', '12:00');
  perform pg_temp.mk_rule(b, 1, '09:00', '12:00');
  update public.schedule_settings set public_slug = 'demo', public_enabled = true,
    public_max_per_hour = 1000, public_max_per_ip_day = 1000;

  -- 1) limite por TELEFONE/dia conta TENTATIVAS: 3 tentativas que falham (horário fora da agenda)
  --    estouram o limite padrão (3); a 4ª já é limite_atingido, mesmo com horário bom.
  perform pg_temp.assert_eq((select public_max_per_phone_day from public.schedule_settings), 3, 'padrão: 3 tentativas por telefone/dia');
  for n in 1..3 loop
    set local role anon;
    r := public.public_book('demo', 'Ana Souza', 'ana@exemplo.com', '+5511900000301', pg_temp.at_local(d, '15:00'));
    reset role;
    perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'slot_indisponivel'), 'tentativa ' || n || ' falha');
  end loop;
  perform pg_temp.assert_eq((select count(*)::int from public.public_book_attempts where phone = '+5511900000301'), 3, 'tentativas falhas ficam registradas');
  set local role anon;
  r := public.public_book('demo', 'Ana Souza', 'ana@exemplo.com', '+5511900000301', pg_temp.at_local(d, '09:00'));
  reset role;
  perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'limite_atingido'), 'limite por telefone atingido');
  perform pg_temp.assert_eq((select count(*)::int from public.appointments), 0, 'telefone no limite não agenda');
  -- outro telefone não é afetado
  set local role anon;
  r := public.public_book('demo', 'Beto Lima', 'beto@exemplo.com', '+5511900000302', pg_temp.at_local(d, '09:00'));
  reset role;
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'outro telefone agenda: ' || r::text);

  -- 2) IP: só vale o ÚLTIMO x-forwarded-for; cf-connecting-ip (forjável) é ignorado
  perform set_config('request.headers', '{"cf-connecting-ip":"203.0.113.50","x-forwarded-for":"6.6.6.6, 7.7.7.7"}', true);
  set local role anon;
  r := public.public_book('demo', 'Caio Dias', 'caio@exemplo.com', '+5511900000303', pg_temp.at_local(d, '10:00'));
  reset role;
  perform pg_temp.assert_eq((select ip from public.public_book_attempts where phone = '+5511900000303'), '7.7.7.7', 'IP vem do último x-forwarded-for, não do cf-connecting-ip');
  perform set_config('request.headers', '{"x-forwarded-for":"6.6.6.6, 7.7.7.7"}', true);
  set local role anon;
  r := public.public_book('demo', 'Dora Reis', 'dora@exemplo.com', '+5511900000304', pg_temp.at_local(d, '10:30'));
  reset role;
  perform pg_temp.assert_eq((select ip from public.public_book_attempts where phone = '+5511900000304'), '7.7.7.7', 'último x-forwarded-for');
  -- cf-connecting-ip forjado, diferente a cada chamada, NÃO muda o bucket: o limite por IP morde
  update public.schedule_settings set public_max_per_ip_day = 3;
  for n in 1..3 loop
    perform set_config('request.headers', json_build_object('cf-connecting-ip', '10.0.0.' || n, 'x-forwarded-for', '1.1.1.1, 9.9.9.9')::text, true);
    set local role anon;
    r := public.public_book('demo', 'Fabio Lima', 'fabio@exemplo.com', '+55119000004' || lpad(n::text, 2, '0'), pg_temp.at_local(d, '16:00'));
    reset role;
  end loop;
  perform pg_temp.assert_eq((select count(*)::int from public.public_book_attempts where ip = '9.9.9.9'), 3, 'cf-connecting-ip forjado cai no mesmo bucket');
  perform set_config('request.headers', '{"cf-connecting-ip":"10.0.0.77","x-forwarded-for":"1.1.1.1, 9.9.9.9"}', true);
  set local role anon;
  r := public.public_book('demo', 'Fabio Lima', 'fabio@exemplo.com', '+5511900000499', pg_temp.at_local(d, '16:00'));
  reset role;
  perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'limite_atingido'), 'cf-connecting-ip novo não burla o limite por IP');
  -- sem header: bucket 'desconhecido'
  delete from public.public_book_attempts where ip = 'desconhecido';
  perform set_config('request.headers', '', true);
  set local role anon;
  r := public.public_book('demo', 'Gil Prado', 'gil@exemplo.com', '+5511900000498', pg_temp.at_local(d, '16:30'));
  reset role;
  perform pg_temp.assert_eq((select ip from public.public_book_attempts where phone = '+5511900000498'), 'desconhecido', 'sem header: bucket desconhecido');
  update public.schedule_settings set public_max_per_ip_day = 1000;

  -- 3) confirmação nunca leva o nome digitado; nome com domínio é saneado ou recusado
  update public.agent_config set company = null;
  set local role anon;
  r := public.public_book('demo', 'Eva golpe.tk Silva', 'eva@exemplo.com', '+5511900000305', pg_temp.at_local(d, '11:00'));
  reset role;
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'nome com domínio .tk agenda saneado: ' || r::text);
  perform pg_temp.assert_eq((select name from public.leads where phone = '+5511900000305'), 'Eva Silva', 'domínio .tk removido do nome');
  perform pg_temp.assert_true((select body from public.outbox where phone = '+5511900000305') !~* 'Eva|Silva|golpe|\.tk',
    'confirmação não contém o nome digitado');
  set local role anon;
  r := public.public_book('demo', 'golpe.tk', 'x@exemplo.com', '+5511900000306', pg_temp.at_local(d, '11:30'));
  reset role;
  perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'dados_invalidos'), 'nome só de domínio .tk é recusado');
  perform pg_temp.assert_eq((select count(*)::int from public.leads where phone = '+5511900000306'), 0, 'nome recusado não cria lead');

  -- 4) lead em opt_out: recusa, nada enfileirado, nada agendado
  insert into public.leads (phone, name, opt_out, stage) values ('+5511900000307', 'Fred', true, 'respondeu');
  set local role anon;
  r := public.public_book('demo', 'Fred Alves', 'fred@exemplo.com', '+5511900000307', pg_temp.at_local(d, '11:00'));
  reset role;
  perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'indisponivel'), 'opt_out recusado com indisponivel');
  perform pg_temp.assert_eq((select count(*)::int from public.outbox where phone = '+5511900000307'), 0, 'opt_out: nada na outbox');
  perform pg_temp.assert_eq((select count(*)::int from public.appointments ap join public.leads l on l.id = ap.lead_id where l.phone = '+5511900000307'), 0, 'opt_out: sem agendamento');
  perform pg_temp.assert_eq((select stage from public.leads where phone = '+5511900000307'), 'respondeu', 'opt_out: estágio intacto');

  -- 5) lead desqualificado/perdido: agenda, não muda o estágio e não enfileira WhatsApp
  perform pg_temp.mk_rule(pg_temp.mk_closer('Duda D'), 1, '16:00', '18:00');
  insert into public.leads (phone, name, stage) values ('+5511900000308', 'Gus', 'desqualificado'), ('+5511900000309', 'Hana', 'perdido');
  set local role anon;
  r := public.public_book('demo', 'Gus Neto', 'gus@exemplo.com', '+5511900000308', pg_temp.at_local(d, '16:00'));
  reset role;
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'desqualificado agenda: ' || r::text);
  perform pg_temp.assert_eq((select stage from public.leads where phone = '+5511900000308'), 'desqualificado', 'desqualificado não muda de estágio');
  perform pg_temp.assert_eq((select count(*)::int from public.outbox where phone = '+5511900000308'), 0, 'desqualificado não recebe WhatsApp');
  perform pg_temp.assert_eq((select count(*)::int from public.appointments ap join public.leads l on l.id = ap.lead_id where l.phone = '+5511900000308'), 1, 'desqualificado: call criada');
  set local role anon;
  r := public.public_book('demo', 'Hana Luz', 'hana@exemplo.com', '+5511900000309', pg_temp.at_local(d, '17:00'));
  reset role;
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'perdido agenda: ' || r::text);
  perform pg_temp.assert_eq((select stage from public.leads where phone = '+5511900000309'), 'perdido', 'perdido não muda de estágio');
  perform pg_temp.assert_eq((select count(*)::int from public.outbox where phone = '+5511900000309'), 0, 'perdido não recebe WhatsApp');
  perform pg_temp.assert_eq((select count(*)::int from public.funnel_events f join public.leads l on l.id = f.lead_id
                             where l.phone in ('+5511900000308', '+5511900000309') and f.stage = 'agendado'), 0, 'funil não registra agendado');

  -- 6) _book_core (book_slot do motor) também só promove a partir de estágio não terminal
  perform pg_temp.as_service();
  perform pg_temp.mk_rule(pg_temp.mk_closer('Cris C'), 1, '14:00', '16:00');
  insert into public.leads (phone, name, stage) values ('+5511900000310', 'Ivo', 'desqualificado'), ('+5511900000311', 'Jade', 'respondeu');
  r := public.book_slot((select id from public.leads where phone = '+5511900000310'), pg_temp.at_local(d, '14:00'));
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'book_slot em desqualificado cria a call: ' || r::text);
  perform pg_temp.assert_eq((select stage from public.leads where phone = '+5511900000310'), 'desqualificado', 'book_slot não promove desqualificado');
  r := public.book_slot((select id from public.leads where phone = '+5511900000311'), pg_temp.at_local(d, '15:00'));
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'book_slot em lead normal: ' || r::text);
  perform pg_temp.assert_eq((select stage from public.leads where phone = '+5511900000311'), 'agendado', 'book_slot promove lead não terminal');
end;
$$;
rollback;

\echo OK 45_public_abuse
