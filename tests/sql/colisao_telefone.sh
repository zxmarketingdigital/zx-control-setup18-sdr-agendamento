#!/usr/bin/env bash
# Migration aborta com mensagem clara (ids) quando duas linhas antigas normalizam para o mesmo
# E.164, e não deixa nenhum telefone alterado. Chamado por scripts/test_db.sh.
set -euo pipefail
cd "$(dirname "$0")/../.."
: "${DB_PORT:?}"
export PGPASSWORD=postgres
BASE=(psql -h 127.0.0.1 -p "$DB_PORT" -U postgres -X -q)
MIG=supabase/migrations/20261118000001_sdr_agendamento.sql
# Banco novo com o mínimo que o Supabase fornece (auth e schema extensions), depois a migration.
"${BASE[@]}" -d postgres -c 'drop database if exists s18_colisao' -c 'create database s18_colisao'
trap '"${BASE[@]}" -d postgres -c "drop database if exists s18_colisao" >/dev/null 2>&1 || true' EXIT
"${BASE[@]}" -d s18_colisao -v ON_ERROR_STOP=1 \
  -c 'create schema if not exists auth' -c 'create schema if not exists extensions' \
  -c 'create table auth.users (id uuid primary key, email text)' \
  -c "create function auth.uid() returns uuid language sql as 'select null::uuid'" \
  -c "create function auth.role() returns text language sql as 'select null::text'"
"${BASE[@]}" -d s18_colisao -v ON_ERROR_STOP=1 -f "$MIG" >/dev/null 2>&1 || { echo "FALHOU: migration não rodou no banco de teste" >&2; exit 1; }
"${BASE[@]}" -d s18_colisao -v ON_ERROR_STOP=1 \
  -c 'alter table public.leads drop constraint leads_phone_e164' \
  -c "insert into public.leads (id, phone) values ('00000000-0000-0000-0000-0000000000c1', '5511977770001'), ('00000000-0000-0000-0000-0000000000c2', '5511977770001@c.us'), ('00000000-0000-0000-0000-0000000000c3', '+5511977770009'), ('00000000-0000-0000-0000-0000000000c4', '5511977770009')"
if saida="$("${BASE[@]}" -d s18_colisao -v ON_ERROR_STOP=1 -f "$MIG" 2>&1)"; then
  echo "FALHOU: a migration passou com telefones que colidem" >&2; exit 1
fi
echo "$saida" | grep -q "Telefones duplicados" || { echo "FALHOU: mensagem de colisão ausente: $saida" >&2; exit 1; }
echo "$saida" | grep -q "0000000000c1" && echo "$saida" | grep -q "0000000000c4" || { echo "FALHOU: ids ausentes na mensagem: $saida" >&2; exit 1; }
echo "$saida" | grep -qi "unique" && { echo "FALHOU: erro genérico de unique: $saida" >&2; exit 1; }
n="$("${BASE[@]}" -d s18_colisao -At -c "select count(*) from public.leads where phone in ('5511977770001','5511977770001@c.us','5511977770009')")"
[ "$n" = "3" ] || { echo "FALHOU: a migration alterou telefones antes de abortar ($n)" >&2; exit 1; }
echo "OK colisão de telefone aborta com mensagem clara"
