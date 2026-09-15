KUBECONFIG ?= $(HOME)/.kube/config
CLUSTER    ?= costaware
K8S_VERSION ?= 1.31.4
KWOK_VERSION ?= v0.6.0
# Imagem do no do kind. Fixada em 1.31 para nao deixar o control plane mais de
# um minor a frente do binario do scheduler (compilado contra k8s v1.31.4).
KIND_IMAGE ?= kindest/node:v1.31.6

# Python via venv: no macOS com Homebrew nao existe `python` (so `python3`) e o
# `pip install` global e bloqueado (externally-managed). O venv resolve os dois.
PY_BOOT ?= python3
VENV    := .venv
PYTHON  := $(VENV)/bin/python

.PHONY: help
help:
	@echo "Fluxo tipico, em ordem:"
	@echo "  make deps       instala dependencias Python"
	@echo "  make gomod      resolve go.mod (demora na primeira vez)"
	@echo "  make test       testes unitarios -- NAO precisa de cluster"
	@echo "  make build      compila o scheduler"
	@echo "  make cluster    cria kind + instala KWOK"
	@echo "  make nodes      aplica os 4 nos do cenario"
	@echo "  make scenario   roda o teste de cenario deterministico"
	@echo "  make bench-nodes  aplica 200 nos falsos para o benchmark"
	@echo "  make bench-local  roda os 3 bracos x REPEATS seeds e agrega (KWOK, local)"
	@echo "  make clean      destroi o cluster"

.PHONY: deps
deps:
	$(PY_BOOT) -m venv $(VENV)
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r requirements.txt

.PHONY: gomod
gomod:
	chmod +x hack/gomod.sh
	./hack/gomod.sh $(K8S_VERSION)

.PHONY: test
test:
	go test ./pkg/costaware/... -v

.PHONY: build
build:
	go build -o bin/costaware-scheduler ./cmd/scheduler
	@echo "binario em ./bin/costaware-scheduler"

.PHONY: cluster
cluster:
	kind create cluster --name $(CLUSTER) --image $(KIND_IMAGE)
	kubectl apply -f https://github.com/kubernetes-sigs/kwok/releases/download/$(KWOK_VERSION)/kwok.yaml
	kubectl apply -f https://github.com/kubernetes-sigs/kwok/releases/download/$(KWOK_VERSION)/stage-fast.yaml
	@echo "aguardando o KWOK ficar pronto..."
	kubectl -n kube-system wait --for=condition=Available --timeout=120s deployment/kwok-controller

.PHONY: nodes
nodes:
	kubectl apply -f deploy/scenario-nodes.yaml
	kubectl get nodes -l scenario=true

.PHONY: scenario
scenario: build nodes
	$(PYTHON) bench/scenario.py --kubeconfig $(KUBECONFIG) --scheduler-bin ./bin/costaware-scheduler

.PHONY: bench-nodes
bench-nodes:
	$(PYTHON) bench/gen_nodes.py --nodes 200 --trap-fraction 0.5 --ratio 10 --out /tmp/bench-nodes.yaml
	kubectl apply -f /tmp/bench-nodes.yaml
	@kubectl get nodes --no-headers | wc -l | xargs echo "nos no cluster:"

# Roda A/B/C x REPEATS seeds contra o KWOK e agrega em results/summary.csv.
# Precisa de `make cluster` e `make build` antes. Ajuste com:
#   make bench-local REPEATS=10 ARMS=A,B,C
REPEATS ?= 10
ARMS    ?= A,B,C
.PHONY: bench-local
bench-local: build
	$(PYTHON) bench/run_local.py --repeats $(REPEATS) --arms $(ARMS) \
		--scheduler-bin ./bin/costaware-scheduler --kubeconfig $(KUBECONFIG)

.PHONY: clean
clean:
	kind delete cluster --name $(CLUSTER)
