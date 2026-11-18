#!/usr/bin/env bash
# Preflight da migration: em um banco com public.leads de OUTRO app (sem agent_config),
# a migration aborta com mensagem clara e não cria nada. Chamado por scripts/test_db.sh.
set -euo pipefail
cd "$(dirname "$0")/../.."
: "${DB_PORT:?}"
export PGPASSWORD=postgres
BASE=(psql -h 127.0.0.1 -p "$DB_PORT" -U postgres -X -q)
"${BASE[@]}" -d postgres -c 'drop database if exists s18_preflight' -c 'create database s18_preflight'
trap '"${BASE[@]}" -d postgres -c "drop database if exists s18_preflight" >/dev/null 2>&1 || true' EXIT
"${BASE[@]}" -d s18_preflight -c 'create table public.leads (id int primary key, nome text)'
if saida="$("${BASE[@]}" -d s18_preflight -v ON_ERROR_STOP=1 -f supabase/migrations/20261118000001_sdr_agendamento.sql 2>&1)"; then
  echo "FALHOU: a migration rodou em um banco com public.leads alheio." >&2
  exit 1
fi
echo "$saida" | grep -qi "use um projeto Supabase novo" || { echo "FALHOU: mensagem do preflight ausente: $saida" >&2; exit 1; }
n="$("${BASE[@]}" -d s18_preflight -At -c "select count(*) from information_schema.tables where table_schema='public' and table_name in ('agent_config','appointments','messages')")"
[ "$n" = "0" ] || { echo "FALHOU: o preflight deixou tabelas para trás ($n)." >&2; exit 1; }
echo "OK preflight aborta em banco com public.leads alheio"

# Tabela alheia de nome comum (blocks) sem marcador: também aborta e não cria nada.
"${BASE[@]}" -d postgres -c 'drop database if exists s18_preflight2' -c 'create database s18_preflight2'
trap '"${BASE[@]}" -d postgres -c "drop database if exists s18_preflight" -c "drop database if exists s18_preflight2" >/dev/null 2>&1 || true' EXIT
"${BASE[@]}" -d s18_preflight2 -c 'create table public.blocks (id int primary key, x text)'
if saida="$("${BASE[@]}" -d s18_preflight2 -v ON_ERROR_STOP=1 -f supabase/migrations/20261118000001_sdr_agendamento.sql 2>&1)"; then
  echo "FALHOU: a migration rodou em um banco com public.blocks alheio." >&2
  exit 1
fi
echo "$saida" | grep -q "blocks" && echo "$saida" | grep -qi "use um projeto Supabase novo" || { echo "FALHOU: mensagem do preflight (blocks) ausente: $saida" >&2; exit 1; }
n="$("${BASE[@]}" -d s18_preflight2 -At -c "select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname='public' and c.relrowsecurity")"
[ "$n" = "0" ] || { echo "FALHOU: o preflight mexeu em RLS de tabela alheia ($n)" >&2; exit 1; }
echo "OK preflight aborta com public.blocks alheio sem marcador"
