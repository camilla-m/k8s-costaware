KUBECONFIG ?= $(HOME)/.kube/config
CLUSTER    ?= costaware
K8S_VERSION ?= 1.31.4
KWOK_VERSION ?= v0.6.0

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
	@echo "  make clean      destroi o cluster"

.PHONY: deps
deps:
	pip install -r requirements.txt

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
	kind create cluster --name $(CLUSTER)
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
	python bench/scenario.py --kubeconfig $(KUBECONFIG) --scheduler-bin ./bin/costaware-scheduler

.PHONY: bench-nodes
bench-nodes:
	python bench/gen_nodes.py --nodes 200 --trap-fraction 0.5 --ratio 10 --out /tmp/bench-nodes.yaml
	kubectl apply -f /tmp/bench-nodes.yaml
	@kubectl get nodes --no-headers | wc -l | xargs echo "nos no cluster:"

.PHONY: clean
clean:
	kind delete cluster --name $(CLUSTER)
