# Неофіційне оновлення RabbitMQ 3.x до 4.1

Наведені нижче інструкції допоможуть оновити брокер RabbitMQ зі старої версії (до 3.13) до 4.1.  У процесі описано два варіанти міграції:

1. **Повне зупинення сервісу** – простий, але потребує тривалого простою.
2. **Поступове оновлення з використанням Shovel** – вимагає кластера, зате зводить downtime до мінімуму.

> **Увага.** Перед початком будь-яких дій зробіть резервну копію каталогу `/var/lib/rabbitmq`.

---

## Попередня підготовка

1. Збережіть налаштування старої ноди:
   ```bash
   rabbitmqctl export_definitions /tmp/defs.json
   ```
2. Приберіть політики з `ha-mode`, щоб вони не заважали новій інсталяції:
   ```bash
   jq '(.policies) |= map(select(.definition."ha-mode" | not))' \
       /tmp/defs.json > /tmp/defs_clean.json
   ```

---

## Спосіб 1. Повне зупинення (більший downtime)

1. Встановіть утиліту `rabbitmq-dump-queue` (потрібен Go):
   ```bash
   dnf install go
   go get github.com/dubek/rabbitmq-dump-queue

   # додаємо в PATH
   export GOPATH=$HOME/go
   export PATH=$PATH:$GOROOT/bin:$GOPATH/bin
   ```
2. Зробіть дамп усіх черг зі старої ноди (користувач має мати повні права):
   ```bash
   python3.9 rabbitmq_dump_all_queues.py export \
       --host 127.0.0.1 \
       -u guest -p 'guest' \
       --out /tmp/rabbitmq_dump_$(date +'%m-%d-%Y') \
       --parallel 12 --batch 50000
   ```
3. Зупиніть стару ноду:
   ```bash
   service rabbitmq-server stop
   ```
4. Встановіть новий RabbitMQ (приклад для CentOS – [офіційна інструкція](https://www.rabbitmq.com/docs/3.13/install-rpm#cloudsmith)) та запустіть сервіс:
   ```bash
   service rabbitmq-server start
   ```
5. **Опційно.** Якщо в попередній версії був плагін `rabbitmq_delayed_message_exchange`, встановіть його з GitHub:
   ```bash
   cd /lib/rabbitmq/lib/rabbitmq_server-4.1.1/plugins
   wget https://github.com/rabbitmq/rabbitmq-delayed-message-exchange/releases/download/v4.1.0/rabbitmq_delayed_message_exchange-4.1.0.ez
   ```
6. Імпортуйте налаштування у нову ноду:
   ```bash
   rabbitmqctl import_definitions /tmp/defs_clean.json
   ```
7. Відновіть повідомлення з дампу:
   ```bash
   python3.9 rabbitmq_dump_all_queues.py import \
       --host 127.0.0.1 -u guest -p 'guest' \
       --indir /tmp/rabbitmq_dump_$(date +'%m-%d-%Y')
   ```

---

## Спосіб 2. Поступове оновлення через Shovel (мінімальний downtime)

Цей варіант підходить, якщо у вас є кластер. Ноду по черзі виводять з кластера, оновлюють та синхронізують повідомлення через плагін `rabbitmq_shovel`.

1. Зупиніть ноду, яку оновлюєте, та встановіть новий RabbitMQ (аналогічно до кроку 4 вище). За потреби можна видалити каталог `mnesia` – це дозволить почати з чистої бази.
2. Імпортуйте збережені налаштування:
   ```bash
   rabbitmqctl import_definitions /tmp/defs_clean.json
   ```
3. Увімкніть плагін:
   ```bash
   rabbitmq-plugins enable rabbitmq_shovel
   ```
4. На обох нодах додайте користувачу (наприклад, `guest`) тег `policymaker`:
   ```bash
   rabbitmqctl set_user_tags guest policymaker
   ```
5. На старій ноді отримайте список черг:
   ```bash
   curl -s -u guest:guest http://127.0.0.1:15672/api/queues | jq -r '.[]'
   ```
6. Запустіть скрипт `dump_with_shovel.sh`, попередньо налаштувавши змінні оточення:
   ```bash
   export SRC_HOST=10.1.1.10   # стара нода
   export DST_HOST=10.1.1.20   # нова нода
   export SRC_HTTP_USER=guest
   export SRC_HTTP_PASS='guest'
   export SHOVEL_USER=guest
   export SHOVEL_PASS='guest'

   chmod +x dump_with_shovel.sh
   ./dump_with_shovel.sh
   ```
7. Слідкуйте за прогресом:
   ```bash
   rabbitmqctl shovel_status --formatter=pretty_table
   # або
   curl -u $SRC_HTTP_USER:$SRC_HTTP_PASS http://$SRC_HOST:15672/api/shovels | jq
   ```
8. Після завершення переконайтесь, що кількість повідомлень у кожній черзі на новій ноді відповідає очікуваній. Потім вимкніть старий брокер і переведіть трафік на оновлений кластер.

---

В обох випадках перевірте коректність роботи нової версії перед поверненням системи у продакшн.
