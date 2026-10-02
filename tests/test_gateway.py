import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from jinja2 import Environment, StrictUndefined
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verify = load('verify_gateway')
prepare = load('prepare_gateway')


def conditions(*names, generation=2):
    return [{'type': name, 'status': 'True', 'observedGeneration': generation} for name in names]


class RoutingChecks(unittest.TestCase):
    def setUp(self):
        self.gc = {'metadata': {'generation': 2}, 'spec': {'controllerName': verify.CONTROLLER},
                   'status': {'conditions': conditions('Accepted')}}
        self.gateway = {'metadata': {'generation': 2}, 'status': {
            'conditions': conditions('Accepted', 'Programmed'), 'listeners': [{
                'name': 'http', 'attachedRoutes': 1,
                'conditions': conditions('Accepted', 'ResolvedRefs', 'Programmed')}]}}
        self.route = {'metadata': {'generation': 2, 'namespace': 'demo'}, 'status': {'parents': [{
            'controllerName': verify.CONTROLLER,
            'parentRef': {'name': 'demo-gateway', 'sectionName': 'http'},
            'conditions': conditions('Accepted', 'ResolvedRefs')}]}}

    def check(self):
        verify.check_routing(self.gc, self.gateway, self.route)

    def test_ready_route(self):
        self.check()

    def test_rejects_old_generation(self):
        self.gateway['metadata']['generation'] = 3
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.check()

    def test_rejects_unresolved_backend(self):
        self.route['status']['parents'][0]['conditions'][1]['status'] = 'False'
        with self.assertRaisesRegex(ValueError, 'ResolvedRefs'):
            self.check()

    def test_rejects_other_controller(self):
        self.gc['spec']['controllerName'] = 'other/controller'
        with self.assertRaisesRegex(ValueError, 'controller'):
            self.check()

    def test_rejects_wrong_parent_namespace(self):
        self.route['status']['parents'][0]['parentRef']['namespace'] = 'other'
        with self.assertRaisesRegex(ValueError, 'not attached'):
            self.check()

    def test_rejects_empty_listener(self):
        self.gateway['status']['listeners'][0]['attachedRoutes'] = 0
        with self.assertRaisesRegex(ValueError, 'no attached route'):
            self.check()

    def test_rejects_missing_condition(self):
        self.gc['status']['conditions'] = []
        with self.assertRaises(ValueError):
            self.check()


class TrafficChecks(unittest.TestCase):
    def test_selects_allocated_nodeport(self):
        services = [{'spec': {'type': 'NodePort', 'ports': [{'port': 80, 'nodePort': 31234}]}}]
        self.assertEqual(verify.select_nodeport(services), 31234)

    def test_rejects_loadbalancer(self):
        with self.assertRaises(ValueError):
            verify.select_nodeport([{'spec': {'type': 'LoadBalancer'}}])

    def test_rejects_ambiguous_services(self):
        with self.assertRaises(ValueError):
            verify.select_nodeport([{}, {}])

    def test_gateway_response(self):
        headers = {'x-gateway': 'envoy-gateway', 'x-app-version': 'v1', 'x-request-id': 'generated-by-envoy'}
        self.assertEqual(verify.check_response(200, headers, b'Hello World!\n'), 'generated-by-envoy')
        for status, body in [(302, b'Hello World!\n'), (200, b'Hello World!'), (503, b'error')]:
            with self.assertRaises(ValueError):
                verify.check_response(status, headers, body)

    def test_rejects_direct_backend_response(self):
        with self.assertRaisesRegex(ValueError, 'header'):
            verify.check_response(200, {'x-app-version': 'v1', 'x-request-id': 'id'}, b'Hello World!\n')

    def test_log_correlation_requires_uri_and_response_id(self):
        uri = '/?gateway_probe=unique'
        log = json.dumps({'uri': uri, 'request_id': 'response-id', 'status': 200, 'method': 'GET'})
        verify.check_backend_log(log, uri, 'response-id')
        for expected_uri, expected_id in [(uri, 'original-client-id'), ('/', 'response-id')]:
            with self.assertRaises(ValueError):
                verify.check_backend_log(log, expected_uri, expected_id)

    def test_http_request_uses_nodeport_and_host_and_closes_connection(self):
        with patch.object(verify.http.client, 'HTTPConnection') as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.getheaders.return_value = [('X-Gateway', 'envoy-gateway')]
            response.read.return_value = b'Hello World!\n'
            status, headers, _ = verify.request('192.0.2.5', 31234, 'demo.local', '/?probe=1')
            connection.assert_called_once_with('192.0.2.5', 31234, timeout=5)
            connection.return_value.request.assert_called_once_with(
                'GET', '/?probe=1', headers={'Host': 'demo.local', 'Connection': 'close'})
            connection.return_value.close.assert_called_once()
            self.assertEqual(status, 200)
            self.assertEqual(headers['x-gateway'], 'envoy-gateway')


class GatewayBundle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.versions = yaml.safe_load((ROOT / 'versions.yml').read_text())
        cls.original = list(yaml.load_all((ROOT / 'vendor/envoy-gateway-v1.9.2.yaml').read_text(),
                                          Loader=getattr(yaml, 'CSafeLoader', yaml.SafeLoader)))
        cls.groups = prepare.prepare(cls.original, cls.versions['envoy_gateway_image'], 'v1.9.2')

    def test_preserves_upstream_resource_set(self):
        identity = lambda d: (d['kind'], d['metadata'].get('namespace'), d['metadata']['name'])
        self.assertEqual({identity(d) for d in self.original if d},
                         {identity(d) for docs in self.groups.values() for d in docs})

    def test_preserves_completed_certificate_job_on_repeat(self):
        jobs = self.groups['certgen']
        self.assertEqual(len(jobs), 1)
        self.assertNotIn('ttlSecondsAfterFinished', jobs[0]['spec'])
        self.assertEqual(jobs[0]['spec']['template']['spec']['containers'][0]['image'],
                         self.versions['envoy_gateway_image'])
        original = next(d for d in self.original if d and d['kind'] == 'Job')
        self.assertEqual(original['spec']['ttlSecondsAfterFinished'], 30)

    def test_shutdown_manager_and_controller_use_same_digest(self):
        config = next(d for d in self.groups['controller'] if d['kind'] == 'ConfigMap')
        settings = yaml.safe_load(config['data']['envoy-gateway.yaml'])
        self.assertEqual(settings['provider']['kubernetes']['shutdownManager']['image'],
                         self.versions['envoy_gateway_image'])
        deployment = next(d for d in self.groups['controller'] if d['kind'] == 'Deployment')
        self.assertEqual(deployment['spec']['template']['spec']['containers'][0]['image'],
                         self.versions['envoy_gateway_image'])

    def test_webhook_does_not_overwrite_generated_ca(self):
        webhook = next(d for d in self.groups['controller'] if d['kind'] == 'MutatingWebhookConfiguration')
        for entry in webhook['webhooks']:
            self.assertNotIn('caBundle', entry['clientConfig'])

    def test_upstream_gateway_api_version_matches_lock(self):
        crd = next(d for d in self.groups['crds'] if d['metadata']['name'] == 'gateways.gateway.networking.k8s.io')
        self.assertEqual(crd['metadata']['annotations']['gateway.networking.k8s.io/bundle-version'],
                         self.versions['gateway_api_version'])

    def test_route_template_links_proxy_gateway_and_backend(self):
        env = Environment(undefined=StrictUndefined)
        env.filters['to_json'] = json.dumps
        source = (ROOT / 'ansible/roles/gateway/templates/resources.yml.j2').read_text()
        docs = list(yaml.safe_load_all(env.from_string(source).render(self.versions, gateway_hostname='demo.local')))
        proxy, gateway_class, gateway, route = docs
        self.assertEqual(proxy['spec']['provider']['kubernetes']['envoyService']['type'], 'NodePort')
        self.assertEqual(gateway['spec']['infrastructure']['parametersRef']['name'], proxy['metadata']['name'])
        self.assertEqual(gateway['spec']['gatewayClassName'], gateway_class['metadata']['name'])
        self.assertEqual(route['spec']['parentRefs'][0]['name'], gateway['metadata']['name'])
        self.assertEqual(route['spec']['hostnames'], ['demo.local'])
        self.assertEqual(route['spec']['rules'][0]['backendRefs'], [{'name': 'nginx', 'port': 80}])

    def test_renderer_is_deterministic(self):
        self.assertEqual(self.groups, prepare.prepare(self.original, self.versions['envoy_gateway_image'], 'v1.9.2'))
