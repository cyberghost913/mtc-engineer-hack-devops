#!/usr/bin/env bash
set -Eeuo pipefail
export KUBECONFIG=/etc/kubernetes/admin.conf
probe_image=${1:?Usage: verify-cluster.sh IMAGE}
[[ $EUID -eq 0 && -f "$KUBECONFIG" ]] || { echo 'Run with sudo on the initialized VM'; exit 1; }
exec 9>/run/lock/devops-foundation-verify.lock
flock -n 9 || { echo 'Another verification is running'; exit 1; }
install -d -m 0700 /var/log/devops-foundation
umask 077
exec > >(tee /var/log/devops-foundation/verify.log) 2>&1
k() { kubectl --request-timeout=30s "$@"; }
namespace="foundation-check-$(date +%s)-${RANDOM}"
created=false
cleanup() {
    rc=$?
    trap - EXIT
    if [[ $created == true ]]; then
        if (( rc != 0 )); then
            k -n "$namespace" get pods -o wide || true
            k -n "$namespace" describe pods || true
            k -n "$namespace" logs client || true
            k -n "$namespace" get events --sort-by=.lastTimestamp || true
        fi
        k delete namespace "$namespace" --wait=false || true
    fi
    if (( rc != 0 )); then echo "FAIL: verification exited with code $rc"; fi
    exit "$rc"
}
trap cleanup EXIT
date -u '+Verification at %Y-%m-%dT%H:%M:%SZ'
test "$(k get --raw=/readyz)" = ok
echo 'PASS API ready'
k version -o json | python3 -c '
import json, sys
from pathlib import Path
lock = json.loads(Path("/etc/devops-foundation/lock.json").read_text())
version = json.load(sys.stdin)["serverVersion"]["gitVersion"]
assert version == "v" + lock["kubernetes_version"], version
print("PASS API version", version)
'
k --request-timeout=190s wait nodes --all --for=condition=Ready --timeout=180s
k get nodes -o json | python3 -c '
import json, sys
from pathlib import Path
lock = json.loads(Path("/etc/devops-foundation/lock.json").read_text())
nodes = json.load(sys.stdin)["items"]
assert len(nodes) == 1, "Expected exactly one node"
node = nodes[0]
assert node["metadata"]["name"] == lock["node_name"], "Unexpected node name"
info = node["status"]["nodeInfo"]
assert info["kubeletVersion"] == "v" + lock["kubernetes_version"], info
assert info["containerRuntimeVersion"] == "containerd://" + lock["containerd_package_version"].split("-", 1)[0], info
print("PASS node and runtime versions", info["kubeletVersion"], info["containerRuntimeVersion"])
'
for workload in daemonset/calico-node deployment/calico-kube-controllers deployment/coredns daemonset/kube-proxy; do
    k --request-timeout=190s -n kube-system rollout status "$workload" --timeout=180s
done
echo 'PASS Calico, CoreDNS and kube-proxy'
k create namespace "$namespace"
created=true
k -n "$namespace" apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: server
  labels:
    app: foundation-probe
spec:
  automountServiceAccountToken: false
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    runAsGroup: 1000
    fsGroup: 1000
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: http
      image: ${probe_image}
      command: [sh, -ec]
      args:
        - printf 'foundation-ok' > /www/index.html; exec httpd -f -p 8080 -h /www
      ports:
        - containerPort: 8080
      readinessProbe:
        httpGet:
          path: /
          port: 8080
        periodSeconds: 2
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop: [ALL]
      resources:
        requests: {cpu: 10m, memory: 16Mi}
        limits: {cpu: 100m, memory: 64Mi}
      volumeMounts:
        - name: www
          mountPath: /www
  volumes:
    - name: www
      emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: probe
spec:
  selector:
    app: foundation-probe
  ports:
    - port: 8080
      targetPort: 8080
EOF
k --request-timeout=190s -n "$namespace" wait pod/server --for=condition=Ready --timeout=180s
pod_ip=$(k -n "$namespace" get pod/server -o jsonpath='{.status.podIP}')
k -n "$namespace" run client --image="$probe_image" --restart=Never --command -- sh -ec "
  nslookup kubernetes.default.svc.cluster.local
  test \"\$(wget -T 5 -qO- http://probe.${namespace}.svc.cluster.local:8080/)\" = foundation-ok
  test \"\$(wget -T 5 -qO- http://${pod_ip}:8080/)\" = foundation-ok
  echo 'PASS DNS, Service routing and direct Pod networking'
"
k --request-timeout=100s -n "$namespace" wait pod/client --for=jsonpath='{.status.phase}'=Succeeded --timeout=90s
k -n "$namespace" logs client
k get nodes -o wide
echo 'PASS foundation verification completed; temporary namespace will be removed'
