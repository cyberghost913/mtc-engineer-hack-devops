#!/usr/bin/env python3
"""Adapt the vendored manifest to VXLAN and a chosen pool, without remote input."""
import copy
import ipaddress
from pathlib import Path
import sys
import yaml


def render(documents, cidr):
    ipaddress.IPv4Network(cidr)
    documents = copy.deepcopy(documents)
    config = next(d for d in documents if d and d['kind'] == 'ConfigMap'
                  and d['metadata']['name'] == 'calico-config')
    config['data']['calico_backend'] = 'vxlan'
    daemon = next(d for d in documents if d and d['kind'] == 'DaemonSet'
                  and d['metadata']['name'] == 'calico-node')
    container = next(c for c in daemon['spec']['template']['spec']['containers']
                     if c['name'] == 'calico-node')
    values = {'CALICO_IPV4POOL_CIDR': cidr, 'CALICO_IPV4POOL_IPIP': 'Never',
              'CALICO_IPV4POOL_VXLAN': 'Always',
              'IP_AUTODETECTION_METHOD': 'kubernetes-internal-ip', 'CLUSTER_TYPE': 'k8s'}
    container['env'] = [e for e in container['env'] if e['name'] not in values]
    container['env'].extend({'name': k, 'value': v} for k, v in values.items())
    for probe in ('livenessProbe', 'readinessProbe'):
        command = container[probe]['exec']['command']
        container[probe]['exec']['command'] = [v for v in command if v not in ('-bird-live', '-bird-ready')]
    return documents


if __name__ == '__main__':
    source, dest, cidr = sys.argv[1:]
    output = yaml.safe_dump_all(render(list(yaml.safe_load_all(Path(source).read_text())), cidr), sort_keys=False)
    target = Path(dest)
    if not target.exists() or target.read_text() != output:
        target.write_text(output)
        print('CHANGED')
    else:
        print('UNCHANGED')
