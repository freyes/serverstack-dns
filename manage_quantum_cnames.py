#!/usr/bin/python
import ConfigParser
import datetime
import os
import logging

from subprocess import check_call

from kombu import Connection, Exchange, Queue
from novaclient.v1_1 import client

CONFIG_FILE = '/etc/serverstack/bastion_dns.conf'

logging.basicConfig(level=logging.INFO)


def get_config():
    config_in = ConfigParser.SafeConfigParser()
    if not config_in.read(CONFIG_FILE):
        raise Exception('Couldnt load config @ %s.' % CONFIG_FILE)

    def _get(setting):
        return config_in.get('DEFAULT', setting)

    conf = {
        'os_username': os.getenv('OS_USERNAME') or _get('os_username'),
        'os_password': os.getenv('OS_PASSWORD') or _get('os_password'),
        'os_tenant_name': (os.getenv('OS_TENANT_NAME') or
                           _get('os_tenant_name')),
        'os_auth_url': os.getenv('OS_AUTH_URL') or _get('os_auth_url'),
        'rabbit_user': _get('rabbit_user'),
        'rabbit_password': _get('rabbit_password'),
        'rabbit_host': _get('rabbit_host'),
        'rabbit_vhost': _get('rabbit_vhost'),
        'rabbit_exchange': _get('rabbit_exchange'),
        'rabbit_topic': _get('rabbit_topic'),
        'include_tenants': _get('include_tenants'),
        'hosts_file': _get('hosts_file'),
        'domain': _get('domain')
    }

    missing = [k for k in conf.iterkeys() if conf[k] is None]
    if missing:
        logging.error('Missing config: %s' % missing)
        raise Exception('Missing config: %s')

    return conf

config = get_config()


def managed_tenants():
    with open(config['include_tenants']) as inc:
        included = inc.readlines()
    return [i.strip() for i in included if not i.startswith('#')]


_client = None


def add_host_entry(hostname, ip, port_id):
    hostname = '%s.%s' % (hostname, config['domain'])
    entry = '%s %s #port %s' % (ip, hostname, port_id)

    with open(config['hosts_file']) as hosts:
        hostnames = hosts.readlines()
    hostnames = [host.strip() for host in hostnames
                 if not host.startswith('#')]

    out = []
    # remove stale entries
    for hostname in hostnames:
        _hostname = hostname.split(' ')
        ip = _hostname[0]
        hn = _hostname[1]
        p = _hostname[2]

        if hn == hostname or p == port_id:
            continue

        out.append(hostname)

    out.append(entry)

    with open(config['hosts_file'], 'wb') as cn:
        cn.write('# Generated %s\n' % datetime.datetime.utcnow())
        cn.write('\n'.join(out))


def remove_host_entry(port_id):
    with open(config['hosts_file']) as hosts:
        hosts = hosts.readlines()
    hosts = [host.strip() for host in hosts if not host.startswith('#')]

    out = []
    for host in hosts:
        if not host.endswith(port_id):
            out.append(host)

    with open(config['hosts_file'], 'wb') as cn:
        cn.write('# Generated %s\n' % datetime.datetime.utcnow())
        cn.write('\n'.join(out))


def get_nova_client():
    global _client
    if _client is not None:
        return _client
    _client = client.Client(config['os_username'], config['os_password'],
                            config['os_tenant_name'], config['os_auth_url'],
                            service_type='compute')
    return _client


def get_instance_hostname(instance_id):
    client = get_nova_client()
    try:
        instance = client.servers.get(instance_id)
        return instance.name.replace(' ', '-')

    except:
        logging.error('Could not get server name for instance %s.' %
                      instance_id)


def reload_zone():
    cmd = ['rndc', 'reload', config['domain']]
    check_call(cmd)


def manage_dns(body):
    if body['event_type'] == 'port.create.end':
        tenant_id = body['_context_tenant_id']
        if tenant_id not in managed_tenants():
            logging.info('Skipping event, not managing DNS for tenant %s.' %
                         tenant_id)
            return

        ip = body['payload']['port']['fixed_ips'][0]['ip_address']
        instance_id = body['payload']['port']['device_id']
        port_id = body['payload']['port']['id']

        hostname = get_instance_hostname(instance_id)
        if not hostname:
            logging.info('Skipping creation of DNS entry for instance %s.' %
                         instance_id)
            return
        logging.info('Creating DNS entry: %s to instance %s with hostname %s.'
                     % (ip, instance_id, hostname))
        add_host_entry(hostname, ip, port_id)
        ensure_dnsmasq()

    if body['event_type'] == 'port.delete.end':
        port_id = body['payload']['port_id']
        logging.info('Deleting DNS entry for port %s.' % port_id)
        remove_host_entry(port_id)
        ensure_dnsmasq()


def process_msg(body, message):
    message.ack()
    try:
        manage_dns(body)
    except Exception as e:
        logging.error('Failed to process notification: %s' % e)


def listen():
    nova_x = Exchange(config['rabbit_exchange'], type='topic', durable=False)
    info_q = Queue(config['rabbit_topic'], exchange=nova_x, durable=False,
                   routing_key=config['rabbit_topic'])

    conn_url = 'amqp://%s:%s@%s/%s' % (config['rabbit_user'],
                                       config['rabbit_password'],
                                       config['rabbit_host'],
                                       config['rabbit_vhost'])
    logging.info('Connecting to rabbit @ %s.' % conn_url)

    with Connection(conn_url) as conn:
        with conn.Consumer(info_q, callbacks=[process_msg]):
            while True:
                try:
                    conn.drain_events()
                except KeyboardInterrupt:
                    break


def ensure_dnsmasq():
    pid_file = os.path.join('/var/run/dnsmasq/', config['domain'] + '.pid')

    cmd = ['dnsmasq', '--strict-order', '--conf-file=', '--bind-interfaces',
           '--domain=%s' % config['domain'], '--no-hosts',
           '--addn-hosts=%s' % config['hosts_file'], '-E', '--pid-file=%s' %
           pid_file]

    if os.path.isfile(pid_file):
        pid = open(pid_file).read().strip()
        proc_cmd = os.path.join('/proc', pid, 'cmdline')
        if os.path.isfile(proc_cmd):
            logging.info('HUPing running dnsmasq (pid: %s)' % pid)
            cmd = ['kill', '-HUP', pid]
            check_call(cmd)
            return
    logging.info('Starting new dnsmasq: %s' % ' '.join(cmd))
    check_call(cmd)


if __name__ == '__main__':
    if not os.path.isfile(config['hosts_file']):
        with open(config['hosts_file'], 'wb'):
            pass
    ensure_dnsmasq()
    listen()
