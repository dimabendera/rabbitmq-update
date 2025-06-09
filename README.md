# Unofficial RabbitMQ 3.x to 4.1 Upgrade Guide

The steps below describe two approaches for upgrading a RabbitMQ broker from a pre-3.13 release to 4.1:

1. **Complete stop** – easier but requires longer downtime.
2. **Rolling upgrade with Shovel** – requires a cluster but keeps downtime to a minimum.

> **Important:** Back up `/var/lib/rabbitmq` before starting.

---

## Preparation

1. Save definitions from the old node:
   ```bash
   rabbitmqctl export_definitions /tmp/defs.json
   ```
2. Remove policies that use `ha-mode` so they do not interfere with the new installation:
   ```bash
   jq '(.policies) |= map(select(.definition."ha-mode" | not))' \
       /tmp/defs.json > /tmp/defs_clean.json
   ```

---

## Method 1: Full stop (longer downtime)

1. Install `rabbitmq-dump-queue` (requires Go):
   ```bash
   dnf install go
   go get github.com/dubek/rabbitmq-dump-queue

   # add to PATH
   export GOPATH=$HOME/go
   export PATH=$PATH:$GOROOT/bin:$GOPATH/bin
   ```
2. Dump all queues from the old node (the user must have full rights):
   ```bash
   python3.9 rabbitmq_dump_all_queues.py export \
       --host 127.0.0.1 \
       -u guest -p 'guest' \
       --out /tmp/rabbitmq_dump_$(date +'%m-%d-%Y') \
       --parallel 12 --batch 50000
   ```
3. Stop the old node:
   ```bash
   service rabbitmq-server stop
   ```
4. Install the new RabbitMQ (CentOS example – [official docs](https://www.rabbitmq.com/docs/3.13/install-rpm#cloudsmith)) and start the service:
   ```bash
   service rabbitmq-server start
   ```
5. **Optional.** If the previous version used `rabbitmq_delayed_message_exchange`, install it from GitHub:
   ```bash
   cd /lib/rabbitmq/lib/rabbitmq_server-4.1.1/plugins
   wget https://github.com/rabbitmq/rabbitmq-delayed-message-exchange/releases/download/v4.1.0/rabbitmq_delayed_message_exchange-4.1.0.ez
   ```
6. Import the definitions to the new node:
   ```bash
   rabbitmqctl import_definitions /tmp/defs_clean.json
   ```
7. Restore messages from the dump:
   ```bash
   python3.9 rabbitmq_dump_all_queues.py import \
       --host 127.0.0.1 -u guest -p 'guest' \
       --indir /tmp/rabbitmq_dump_$(date +'%m-%d-%Y')
   ```

---

## Method 2: Rolling upgrade using Shovel (minimal downtime) (it is advisable not to use - it is necessary to test and supplement)

This approach is for clusters. Each node is upgraded in turn and messages are synced via the `rabbitmq_shovel` plugin.

1. Stop the node being upgraded and install the new RabbitMQ (same as step 4 above). Optionally remove the `mnesia` directory to start with a clean database.
2. Import the saved definitions:
   ```bash
   rabbitmqctl import_definitions /tmp/defs_clean.json
   ```
3. Enable the plugin:
   ```bash
   rabbitmq-plugins enable rabbitmq_shovel
   ```
4. On both nodes add the `policymaker` tag to the user (e.g. `guest`):
   ```bash
   rabbitmqctl set_user_tags guest policymaker
   ```
5. On the old node get the list of queues:
   ```bash
   curl -s -u guest:guest http://127.0.0.1:15672/api/queues | jq -r '.[]'
   ```
6. Run `dump_with_shovel.sh` after setting the environment variables:
   ```bash
   export SRC_HOST=10.1.1.10   # old node
   export DST_HOST=10.1.1.20   # new node
   export SRC_HTTP_USER=guest
   export SRC_HTTP_PASS='guest'
   export SHOVEL_USER=guest
   export SHOVEL_PASS='guest'

   chmod +x dump_with_shovel.sh
   ./dump_with_shovel.sh
   ```
7. Monitor the progress:
   ```bash
   rabbitmqctl shovel_status --formatter=pretty_table
   # or
   curl -u $SRC_HTTP_USER:$SRC_HTTP_PASS http://$SRC_HOST:15672/api/shovels | jq
   ```
8. When finished, confirm that each queue on the new node contains the expected number of messages. Then disable the old broker and switch traffic to the upgraded cluster.

---

Check that the new version works correctly before returning the system to production.
