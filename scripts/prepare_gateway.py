#!/usr/bin/env python3
"""Split the official bundle into ordered, repeatable installation phases."""
import copy
import hashlib
from pathlib import Path
import sys
import yaml


def prepare(documents, image, version):
    groups = {name: [] for name in ('namespace', 'crds', 'controller', 'certgen')}
    documents = copy.deepcopy(documents)
    config = next(d for d in documents if d and d['kind'] == 'ConfigMap'
                  and d['metadata']['name'] == 'envoy-gateway-config')
    settings = yaml.safe_load(config['data']['envoy-gateway.yaml'])
    settings['provider']['kubernetes']['shutdownManager']['image'] = image
    config['data']['envoy-gateway.yaml'] = yaml.safe_dump(settings, sort_keys=False)
    checksum = hashlib.sha256(config['data']['envoy-gateway.yaml'].encode()).hexdigest()
    for doc in documents:
        if not doc:
            continue
        kind = doc['kind']
        labels = doc['metadata'].setdefault('labels', {})
        labels.update({'app.kubernetes.io/managed-by': 'devops-foundation',
                       'app.kubernetes.io/part-of': 'devops-gateway'})
        if 'app.kubernetes.io/version' in labels:
            labels['app.kubernetes.io/version'] = version
        annotations = doc['metadata'].get('annotations', {})
        for key in list(annotations):
            if key.startswith('helm.sh/hook'):
                del annotations[key]
        if kind in ('Deployment', 'Job'):
            for container in doc['spec']['template']['spec']['containers']:
                if container['image'] == 'envoyproxy/gateway:' + version:
                    container['image'] = image
        if kind == 'Deployment':
            doc['spec']['template']['metadata'].setdefault('annotations', {})['checksum/controller-config'] = checksum
        if kind == 'Job':
            # Preserve a completed job: a repeat apply must not regenerate certificates.
            doc['spec'].pop('ttlSecondsAfterFinished', None)
            group = 'certgen'
        elif kind == 'Namespace':
            group = 'namespace'
        elif kind in ('CustomResourceDefinition', 'ValidatingAdmissionPolicy', 'ValidatingAdmissionPolicyBinding'):
            group = 'crds'
        else:
            group = 'controller'
        groups[group].append(doc)
    if any(not documents for documents in groups.values()):
        raise ValueError('The upstream installation bundle has an unexpected structure')
    return groups


if __name__ == '__main__':
    source, output_dir, image, version = sys.argv[1:]
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    groups = prepare(list(yaml.safe_load_all(Path(source).read_text())), image, version)
    changed = False
    for name, documents in groups.items():
        path = output / (name + '.yaml')
        text = yaml.safe_dump_all(documents, sort_keys=False)
        if not path.exists() or path.read_text() != text:
            path.write_text(text)
            changed = True
    print('CHANGED' if changed else 'UNCHANGED')
