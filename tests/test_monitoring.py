import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import unittest
from jinja2 import Environment, StrictUndefined
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / 'ansible/roles/monitoring'
spec = importlib.util.spec_from_file_location('verify_monitoring', ROOT / 'scripts/verify_monitoring.py')
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)
NOW = 1801432800.0
RECENT = datetime.fromtimestamp(NOW - 5, timezone.utc).isoformat()


class MonitoringChecks(unittest.TestCase):
    def setUp(self):
        self.pods = {'nginx-a', 'nginx-b'}
        self.targets = {'activeTargets': []}
        for job, count in monitor.EXPECTED_TARGETS.items():
            for i in range(count):
                labels = {'job': job}
                if job == 'nginx':
                    labels['pod'] = ['nginx-a', 'nginx-b'][i]
                self.targets['activeTargets'].append(
                    dict(labels=labels, health='up', lastError='', lastScrape=RECENT))
        self.vector = {'resultType': 'vector', 'result': [
            {'metric': {'pod': name}, 'value': [NOW, '1']} for name in sorted(self.pods)]}
        self.rules = {'groups': [{'rules': [
            dict(name=name, health='ok', lastEvaluation=RECENT) for name in monitor.EXPECTED_RULES]}]}
        self.pvc = {'metadata': {'uid': 'current-uid'}, 'spec': {'volumeName': 'devops-prometheus'},
                    'status': {'phase': 'Bound'}}
        self.pv = {'status': {'phase': 'Bound'}, 'spec': {'persistentVolumeReclaimPolicy': 'Retain',
                   'local': {'path': '/var/lib/devops-foundation/prometheus'},
                   'claimRef': {'name': 'prometheus-data', 'namespace': 'monitoring', 'uid': 'current-uid'}}}

    def test_accepts_complete_fresh_target_inventory(self):
        self.assertEqual(monitor.check_targets(self.targets, self.pods, NOW), monitor.EXPECTED_TARGETS)

    def test_rejects_missing_discovery_even_when_remaining_targets_are_up(self):
        self.targets['activeTargets'].pop()
        with self.assertRaisesRegex(ValueError, 'inventory'):
            monitor.check_targets(self.targets, self.pods, NOW)

    def test_rejects_duplicate_scrapes_of_one_replica(self):
        for target in self.targets['activeTargets']:
            if target['labels']['job'] == 'nginx':
                target['labels']['pod'] = 'nginx-a'
        with self.assertRaisesRegex(ValueError, 'both current'):
            monitor.check_targets(self.targets, self.pods, NOW)

    def test_rejects_old_scrape_despite_up_status(self):
        self.targets['activeTargets'][0]['lastScrape'] = '2000-01-01T00:00:00Z'
        with self.assertRaisesRegex(ValueError, 'timestamp'):
            monitor.check_targets(self.targets, self.pods, NOW)

    def test_rejects_down_target(self):
        self.targets['activeTargets'][0]['health'] = 'down'
        with self.assertRaisesRegex(ValueError, 'Unhealthy'):
            monitor.check_targets(self.targets, self.pods, NOW)

    def test_metric_requires_both_current_pods(self):
        monitor.check_vector(self.vector, 2, exact=1, pods=self.pods, now=NOW)
        self.vector['result'][0]['metric']['pod'] = 'old-pod'
        with self.assertRaisesRegex(ValueError, 'current'):
            monitor.check_vector(self.vector, 2, pods=self.pods, now=NOW)

    def test_rejects_empty_nan_inf_and_unhealthy_metrics(self):
        for bad in ['NaN', '+Inf', '-1', '0']:
            with self.subTest(bad=bad):
                self.vector['result'][0]['value'][1] = bad
                with self.assertRaises(ValueError):
                    monitor.check_vector(self.vector, 2, exact=1, now=NOW)
        with self.assertRaises(ValueError):
            monitor.check_vector({'resultType': 'vector', 'result': []}, 2, now=NOW)

    def test_source_sample_timestamp_is_checked_independently(self):
        # timestamp(metric) has a current evaluation timestamp but an old value.
        data = {'resultType': 'vector', 'result': [{'metric': {}, 'value': [NOW, str(NOW - 120)]}]}
        source_time = monitor.check_vector(data, 1, now=NOW)[0]
        with self.assertRaisesRegex(ValueError, 'timestamp'):
            monitor.require_recent(source_time, NOW)

    def test_accepts_evaluated_rules_but_rejects_missing_and_failed_rules(self):
        monitor.check_rules(self.rules, NOW)
        failed = copy.deepcopy(self.rules)
        failed['groups'][0]['rules'][0]['health'] = 'err'
        with self.assertRaisesRegex(ValueError, 'evaluation'):
            monitor.check_rules(failed, NOW)
        self.rules['groups'][0]['rules'].pop()
        with self.assertRaisesRegex(ValueError, 'missing'):
            monitor.check_rules(self.rules, NOW)

    def test_storage_binding_must_match_claim_uid(self):
        monitor.check_storage(self.pvc, self.pv)
        self.pv['spec']['claimRef']['uid'] = 'deleted-claim'
        with self.assertRaisesRegex(ValueError, 'different claim'):
            monitor.check_storage(self.pvc, self.pv)

    def test_rejects_ephemeral_or_unbound_storage(self):
        for mutate in [lambda p: p['status'].update(phase='Released'),
                       lambda p: p['spec'].update(persistentVolumeReclaimPolicy='Delete'),
                       lambda p: p['spec'].pop('local')]:
            pv = copy.deepcopy(self.pv)
            mutate(pv)
            with self.assertRaises(ValueError):
                monitor.check_storage(self.pvc, pv)

    def test_rejects_stale_rollout_and_wrong_image(self):
        d = {'metadata': {'generation': 2}, 'spec': {'replicas': 1, 'template': {'spec': {
            'containers': [{'image': 'pinned'}]}}}, 'status': {'observedGeneration': 2,
            'updatedReplicas': 1, 'availableReplicas': 1, 'readyReplicas': 1}}
        monitor.check_deployment(d, 'pinned')
        with self.assertRaisesRegex(ValueError, 'image'):
            monitor.check_deployment(d, 'other')
        d['status']['observedGeneration'] = 1
        with self.assertRaisesRegex(ValueError, 'generation'):
            monitor.check_deployment(d, 'pinned')


class MonitoringManifests(unittest.TestCase):
    def setUp(self):
        self.env = Environment(undefined=StrictUndefined)
        self.env.filters['to_json'] = json.dumps
        self.env.filters['hash'] = lambda text, algorithm: hashlib.new(algorithm, text.encode()).hexdigest()
        self.values = yaml.safe_load((ROOT / 'versions.yml').read_text())
        self.values.update(prometheus_config=(ROLE / 'files/prometheus.yml').read_text(),
                           prometheus_rules=(ROLE / 'files/rules.yml').read_text(),
                           monitoring_node_hostname='actual-node-hostname')

    def render(self, name):
        return list(yaml.safe_load_all(self.env.from_string((ROLE / f'templates/{name}.yml.j2').read_text())
                                       .render(self.values)))

    def test_persistent_volume_and_claim_bind_without_a_dynamic_provisioner(self):
        pv, pvc = self.render('storage')
        self.assertEqual(pvc['spec']['volumeName'], pv['metadata']['name'])
        self.assertEqual(pvc['spec']['storageClassName'], '')
        self.assertEqual(pv['spec']['persistentVolumeReclaimPolicy'], 'Retain')
        self.assertEqual(pv['spec']['nodeAffinity']['required']['nodeSelectorTerms'][0]
                         ['matchExpressions'][0]['values'], ['actual-node-hostname'])

    def test_configmap_matches_rollout_checksum_and_rule_updates_trigger_restart(self):
        resources = self.render('resources')
        cm = resources[0]
        deploy = next(r for r in resources if r['kind'] == 'Deployment' and r['metadata']['name'] == 'prometheus')
        annotations = deploy['spec']['template']['metadata']['annotations']
        for key, filename in [('config', 'prometheus.yml'), ('rules', 'rules.yml')]:
            self.assertEqual(annotations['checksum/prometheus-' + key],
                             hashlib.sha256(cm['data'][filename].encode()).hexdigest())
        self.values['prometheus_rules'] += '\n# changed\n'
        changed = next(r for r in self.render('resources') if r['kind'] == 'Deployment' and r['metadata']['name'] == 'prometheus')
        self.assertNotEqual(annotations, changed['spec']['template']['metadata']['annotations'])

    def test_prometheus_uses_one_writer_and_checks_config_before_start(self):
        deploy = next(r for r in self.render('resources') if r['kind'] == 'Deployment' and r['metadata']['name'] == 'prometheus')
        self.assertEqual(deploy['spec']['strategy']['type'], 'Recreate')
        pod = deploy['spec']['template']['spec']
        self.assertEqual(pod['initContainers'][0]['image'], pod['containers'][0]['image'])
        self.assertEqual(pod['initContainers'][0]['command'], ['/bin/promtool'])
        self.assertEqual(pod['volumes'][1]['persistentVolumeClaim']['claimName'], 'prometheus-data')

    def test_metrics_are_not_exposed_by_nodeport_and_containers_are_restricted(self):
        for resource in self.render('resources'):
            if resource['kind'] == 'Service':
                self.assertEqual(resource['spec']['type'], 'ClusterIP')
            if resource['kind'] == 'Deployment':
                pod = resource['spec']['template']['spec']
                self.assertTrue(pod['securityContext']['runAsNonRoot'])
                for container in pod['containers'] + pod.get('initContainers', []):
                    self.assertFalse(container['securityContext']['allowPrivilegeEscalation'])
                    self.assertTrue(container['securityContext']['readOnlyRootFilesystem'])

    def test_registry_evidence_matches_all_three_pinned_images(self):
        entries = json.loads((ROOT / 'docs/monitoring-image-lock.json').read_text())
        self.assertEqual({e['image'] for e in entries}, {self.values[k] for k in
                         ['prometheus_image', 'nginx_exporter_image', 'kube_state_metrics_image']})
        for entry in entries:
            self.assertRegex(entry['image'], r'@sha256:[0-9a-f]{64}$')
            self.assertTrue({'amd64', 'arm64'} <= {p['architecture'] for p in entry['platforms']})

    def test_pod_discovery_selects_exporter_ports_without_hiding_unready_targets(self):
        config = yaml.safe_load(self.values['prometheus_config'])
        nginx = next(j for j in config['scrape_configs'] if j['job_name'] == 'nginx')
        envoy = next(j for j in config['scrape_configs'] if j['job_name'] == 'envoy')
        self.assertIsNotNone(re.fullmatch(nginx['relabel_configs'][0]['regex'], 'nginx-exporter;metrics'))
        self.assertIsNone(re.fullmatch(nginx['relabel_configs'][0]['regex'], 'nginx;http'))
        self.assertIsNotNone(re.fullmatch(envoy['relabel_configs'][0]['regex'], 'envoy;19001'))
        self.assertIsNone(re.fullmatch(envoy['relabel_configs'][0]['regex'], 'shutdown-manager;19001'))
        self.assertNotIn('__meta_kubernetes_pod_ready', self.values['prometheus_config'])
        self.assertEqual(set(monitor.EXPECTED_TARGETS), {j['job_name'] for j in config['scrape_configs']})

    def test_rbac_does_not_grant_secrets_or_write_verbs(self):
        for doc in yaml.safe_load_all((ROLE / 'files/rbac.yml').read_text()):
            for rule in doc.get('rules', []):
                self.assertNotIn('secrets', rule.get('resources', []))
                self.assertLessEqual(set(rule['verbs']), {'get', 'list', 'watch'})

    def test_nginx_exporter_reads_loopback_and_keeps_metrics_off_app_service(self):
        self.values['nginx_config'] = (ROOT / 'ansible/roles/application/files/nginx.conf').read_text()
        template = (ROOT / 'ansible/roles/application/templates/resources.yml.j2').read_text()
        _, deploy, service = list(yaml.safe_load_all(self.env.from_string(template).render(self.values)))
        exporter = deploy['spec']['template']['spec']['containers'][1]
        self.assertIn('--nginx.scrape-uri=http://127.0.0.1:8081/stub_status', exporter['args'])
        self.assertEqual(exporter['image'], self.values['nginx_exporter_image'])
        self.assertEqual([p['port'] for p in service['spec']['ports']], [80])
