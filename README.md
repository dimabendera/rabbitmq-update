# Не офеційне оновлення rabbitmq<3.13 до rabbitmq 4.1

## Попередні налаштувань
Для початку про всяк випадок копіюємо /var/lib/rabbitmq щоб якщо щось пішло не так ми могли повернутись до попередньої весії

Експорт юзерів паролів черг зі старої ноди
rabbitmqctl export_definitions /tmp/defs.json 

Прибираю ha-mode
jq '(.policies) |= map(select(.definition."ha-mode" | not))' \
    /tmp/defs.json > /tmp/defs_clean.json

## Встанвовлюємо Новий реббіт Спосіб 1 (більший down time)

Встановлююмо утіліту rabbitmq-dump-queue
```bash
dnf install go
go get github.com/dubek/rabbitmq-dump-queue

# додаємо в PATH
export GOPATH=$HOME/go
export PATH=$PATH:$GOROOT/bin:$GOPATH/bin
```

Дамплю всі черги на старій ноді (переконайся що в користувача є всі права)
```   
python3.9 rabbitmq_dump_all_queues.py export \
    --host 127.0.0.1 \
    -u guest -p 'guest' \
    --out /tmp/rabbitmq_dump_$(date +'%m-%d-%Y') \
    --parallel 12 --batch 50000
```

Зупиняю старій ноди 
```
service rabbitmq-server stop
```


Встановлюю новий rabbitmq
Наприклад для centos 
https://www.rabbitmq.com/docs/3.13/install-rpm#cloudsmith
і запускаю
```
service rabbitmq-server start
```


(Опційно)
Додаю плагіни, які є в старій версії rabbitmq_delayed_message_exchange-4.1.0
cd /lib/rabbitmq/lib/rabbitmq_server-4.1.1/plugins
wget https://github.com/rabbitmq/rabbitmq-delayed-message-exchange/releases/download/v4.1.0/rabbitmq_delayed_message_exchange-4.1.0.ez

Імпорт юзерів паролів черг в нову ноду
rabbitmqctl import_definitions /tmp/defs_clean.json

Вставляємо повідомлення
```
python3.9 rabbitmq_dump_all_queues.py import \
   --host 127.0.0.1 -u guest -p 'guest' \
   --indir /tmp/rabbitmq_dump_$(date +'%m-%d-%Y') 
```


### Спосіб 2 (менший down time)
Спрацює якщо в вас є кластер - виводимо одну ноду з кластера, оновлюємо, і синхронимо її через плагін rabbitmq_shovel синхронимо різні версії, а потім оновлюємо іншу і доєднуємо до кластера  


Зупиняю старій ноди 
```
service rabbitmq-server stop
```

Встановлюю новий rabbitmq
Наприклад для centos 
https://www.rabbitmq.com/docs/3.13/install-rpm#cloudsmith
і запускаю
service rabbitmq-server start

mnesia можна просто видалити

Імпорт юзерів паролів черг в нову ноду
```
rabbitmqctl import_definitions /tmp/defs_clean.json
```

Ставлю утіліту
```
rabbitmq-plugins enable rabbitmq_shovel
```

На новій і на старій ноді сетапимо для користувача (наприклад guest)
```
rabbitmqctl set_user_tags guest policymaker
```

На ноді зі старішою версією Зняти список черг
```
curl -s -u guest:guest http://127.0.0.1:15672/api/queues | jq -r '.[]'
```

Запускаємо скріпт сінхрону черг
```
export SRC_HOST=10.1.1.10
export DST_HOST=10.1.1.20
export SRC_HTTP_USER=guest
export SRC_HTTP_PASS='guest'
export SHOVEL_USER=guest
export SHOVEL_PASS='guest'

chmod +x dump_with_shovel.sh
./dump_with_shovel.sh

```

Моніторинг прогресу
```
rabbitmqctl shovel_status --formatter=pretty_table
# або
curl -u $SRC_HTTP_USER:$SRC_HTTP_PASS http://$SRC_HOST:15672/api/shovels | jq
```

Після завершення
Перевірити на новому кластері, що Ready-count для кожної черги дорівнює очікуваному.
Вимкнути старий брокер, переключити прод трафік.





