#!/usr/bin/env bash
# Sobe o Karpenter DE VERDADE (nucleo sigs.k8s.io/karpenter) com o cloud
# provider kwok (sem AWS, sem credenciais, sem custo) para o braco D do
# benchmark -- "por que nao Karpenter?", a pergunta 1 da banca no README.
#
# Cluster SEPARADO do `costaware` (kind) usado pelos bracos A/B/C: o Karpenter
# provisiona nos do ZERO (nao coloca pods num pool fixo de 200 nos), entao os
# dois mecanismos nao cabem no mesmo cluster sem interferir.
#
# Requisitos instalados uma vez (brew): kind, ko, helm, gettext (envsubst).
#
# Uso:
#   ./hack/setup-karpenter-kwok.sh              # sobe tudo, idempotente
#   ./hack/setup-karpenter-kwok.sh --teardown    # kind delete cluster
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." &>/dev/null && pwd)"
KARPENTER_REPO="https://github.com/kubernetes-sigs/karpenter.git"
KARPENTER_COMMIT="da15327e3b062cbea965a00c70df5c287d889421"  # pinado em 2026-09-16, para reprodutibilidade
KARPENTER_SRC="${ROOT_DIR}/.karpenter-src"

export KIND_CLUSTER_NAME="${KIND_CLUSTER_NAME:-karpenter-kwok}"
export KWOK_REPO=kind.local
export KO_DOCKER_REPO=kind.local

if [[ "${1:-}" == "--teardown" ]]; then
  kind delete cluster --name "${KIND_CLUSTER_NAME}"
  exit 0
fi

case "$(uname -m)" in
  arm64|aarch64) PLATFORM="linux/arm64" ;;
  x86_64|amd64)  PLATFORM="linux/amd64" ;;
  *) echo "arquitetura desconhecida: $(uname -m); ajuste PLATFORM manualmente" >&2; exit 1 ;;
esac

echo ">>> 1/8: clonando karpenter @ ${KARPENTER_COMMIT:0:12} (uma vez)"
if [[ ! -d "${KARPENTER_SRC}/.git" ]]; then
  git clone "${KARPENTER_REPO}" "${KARPENTER_SRC}"
fi
git -C "${KARPENTER_SRC}" fetch --depth 1 origin "${KARPENTER_COMMIT}"
git -C "${KARPENTER_SRC}" checkout --detach "${KARPENTER_COMMIT}"

echo ">>> 2/8: cluster kind '${KIND_CLUSTER_NAME}'"
if ! kind get clusters 2>/dev/null | grep -qx "${KIND_CLUSTER_NAME}"; then
  kind create cluster --name "${KIND_CLUSTER_NAME}"
else
  echo "    ja existe, reaproveitando"
fi
kubectl config use-context "kind-${KIND_CLUSTER_NAME}" >/dev/null

echo ">>> 3/8: instalando sigs.k8s.io/kwok (fake kubelet)"
(cd "${KARPENTER_SRC}" && ./hack/install-kwok.sh)

echo ">>> 4/8: aplicando CRDs do Karpenter"
kubectl apply -f "${KARPENTER_SRC}/kwok/charts/crds"

echo ">>> 5/8: gerando catalogo de instance types a partir do preco real"
"${ROOT_DIR}/.venv/bin/python" "${ROOT_DIR}/bench/gen_karpenter_instance_types.py" \
  --out deploy/karpenter/instance-types.json
kubectl -n kube-system create configmap karpenter-instance-types \
  --from-file=instance-types.json="${ROOT_DIR}/deploy/karpenter/instance-types.json" \
  --dry-run=client -o yaml | kubectl apply -f -

echo ">>> 6/8: build da imagem do controller (ko, plataforma ${PLATFORM}, carregada direto no kind)"
IMG=$(cd "${KARPENTER_SRC}" && ko build --platform="${PLATFORM}" sigs.k8s.io/karpenter/kwok)
IMG_REPOSITORY="${IMG%:*}"
IMG_TAG="${IMG##*:}"
echo "    ${IMG_REPOSITORY}:${IMG_TAG}"

echo ">>> 7/8: helm upgrade --install"
helm upgrade --install karpenter "${KARPENTER_SRC}/kwok/charts" \
  --namespace kube-system --skip-crds \
  --kube-context "kind-${KIND_CLUSTER_NAME}" \
  -f "${ROOT_DIR}/deploy/karpenter/helm-values.yaml" \
  --set controller.image.repository="${IMG_REPOSITORY}" \
  --set controller.image.tag="${IMG_TAG}"
kubectl -n kube-system rollout status deployment/karpenter --timeout=120s

echo ">>> 8/8: NodePool/KWOKNodeClass + taint do control-plane"
kubectl apply -f "${ROOT_DIR}/deploy/karpenter/nodepool.yaml"
kubectl taint nodes "${KIND_CLUSTER_NAME}-control-plane" \
  CriticalAddonsOnly=true:NoSchedule --overwrite

echo ">>> pronto. contexto: kind-${KIND_CLUSTER_NAME}"
