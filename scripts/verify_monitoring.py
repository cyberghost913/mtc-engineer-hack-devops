#!/usr/bin/env python3
"""Verify real Prometheus discovery, fresh samples, rules and persistent storage."""
from collections import Counter
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlencode

EXPECTED_TARGETS = {'prometheus': 1, 'kube-state-metrics': 1, 'kubernetes-apiserver': 1,
                    'kubelet': 1, 'kubernetes-resources': 1, 'nginx': 2, 'envoy': 1}
EXPECTED_RULES = {'ScrapeTargetDown', 'MonitoringJobMissing', 'NginxBackendDown',
                  'NginxReplicaMetricsMissing', 'NodeNotReady', 'NginxReplicasUnavailable',
                  'ApplicationContainerRestarts'}
API = '/api/v1/namespaces/monitoring/services/http:prometheus:9090/proxy'


def kubectl(*args):
    result = subprocess.run(
        ['kubectl', '--kubeconfig=/etc/kubernetes/admin.conf', '--request-timeout=20s', *args],
        capture_output=True, text=True, timeout=30)
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


def api(path, **params):
    suffix = '?' + urlencode(params) if params else ''
    response = json.loads(kubectl('get', '--raw=' + API + path + suffix))
    if response.get('status') != 'success' or response.get('warnings'):
        raise ValueError(f'Prometheus API failed or returned warnings: {response}')
    return response['data']


def require_recent(timestamp, now, max_age=60):
    if isinstance(timestamp, str):
        timestamp = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).timestamp()
    if not math.isfinite(timestamp) or not -5 <= now - timestamp <= max_age:
        raise ValueError('Stale or future monitoring timestamp')


def check_targets(data, nginx_pods, now=None):
    now = time.time() if now is None else now
    targets = data.get('activeTargets', [])
    counts = Counter(t.get('labels', {}).get('job') for t in targets)
    if counts != EXPECTED_TARGETS:
        raise ValueError(f'Unexpected target inventory: {dict(counts)}; expected {EXPECTED_TARGETS}')
    for target in targets:
        if target.get('health') != 'up' or target.get('lastError'):
            raise ValueError(f'Unhealthy target: {target.get("labels")}: {target.get("lastError")}')
        require_recent(target.get('lastScrape', ''), now)
    scraped_pods = [t['labels'].get('pod') for t in targets if t['labels']['job'] == 'nginx']
    if len(set(scraped_pods)) != 2 or set(scraped_pods) != set(nginx_pods):
        raise ValueError('Scraped Nginx targets do not match both current application Pods')
    return dict(counts)


def check_vector(data, count, *, minimum=0, exact=None, pods=None, now=None):
    now = time.time() if now is None else now
    results = data.get('result', [])
    if data.get('resultType') != 'vector' or len(results) != count:
        raise ValueError(f'Expected {count} metric samples, got {data}')
    values = []
    for result in results:
        timestamp, raw = result['value']
        require_recent(timestamp, now)
        value = float(raw)
        if not math.isfinite(value) or value < minimum or (exact is not None and value != exact):
            raise ValueError(f'Unexpected metric value: {raw}')
        values.append(value)
    if pods is not None and {r['metric'].get('pod') for r in results} != set(pods):
        raise ValueError('Metric samples do not belong to both current application Pods')
    return values


def check_rules(data, now=None):
    now = time.time() if now is None else now
    rules = [r for group in data.get('groups', []) for r in group.get('rules', [])]
    if len(rules) != len(EXPECTED_RULES) or {r.get('name') for r in rules} != EXPECTED_RULES:
        raise ValueError('Expected alert rules are missing or duplicated')
    for rule in rules:
        if rule.get('health') != 'ok' or rule.get('lastError'):
            raise ValueError(f'Rule evaluation failed: {rule.get("name")}')
        require_recent(rule.get('lastEvaluation', ''), now)


def check_storage(pvc, pv):
    if pvc.get('status', {}).get('phase') != 'Bound' or pv.get('status', {}).get('phase') != 'Bound':
        raise ValueError('Prometheus persistent storage is not Bound')
    if pvc['spec'].get('volumeName') != 'devops-prometheus':
        raise ValueError('PVC uses an unexpected volume')
    spec = pv['spec']
    if spec.get('persistentVolumeReclaimPolicy') != 'Retain':
        raise ValueError('Prometheus volume must retain its data')
    if spec.get('local', {}).get('path') != '/var/lib/devops-foundation/prometheus':
        raise ValueError('Unexpected Prometheus storage path')
    claim = spec.get('claimRef', {})
    if (claim.get('namespace'), claim.get('name'), claim.get('uid')) != (
            'monitoring', 'prometheus-data', pvc['metadata']['uid']):
        raise ValueError('Persistent volume is bound to a different claim')


def check_deployment(deployment, image):
    status = deployment.get('status', {})
    if (status.get('observedGeneration', 0) < deployment['metadata']['generation']
            or deployment['spec']['replicas'] != 1
            or any(status.get(key) != 1 for key in ('updatedReplicas', 'readyReplicas', 'availableReplicas'))):
        raise ValueError('Monitoring deployment is not ready at the current generation')
    if deployment['spec']['template']['spec']['containers'][0]['image'] != image:
        raise ValueError('Monitoring image differs from the installation lock')


def wait_for(check, seconds=240):
    deadline = time.monotonic() + seconds
    while True:
        try:
            return check()
        except (ValueError, KeyError, RuntimeError, OSError, subprocess.SubprocessError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(5)


def verify(report):
    def note(message):
        print(message, flush=True)
        report.write(message + '\n')
        report.flush()

    install = json.loads(Path('/etc/devops-foundation/monitoring-lock.json').read_text())
    for name, key in [('prometheus', 'prometheus_image'), ('kube-state-metrics', 'kube_state_metrics_image')]:
        wait_for(lambda: check_deployment(get('deployment', name, 'monitoring'), install[key]))
    check_storage(get('pvc', 'prometheus-data', 'monitoring'), get('pv', 'devops-prometheus'))
    note('PASS monitoring deployments, pinned images and Bound persistent storage with Retain policy')

    pods = get('pods', namespace='demo', selector='app.kubernetes.io/name=nginx')['items']
    pods = [p for p in pods if not p['metadata'].get('deletionTimestamp')]
    if len(pods) != 2:
        raise ValueError('Expected two current application Pods')
    for pod in pods:
        exporters = [c for c in pod['spec']['containers'] if c['name'] == 'nginx-exporter']
        if len(exporters) != 1 or exporters[0]['image'] != install['nginx_exporter_image']:
            raise ValueError('Nginx exporter missing or image differs from the installation lock')
    names = {p['metadata']['name'] for p in pods}
    inventory = wait_for(lambda: check_targets(api('/api/v1/targets', state='active'), names))
    note('PASS fresh, healthy targets: ' + json.dumps(inventory, sort_keys=True))

    # PromQL instant-vector timestamps are query evaluation times. Freshness is
    # verified separately with timestamp(metric), not just vector sample times.
    queries = [
        ('nginx_up{job="nginx"}', 2, {'exact': 1, 'pods': names}),
        ('nginx_http_requests_total{job="nginx"}', 2, {'pods': names}),
        ('kube_node_status_condition{job="kube-state-metrics",condition="Ready",status="true"}', 1, {'exact': 1}),
        ('kube_deployment_status_replicas_available{job="kube-state-metrics",namespace="demo",deployment="nginx"}',
         1, {'exact': 2}),
        ('node_cpu_usage_seconds_total{job="kubernetes-resources"}', 1, {}),
        ('container_memory_working_set_bytes{job="kubernetes-resources",namespace="demo",container="nginx"}',
         2, {'minimum': 1, 'pods': names}),
        ('envoy_server_live{job="envoy"}', 1, {'exact': 1}),
    ]
    for query, count, options in queries:
        def metric_ready():
            values = check_vector(api('/api/v1/query', query=query), count, **options)
            timestamps = check_vector(api('/api/v1/query', query=f'timestamp({query})'), count)
            for timestamp in timestamps:
                require_recent(timestamp, time.time())
            return values
        values = wait_for(metric_ready)
        note(f'PASS metric {query}: {values}')
    wait_for(lambda: check_rules(api('/api/v1/rules')))
    note('PASS all seven alert rules loaded and evaluated without errors')
    note('Open UI on VM: sudo kubectl --kubeconfig=/etc/kubernetes/admin.conf -n monitoring '
         'port-forward --address=127.0.0.1 service/prometheus 9090:9090')
    note('PASS monitoring verification completed. Persistence after restart and alert delivery are separate checks.')


if __name__ == '__main__':
    try:
        if os.geteuid() != 0 or not Path('/etc/kubernetes/admin.conf').is_file():
            raise RuntimeError('Run with sudo on the initialized Ubuntu VM')
        with open('/run/lock/devops-foundation-monitoring-verify.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.umask(0o077)
            directory = Path('/var/log/devops-foundation')
            directory.mkdir(mode=0o700, exist_ok=True)
            with (directory / 'verify-monitoring.log').open('w') as report:
                report.write(datetime.now(timezone.utc).isoformat() + '\n')
                try:
                    verify(report)
                except Exception as error:
                    report.write(f'FAIL: {error}\n')
                    raise
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f'FAIL: {error}', file=sys.stderr)
        try:
            print(kubectl('-n', 'monitoring', 'get', 'pods,pvc,deployments', '-o', 'wide'))
        except (OSError, RuntimeError, subprocess.SubprocessError):
            pass
        sys.exit(1)
