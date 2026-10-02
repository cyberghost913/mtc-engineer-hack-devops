import importlib.util
from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('render_calico', ROOT / 'scripts/render_calico.py')
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


class CalicoManifest(unittest.TestCase):
    def test_vxlan_manifest_preserves_resources_and_pins_images(self):
        original = list(yaml.safe_load_all((ROOT / 'vendor/calico-v3.32.2.yaml').read_text()))
        output = renderer.render(original, '10.244.0.0/16')
        self.assertEqual([(d['kind'], d['metadata']['name']) for d in original if d],
                         [(d['kind'], d['metadata']['name']) for d in output if d])
        daemon = next(d for d in output if d and d['kind'] == 'DaemonSet')
        pod = daemon['spec']['template']['spec']
        for container in pod['containers'] + pod['initContainers']:
            self.assertTrue(container['image'].endswith(':v3.32.2'))
        node = next(c for c in pod['containers'] if c['name'] == 'calico-node')
        env = {e['name']: e.get('value') for e in node['env']}
        self.assertEqual(env['CALICO_IPV4POOL_CIDR'], '10.244.0.0/16')
        self.assertEqual(env['CALICO_IPV4POOL_VXLAN'], 'Always')
        self.assertEqual(env['CALICO_IPV4POOL_IPIP'], 'Never')
        self.assertIn('-felix-ready', node['readinessProbe']['exec']['command'])
        self.assertNotIn('-bird-ready', node['readinessProbe']['exec']['command'])
        self.assertEqual(output, renderer.render(original, '10.244.0.0/16'))
