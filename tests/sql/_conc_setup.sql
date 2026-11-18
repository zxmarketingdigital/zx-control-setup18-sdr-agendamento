-- Fixture do teste de concorrência (scripts/test_db.sh). Os dados são COMMITADOS de
-- propósito, para várias sessões disputarem o mesmo horário; o runner apaga depois.
-- Imprime, em uma linha só, o horário disputado.
\set QUIET on
insert into public.closers (id, name, email) values
  ('00000000-0000-0000-0000-00000000c0c0', 'Closer Concorrência', 'conc@demo.invalid');
insert into public.availability_rules (closer_id, weekday, start_time, end_time)
select '00000000-0000-0000-0000-00000000c0c0', extract(dow from d)::int, '09:00', '12:00'
from (select ((now() at time zone 'America/Sao_Paulo')::date + 3) as d) x;
insert into public.leads (id, phone, name)
select ('00000000-0000-0000-0000-0000000000' || lpad(i::text, 2, '0'))::uuid,
       '+55119000009' || lpad(i::text, 2, '0'), 'Lead concorrência ' || i
from generate_series(10, 17) i;
\set QUIET off
\pset tuples_only on
\pset format unaligned
select (((now() at time zone 'America/Sao_Paulo')::date + 3 + time '09:00') at time zone 'America/Sao_Paulo')::text;
