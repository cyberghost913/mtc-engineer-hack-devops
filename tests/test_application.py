import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
from jinja2 import Environment, StrictUndefined
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('verify_app', ROOT / 'scripts/verify_app.py')
verify_app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify_app)


class ApplicationManifest(unittest.TestCase):
    def render(self, config_suffix=''):
        env = Environment(undefined=StrictUndefined)
        env.filters['to_json'] = json.dumps
        env.filters['hash'] = lambda text, algorithm: hashlib.new(algorithm, text.encode()).hexdigest()
        variables = yaml.safe_load((ROOT / 'versions.yml').read_text())
        variables['nginx_config'] = (ROOT / 'ansible/roles/application/files/nginx.conf').read_text() + config_suffix
        template = (ROOT / 'ansible/roles/application/templates/resources.yml.j2').read_text()
        return list(yaml.safe_load_all(env.from_string(template).render(variables)))

    def test_service_selects_both_replicas_and_named_port(self):
        config, deployment, service = self.render()
        self.assertEqual(deployment['spec']['replicas'], 2)
        pod = deployment['spec']['template']
        self.assertEqual(service['spec']['selector'], deployment['spec']['selector']['matchLabels'])
        self.assertTrue(service['spec']['selector'].items() <= pod['metadata']['labels'].items())
        self.assertEqual(service['spec']['ports'][0]['targetPort'], pod['spec']['containers'][0]['ports'][0]['name'])
        self.assertEqual(service['spec']['type'], 'ClusterIP')

    def test_config_change_triggers_rollout(self):
        initial = self.render()[1]['spec']['template']['metadata']['annotations']
        changed = self.render('\n# test configuration change\n')[1]['spec']['template']['metadata']['annotations']
        self.assertNotEqual(initial, changed)

    def test_configmap_preserves_nginx_configuration_and_checksum(self):
        config, deployment, _ = self.render()
        text = (ROOT / 'ansible/roles/application/files/nginx.conf').read_text()
        self.assertEqual(config['data']['nginx.conf'], text)
        checksum = deployment['spec']['template']['metadata']['annotations']['checksum/nginx-config']
        self.assertEqual(checksum, hashlib.sha256(text.encode()).hexdigest())

    def test_nonroot_readonly_container_has_writable_tmp(self):
        pod = self.render()[1]['spec']['template']['spec']
        container = pod['containers'][0]
        self.assertTrue(pod['securityContext']['runAsNonRoot'])
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertTrue(container['securityContext']['readOnlyRootFilesystem'])
        self.assertEqual(container['securityContext']['capabilities']['drop'], ['ALL'])
        self.assertIn('/tmp', [v['mountPath'] for v in container['volumeMounts']])

    def test_image_digests_match_registry_evidence(self):
        versions = yaml.safe_load((ROOT / 'versions.yml').read_text())
        evidence = json.loads((ROOT / 'docs/image-lock.json').read_text())
        self.assertEqual({versions['nginx_image'], versions['probe_image']}, {e['image'] for e in evidence})
        for entry in evidence:
            self.assertTrue({'amd64', 'arm64'} <= {p['architecture'] for p in entry['platforms']})


class LogVerification(unittest.TestCase):
    def setUp(self):
        self.request_id = 'a' * 32
        self.records = [dict(request_id=self.request_id, uri=uri, status=status,
                             method='GET', pod='nginx-a', request_time=0.001)
                        for uri, status in [('/', 200), ('/missing-' + self.request_id, 404)]]
        self.error = f'2026/10/01 [error] open() "/missing-{self.request_id}" failed (2: No such file)'

    def lines(self):
        return '\n'.join([json.dumps(r) for r in self.records] + [self.error])

    def test_accepts_correlated_access_and_error_logs(self):
        verify_app.check_logs(self.lines(), self.request_id)

    def test_rejects_stale_request_id(self):
        with self.assertRaisesRegex(ValueError, 'Missing JSON access'):
            verify_app.check_logs(self.lines(), 'b' * 32)

    def test_rejects_missing_error_stream(self):
        self.error = ''
        with self.assertRaisesRegex(ValueError, 'Missing error'):
            verify_app.check_logs(self.lines(), self.request_id)

    def test_rejects_string_status_code(self):
        self.records[0]['status'] = '200'
        with self.assertRaises(ValueError):
            verify_app.check_logs(self.lines(), self.request_id)

    def test_rejects_missing_latency(self):
        del self.records[0]['request_time']
        with self.assertRaisesRegex(ValueError, 'duration'):
            verify_app.check_logs(self.lines(), self.request_id)

    def test_probe_cannot_interpolate_arbitrary_input(self):
        with self.assertRaises(ValueError):
            verify_app.client_pod('probe', 'id; unexpected-command', 'image')


class DeploymentVerification(unittest.TestCase):
    def setUp(self):
        self.deployment = {'metadata': {'generation': 2}, 'spec': {'replicas': 2},
                           'status': {'observedGeneration': 2, 'updatedReplicas': 2,
                                      'readyReplicas': 2, 'availableReplicas': 2}}
        self.pods = [{'metadata': {'name': f'nginx-{i}'},
                      'spec': {'containers': [{'name': 'nginx', 'image': 'pinned'}]},
                      'status': {'podIP': f'10.244.0.{i}', 'conditions': [{'type': 'Ready', 'status': 'True'}]}}
                     for i in (1, 2)]
        self.slices = [{'endpoints': [{'addresses': [p['status']['podIP']], 'conditions': {'ready': True}}
                                     for p in self.pods]}]

    def test_healthy_deployment(self):
        self.assertEqual(verify_app.check_deployment(self.deployment, self.pods, self.slices, 'pinned'),
                         ['nginx-1', 'nginx-2'])

    def test_rejects_unobserved_rollout(self):
        self.deployment['status']['observedGeneration'] = 1
        with self.assertRaises(ValueError):
            verify_app.check_deployment(self.deployment, self.pods, self.slices, 'pinned')

    def test_rejects_incomplete_service_endpoints(self):
        self.slices[0]['endpoints'].pop()
        with self.assertRaisesRegex(ValueError, 'Service endpoints'):
            verify_app.check_deployment(self.deployment, self.pods, self.slices, 'pinned')

    def test_rejects_unpinned_image(self):
        self.pods[0]['spec']['containers'][0]['image'] = 'latest'
        with self.assertRaisesRegex(ValueError, 'image'):
            verify_app.check_deployment(self.deployment, self.pods, self.slices, 'pinned')
