-- Rodada 6: CHECK de telefone validado, e-mail com < > rejeitado (email_invalido),
-- outbox com JID normalizada e linha com wa_key_id nunca rebaixada.
-- (O aborto por telefone ambíguo é testado em telefone_ambiguo.sh.)
\i tests/sql/_helpers.sql

-- ---- outbox: JID vira E.164; wa_key_id nunca é rebaixada; só a linha sem chave e fora do E.164 vai a erro ----
insert into public.outbox (phone, body, status, wa_key_id) values
  ('5511977761001@s.whatsapp.net', 'jid pendente', 'pendente', null),
  ('5511977761002', 'digitos enviando', 'enviando', null),
  ('(11) 99999-0000', 'lixo com chave', 'enviando', 'KEY-JA-ENVIADA'),
  ('(11) 99999-0001', 'lixo sem chave', 'pendente', null);

\i supabase/migrations/20261118000001_sdr_agendamento.sql

do $$
begin
  perform pg_temp.assert_eq((select status from public.outbox where body = 'jid pendente'), 'pendente', 'outbox com JID normalizado não vira erro');
  perform pg_temp.assert_eq((select phone from public.outbox where body = 'jid pendente'), '+5511977761001', 'outbox JID virou E.164');
  perform pg_temp.assert_eq((select phone from public.outbox where body = 'digitos enviando'), '+5511977761002', 'outbox só dígitos com DDI virou E.164');
  perform pg_temp.assert_eq((select status from public.outbox where body = 'digitos enviando'), 'enviando', 'linha normalizada mantém status');
  perform pg_temp.assert_eq((select status from public.outbox where body = 'lixo com chave'), 'enviando', 'linha com wa_key_id nunca rebaixada');
  perform pg_temp.assert_eq((select status from public.outbox where body = 'lixo sem chave'), 'erro', 'sem wa_key_id e fora do E.164 vai para erro');
  perform pg_temp.assert_true((select last_error like 'telefone_invalido%' from public.outbox where body = 'lixo sem chave'), 'motivo telefone_invalido');
  perform pg_temp.assert_true((select convalidated from pg_constraint where conname = 'leads_phone_e164'), 'CHECK leads_phone_e164 validado');
end;
$$;
delete from public.outbox where body in ('jid pendente', 'digitos enviando', 'lixo com chave', 'lixo sem chave');

-- ---- e-mail da página pública: só caracteres seguros ----
begin;
do $$
declare
  d date := pg_temp.next_dow(1);
  a uuid := pg_temp.mk_closer('Ana A');
  r jsonb;
  e text;
begin
  perform pg_temp.mk_rule(a, 1, '09:00', '12:00');
  update public.schedule_settings set public_slug = 'demo', public_enabled = true,
    public_max_per_hour = 1000, public_max_per_ip_day = 1000, public_max_per_phone_day = 1000;
  foreach e in array array['<svg/onload=x>@x.co', 'a"b@x.co', 'a(b)@x.co', 'a\b@x.co', 'ana@exemplo', 'ana@exemplo.c', 'a<b>@x.co'] loop
    set local role anon;
    r := public.public_book('demo', 'Ana Souza', e, '+5511900005101', pg_temp.at_local(d, '09:00'));
    reset role;
    perform pg_temp.assert_eq(r, jsonb_build_object('ok', false, 'error', 'email_invalido'), 'e-mail recusado: ' || e);
  end loop;
  perform pg_temp.assert_eq((select count(*)::int from public.leads), 0, 'e-mail inválido não cria lead');
  set local role anon;
  r := public.public_book('demo', 'Ana Souza', 'ana.souza+x@exemplo.com.br', '+5511900005101', pg_temp.at_local(d, '09:00'));
  reset role;
  perform pg_temp.assert_true((r ->> 'ok')::boolean, 'e-mail válido agenda: ' || r::text);
end;
$$;
rollback;

\echo OK 51_rodada6
