#!/usr/bin/env bash
# Telefone local ('(11) 99999-9999', '11999999999') que sobra depois da normalização inequívoca
# aborta a migration com os ids e a instrução, e não altera nenhum telefone. Chamado por scripts/test_db.sh.
set -euo pipefail
cd "$(dirname "$0")/../.."
: "${DB_PORT:?}"
export PGPASSWORD=postgres
BASE=(psql -h 127.0.0.1 -p "$DB_PORT" -U postgres -X -q)
MIG=supabase/migrations/20261118000001_sdr_agendamento.sql
"${BASE[@]}" -d postgres -c 'drop database if exists s18_ambiguo' -c 'create database s18_ambiguo'
trap '"${BASE[@]}" -d postgres -c "drop database if exists s18_ambiguo" >/dev/null 2>&1 || true' EXIT
"${BASE[@]}" -d s18_ambiguo -v ON_ERROR_STOP=1 \
  -c 'create schema if not exists auth' -c 'create schema if not exists extensions' \
  -c 'create table auth.users (id uuid primary key, email text)' \
  -c "create function auth.uid() returns uuid language sql as 'select null::uuid'" \
  -c "create function auth.role() returns text language sql as 'select null::text'"
"${BASE[@]}" -d s18_ambiguo -v ON_ERROR_STOP=1 -f "$MIG" >/dev/null 2>&1 || { echo "FALHOU: migration não rodou no banco de teste" >&2; exit 1; }
"${BASE[@]}" -d s18_ambiguo -v ON_ERROR_STOP=1 \
  -c 'alter table public.leads drop constraint leads_phone_e164' \
  -c "insert into public.leads (id, phone) values ('00000000-0000-0000-0000-0000000000d1', '(11) 99999-9999'), ('00000000-0000-0000-0000-0000000000d2', '11999999999'), ('00000000-0000-0000-0000-0000000000d3', '5511977770003')"
if saida="$("${BASE[@]}" -d s18_ambiguo -v ON_ERROR_STOP=1 -f "$MIG" 2>&1)"; then
  echo "FALHOU: a migration passou com telefone ambíguo" >&2; exit 1
fi
echo "$saida" | grep -q "0000000000d1" && echo "$saida" | grep -q "0000000000d2" || { echo "FALHOU: ids ausentes: $saida" >&2; exit 1; }
echo "$saida" | grep -q "0000000000d3" && { echo "FALHOU: lead inequívoco listado como ambíguo: $saida" >&2; exit 1; }
echo "$saida" | grep -q "corrija esses telefones com DDI" || { echo "FALHOU: instrução ausente: $saida" >&2; exit 1; }
n="$("${BASE[@]}" -d s18_ambiguo -At -c "select count(*) from public.leads where phone in ('(11) 99999-9999','11999999999','5511977770003')")"
[ "$n" = "3" ] || { echo "FALHOU: a migration alterou telefones antes de abortar ($n)" >&2; exit 1; }
echo "OK telefone ambíguo aborta com ids"
