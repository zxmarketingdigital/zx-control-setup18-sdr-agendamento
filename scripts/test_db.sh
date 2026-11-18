#!/usr/bin/env bash
# Testes de banco do Setup 18 (SDR de Agendamento).
# Sobe um Supabase LOCAL só com o Postgres, recria o banco pelas migrations, roda
# cada tests/sql/*.sql (qualquer falha derruba o script), faz o teste de concorrência
# e para a stack ao sair. Autoria e suporte: ZX LAB (https://suporte.zxlab.com.br/hub)
#
# Precisa de: Docker rodando, Supabase CLI e psql.
#   bash scripts/test_db.sh
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

# Porta do Postgres vem do próprio config.toml (faixa 5562x).
DB_PORT="$(awk '/^\[db\]/{f=1;next} /^\[/{f=0} f && /^port *=/{gsub(/[^0-9]/,"",$3); print $3; exit}' supabase/config.toml)"
: "${DB_PORT:?não achei [db].port em supabase/config.toml}"
export PGPASSWORD=postgres
PSQL=(psql -h 127.0.0.1 -p "$DB_PORT" -U postgres -d postgres -X -q -v ON_ERROR_STOP=1)

for bin in docker supabase psql; do
  command -v "$bin" >/dev/null 2>&1 || { echo "ERRO: '$bin' não encontrado no PATH." >&2; exit 2; }
done
docker info >/dev/null 2>&1 || { echo "ERRO: o Docker não está rodando." >&2; exit 2; }

cleanup() {
  local rc=$?
  echo "==> parando o Supabase local"
  supabase stop --no-backup >/dev/null 2>&1 || true
  if [ "$rc" -eq 0 ]; then echo "==> TUDO OK"; else echo "==> FALHOU (código $rc)" >&2; fi
  exit "$rc"
}
trap cleanup EXIT

echo "==> subindo o Supabase local (só o banco, portas ${DB_PORT%?}x)"
supabase start -x gotrue,realtime,storage-api,imgproxy,kong,mailpit,postgrest,postgres-meta,studio,edge-runtime,logflare,vector,supavisor

echo "==> recriando o banco pelas migrations (db reset)"
supabase db reset

echo "==> testes SQL"
shopt -s nullglob
total=0
for f in tests/sql/*.sql; do
  case "$(basename "$f")" in _*) continue ;; esac   # _helpers e fixtures não são testes
  echo "--- $f"
  "${PSQL[@]}" -f "$f"
  total=$((total + 1))
done
[ "$total" -gt 0 ] || { echo "ERRO: nenhum teste em tests/sql/" >&2; exit 1; }

echo "--- preflight: aborta em banco com tabelas alheias"
DB_PORT="$DB_PORT" bash tests/sql/preflight_alheio.sh
total=$((total + 1))

echo "--- colisão de telefone ao normalizar aborta com mensagem clara"
DB_PORT="$DB_PORT" bash tests/sql/colisao_telefone.sh
total=$((total + 1))

echo "--- telefone ambíguo aborta a migration com os ids"
DB_PORT="$DB_PORT" bash tests/sql/telefone_ambiguo.sh
total=$((total + 1))

echo "--- seed_demo.sql recusa banco já configurado pelo aluno"
"${PSQL[@]}" -c "update public.agent_config set company = 'Empresa Real', agent_name = 'Real'"
if "${PSQL[@]}" -f supabase/seed_demo.sql >/dev/null 2>&1; then
  echo "FALHOU: seed_demo.sql rodou sobre um agent_config já configurado." >&2
  exit 1
fi
agora="$("${PSQL[@]}" -At -c "select company || '/' || agent_name from public.agent_config")"
[ "$agora" = "Empresa Real/Real" ] || { echo "FALHOU: seed alterou a configuração do aluno ($agora)." >&2; exit 1; }
"${PSQL[@]}" -c "update public.agent_config set company = null, agent_name = 'Ana'"
echo "OK seed recusado (configuração intacta)"
total=$((total + 1))

echo "--- gate: grants só nos objetos do setup"
if grep -niE '(all|on all) +(tables|sequences|functions) +in +schema' supabase/migrations/*.sql; then
  echo "FALHOU: grant/revoke em todo o schema public (afetaria objetos de outros apps)." >&2
  exit 1
fi
echo "OK gate de grants"

echo "--- concorrência: 8 sessões disputando o mesmo horário"
SLOT="$("${PSQL[@]}" -f tests/sql/_conc_setup.sql | tail -n 1)"
[ -n "$SLOT" ] || { echo "ERRO: fixture de concorrência não devolveu o horário" >&2; exit 1; }
tmp="$(mktemp -d)"
for i in 10 11 12 13 14 15 16 17; do
  (
    "${PSQL[@]}" -At -c "select set_config('request.jwt.claims', '{\"role\":\"service_role\"}', false)" \
      -c "select coalesce(public.book_slot('00000000-0000-0000-0000-0000000000${i}', '${SLOT}'::timestamptz) ->> 'ok', 'null')" \
      | tail -n 1 > "$tmp/$i.out"
  ) &
done
wait
oks="$(cat "$tmp"/*.out | grep -c '^true$' || true)"
falhas="$(cat "$tmp"/*.out | grep -c '^false$' || true)"
conta="$("${PSQL[@]}" -At -c "select count(*) from public.appointments where closer_id = '00000000-0000-0000-0000-00000000c0c0'")"
"${PSQL[@]}" -c "delete from public.leads where id::text like '00000000-0000-0000-0000-0000000000%'" \
              -c "delete from public.closers where id = '00000000-0000-0000-0000-00000000c0c0'"
rm -rf "$tmp"
if [ "$oks" != "1" ] || [ "$falhas" != "7" ] || [ "$conta" != "1" ]; then
  echo "FALHOU concorrência: ok=$oks falhas=$falhas agendamentos=$conta (esperado 1 / 7 / 1)" >&2
  exit 1
fi
echo "OK concorrência (1 venceu, 7 recusados, 1 agendamento gravado)"
total=$((total + 1))

echo "--- gate: nomes e termos proibidos em arquivos públicos"
if grep -rniE --include='*.sql' --include='*.sh' --include='*.toml' \
     '\b([V]ictor|[Á]lvaro|[A]lvaro|[C]amila|[S]andro|[P]aulo|[R]afael)\b|seu [M]ac\b' supabase tests/sql scripts; then
  echo "FALHOU: nome ou termo proibido acima." >&2
  exit 1
fi
echo "OK gate de nomes"

echo "==> $total grupos de teste passaram"
