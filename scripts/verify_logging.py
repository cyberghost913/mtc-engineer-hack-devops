#!/usr/bin/env python3
"""Send unique Gateway requests and find their access/error records in Fluentd output."""
import fcntl
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

ARCHIVE = Path('/var/lib/devops-foundation/fluentd/archive')
FILE_NAME = re.compile(r'nginx\.\d{12}\.jsonl')
PROXY_SELECTOR = ('gateway.envoyproxy.io/owning-gateway-namespace=demo,'
                  'gateway.envoyproxy.io/owning-gateway-name=demo-gateway')


def kubectl(*args):
    result = subprocess.run(
        ['kubectl', '--kubeconfig=/etc/kubernetes/admin.conf', '--request-timeout=20s', *args],
        text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout


def get(kind, name=None, namespace=None, selector=None):
    args = ['get', kind]
    if name:
        args.append(name)
    if namespace:
        args += ['-n', namespace]
    if selector:
        args += ['-l', selector]
    return json.loads(kubectl(*args, '-o', 'json'))


def check_collector(daemonset, image):
    status = daemonset.get('status', {})
    if status.get('observedGeneration', 0) < daemonset['metadata']['generation']:
        raise ValueError('Collector DaemonSet status is stale')
    for key in ('desiredNumberScheduled', 'currentNumberScheduled', 'updatedNumberScheduled',
                'numberReady', 'numberAvailable'):
        if status.get(key) != 1:
            raise ValueError('Expected one updated, ready collector on the single node')
    if status.get('numberMisscheduled', 0):
        raise ValueError('Collector is scheduled on an unexpected node')
    if daemonset['spec']['template']['spec']['containers'][0]['image'] != image:
        raise ValueError('Fluentd image differs from the installation lock')


def request(address, port, hostname, path):
    connection = http.client.HTTPConnection(address, port, timeout=5)
    try:
        connection.request('GET', path, headers={'Host': hostname, 'Connection': 'close'})
        response = connection.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read(65536)
    finally:
        connection.close()


def check_response(response, expected_status):
    status, headers, body = response
    if status != expected_status or (status == 200 and body != b'Hello World!\n'):
        raise ValueError(f'Unexpected probe response: HTTP {status}')
    if headers.get('x-gateway') != 'envoy-gateway' or headers.get('x-app-version') != 'v1':
        raise ValueError('Probe did not traverse the configured Gateway and Nginx')
    request_id = headers.get('x-request-id', '')
    if not re.fullmatch(r'[A-Za-z0-9._:-]{1,64}', request_id):
        raise ValueError('Response has no valid request ID')
    return request_id


def read_archive(directory, max_files=16, max_bytes=2 * 1024 * 1024):
    """Read bounded tails of recent output files; ignore partial writes and symlinks."""
    directory = Path(directory)
    if any(p.is_symlink() for p in [directory, *directory.parents]):
        raise ValueError('Archive path contains a symbolic link')
    files = [p for p in directory.glob('nginx.*.jsonl')
             if FILE_NAME.fullmatch(p.name) and not p.is_symlink() and p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    records = []
    for path in files[:max_files]:
        with path.open('rb') as stream:
            size = os.fstat(stream.fileno()).st_size
            if size > max_bytes:
                stream.seek(size - max_bytes)
                stream.readline()  # Discard an incomplete first record.
            raw = stream.read(max_bytes)
        for line in raw.splitlines(keepends=True):
            if not line.endswith(b'\n'):
                continue  # A concurrent flush may not have completed the last line.
            try:
                record = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def source_matches(record, pods, node_name):
    pod = record.get('pod')
    uid = pods.get(pod)
    if not uid or any(record.get(k) != v for k, v in {
            'namespace': 'demo', 'container': 'nginx', 'pod_uid': uid,
            'node': node_name, 'collector': 'fluentd'}.items()):
        return False
    prefix = f'/var/log/pods/demo_{pod}_{uid}/nginx/'
    source = record.get('source_path', '')
    return isinstance(source, str) and source.startswith(prefix) and '/' not in source[len(prefix):]


def check_records(records, probes, pods, node_name):
    matched = {}
    for status, probe in probes.items():
        for record in records:
            app = record.get('app')
            if not source_matches(record, pods, node_name) or not isinstance(app, dict):
                continue
            if record.get('stream') != 'stdout' or record.get('cri_flag') != 'F':
                continue
            if not all(app.get(k) == v for k, v in {'uri': probe['uri'], 'request_id': probe['request_id'],
                                                  'status': status, 'method': 'GET', 'pod': record['pod']}.items()):
                continue
            duration = app.get('request_time')
            if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not math.isfinite(duration) or duration < 0:
                raise ValueError('Collected access record has invalid request_time')
            matched[status] = record
            break
        if status not in matched:
            raise ValueError(f'No collected access record for the fresh HTTP {status} probe')
    error_probe = probes[404]
    for record in records:
        if (not source_matches(record, pods, node_name) or record.get('stream') != 'stderr'
                or record.get('pod') != matched[404]['pod']):
            continue
        message = record.get('message', '')
        if isinstance(message, str) and all(text in message for text in
                ('[error]', 'failed', error_probe['uri'])):
            return {'access_200': matched[200], 'access_404': matched[404], 'error_404': record}
    raise ValueError('No collected stderr error corresponding to the fresh 404 probe')


def wait_for(check, seconds=180):
    deadline = time.monotonic() + seconds
    while True:
        try:
            return check()
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError, http.client.HTTPException):
            if time.monotonic() >= deadline:
                raise
            time.sleep(3)


def verify(report):
    def note(message):
        print(message, flush=True)
        report.write(message + '\n')
        report.flush()

    settings = Path('/etc/devops-foundation')
    install = json.loads((settings / 'logging-lock.json').read_text())
    foundation = json.loads((settings / 'lock.json').read_text())
    gateway = json.loads((settings / 'gateway-settings.json').read_text())
    address = str(ipaddress.IPv4Address(foundation['cluster_address']))
    hostname = gateway['hostname']
    wait_for(lambda: check_collector(get('daemonset', 'fluentd', 'logging'), install['fluentd_image']))
    collector_pods = get('pods', namespace='logging', selector='app.kubernetes.io/name=fluentd')['items']
    collector_pods = [p for p in collector_pods if not p['metadata'].get('deletionTimestamp')]
    if len(collector_pods) != 1 or collector_pods[0]['spec']['nodeName'] != install['node_name']:
        raise ValueError('Collector is not on the locked node')
    init_status = collector_pods[0].get('status', {}).get('initContainerStatuses', [])
    if not any(s['name'] == 'check-config' and s.get('state', {}).get('terminated', {}).get('exitCode') == 0
               for s in init_status):
        raise ValueError('Fluentd dry-run has not completed successfully')
    note('PASS one ready collector, pinned image, node identity and Fluentd dry-run')

    services = get('services', namespace='envoy-gateway-system', selector=PROXY_SELECTOR)['items']
    if len(services) != 1 or services[0]['spec']['type'] != 'NodePort':
        raise ValueError('Expected one NodePort Service for demo-gateway')
    ports = [p['nodePort'] for p in services[0]['spec']['ports'] if p['port'] == 80]
    if len(ports) != 1 or not 30000 <= ports[0] <= 32767:
        raise ValueError('Gateway HTTP NodePort is invalid')
    token = uuid.uuid4().hex
    probes = {200: {'uri': '/?logging_probe=' + token}, 404: {'uri': '/missing-' + token}}
    pods = get('pods', namespace='demo', selector='app.kubernetes.io/name=nginx')['items']
    identities = {p['metadata']['name']: p['metadata']['uid'] for p in pods if not p['metadata'].get('deletionTimestamp')}
    if len(identities) != 2:
        raise ValueError('Expected two current Nginx Pods before sending probes')
    for status, probe in probes.items():
        probe['request_id'] = check_response(request(address, ports[0], hostname, probe['uri']), status)
    note('PASS fresh Gateway requests: HTTP 200 with exact body and intentional HTTP 404')
    records = wait_for(lambda: check_records(read_archive(ARCHIVE), probes, identities, install['node_name']))
    for kind, record in records.items():
        note(f'PASS collected {kind}: ' + json.dumps(record, ensure_ascii=False, sort_keys=True))
    position = Path(install['storage_path']) / 'positions/nginx.pos'
    if not position.is_file() or position.stat().st_size == 0:
        raise ValueError('Persistent tail positions are missing')
    for command in [['systemctl', 'is-active', '--quiet', 'devops-log-retention.timer'],
                    ['systemctl', 'is-enabled', '--quiet', 'devops-log-retention.timer']]:
        subprocess.run(command, check=True, timeout=10)
    note('PASS persistent tail positions and enabled/active retention timer')
    note('PASS logging verification completed: fresh access/error records found in Fluentd archive, not kubectl logs')


if __name__ == '__main__':
    try:
        if os.geteuid() != 0 or not Path('/etc/kubernetes/admin.conf').is_file():
            raise RuntimeError('Run with sudo on the initialized Ubuntu VM')
        with open('/run/lock/devops-foundation-logging-verify.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.umask(0o077)
            directory = Path('/var/log/devops-foundation')
            directory.mkdir(mode=0o700, exist_ok=True)
            with (directory / 'verify-logging.log').open('w') as report:
                report.write(time.strftime('Verification at %Y-%m-%dT%H:%M:%SZ\n', time.gmtime()))
                try:
                    verify(report)
                except Exception as error:
                    report.write(f'FAIL: {error}\n')
                    raise
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError, http.client.HTTPException) as error:
        print(f'FAIL: {error}', file=sys.stderr)
        try:
            print(kubectl('-n', 'logging', 'get', 'pods,daemonsets', '-o', 'wide'))
        except (OSError, RuntimeError, subprocess.SubprocessError):
            pass
        sys.exit(1)
