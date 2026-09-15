# Varredura de sensibilidade de R (ablação preço x inércia)

Executado em 2026-09-15 nesta maquina (macOS, colima + kind + KWOK v0.6.0),
via `make bench-sweep REPEATS=8`. Reproduzivel com:

```bash
make cluster && make bench-nodes
make bench-sweep REPEATS=8 R_VALUES=0,1,10,100
```

## Por que esta rodada existe

`docs/first-benchmark.md` mediu C contra B com `transitionRatio` (R) fixo em
10 e reportou 65.7% de economia. Isso responde "Phi bate bin-packing cego a
preco?", mas nao separa **duas fontes distintas de ganho** que `Phi` mistura:

```
Phi = (1-w) * (alpha + [frio] * delta) - w * utilizacao * alpha
```

- o termo `alpha` (preco por core) -- pura consciencia de preco;
- o termo `delta` (penalidade de frio, escalada por R) -- inercia temporal,
  a contribuicao que a tese chama de "Startup-Cost Heterogeneity" (Teorema 2,
  Condicao 2).

`R=0` desliga `delta` inteiramente. Se a economia de C-R0 contra B for
parecida com a de C-R10/C-R100, o ganho medido vem do preco, nao da inercia --
e isso muda o que a tese pode alegar como contribuicao.

## Metodo

Igual ao de `docs/first-benchmark.md` (200 nos KWOK, `trap-fraction=0.5`,
`ratio=10`; 150 pods, 10 slots de 18s, chegada uniforme +-30%, seeds 1-8),
com um braco C por valor de R em vez de um so:

- **A** — kube-scheduler default (`LeastAllocated`)
- **B** — `NodeResourcesFit` `MostAllocated` -- baseline forte
- **C-R0, C-R1, C-R10, C-R100** — `CostAware`, `packingWeight=0.2`,
  `coldNodeAgeSeconds=0`, variando so `transitionRatio`

Cada variante roda como um `./bin/costaware-scheduler` separado
(`deploy/bench-C.yaml` com `transitionRatio` e `schedulerName` sobrescritos
por `bench/run_local.py --r-values`). Confirmado nos logs (`plugin.go:97
"CostAware plugin initialized" transitionRatio=<valor>`) que cada variante
carregou o R pretendido -- nao e um artefato de configuracao repetida.

## Resultado

| variante | n | custo total US$ (media +- dp) | ativacoes | nos ativos (media) |
|---|---|---|---|---|
| A (default)   | 8 | 1.4397 +- 0.0366 | 169.5 | 106.5 |
| B (binpack)   | 8 | 0.2149 +- 0.0133 |  30.9 |  24.4 |
| C-R0          | 8 | 0.0752 +- 0.0040 |  17.0 |  13.6 |
| C-R1          | 8 | 0.0759 +- 0.0045 |  17.4 |  13.8 |
| C-R10         | 8 | 0.0734 +- 0.0027 |  17.2 |  13.3 |
| C-R100        | 8 | 0.0746 +- 0.0027 |  18.1 |  13.5 |

**C-Rx vs B**, Wilcoxon signed-rank **exato** (`scipy.stats.wilcoxon`, n=8
seeds pareadas):

| R | economia vs B | W | p (bicaudal, exato) |
|---|---|---|---|
| 0   | 65.0% | 0 | 0.0078 |
| 1   | 64.7% | 0 | 0.0078 |
| 10  | 65.8% | 0 | 0.0078 |
| 100 | 65.3% | 0 | 0.0078 |

`W=0` em todos os quatro: C bateu B nas 8 de 8 seeds, em todo valor de R --
p=0.0078 e o minimo possivel para n=8 (2 x (1/2)^8), entao a significancia
estatistica do ganho contra B nao e a pergunta interessante aqui. A pergunta
interessante e a coluna "economia": **ela varia entre 64.7% e 65.8% -- uma
faixa de 1.1 ponto percentual, menor que o desvio entre seeds de uma unica
configuracao** (dp de C-R10 sozinho e da ordem de 0.0027/0.0734 ~ 3.7% do
custo total). R=100 e R=0 sao, na pratica, indistinguiveis nesta rodada.

## O achado (e ele e desconfortavel, de proposito)

**A economia de C sobre B nao muda com R.** As ativacoes de no tambem nao
mudam com R (17.0 a 18.1 -- variacao dentro do ruido entre seeds). Isso quer
dizer que, nas condicoes deste experimento, **o ganho vem quase todo do termo
de preco (`alpha`) e do packing, nao da inercia temporal (`delta`)** -- a
condicao de heterogeneidade de custo de start-up que o Teorema 2 descreve nao
esta sendo o que decide o placement aqui, mesmo em R=100 (o regime "critico"
que o comentario do `deploy/arm-C-costaware.yaml` chama de cold-start de
imagem de modelo de IA).

Isto e exatamente o tipo de resultado que `README.md` (pergunta 4 da banca) ja
pedia para reportar sem esconder: *"E se Phi empatar com MostAllocated? E um
resultado valido e previsto... Reporte, nao esconda."* Aqui e uma variante
disso -- nao um empate com o baseline, mas uma insensibilidade a R que
precisa de explicacao antes da qualificacao.

### Hipoteses (nenhuma testada ainda)

1. **Horizonte curto demais.** 10 slots x 18s = 180s. A inercia so importa
   quando um no fica ativo tempo suficiente para o custo amortizado de
   `delta` pesar contra reaproveita-lo depois. Num horizonte curto, a maioria
   dos nos so e usada uma vez -- nao ha "depois" para a inercia proteger.
   Teste direto: repetir com `--slots 40 --slot-seconds 30` (20 min por
   execucao) e comparar.
2. **O mix trap/stable ja ordena certo so pelo preco.** `alpha_trap < alpha_stable`
   por construcao (`bench/gen_nodes.py`) -- entao mesmo com `delta=0` (R=0),
   `Phi` já prefere os nos baratos, e a unica coisa que R=100 poderia mudar e
   se um no *stable ja quente* perde para um *trap frio*. Se a maior parte da
   carga cabe nos nos baratos sem forcar essa troca, R nunca fica ativo o
   suficiente pra aparecer na media.
3. **`packingWeight=0.2` e `NodeResourcesFit` peso 3 dominam a decisao de
   consolidacao**, deixando pouco espaco para o termo de preco (e portanto
   para `delta`, que so entra multiplicado por ele) decidir o resultado
   final. Testar com `packingWeight=0` isola isso.

Nenhuma das tres esta descartada. A honesta e dizer, por ora: **o efeito de R
medido aqui e pequeno ou nulo, e a causa mais provavel e o horizonte curto**
-- mas isso precisa ser testado, nao assumido, antes de entrar no texto da
qualificacao como conclusao.

## O que isto muda no argumento da tese

Nao invalida `docs/first-benchmark.md` (os 65.7% ali, com R=10, sao
consistentes com os 65.8% de C-R10 aqui). Mas **enfraquece a alegacao de que
a inercia temporal e o diferencial da tese** enquanto as hipoteses acima nao
forem testadas -- hoje, com os dados que existem, a alegacao defensavel e mais
modesta: *"Phi bate bin-packing cego a preco, e a maior parte do ganho medido
vem da parte de preco, nao da parte temporal; a contribuicao temporal ainda
nao foi isolada de forma a aparecer neste horizonte."*

## Proximo passo de graca (sem nuvem)

Rodar a hipotese 1 primeiro -- e a mais barata de testar e a mais provavel:

```bash
make bench-sweep REPEATS=8 R_VALUES=0,100  # so os extremos, horizonte 10x maior
python bench/run_local.py --arms C --r-values 0,100 --repeats 8 \
    --slots 40 --slot-seconds 30 --skip-nodes
```

Se a diferenca entre R=0 e R=100 continuar pequena num horizonte 10x maior,
a hipotese 1 cai e sobra a 2 ou a 3 -- testaveis do mesmo jeito, sem custo.

## Onde estao os dados brutos

`results/summary.csv` (uma linha por variante x seed), `results/raw/*.csv`
(custo por slot), `results/logs/*.log` (scheduler/cost/workload de cada
execucao, incluindo a linha `CostAware plugin initialized transitionRatio=...`
que confirma o R efetivamente carregado por variante). Nao versionados
(`.gitignore`) -- regenere com os comandos acima.
