Додати 3 ноду 
збільшити File descriptors

Створити vhost з дефолтними чергами quorum
```bash
rabbitmqctl add_vhost /quorum --description "HA vhost" --default-queue-type quorum
rabbitmqctl set_permissions -p /quorum app '.*' '.*' '.*'
```

Налаштуйте Federation:
```bash
rabbitmqctl set_parameter \
    --vhost /quorum \
    federation-upstream quorum-migration-upstream \
    '{"uri":"amqp:///%2f","trust-user-id":true}'
```


Замінити в файлі '"vhost":"/",'  на '"vhost":"/quorum",'. почистіть ha-* й x-queue-mode, x-max-priority, x-overflow …
cat migrate_queues_definitions_to_quorum.py 
```python
import json
import argparse
import subprocess

def main():
    parser = argparse.ArgumentParser(description="Script to modify RabbitMQ definitions JSON for migration to quorum queues in a new vhost.")
    parser.add_argument("input", help="Path to the input JSON file (old_definitions.json)")
    parser.add_argument("output", help="Path to the output JSON file (modified_definitions.json)")
    parser.add_argument("--old_vhost", default="/", help="Old virtual host name (default: /)")
    parser.add_argument("--new_vhost", default="/quorum", help="New virtual host name (default: /quorum)")
    parser.add_argument("--host", default="localhost", help="RabbitMQ management host (default: localhost)")
    parser.add_argument("--port", default=15672, type=int, help="RabbitMQ management port (default: 15672)")
    parser.add_argument("--username", default="guest", help="RabbitMQ management username (default: guest)")
    parser.add_argument("--password", default="guest", help="RabbitMQ management password (default: guest)")
    parser.add_argument("--do_import", action="store_true", help="If set, import the modified JSON to RabbitMQ after editing")

    args = parser.parse_args()

    # Load the JSON
    with open(args.input, 'r') as f:
        data = json.load(f)

    # Sections to keep and process
    sections = ['queues', 'exchanges', 'bindings', 'policies', 'parameters']

    # No filtering needed for single vhost export
    # Do not set 'vhost' in items, as import uses -V

    # Remove unnecessary sections
    for key in list(data.keys()):
        if key not in sections and key not in ['rabbit_version', 'product_version', 'product_name']:
            del data[key]

    # Edit queues
    for queue in data.get('queues', []):
        queue['durable'] = True
        arguments = queue.get('arguments', {})
        # Remove x-queue-type
        arguments.pop('x-queue-type', None)
        # Remove x-max-priority
        arguments.pop('x-max-priority', None)
        # Remove x-queue-mode
        arguments.pop('x-queue-mode', None)
        # Change x-overflow if reject-publish-dlx
        if 'x-overflow' in arguments and arguments['x-overflow'] == 'reject-publish-dlx':
            arguments['x-overflow'] = 'reject-publish'

    # Edit policies
    policies = data.get('policies', [])
    ha_keys = ['ha-mode', 'ha-params', 'ha-sync-mode', 'ha-promote-on-shutdown', 'ha-promote-on-failure']
    new_policies = []
    for policy in policies:
        definition = policy.get('definition', {})
        # Remove HA-related keys
        for key in ha_keys:
            definition.pop(key, None)
        # Change overflow if needed
        if 'overflow' in definition and definition['overflow'] == 'reject-publish-dlx':
            definition['overflow'] = 'reject-publish'
        # Remove federation from exchange policies
        apply_to = policy.get('apply-to', 'all')
        if apply_to in ['exchanges', 'all']:
            for key in list(definition.keys()):
                if key.startswith('federation-'):
                    del definition[key]
        # Drop if definition is empty
        if definition:
            new_policies.append(policy)

    data['policies'] = new_policies

    # Add federation policy for queues (without 'vhost')
    federation_policy = {
        'name': 'federation-migration',
        'pattern': '.*',
        'apply-to': 'queues',
        'definition': {'federation-upstream-set': 'quorum-migration-upstream'},
        'priority': 10
    }
    data['policies'].append(federation_policy)

    # Save modified JSON
    with open(args.output, 'w') as f:
        json.dump(data, f, indent=4)

    print(f"Modified JSON saved to {args.output}")

    # Optionally import
    if args.do_import:
        cmd = [
            'rabbitmqadmin',
            '--host', args.host,
            '--port', str(args.port),
            '--username', args.username,
            '--password', args.password,
            '--vhost', args.new_vhost,
            'import', args.output
        ]
        try:
            subprocess.run(cmd, check=True)
            print("Imported modified definitions to RabbitMQ")
        except subprocess.CalledProcessError as e:
            print(f"Error importing: {e}")

if __name__ == "__main__":
    main()
```

Запуск
```
python3 migrate_queues_definitions_to_quorum.py /tmp/old_definitions.json /tmp/modified_definitions.json
```

Імпорт
```bash
rabbitmqadmin import -V /quorum modified_definitions.json
```

Застосовую синхрон черг тест для однієї черги
```bash
rabbitmqctl set_policy \
   --vhost /quorum \
   federate-all-queues '^.*$' \
   '{"federation-upstream":"quorum-migration-upstream"}' \
   --apply-to queues \
   --priority 10
```

Черги не заповняться автоматично – Federation притягне повідомлення тільки під споживачів. 
Тож «починайте розгрібати» просто через /quorum; система сама візьме дані зі старих черг.
Коли все з’їдено – старий vhost можна вимкнути.

Усюди в конфіг файлі додати vhost: "/quorum"
