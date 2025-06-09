#!/usr/bin/env bash
set -euo pipefail

########################################
# Конфігурація через змінні середовища #
########################################
: "${SRC_HOST:=oldhost}"            # брокер-джерело
: "${DST_HOST:=newhost}"            # брокер-приймач
: "${SRC_HTTP_USER:=admin}"         # HTTP-користувач (MGMT API) на SRC
: "${SRC_HTTP_PASS:=adminPass!}"    # його пароль
: "${SHOVEL_USER:=shovel_user}"     # AMQP-користувач для копіювання
: "${SHOVEL_PASS:=s3cr3t}"          # його пароль
: "${SRC_MGMT_PORT:=15672}"         # MGMT-порт SRC
: "${DST_AMQP_PORT:=5672}"          # AMQP-порт DST
: "${VHOST_FILTER:=}"               # опціонально: «/», «my_vhost» або патерн jq

########################################
# 1. Підготовка прав                   #
########################################
echo "⏱  Додаю тег policymaker для $SHOVEL_USER"
rabbitmqctl set_user_tags "$SHOVEL_USER" policymaker

########################################
# 2. Створення Shovel-параметрів       #
########################################
created=()   # масив для подальшого очищення

# Запит списку черг
queues_json=$(curl -s -u "$SRC_HTTP_USER:$SRC_HTTP_PASS" \
  "http://${SRC_HOST}:${SRC_MGMT_PORT}/api/queues")

# Опціональний фільтр по vhost
if [[ -n "$VHOST_FILTER" ]]; then
  queues_json=$(echo "$queues_json" | jq "[.[] | select(.vhost | test(\"$VHOST_FILTER\"))]")
fi

echo "🔎  Знайдено $(echo "$queues_json" | jq length) черг(и); створюю Shovel-и…"

echo "$queues_json" \
| jq -r '.[] | @base64' \
| while read -r row_b64; do
    _jq() { echo "$row_b64" | base64 --decode | jq -r "$1"; }
    vhost=$(_jq '.vhost')
    qname=$(_jq '.name')

    # URL-encoded vhost
    venc=$(python3 - <<PY
import urllib.parse, sys, os; print(urllib.parse.quote(sys.argv[1], safe=""))
PY
"$vhost")

    # Назва параметра
    sname="dump_${vhost//[^a-zA-Z0-9_-]/_}_${qname//[^a-zA-Z0-9_-]/_}"

    # Тіло запиту
    read -r -d '' body <<JSON
{
  "value": {
    "src-uri":  "amqp://${SHOVEL_USER}:${SHOVEL_PASS}@${SRC_HOST}/${venc}",
    "src-queue": "${qname}",
    "dest-uri": "amqp://${SHOVEL_USER}:${SHOVEL_PASS}@${DST_HOST}:${DST_AMQP_PORT}/${venc}",
    "dest-queue": "${qname}",
    "ack-mode": "on-confirm",
    "prefetch-count": 500,
    "src-delete-after": "never"          /* важливо: не чіпаємо вихідну чергу */
  }
}
JSON

    # Створення параметра
    curl -s -u "$SRC_HTTP_USER:$SRC_HTTP_PASS" \
         -H 'content-type:application/json' \
         -X PUT "http://${SRC_HOST}:${SRC_MGMT_PORT}/api/parameters/shovel/${venc}/${sname}" \
         -d "$body" >/dev/null

    echo "🚀  Shovel '${sname}' створено для vhost='${vhost}', queue='${qname}'"
    created+=("${vhost}|${sname}")
done

########################################
# 3. Очистка при виході                #
########################################
cleanup() {
  echo -e "\n🧹  Видаляю Shovel-параметри, створені скриптом…"
  for entry in "${created[@]}"; do
    IFS='|' read -r vhost sname <<< "$entry"
    rabbitmqctl clear_parameter shovel "$vhost" "$sname" || true
    echo "✅  $vhost/$sname очищено"
  done
}
trap cleanup EXIT

echo -e "\n✅  Усі Shovel-и запущено.  Скрипт працюватиме, допоки ви його не завершите (Ctrl-C)…"
# Блокуємося, слідкуючи за станом Shovel-ів
while true; do
  sleep 30
  active=$(rabbitmqctl shovel_status --formatter=json | jq '[.[] | select(.state=="running")] | length')
  echo "⏰  $(date '+%H:%M:%S') – активних Shovel-ів: $active"
done
