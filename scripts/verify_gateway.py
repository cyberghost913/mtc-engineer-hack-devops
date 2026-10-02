#!/usr/bin/env python3
"""Verify Gateway API status and a real HTTP request to the node's NodePort."""
import fcntl
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import uuid

CONTROLLER = 'gateway.envoyproxy.io/gatewayclass-controller'
PROXY_SELECTOR = ('gateway.envoyproxy.io/owning-gateway-namespace=demo,'
                  'gateway.envoyproxy.io/owning-gateway-name=demo-gateway')


def kubectl(*args):
    result = subprocess.run(
        ['kubectl', '--kubeconfig=/etc/kubernetes/admin.conf', '--request-timeout=15s', *args],
        text=True, capture_output=True, timeout=20)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout


def get(resource, name=None, namespace=None, selector=None):
    args = ['get', resource]
    if name:
        args.append(name)
    if namespace:
        args += ['-n', namespace]
    if selector:
        args += ['-l', selector]
    return json.loads(kubectl(*args, '-o', 'json'))


def require_conditions(conditions, expected, generation):
    for condition_type in expected:
        matches = [c for c in conditions if c.get('type') == condition_type]
        if len(matches) != 1 or matches[0].get('status') != 'True':
            raise ValueError(f'Condition {condition_type} is not True: {matches}')
        if matches[0].get('observedGeneration') != generation:
            raise ValueError(f'Condition {condition_type} is stale')


def check_routing(gateway_class, gateway, route):
    if gateway_class['spec']['controllerName'] != CONTROLLER:
        raise ValueError('Unexpected Gateway controller')
    require_conditions(gateway_class.get('status', {}).get('conditions', []),
                       ['Accepted'], gateway_class['metadata']['generation'])
    require_conditions(gateway.get('status', {}).get('conditions', []),
                       ['Accepted', 'Programmed'], gateway['metadata']['generation'])
    listeners = [l for l in gateway.get('status', {}).get('listeners', []) if l.get('name') == 'http']
    if len(listeners) != 1 or listeners[0].get('attachedRoutes', 0) < 1:
        raise ValueError('HTTP listener has no attached route')
    require_conditions(listeners[0].get('conditions', []), ['Accepted', 'ResolvedRefs', 'Programmed'],
                       gateway['metadata']['generation'])
    parents = [p for p in route.get('status', {}).get('parents', [])
               if p.get('controllerName') == CONTROLLER
               and p.get('parentRef', {}).get('name') == 'demo-gateway'
               and p['parentRef'].get('namespace', route['metadata']['namespace']) == 'demo'
               and p['parentRef'].get('sectionName') == 'http']
    if len(parents) != 1:
        raise ValueError('HTTPRoute is not attached to the expected Gateway listener')
    require_conditions(parents[0].get('conditions', []), ['Accepted', 'ResolvedRefs'],
                       route['metadata']['generation'])


def select_nodeport(services):
    if len(services) != 1:
        raise ValueError('Expected exactly one Service owned by demo-gateway')
    service = services[0]
    if service['spec']['type'] != 'NodePort':
        raise ValueError('Gateway Service must use NodePort')
    ports = [p for p in service['spec']['ports'] if p['port'] == 80 and p.get('protocol', 'TCP') == 'TCP']
    if len(ports) != 1 or not 30000 <= ports[0].get('nodePort', 0) <= 32767:
        raise ValueError('Gateway HTTP NodePort is missing or outside the standard range')
    return ports[0]['nodePort']


def request(address, port, host, path):
    # http.client talks directly to NodePort and does not use HTTP_PROXY or follow redirects.
    connection = http.client.HTTPConnection(address, port, timeout=5)
    try:
        connection.request('GET', path, headers={'Host': host, 'Connection': 'close'})
        response = connection.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read(65536)
    finally:
        connection.close()


def check_response(status, headers, body):
    if status != 200 or body != b'Hello World!\n':
        raise ValueError(f'Unexpected Gateway response: HTTP {status}, body={body[:100]!r}')
    if headers.get('x-gateway') != 'envoy-gateway' or headers.get('x-app-version') != 'v1':
        raise ValueError('Gateway filter or application response header is missing')
    request_id = headers.get('x-request-id', '')
    if not re.fullmatch(r'[A-Za-z0-9._:-]{1,64}', request_id):
        raise ValueError('Response lacks a valid request ID')
    return request_id


def check_backend_log(text, uri, request_id):
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and all(record.get(k) == v for k, v in {
            'uri': uri, 'request_id': request_id, 'status': 200, 'method': 'GET'}.items()):
            return
    raise ValueError('No matching Nginx access log for the request through Gateway')


def wait_for(check, seconds=180):
    deadline = time.monotonic() + seconds
    while True:
        try:
            return check()
        except (ValueError, RuntimeError, OSError, http.client.HTTPException, subprocess.SubprocessError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)


def verify(report):
    def note(text):
        print(text, flush=True)
        report.write(text + '\n')
        report.flush()

    settings_dir = Path('/etc/devops-foundation')
    lock = json.loads((settings_dir / 'lock.json').read_text())
    install = json.loads((settings_dir / 'gateway-lock.json').read_text())
    host = json.loads((settings_dir / 'gateway-settings.json').read_text())['hostname']
    address = str(ipaddress.IPv4Address(lock['cluster_address']))
    if not re.fullmatch(r'[a-z0-9]+([.-][a-z0-9]+)*', host):
        raise ValueError('Invalid saved Gateway hostname')

    def route_ready():
        check_routing(get('gatewayclass', 'devops-envoy'), get('gateway', 'demo-gateway', 'demo'),
                      get('httproute', 'nginx', 'demo'))
    wait_for(route_ready, 300)
    note('PASS GatewayClass Accepted; Gateway Programmed; HTTPRoute Accepted and ResolvedRefs')

    for resource, image in [('deployment/envoy-gateway', install['controller_image'])]:
        deployment = get(resource, namespace='envoy-gateway-system')
        if deployment['spec']['template']['spec']['containers'][0]['image'] != image:
            raise ValueError('Controller image differs from the Gateway installation lock')
    proxies = get('deployments', namespace='envoy-gateway-system', selector=PROXY_SELECTOR)['items']
    if len(proxies) != 1:
        raise ValueError('Expected one managed Envoy Proxy deployment')
    containers = proxies[0]['spec']['template']['spec']['containers']
    if not any(c['image'] == install['proxy_image'] for c in containers):
        raise ValueError('Proxy image differs from the Gateway installation lock')
    port = wait_for(lambda: select_nodeport(get('services', namespace='envoy-gateway-system',
                                                selector=PROXY_SELECTOR)['items']))
    note(f'PASS managed NodePort Service: {address}:{port}')
    uri = '/?gateway_probe=' + uuid.uuid4().hex
    request_id = wait_for(lambda: check_response(*request(address, port, host, uri)), 90)
    note('PASS HTTP 200, exact response, X-Gateway and X-App-Version headers')
    negative_host = 'unmatched-' + uuid.uuid4().hex + '.invalid'
    negative = request(address, port, negative_host, '/')
    if negative[0] != 404 or 'x-app-version' in negative[1] or b'Hello World!' in negative[2]:
        raise ValueError('Unmatched hostname was not rejected by Gateway with HTTP 404')
    note('PASS unmatched hostname rejected with HTTP 404')

    def log_ready():
        pods = get('pods', namespace='demo', selector='app.kubernetes.io/name=nginx')['items']
        text = '\n'.join(kubectl('-n', 'demo', 'logs', p['metadata']['name'], '-c', 'nginx',
                                 '--since=5m', '--tail=2000') for p in pods if not p['metadata'].get('deletionTimestamp'))
        check_backend_log(text, uri, request_id)
    wait_for(log_ready, 30)
    note(f'PASS backend access log correlated with Gateway request; request_id={request_id}')
    endpoint = {'node_address': address, 'node_port': port, 'hostname': host, 'url': f'http://{address}:{port}/'}
    (settings_dir / 'gateway-endpoint.json').write_text(json.dumps(endpoint, indent=2) + '\n')
    note('External check: curl --fail-with-body -i -H ' + shlex.quote('Host: ' + host)
         + ' ' + shlex.quote(endpoint['url']))
    note('PASS Gateway verification completed from the Ubuntu node. External client reachability remains a separate check.')


if __name__ == '__main__':
    try:
        if os.geteuid() != 0 or not Path('/etc/kubernetes/admin.conf').is_file():
            raise RuntimeError('Run with sudo on the initialized Ubuntu VM')
        with open('/run/lock/devops-foundation-gateway-verify.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.umask(0o077)
            directory = Path('/var/log/devops-foundation')
            directory.mkdir(mode=0o700, exist_ok=True)
            with (directory / 'verify-gateway.log').open('w') as report:
                report.write(time.strftime('Verification at %Y-%m-%dT%H:%M:%SZ\n', time.gmtime()))
                try:
                    verify(report)
                except Exception as error:
                    report.write(f'FAIL: {error}\n')
                    raise
    except (OSError, RuntimeError, ValueError, KeyError, http.client.HTTPException, subprocess.SubprocessError) as error:
        print(f'FAIL: {error}', file=sys.stderr)
        for resource in ['gateways', 'httproutes']:
            try:
                print(kubectl('-n', 'demo', 'get', resource, '-o', 'yaml'))
            except (RuntimeError, subprocess.SubprocessError, OSError):
                pass
        sys.exit(1)
