#!/usr/bin/env bash
# Gera go.mod com os replace directives necessarios para compilar contra
# k8s.io/kubernetes.
#
# POR QUE ISSO E PRECISO: k8s.io/kubernetes declara todos os seus submodulos
# de staging (k8s.io/api, k8s.io/client-go, ...) com a versao ficticia
# v0.0.0, que nao existe no proxy. Sem um replace apontando cada um para a
# versao real correspondente, `go mod tidy` falha com erros do tipo
# "k8s.io/api@v0.0.0: invalid version: unknown revision v0.0.0".
#
# A regra de mapeamento: kubernetes v1.31.4 -> staging v0.31.4
#
# Uso:  ./hack/gomod.sh 1.31.4

set -euo pipefail

K8S_VERSION="${1:-1.31.4}"
STAGING_VERSION="v0.${K8S_VERSION#1.}"   # 1.31.4 -> v0.31.4
MODULE="github.com/camilla-m/k8s-costaware"

echo ">>> kubernetes v${K8S_VERSION}, staging ${STAGING_VERSION}"

# Lista de submodulos de staging do k8s 1.31. Se voce mudar de minor e o tidy
# reclamar de algum modulo ausente, adicione-o aqui.
STAGING=(
  api
  apiextensions-apiserver
  apimachinery
  apiserver
  cli-runtime
  client-go
  cloud-provider
  cluster-bootstrap
  code-generator
  component-base
  component-helpers
  controller-manager
  cri-api
  cri-client
  csi-translation-lib
  dynamic-resource-allocation
  endpointslice
  kms
  kube-aggregator
  kube-controller-manager
  kube-proxy
  kube-scheduler
  kubectl
  kubelet
  metrics
  mount-utils
  pod-security-admission
  sample-apiserver
  sample-cli-plugin
  sample-controller
)

if [[ ! -f go.mod ]]; then
  go mod init "${MODULE}"
fi

go mod edit -require="k8s.io/kubernetes@v${K8S_VERSION}"

for s in "${STAGING[@]}"; do
  go mod edit -replace="k8s.io/${s}=k8s.io/${s}@${STAGING_VERSION}"
done

echo ">>> rodando go mod tidy (pode levar alguns minutos na primeira vez)"
go mod tidy

echo ">>> pronto. Verificando que o pacote compila:"
go build ./... && echo "OK"
