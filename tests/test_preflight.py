import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('preflight', Path(__file__).resolve().parents[1] / 'scripts/preflight.py')
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class NetworkChecks(unittest.TestCase):
    def setUp(self):
        self.interfaces = [{'ifname': 'eth0', 'addr_info': [
            {'family': 'inet', 'local': '192.168.64.10', 'prefixlen': 24}]}]
        self.routes = [{'dst': 'default', 'gateway': '192.168.64.1'},
                       {'dst': '192.168.64.0/24', 'dev': 'eth0'}]

    def check(self, **overrides):
        args = dict(pod_cidr='10.244.0.0/16', service_cidr='10.96.0.0/12',
                    address='192.168.64.10', interfaces=self.interfaces, routes=self.routes)
        args.update(overrides)
        preflight.check_networks(**args)

    def test_valid_network(self):
        self.check()

    def test_reject_overlap_with_host(self):
        with self.assertRaisesRegex(ValueError, 'overlaps host'):
            self.check(pod_cidr='192.168.0.0/16')

    def test_reject_overlap_between_clusters(self):
        with self.assertRaisesRegex(ValueError, 'CIDRs overlap'):
            self.check(service_cidr='10.244.0.0/16')

    def test_reject_vpn_route(self):
        self.routes.append({'dst': '10.0.0.0/8', 'dev': 'tun0'})
        with self.assertRaisesRegex(ValueError, 'overlaps host'):
            self.check()

    def test_reject_wrong_node_address(self):
        with self.assertRaisesRegex(ValueError, 'assigned'):
            self.check(address='192.168.64.20')

    def test_allow_own_calico_routes_on_repeat(self):
        self.routes.extend([{'dst': '10.244.0.0/26', 'type': 'blackhole'},
                            {'dst': '10.244.0.1', 'dev': 'cali123'}])
        self.check(managed=True)

    def test_reject_foreign_route_even_on_repeat(self):
        self.routes.append({'dst': '10.244.10.0/24', 'dev': 'eth1'})
        with self.assertRaisesRegex(ValueError, 'overlaps host'):
            self.check(managed=True)

    def test_reject_noncanonical_cidr(self):
        with self.assertRaises(ValueError):
            self.check(pod_cidr='10.244.1.1/16')

    def test_reject_too_small_pool(self):
        with self.assertRaisesRegex(ValueError, 'between /16 and /26'):
            self.check(pod_cidr='10.244.0.0/28')


class LockChecks(unittest.TestCase):
    def test_fresh_and_repeat(self):
        preflight.check_lock({'pod_cidr': '10.244.0.0/16'}, None)
        preflight.check_lock({'version': '1.35.9'}, {'version': '1.35.9'})

    def test_reject_version_change(self):
        with self.assertRaisesRegex(ValueError, 'version'):
            preflight.check_lock({'version': '1.35.9'}, {'version': '1.35.8'})

    def test_reject_removed_lock_key(self):
        with self.assertRaisesRegex(ValueError, 'cidr'):
            preflight.check_lock({}, {'cidr': '10.244.0.0/16'})
