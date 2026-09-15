# Primeiro benchmark local (KWOK, 8 seeds)

Executado em 2026-09-07 nesta maquina (macOS, colima + kind + KWOK v0.6.0,
control-plane v1.31.6), via `make bench-local REPEATS=8`. Reproduzivel com:

```bash
make cluster && make bench-nodes && make bench-local REPEATS=8
```

## Metodo

- 200 nos falsos KWOK (`bench/gen_nodes.py --trap-fraction 0.5 --ratio 10`):
  metade classe `trap` (barata por core, boot inflado 10x), metade `stable`
  (cara por core, boot baixo) — a condicao de heterogeneidade preco x delta
  (Teorema 2, Condicao 2).
- Carga: `bench/workload.py --pods 150 --slots 10 --slot-seconds 18`, chegada
  `uniform` (+-30%), vida media 6 slots, seeds 1-8.
- Tres bracos, cada um um `./bin/costaware-scheduler` separado
  (`deploy/bench-{A,B,C}.yaml`):
  - **A** — kube-scheduler default (`LeastAllocated`) — baseline fraco.
  - **B** — `NodeResourcesFit` com `MostAllocated` — baseline forte, bin-packing
    cego a preco.
  - **C** — `CostAware` (Phi), `transitionRatio=10`, `packingWeight=0.2`,
    `coldNodeAgeSeconds=0`.
- Custo amostrado a cada slot por `bench/cost.py`, somando o preco/hora dos nos
  com >=1 pod de workload.

## Resultado

| braco | n | custo total US$ (media +- dp) | ativacoes | nos ativos (media) | pendentes (max) |
|---|---|---|---|---|---|
| A (default)   | 8 | 1.4346 +- 0.0372 | 169.5 | 106.0 | 4 |
| B (binpack)   | 8 | 0.2141 +- 0.0115 |  30.5 |  24.3 | 6 |
| C (CostAware) | 8 | **0.0734 +- 0.0050** | **17.8** | 13.3 | 6 |

**C vs B** (a comparacao que importa — contra o baseline forte, nao o strawman):

- economia de custo media: **65.7%**
- C bateu B nas **8 de 8** seeds pareadas (Wilcoxon signed-rank, W=0, z=-2.45,
  p (bicaudal, aprox. normal) ~= 0.014)
- ativacoes de no (proxy de delta): 17.8 vs 30.5, **-42%**

C vs A: -94.9% (esperado — A e o strawman que a literatura anterior batia).

## O que isto responde, e o que nao responde

Responde: *dado bin-packing cego a preco como oponente, Phi explora a
heterogeneidade preco x delta e ganha, de forma estatisticamente estavel.*
E a pergunta certa para a qualificacao — a Secao 4 do README explica por que A
nao e o baseline relevante.

NAO responde "quanto se economiza na producao". Para isso faltam, em ordem de
prioridade:

1. **Precos reais.** `pkg/costaware/pricing.go` e `bench/gen_nodes.py` usam uma
   tabela chutada. Puxar da AWS Price List API (regiao + data registradas) —
   Semana 3 do plano no README.
2. **delta medido**, nao `boot_seconds x ratio=10` inventado. Ver
   `docs/measuring-delta.md`.
3. **Karpenter como 4o braco**, num cluster real (nao KWOK) — consolidacao
   ligada, mesma carga.
4. **n maior e Wilcoxon exato.** n=8 e a aproximacao normal bastam para um
   primeiro sinal, nao para a tese. `scipy.stats.wilcoxon(exact=True)`, n>=10,
   e reportar tambem effect size.
5. Esta rodada usa `boot_seconds x 10` (o `--ratio` do gerador) SOBRE um
   `transitionRatio=10` no plugin — dois fatores de 10 empilhados. Vale rodar
   a varredura de sensibilidade R in {0,1,10,100} do plano de 8 semanas para
   separar os dois efeitos em vez de compor-los por acidente.

## Onde estao os dados brutos

`results/summary.csv` (uma linha por braco x seed), `results/raw/*.csv` (custo
por slot), `results/logs/*.log` (scheduler/cost/workload de cada execucao).
Nao versionados (`.gitignore`) — regenere com o comando acima.
