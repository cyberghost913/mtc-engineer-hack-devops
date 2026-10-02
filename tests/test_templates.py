import json
from pathlib import Path
import tomllib
import unittest
from jinja2 import Environment, StrictUndefined
import yaml

ROOT = Path(__file__).resolve().parents[1]


class Templates(unittest.TestCase):
    def setUp(self):
        self.env = Environment(undefined=StrictUndefined)
        self.env.filters['to_json'] = json.dumps
        self.env.tests['search'] = lambda text, needle: needle in text
        self.values = yaml.safe_load((ROOT / 'versions.yml').read_text())
        self.values.update(cluster_address='192.168.64.10', node_name='devops-control-plane',
                           cluster_name='devops-lab', pod_cidr='10.244.0.0/16', service_cidr='10.96.0.0/12')

    def test_kubeadm_config_parses_with_consistent_cgroups_and_networks(self):
        path = ROOT / 'ansible/roles/cluster/templates/kubeadm.yml.j2'
        docs = list(yaml.safe_load_all(self.env.from_string(path.read_text()).render(self.values)))
        self.assertEqual(len(docs), 4)
        self.assertEqual(docs[0]['nodeRegistration']['kubeletExtraArgs'][0]['value'], '192.168.64.10')
        self.assertEqual(docs[1]['networking']['podSubnet'], '10.244.0.0/16')
        self.assertEqual(docs[2]['cgroupDriver'], 'systemd')
        self.assertEqual(docs[3]['mode'], 'iptables')

    def test_containerd_2_config_has_cri_and_systemd_cgroups(self):
        path = ROOT / 'ansible/roles/runtime/templates/config.toml.j2'
        self.values['kubeadm_images'] = {'stdout_lines': ['registry.k8s.io/pause:3.10.1']}
        doc = tomllib.loads(self.env.from_string(path.read_text()).render(self.values))
        self.assertEqual(doc['version'], 3)
        plugins = doc['plugins']
        self.assertTrue(plugins['io.containerd.cri.v1.runtime']['containerd']['runtimes']['runc']['options']['SystemdCgroup'])
        self.assertEqual(plugins['io.containerd.cri.v1.images']['pinned_images']['sandbox'], 'registry.k8s.io/pause:3.10.1')
