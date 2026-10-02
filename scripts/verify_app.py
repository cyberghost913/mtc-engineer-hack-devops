#!/usr/bin/env python3
"""Verify the real Service path and both application log streams on the VM."""
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

NAMESPACE = 'demo'
SELECTOR = 'app.kubernetes.io/name=nginx'


def kubectl(*args, data=None, timeout=40):
    result = subprocess.run(
        ['kubectl', '--kubeconfig=/etc/kubernetes/admin.conf', '--request-timeout=30s', *args],
        input=data, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'kubectl {args[0]} failed: {result.stderr.strip()}')
    return result.stdout


def check_deployment(deployment, pods, slices, image):
    status = deployment.get('status', {})
    if status.get('observedGeneration', 0) < deployment['metadata']['generation']:
        raise ValueError('Deployment controller has not observed the current generation')
    if deployment['spec']['replicas'] != 2 or any(status.get(k) != 2 for k in
            ('updatedReplicas', 'readyReplicas', 'availableReplicas')):
        raise ValueError('Expected two updated, ready and available replicas')
    ready_pods = []
    for pod in pods:
        if pod['metadata'].get('deletionTimestamp'):
            continue
        if not any(c['type'] == 'Ready' and c['status'] == 'True'
                   for c in pod.get('status', {}).get('conditions', [])):
            raise ValueError('An application Pod is not Ready')
        nginx = next(c for c in pod['spec']['containers'] if c['name'] == 'nginx')
        if nginx['image'] != image:
            raise ValueError('Application image differs from the pinned image')
        ready_pods.append(pod)
    if len(ready_pods) != 2:
        raise ValueError('Expected exactly two non-terminating application Pods')
    addresses = {addr for s in slices for endpoint in s.get('endpoints', [])
                 if endpoint.get('conditions', {}).get('ready') is True
                 for addr in endpoint.get('addresses', [])}
    if {p['status']['podIP'] for p in ready_pods} != addresses:
        raise ValueError('Service endpoints do not match both ready application Pods')
    return [p['metadata']['name'] for p in ready_pods]


def check_logs(text, request_id):
    records = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
            if isinstance(record, dict):
                records.append(record)
        except json.JSONDecodeError:
            pass  # Nginx error output is text, access output is JSON.
    for uri, status in [('/', 200), (f'/missing-{request_id}', 404)]:
        match = [r for r in records if r.get('request_id') == request_id
                 and r.get('uri') == uri and r.get('status') == status]
        if not match:
            raise ValueError(f'Missing JSON access record: {uri}, HTTP {status}')
        record = match[0]
        if record.get('method') != 'GET' or not record.get('pod'):
            raise ValueError('Access record lacks method or Pod identity')
        if not isinstance(record.get('request_time'), (float, int)):
            raise ValueError('Request duration is not numeric')
    if not any('[error]' in line and f'/missing-{request_id}' in line
               and 'failed' in line for line in text.splitlines()):
        raise ValueError('Missing error record for the deliberately absent file')


def client_pod(name, request_id, image):
    if not re.fullmatch(r'[a-f0-9]{32}', request_id):
        raise ValueError('The probe ID must be a generated UUID hex string')
    script = r'''
cd /tmp
printf 'Hello World!\n' > expected
wget -T 5 -S -O body --header='X-Request-ID: __ID__' http://nginx.demo.svc.cluster.local/ 2>headers
grep -Eq 'HTTP/[0-9.]+ 200([[:space:]]|$)' headers
grep -i 'X-Request-ID: __ID__' headers
grep -i 'X-App-Version: v1' headers
cmp expected body
if wget -T 5 -S -O missing --header='X-Request-ID: __ID__' http://nginx.demo.svc.cluster.local/missing-__ID__ 2>missing-headers; then
    echo 'Expected HTTP 404'; exit 1
fi
grep -Eq 'HTTP/[0-9.]+ 404([[:space:]]|$)' missing-headers
wget -T 5 -S -O body --header='X-Request-ID: invalid value' http://nginx.demo.svc.cluster.local/ 2>generated-headers
grep -Ei 'X-Request-ID: [a-f0-9]{32}[[:space:]]*$' generated-headers
cmp expected body
printf 'ok\n' > expected-health
wget -T 5 -qO health http://nginx.demo.svc.cluster.local/healthz
cmp expected-health health
printf 'ready\n' > expected-ready
wget -T 5 -qO ready http://nginx.demo.svc.cluster.local/readyz
cmp expected-ready ready
echo 'PASS HTTP 200, exact body, HTTP 404, request IDs and health endpoints'
'''.replace('__ID__', request_id)
    return {
        'apiVersion': 'v1', 'kind': 'Pod',
        'metadata': {'name': name, 'namespace': NAMESPACE, 'labels': {'app.kubernetes.io/name': 'nginx-probe'}},
        'spec': {
            'restartPolicy': 'Never', 'activeDeadlineSeconds': 120,
            'automountServiceAccountToken': False,
            'securityContext': {'runAsNonRoot': True, 'runAsUser': 1000, 'runAsGroup': 1000,
                                'fsGroup': 1000, 'seccompProfile': {'type': 'RuntimeDefault'}},
            'containers': [{
                'name': 'probe', 'image': image, 'command': ['sh', '-ec', script],
                'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                                    'capabilities': {'drop': ['ALL']}},
                'resources': {'requests': {'cpu': '10m', 'memory': '16Mi'},
                              'limits': {'cpu': '100m', 'memory': '64Mi'}},
                'volumeMounts': [{'name': 'tmp', 'mountPath': '/tmp'}]}],
            'volumes': [{'name': 'tmp', 'emptyDir': {'sizeLimit': '8Mi'}}]}}


def verify(probe_image, nginx_image, report):
    def note(message):
        print(message, flush=True)
        report.write(message + '\n')
        report.flush()

    request_id = uuid.uuid4().hex
    name = 'nginx-check-' + request_id[:12]
    created = False
    try:
        deployment = json.loads(kubectl('-n', NAMESPACE, 'get', 'deployment/nginx', '-o', 'json'))
        pods = json.loads(kubectl('-n', NAMESPACE, 'get', 'pods', '-l', SELECTOR, '-o', 'json'))['items']
        slices = json.loads(kubectl('-n', NAMESPACE, 'get', 'endpointslices',
                                   '-l', 'kubernetes.io/service-name=nginx', '-o', 'json'))['items']
        names = check_deployment(deployment, pods, slices, nginx_image)
        note('PASS two ready replicas, pinned image and Service endpoints')
        for pod in names:
            kubectl('-n', NAMESPACE, 'exec', pod, '-c', 'nginx', '--', 'nginx', '-t')
        note('PASS nginx -t in both application containers')
        created = True  # Cleanup also runs if create succeeds but its response is lost.
        kubectl('create', '-f', '-', data=json.dumps(client_pod(name, request_id, probe_image)))
        deadline = time.monotonic() + 150
        while True:
            pod = json.loads(kubectl('-n', NAMESPACE, 'get', 'pod', name, '-o', 'json'))
            phase = pod.get('status', {}).get('phase')
            if phase == 'Succeeded':
                break
            if phase == 'Failed' or time.monotonic() >= deadline:
                raise RuntimeError(f'Test Pod did not succeed: {phase}')
            time.sleep(2)
        note(kubectl('-n', NAMESPACE, 'logs', name).strip())
        last_error = None
        for attempt in range(15):
            logs = '\n'.join(kubectl('-n', NAMESPACE, 'logs', p, '-c', 'nginx',
                                     '--since=5m', '--tail=2000') for p in names)
            try:
                check_logs(logs, request_id)
                last_error = None
                break
            except ValueError as error:
                last_error = error
                time.sleep(1)
        if last_error:
            raise last_error
        note('PASS JSON access logs (200 and 404) and corresponding error log')
        note(f'PASS application verification completed; request_id={request_id}')
        note('Log collection through Fluentd/Filebeat is not tested at this stage.')
    except (ValueError, RuntimeError, subprocess.SubprocessError) as error:
        note(f'FAIL {error}')
        for args in [('get', 'pods', '-o', 'wide'), ('describe', 'pod', name), ('logs', name)]:
            try:
                note(kubectl('-n', NAMESPACE, *args))
            except (RuntimeError, subprocess.SubprocessError):
                pass
        raise
    finally:
        if created:
            kubectl('-n', NAMESPACE, 'delete', 'pod', name, '--ignore-not-found', '--wait=false')


if __name__ == '__main__':
    try:
        if os.geteuid() != 0 or not Path('/etc/kubernetes/admin.conf').is_file():
            raise RuntimeError('Run with sudo on the initialized Ubuntu VM')
        with open('/run/lock/devops-foundation-app-verify.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.umask(0o077)
            directory = Path('/var/log/devops-foundation')
            directory.mkdir(mode=0o700, exist_ok=True)
            with (directory / 'verify-app.log').open('w') as report:
                report.write(time.strftime('Verification at %Y-%m-%dT%H:%M:%SZ\n', time.gmtime()))
                verify(sys.argv[1], sys.argv[2], report)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f'FAIL: {error}', file=sys.stderr)
        sys.exit(1)
