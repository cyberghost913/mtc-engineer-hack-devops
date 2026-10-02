import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from jinja2 import Environment, StrictUndefined
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / 'ansible/roles/logging'


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f'scripts/{name}.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


verify = module('verify_logging')
cleanup = module('cleanup_logs')


class DeliveredLogs(unittest.TestCase):
    def setUp(self):
        self.pods = {'nginx-a': 'uid-a', 'nginx-b': 'uid-b'}
        self.probes = {200: {'uri': '/?logging_probe=fresh', 'request_id': 'response-id-200'},
                       404: {'uri': '/missing-fresh', 'request_id': 'response-id-404'}}
        self.records = []
        for status, probe in self.probes.items():
            pod = 'nginx-a' if status == 200 else 'nginx-b'
            record = dict(namespace='demo', container='nginx', pod=pod, pod_uid=self.pods[pod],
                          source_path=f'/var/log/pods/demo_{pod}_{self.pods[pod]}/nginx/0.log',
                          collector='fluentd', node='node-a', stream='stdout', cri_flag='F')
            record['app'] = dict(probe, status=status, method='GET', pod=pod, request_time=0.001)
            self.records.append(record)
        error = copy.deepcopy(self.records[1])
        error.pop('app')
        error.update(stream='stderr', message='[error] open() "/usr/share/nginx/html/missing-fresh" failed (2: No such file)')
        self.records.append(error)

    def check(self):
        return verify.check_records(self.records, self.probes, self.pods, 'node-a')

    def test_accepts_correlated_access_and_error_from_current_pods(self):
        self.assertEqual(set(self.check()), {'access_200', 'access_404', 'error_404'})

    def test_rejects_old_uri_despite_matching_request_id(self):
        self.records[0]['app']['uri'] = '/?logging_probe=old'
        with self.assertRaisesRegex(ValueError, 'fresh HTTP 200'):
            self.check()

    def test_requires_response_request_id_even_when_uri_matches(self):
        self.records[0]['app']['request_id'] = 'original-client-id'
        with self.assertRaises(ValueError):
            self.check()

    def test_rejects_old_pod_uid_and_foreign_source(self):
        for key, bad in [('pod_uid', 'replaced-pod'), ('namespace', 'other'), ('container', 'nginx-exporter'),
                         ('source_path', '/var/log/pods/other_pod_uid/nginx/0.log'), ('node', 'other-node')]:
            with self.subTest(key=key):
                original = self.records[0][key]
                self.records[0][key] = bad
                with self.assertRaises(ValueError):
                    self.check()
                self.records[0][key] = original

    def test_unparsed_access_or_partial_fragment_cannot_pass(self):
        self.records[0]['cri_flag'] = 'P'
        with self.assertRaises(ValueError):
            self.check()
        self.records[0]['cri_flag'] = 'F'
        self.records[0].pop('app')
        with self.assertRaises(ValueError):
            self.check()

    def test_error_must_match_the_404_pod_and_unique_uri(self):
        self.records[2]['message'] = '[error] unrelated request failed'
        with self.assertRaisesRegex(ValueError, 'stderr'):
            self.check()

    def test_stdout_alone_does_not_prove_error_delivery(self):
        self.records[2]['stream'] = 'stdout'
        with self.assertRaisesRegex(ValueError, 'stderr'):
            self.check()

    def test_rejects_bad_latency_and_string_status(self):
        for bad in [True, -1, float('nan'), '0.01']:
            self.records[0]['app']['request_time'] = bad
            with self.assertRaisesRegex(ValueError, 'request_time'):
                self.check()
        self.records[0]['app']['request_time'] = 0
        self.records[0]['app']['status'] = '200'
        with self.assertRaises(ValueError):
            self.check()

    def test_http_response_must_traverse_gateway_and_match_body(self):
        headers = {'x-gateway': 'envoy-gateway', 'x-app-version': 'v1', 'x-request-id': 'envoy-generated'}
        self.assertEqual(verify.check_response((200, headers, b'Hello World!\n'), 200), 'envoy-generated')
        with self.assertRaises(ValueError):
            verify.check_response((200, headers, b'wrong'), 200)
        del headers['x-gateway']
        with self.assertRaisesRegex(ValueError, 'Gateway'):
            verify.check_response((404, headers, b'not found'), 404)

    def test_reader_uses_only_finalized_archives_and_ignores_partial_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            good = json.dumps(self.records[0]).encode() + b'\n'
            (root / 'nginx.202610021030.jsonl').write_bytes(good + b'{"partial":')
            (root / 'buffer.b123.log').write_bytes(good)
            (root / 'nginx.pos').write_bytes(good)
            (root / 'nginx.202610021031.jsonl').symlink_to(root / 'nginx.202610021030.jsonl')
            self.assertEqual(verify.read_archive(root), [self.records[0]])

    def test_reader_bounds_bytes_and_does_not_accept_truncated_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / 'nginx.202610021030.jsonl'
            path.write_text(json.dumps({'padding': 'x' * 500}) + '\n{"tail":true}\n')
            self.assertEqual(verify.read_archive(path.parent, max_bytes=100), [{'tail': True}])

    def test_collector_requires_updated_generation_and_pinned_image(self):
        ds = {'metadata': {'generation': 2}, 'spec': {'template': {'spec': {'containers': [{'image': 'pinned'}]}}},
              'status': dict(observedGeneration=2, desiredNumberScheduled=1, currentNumberScheduled=1,
                             updatedNumberScheduled=1, numberReady=1, numberAvailable=1)}
        verify.check_collector(ds, 'pinned')
        with self.assertRaisesRegex(ValueError, 'image'):
            verify.check_collector(ds, 'other')
        ds['status']['observedGeneration'] = 1
        with self.assertRaisesRegex(ValueError, 'stale'):
            verify.check_collector(ds, 'pinned')


class ArchiveRetention(unittest.TestCase):
    def test_only_old_regular_finalized_files_are_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            now = 2000000000
            for name in ['nginx.202610010000.jsonl', 'nginx.202610020000.jsonl', 'nginx.pos', 'buffer.b1.log', 'foreign.txt']:
                p = root / name
                p.write_text('data')
                os.utime(p, (now - 400000, now - 400000))
            os.utime(root / 'nginx.202610020000.jsonl', (now, now))
            (root / 'nginx.202609010000.jsonl').symlink_to(root / 'foreign.txt')
            (root / 'nginx.202609020000.jsonl').mkdir()
            selected = cleanup.expired_files(root, now)
            self.assertEqual([p.name for p in selected], ['nginx.202610010000.jsonl'])

    def test_refuses_symlinked_archive_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / 'real').mkdir()
            (root / 'link').symlink_to(root / 'real', target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'symbolic'):
                cleanup.expired_files(root / 'link', 2000000000)

    def test_dry_run_does_not_delete_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / 'nginx.202601010000.jsonl'
            path.write_text('archive')
            os.utime(path, (0, 0))
            with patch.object(cleanup, 'ARCHIVE', Path(tmp).resolve()), patch('sys.argv', ['cleanup_logs.py', '--dry-run']), patch('builtins.print'):
                cleanup.main()
            self.assertTrue(path.exists())


class LoggingManifest(unittest.TestCase):
    def setUp(self):
        self.env = Environment(undefined=StrictUndefined)
        self.env.filters['to_json'] = json.dumps
        self.env.filters['hash'] = lambda value, algorithm: hashlib.new(algorithm, value.encode()).hexdigest()
        self.values = yaml.safe_load((ROOT / 'versions.yml').read_text())
        self.values.update(fluentd_config=(ROLE / 'files/fluent.conf').read_text(),
                           fluentd_health=(ROLE / 'files/health.rb').read_text(), logging_node_hostname='actual-node')

    def render(self):
        return list(yaml.safe_load_all(self.env.from_string((ROLE / 'templates/resources.yml.j2').read_text())
                                       .render(self.values)))

    def test_source_logs_are_read_only_and_state_persists_on_node(self):
        _, ds = self.render()
        pod = ds['spec']['template']['spec']
        mounts = {m['name']: m for m in pod['containers'][0]['volumeMounts']}
        volumes = {v['name']: v for v in pod['volumes']}
        self.assertTrue(mounts['pod-logs']['readOnly'])
        self.assertEqual(volumes['pod-logs']['hostPath']['path'], '/var/log/pods')
        self.assertEqual(volumes['data']['hostPath']['path'], '/var/lib/devops-foundation/fluentd')
        self.assertEqual(volumes['data']['hostPath']['type'], 'Directory')
        self.assertNotIn('hostNetwork', pod)
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertFalse(pod['containers'][0]['securityContext']['privileged'])

    def test_one_writer_and_dry_run_use_same_image_and_config(self):
        _, ds = self.render()
        self.assertEqual(ds['spec']['updateStrategy']['rollingUpdate'], {'maxUnavailable': 1, 'maxSurge': 0})
        pod = ds['spec']['template']['spec']
        self.assertEqual(pod['nodeSelector']['kubernetes.io/hostname'], 'actual-node')
        self.assertEqual(pod['containers'][0]['image'], pod['initContainers'][0]['image'])
        self.assertIn('--dry-run', pod['initContainers'][0]['args'])
        self.assertEqual(pod['containers'][0]['args'][-1], pod['initContainers'][0]['args'][-1])

    def test_configmap_is_exact_and_checksum_updates_on_change(self):
        cm, ds = self.render()
        self.assertEqual(cm['data']['fluent.conf'], self.values['fluentd_config'])
        annotations = ds['spec']['template']['metadata']['annotations']
        self.assertEqual(annotations['checksum/fluentd-config'], hashlib.sha256(cm['data']['fluent.conf'].encode()).hexdigest())
        self.values['fluentd_config'] += '\n# updated\n'
        self.assertNotEqual(self.render()[1]['spec']['template']['metadata']['annotations'], annotations)

    def test_registry_evidence_matches_pinned_multiarch_image(self):
        entry = json.loads((ROOT / 'docs/logging-image-lock.json').read_text())[0]
        self.assertEqual(self.values['fluentd_image'], entry['image'])
        self.assertRegex(entry['image'], r'@sha256:[0-9a-f]{64}$')
        self.assertTrue({'amd64', 'arm64'} <= {p['architecture'] for p in entry['platforms']})
