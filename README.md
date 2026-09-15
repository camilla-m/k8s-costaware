# k8s-costaware

Plugin `Score` do kube-scheduler que realiza o custo percebido
`Phi_i = alpha_i + I(i ∉ S_{t-1}) · delta_i` dentro de um cluster Kubernetes real,
mais o harness de avaliacao em KWOK.

Este repositorio e o **artefato de qualificacao**: o objetivo nao e reproduzir os
ganhos de 55–90% dos artigos anteriores, e sim medir quanto sobra deles quando o
baseline e forte e o ambiente e real.

---

# PASSO A PASSO

## 0. Instalar as ferramentas

macOS:

```bash
brew install go kind kubectl
brew install --cask docker    # ou: brew install colima && colima start --cpus 4 --memory 8
```

Linux:

```bash
# Go 1.22+, Docker, kubectl e kind pelos canais da sua distro
go version && docker ps && kind version && kubectl version --client
```

Confira que o Docker esta rodando antes de continuar. `docker ps` tem que
responder sem erro.

## 1. Subir para o GitHub

```bash
cd k8s-costaware
git init
git add .
git commit -m "plugin cost-aware + harness de avaliacao"
gh repo create camilla-m/k8s-costaware --public --source=. --push
```

O CI (`.github/workflows/ci.yml`) roda sozinho no push. A **primeira** execucao
leva ~15 min (baixa a arvore de dependencias do k8s); as seguintes, ~2 min.
Se o build quebrar, o log do Actions te diz exatamente o que falta — e voce
descobre isso sem esperar o download na sua maquina.

## 2. Testes unitarios (nao precisa de cluster)

```bash
make deps      # dependencias Python
make gomod     # DEMORA: 5-15 min na primeira vez, baixa alguns GB
make test
```

Se `make gomod` falhar com `k8s.io/<algo>@v0.0.0: invalid version`, e um
submodulo de staging faltando: adicione o nome ao array `STAGING` em
`hack/gomod.sh` e rode de novo.

Se falhar com erro de assinatura em `Score` ou `NewNodeInfo`, e a divergencia de
API entre minors. Rode:

```bash
go doc k8s.io/kubernetes/pkg/scheduler/framework.ScorePlugin
```

e ajuste `pkg/costaware/plugin.go` conforme o que ele imprimir.

**Nao siga adiante ate `make test` passar.** Depurar uma funcao pura leva
segundos; depurar a mesma funcao dentro de um scheduler dentro de um container
leva uma tarde.

## 3. Cenario deterministico (precisa de cluster)

```bash
make build
make cluster     # cria kind + instala KWOK
make scenario    # roda os 6 casos
```

Saida esperada: `6/6 casos passaram`.

Se algum falhar, o proprio script imprime o no esperado e o obtido. O
diagnostico mais util:

```bash
kubectl get pods -n costaware-scenario -o wide
kubectl describe pods -n costaware-scenario
kubectl get events -n costaware-scenario --sort-by=.lastTimestamp
```

## 4. Benchmark local (opcional; ~90 min para 8 seeds)

Um comando roda os tres bracos x N seeds contra o KWOK e agrega:

```bash
make bench-nodes                        # 200 nos falsos (uma vez)
make bench-local REPEATS=8              # A/B/C x 8 seeds, ~90 min
```

Saida em `results/`:

- `summary.csv` — uma linha por (braco, seed), escrita incrementalmente
- `raw/run-<arm>-s<seed>.csv` — custo por slot de cada execucao
- e, no stdout, media +- desvio por braco e um Wilcoxon pareado C vs B

Cada execucao sobe `./bin/costaware-scheduler` como subprocesso com um
`schedulerName` dedicado (`bench-a`/`-b`/`-c`, configs em `deploy/bench-*.yaml`),
roda `bench/workload.py` + `bench/cost.py` em paralelo, e apaga o namespace ao
fim. **Execucoes separadas** — o `run_local.py` cuida da drenagem entre elas.

Para iterar rapido: `make bench-local REPEATS=1 ARMS=C`, ou
`python bench/run_local.py --repeats 1 --arms C --skip-nodes --pods 80 --slots 5`.

> Os precos vem da tabela **chutada** de `pkg/costaware/pricing.go` /
> `bench/gen_nodes.py`, nao da AWS. O KWOK nao mede latencia real e o Wilcoxon
> aqui e a aproximacao normal. Isto responde *"Phi bate bin-packing cego a
> preco?"*, nao *"quanto se economiza na producao?"*. Para publicar: precos
> reais (Price List API), `delta` medido (Semana 3) e `scipy.stats.wilcoxon`.

## 5. Limpar

```bash
make clean
```

---

## As tres decisoes de projeto

**1. O baseline forte e `MostAllocated`, nao o kube-scheduler default.**
O default usa `LeastAllocated` e espalha a carga — comparar contra ele reproduz o
strawman dos trabalhos anteriores. O `NodeResourcesFit` ja oferece bin-packing de
graca. A pergunta de pesquisa honesta e: *bin-packing e cego a preco — empacota por
razao de recurso, nao por $/recurso*. E ai que `Phi` pode ganhar.

**2. `delta` reinterpretado para pool fixo.**
No modelo, `delta` e custo de ligar no. Num cluster real todos os nos no informer
ja estao `Ready`. O mapeamento honesto: um no e *frio* se esta vazio (zero pods
nao-DaemonSet), porque colocar o primeiro pod nele **impede o scale-down** — e o
custo disso e exatamente `delta`. Tambem sao frios nos `NotReady` e nos com idade
abaixo da janela de boot.

**3. `Score`, nao `Filter`.**
`Phi` e criterio de ordenacao, e `Score` + `NormalizeScore` reproduzem o passo
`Nsorted` do TGCH quase literalmente. O first-fit do algoritmo vira o `Filter`
nativo do `NodeResourcesFit`.

---

## O que este artefato NAO cobre (e precisa ser dito na banca)

- **Migracao de pods.** `y_ijt` variando com `t` implica despejo. Isso nao passa
  pelo scheduler — passa pelo descheduler ou por um controlador de consolidacao,
  respeitando PodDisruptionBudgets. E o Componente 2, fora do MVP.
- **Provisionamento.** Ligar/desligar no e competencia do autoscaler
  (Cluster Autoscaler / Karpenter). O artefato completo da tese **atravessa duas
  pecas do Kubernetes**, e esse e o melhor reposicionamento da contribuicao:
  *placement e provisioning sao decididos por componentes desacoplados e reativos;
  propomos a decisao conjunta, com horizonte temporal.*

---

## Build

```bash
go mod init github.com/camilla-m/k8s-costaware
# PINE a versao. A API do framework muda entre minors.
go get k8s.io/kubernetes@v1.31.4
go mod tidy
go build -o bin/costaware-scheduler ./cmd/scheduler
```

> **Atencao de API:** `plugin.go` mira o framework do k8s **v1.31.x**, onde
> `ScorePlugin.Score` recebe `nodeName string`. Em v1.32+ a assinatura passa a
> receber `*framework.NodeInfo`. Se voce subir de versao, ajuste `Score()`.
>
> O decode dos args usa `framework/runtime.DecodeInto` (em v1.31 a funcao vive
> no subpacote `runtime`, nao em `framework`). O `hack/gomod.sh` tambem fixa a
> linha `go 1.22` no go.mod — o `go mod init` de um toolchain mais novo grava
> uma versao que o CI (setup-go 1.22) recusa.

## Ambiente

```bash
kind create cluster --name costaware
kubectl apply -f https://github.com/kubernetes-sigs/kwok/releases/download/v0.6.0/kwok.yaml
kubectl apply -f https://github.com/kubernetes-sigs/kwok/releases/download/v0.6.0/stage-fast.yaml

python bench/gen_nodes.py --nodes 200 --trap-fraction 0.5 --ratio 10 --out /tmp/nodes.yaml
kubectl apply -f /tmp/nodes.yaml
```

## Rodar um braco manualmente

O `make bench-local` (secao 4) ja faz isso em loop. Para rodar um braco a mao,
contra o KWOK, com um scheduler separado:

```bash
# injeta o kubeconfig no config (com --config o kube-scheduler ignora --kubeconfig)
python - <<'PY'
import yaml, os
d = yaml.safe_load(open("deploy/bench-C.yaml"))
d.setdefault("clientConnection", {})["kubeconfig"] = os.path.expanduser("~/.kube/config")
yaml.safe_dump(d, open("/tmp/bench-C.yaml", "w"))
PY
./bin/costaware-scheduler --config /tmp/bench-C.yaml --secure-port=0 --v=2 &
python bench/workload.py --pods 150 --slots 10 --slot-seconds 18 \
    --scheduler-name bench-c --namespace bench-c --seed 1 --out results/arrivals-C.csv &
python bench/cost.py --arm C --duration 180 --interval 18 --out results/run-C.csv
```

Os `deploy/arm-*.yaml` sao a variante para **substituir** o scheduler de um
cluster kubeadm (schedulerName `default-scheduler`, kubeconfig em
`/etc/kubernetes/scheduler.conf`) — nao servem para rodar solto na sua maquina.

---

## Plano de 8 semanas ate a qualificacao

| Sem | Entrega | Criterio de pronto |
|-----|---------|--------------------|
| 1 | Build do plugin + KWOK de 200 nos no ar | um pod agendado pelo Arm C, log mostrando `Phi` por no |
| 2 | Harness completo: gerador de nos, driver de carga, contabilidade | 3 bracos rodam ponta a ponta em 1h sem intervencao |
| 3 | Precos reais via AWS Price List API + campanha de medicao de `delta` | tabela `instance_type -> (preco, boot-to-Ready medido)` com data de coleta |
| 4 | Varredura de sensibilidade R ∈ {0, 1, 10, 100} × 5 tamanhos × 10 repeticoes | CSVs completos, Wilcoxon pareado C vs B |
| 5 | Karpenter num EKS pequeno (10–20 nos) como 4o braco | consolidacao ligada, mesma carga, mesmo horizonte |
| 6 | Ablacao: R=0 isola "preco" de "inercia" | quantificado quanto do ganho vem de cada termo |
| 7 | Redacao do capitulo de artefato + reharmonizacao dos resultados antigos | todos os papers reexpressos na mesma metrica ($/hora de no ativo) |
| 8 | Slides + ensaio da banca | respostas prontas para as 5 perguntas abaixo |

### Semana 3 merece destaque

Nao existe tabela publicada de latencia de provisionamento ate `Ready` por tipo de
instancia, nem de tempo de pull de imagem de varios GB. Medir isso — EKS e GKE,
CPU e GPU, imagem pequena e imagem de modelo — **e uma contribuicao autonoma** e
provavelmente rende um artigo curto. Alem disso, e o unico jeito de `delta` deixar
de ser um numero inventado.

---

## Perguntas que a banca vai fazer

1. *Por que nao Karpenter?* — Resposta: Karpenter e o braco 4 e a comparacao esta
   na Secao X. O diferencial e o horizonte temporal e a decisao conjunta.
2. *De onde vem `alpha`?* — Price List API, regiao e data registradas.
3. *De onde vem `delta`?* — Medido, Semana 3. Nao assumido.
4. *E se `Phi` empatar com `MostAllocated`?* — E um resultado valido e previsto:
   significa que a heterogeneidade de preco na instancia nao ativa a Condicao 2 do
   Teorema 2. Reporte, nao esconda.
5. *Cadê a migracao?* — Fora do escopo do MVP, e o Componente 2; explique o
   descheduler e os PDBs.

---

## Pendencias conhecidas no codigo

- `NormalizeScore` faz min-max sobre o conjunto viavel; isso torna `Phi` relativo
  ao ciclo. Verifique se isso e desejavel — a alternativa e normalizar contra um
  teto global de preco, o que da scores comparaveis entre ciclos.
- O termo de packing e misturado a `Phi` antes da normalizacao. Considere
  normalizar os dois termos separadamente antes de combinar.
- `isCold` percorre `nodeInfo.Pods` a cada avaliacao. Em clusters grandes, meca
  `scheduler_plugin_execution_duration_seconds` antes de assumir que e barato.
- O estado de ciclo (`cycleState.raw`) e escrito em paralelo por todas as
  goroutines de `Score` — hoje protegido por `sync.Mutex` e criado em
  `PreScore`. Sem isso o scheduler inteiro morre com *concurrent map writes* em
  escala (200+ nos); `plugin_test.go` cobre o caso com `-race`. Se um dia trocar
  o mapa por algo lock-free, mantenha o teste.
