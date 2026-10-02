#!/usr/bin/env python3
"""Read-only preflight. Standard library only, executed on the target VM."""
import base64
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys


def run(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=20)
    return result.returncode, result.stdout.strip()


def check_networks(pod_cidr, service_cidr, address, interfaces, routes, managed=False):
    pods = ipaddress.IPv4Network(pod_cidr)
    services = ipaddress.IPv4Network(service_cidr)
    node = ipaddress.IPv4Address(address)
    if not 16 <= pods.prefixlen <= 26:
        raise ValueError('Pod CIDR must be between /16 and /26 for this Calico profile')
    if not 12 <= services.prefixlen <= 27:
        raise ValueError('Service CIDR must be between /12 and /27')
    if pods.overlaps(services):
        raise ValueError('Pod and Service CIDRs overlap')
    local = []
    host_networks = []
    for interface in interfaces:
        device = interface.get('ifname', '')
        if managed and device.startswith(('cali', 'vxlan.calico', 'tunl')):
            continue
        for info in interface.get('addr_info', []):
            if info.get('family') == 'inet':
                local.append(info['local'])
                host_networks.append(ipaddress.ip_network(
                    f"{info['local']}/{info['prefixlen']}", strict=False))
    if str(node) not in local or node.is_loopback or node.is_link_local:
        raise ValueError('cluster_address must be a non-loopback IPv4 assigned to this VM')
    for route in routes:
        dest = route.get('dst', 'default')
        if dest == 'default':
            continue
        network = ipaddress.ip_network(dest, strict=False)
        if managed and network.subnet_of(pods) and (
            route.get('dev', '').startswith(('cali', 'vxlan.calico', 'tunl'))
            or route.get('type') == 'blackhole'
        ):
            continue
        host_networks.append(network)
    for network in host_networks:
        if pods.overlaps(network) or services.overlaps(network):
            raise ValueError(f'Cluster CIDR overlaps host network/route {network}')


def check_lock(requested, existing):
    if existing is not None and requested != existing:
        keys = sorted(k for k in requested.keys() | existing.keys()
                      if requested.get(k) != existing.get(k))
        raise ValueError('Existing foundation settings differ: ' + ', '.join(keys)
                         + '. Restore settings; bootstrap is not an upgrade/migration tool.')


def installed(package):
    code, output = run('dpkg-query', '-W', '-f=${Status}\t${Version}', package)
    return output.split('\t')[-1] if code == 0 and output.startswith('install ok installed') else None


def main(config):
    if platform.system() != 'Linux' or os.geteuid() != 0:
        raise ValueError('Run on the Ubuntu VM with sudo/become; never on the macOS host')
    release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    if release.get('ID', '').strip('"') != 'ubuntu' or release.get('VERSION_ID', '').strip('"') != '24.04':
        raise ValueError('Only Ubuntu 24.04 is supported by this profile')
    if platform.machine() not in ('x86_64', 'aarch64'):
        raise ValueError('Only amd64 and arm64 are supported')
    if not Path('/run/systemd/system').is_dir():
        raise ValueError('A booted VM with systemd is required')
    if run('systemd-detect-virt', '--container', '--quiet')[0] == 0:
        raise ValueError('Use a VM or dedicated host, not a container')
    if os.cpu_count() < 2:
        raise ValueError('At least 2 vCPU required; 4 recommended')
    memory = int(re.search(r'MemTotal:\s+(\d+)', Path('/proc/meminfo').read_text())[1])
    if memory < 3_500_000:
        raise ValueError('At least 4 GB allocated RAM required; 8 GB recommended')
    if shutil.disk_usage('/var/lib').free < 15 * 1024 ** 3:
        raise ValueError('At least 15 GiB free in /var/lib required')
    for key in ('cluster_name', 'node_name'):
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', config[key]):
            raise ValueError(f'{key} must be a DNS label, max 63 characters')
    marker = Path('/etc/devops-foundation/lock.json')
    existing = json.loads(marker.read_text()) if marker.exists() else None
    check_lock(config, existing)
    if existing is None:
        for path in ('/etc/kubernetes/admin.conf', '/etc/kubernetes/kubelet.conf'):
            if Path(path).exists():
                raise ValueError('Existing unmanaged Kubernetes installation detected')
        for path in ('/etc/cni/net.d', '/var/lib/etcd', '/etc/kubernetes/manifests'):
            if Path(path).exists() and any(Path(path).iterdir()):
                raise ValueError(f'Existing unmanaged cluster data: {path}')
        for package in ('docker.io', 'docker-ce', 'containerd', 'containerd.io', 'runc', 'kubelet', 'kubeadm'):
            if installed(package):
                raise ValueError(f'{package} already installed; use a clean dedicated VM')
        for port in (6443, 2379, 2380, 10250, 10257, 10259):
            with socket.socket() as sock:
                try:
                    sock.bind(('0.0.0.0', port))
                except OSError as exc:
                    raise ValueError(f'Port {port} is already occupied') from exc
    else:
        for package, version in [('containerd.io', config['containerd_package_version'])] + [
            (name, config['kubernetes_package_version']) for name in ('kubeadm', 'kubelet', 'kubectl')
        ]:
            actual = installed(package)
            if actual and actual != version:
                raise ValueError(f'{package} version drift: installed {actual}, expected {version}')
    for service in ('firewalld', 'NetworkManager'):
        if run('systemctl', 'is-active', '--quiet', service)[0] == 0:
            raise ValueError(f'{service} is active; this profile requires Ubuntu Server without it')
    if shutil.which('ufw') and 'Status: active' in run('ufw', 'status')[1]:
        raise ValueError('UFW is active; configure a dedicated lab network before bootstrap')
    code, interface_json = run('ip', '-j', '-4', 'address', 'show')
    if code:
        raise ValueError('Cannot inspect IPv4 interfaces')
    code, routes_json = run('ip', '-j', '-4', 'route', 'show', 'table', 'main')
    if code:
        raise ValueError('Cannot inspect IPv4 routes')
    check_networks(config['pod_cidr'], config['service_cidr'], config['cluster_address'],
                   json.loads(interface_json), json.loads(routes_json), existing is not None)
    for domain in ('pkgs.k8s.io', 'download.docker.com', 'registry.k8s.io', 'docker.io', 'quay.io'):
        socket.getaddrinfo(domain, 443)
    print('PASS: Ubuntu 24.04, resources, dedicated host, lock, networks, DNS')
    print('Registry HTTPS access and image availability will be checked during installation.')
    print('Remote VPN/client networks are not discoverable here: verify CIDRs manually.')


if __name__ == '__main__':
    try:
        main(json.loads(base64.b64decode(sys.argv[1])))
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f'FAIL: {error}', file=sys.stderr)
        sys.exit(1)
