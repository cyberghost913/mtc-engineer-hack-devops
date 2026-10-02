#!/usr/bin/env python3
"""Verify checked-in upstream files without network access."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1] / 'vendor'
entries = json.loads((root / 'sources.json').read_text())
expected = {'calico-v3.32.2.yaml', 'docker.asc', 'kubernetes-release.key',
            'envoy-gateway-v1.9.2.yaml', 'envoy-gateway-LICENSE'}
if {entry['file'] for entry in entries} != expected:
    raise SystemExit('Missing vendor dependencies')
for entry in entries:
    digest = hashlib.sha256((root / entry['file']).read_bytes()).hexdigest()
    if digest != entry['sha256']:
        raise SystemExit(f"Checksum mismatch: {entry['file']}")
    print(f"OK {entry['file']}")
