# Braço D: Karpenter de verdade vs Φ e vs bin-packing cego a preço

Executado em 2026-09-18 nesta máquina, via `make karpenter-setup && make
bench-karpenter REPEATS=8`. Responde à pergunta 1 da banca ("por que não
Karpenter?") com o Karpenter **rodando de verdade**, não um simulador.

## Método

- **Núcleo `sigs.k8s.io/karpenter`** (commit `da15327`, pinado — ver
  `hack/setup-karpenter-kwok.sh`), cloud provider **kwok** (fake, sem AWS,
  sem credenciais, sem custo). Validado manualmente antes da campanha:
  provisiona sob demanda, desprovisiona quando vazio, e a própria decisão de
  consolidação já é cost-aware (`"savings: $X"` nos logs do controller).
- Cluster **separado** (`kind-karpenter-kwok`) do usado pelos braços A/B/C
  (`kind-costaware`) — o Karpenter provisiona nós do zero, não usa um pool
  fixo de 200 nós.
- Catálogo de instance types (`deploy/karpenter/instance-types.json`,
  gerado por `bench/gen_karpenter_instance_types.py`): os **mesmos 4 tipos**
  de `bench/gen_nodes.py`, aos **mesmos preços reais** de
  `docs/pricing/aws-on-demand-us-east-1-2026-09-14.json`, on-demand e spot
  (spot = on-demand × 0,35, a mesma aproximação documentada em
  `pkg/costaware/pricing.go`). O Karpenter escolhe livremente entre os 8
  (4 tipos × 2 capacity types) com o **próprio** algoritmo — não restringido
  a uma classe trap/stable como os outros braços, para não viesar a favor do
  meu plugin.
- `NodePool`: `consolidationPolicy: WhenEmptyOrUnderutilized`,
  `consolidateAfter: 30s` — **não afinado** para favorecer nenhum lado; é o
  mesmo valor validado manualmente antes de rodar a campanha.
- Carga: `bench/workload.py --pods 150 --slots 10 --slot-seconds 18`, seeds
  1–8 — **idênticos** aos braços A/B/C, para permitir comparação pareada por
  seed.

## Resultado

| braço | n | custo total US$ (média ± dp) | ativações | nós ativos (média) |
|---|---|---|---|---|
| B (binpack, baseline forte) | 8 | 0,2318 ± 0,0121 | 34,8 | 28,3 |
| C-R10 (CostAware, Φ) | 8 | 0,1032 ± 0,0036 | 22,0 | 18,3 |
| **D (Karpenter real)** | 8 | **0,0711 ± 0,0044** | **16,2** | **13,4** |

**D vs B** — Wilcoxon exato (scipy), n=8 seeds pareadas: W=0, p=0,0078. D
custa **69,3% menos** que B. D venceu nas 8 de 8 seeds.

**D vs C-R10** — mesmo teste: W=0, p=0,0078. D custa **31,1% menos** que
C-R10 nesta rodada. **Mas leia a ressalva abaixo antes de usar este número.**

## A ressalva que importa: contaminação por concorrência de recursos

B e C-R10 acima foram **re-executados** nesta sessão (os CSVs por seed da
campanha original de `docs/sensitivity-sweep.md` foram apagados sem querer
num `rm -rf` anterior) — e essa re-execução rodou com **os dois clusters
kind simultaneamente** (`costaware` e `karpenter-kwok`), disputando os
mesmos 4 CPUs/8GB do colima. A campanha original de `sensitivity-sweep.md`
rodou com **só** o `costaware` no ar.

Os números batem essa suspeita:

| | C-R10 original (só 1 cluster) | C-R10 re-executado (2 clusters) |
|---|---|---|
| custo médio | US$ 0,0734 ± 0,0027 | US$ 0,1032 ± 0,0036 |
| ativações | 17,2 | 22,0 |
| nós ativos | 13,3 | 18,3 |

**+40% de custo** entre duas execuções nominalmente idênticas do mesmo braço
C-R10. B também subiu, mas bem menos (US$ 0,2149 → 0,2318, +8%). A hipótese
mais provável: sob disputa de CPU, o scheduler custom (que roda como
processo Go concorrente com o resto) sofre mais atraso do que o `kwok`
fake-kubelet ou o Karpenter (que rodam como pods no cluster, com prioridade
de agendamento diferente) — os pods do driver de carga acabam se espalhando
por mais nós antes do scheduler convergir.

**Coincidência reveladora**: os números de ativação/nós ativos de D (16,2 /
13,4) ficam muito mais próximos do C-R10 **original, não contaminado**
(17,2 / 13,3) do que do C-R10 re-executado (22,0 / 18,3). Se o C-R10 limpo
for o número certo, **D e C-R10 estão praticamente empatados** — a diferença
de custo cairia de 31,1% para uns **3%**, dentro do ruído entre seeds de
qualquer um dos dois braços isoladamente.

**Conclusão honesta**: D é robustamente e substancialmente mais barato que
B (a diferença sobrevive a qualquer uma das duas medições de B, 66,9% ou
69,3%). **D vs C-R10 é inconclusivo com os dados que existem hoje** — pode
ser uma vitória real de ~31% do Karpenter sobre Φ, ou pode ser um empate
técnico mascarado por contenção de CPU. Não dá pra saber qual sem um
re-run limpo.

## Próximo passo de graça (sem nuvem)

Re-rodar B e C-R10 com **só** o cluster `costaware` no ar (`kind delete
cluster --name karpenter-kwok` antes, `make karpenter-setup` depois de
terminar) — ou, melhor, aumentar os recursos do colima
(`colima stop && colima start --cpus 6 --memory 12`, se a máquina aguentar)
para rodar os dois clusters sem disputa. Depois comparar D contra esse
C-R10 limpo. Isso é só tempo de máquina, não custa nada.

## O outro achado: desprovisionamento serializado

Confirmado nos logs do controller do Karpenter, consistente ao longo de
toda a campanha (exemplo, 2026-09-18 05:48–05:50 UTC):

```
05:48:10.910  disrupting node(s) ... decision=delete
05:48:35.864  disrupting node(s) ... decision=delete   (+24,95s)
05:49:00.807  disrupting node(s) ... decision=delete   (+24,95s)
05:49:25.728  disrupting node(s) ... decision=delete   (+24,92s)
05:49:50.676  disrupting node(s) ... decision=delete   (+24,95s)
05:50:15.622  disrupting node(s) ... decision=delete   (+24,94s)
```

O Karpenter desprovisiona nós vazios **um de cada vez, a cada ~25s** — não
em paralelo, mesmo quando vários nós estão simultaneamente ociosos e
elegíveis. Isso não é um artefato do harness (medido de forma consistente
em todos os seeds da campanha) — é uma característica real e deliberada do
Karpenter (rate-limit para evitar disrupção em massa).

Isto é **evidência empírica direta**, não especulação, do argumento central
da tese: *"placement e provisioning são decididos por componentes
desacoplados e reativos"*. Um controlador que resolve a decisão conjunta
(placement + provisioning, com horizonte temporal) não está sujeito a essa
latência de fila — ele decide de uma vez. Quanto essa latência custa em USD
num cenário real de scale-down é uma pergunta em aberto (depende de quantos
nós ficam ociosos simultaneamente), mas o mecanismo está documentado e
medido, não assumido.

## Onde estão os dados brutos

`results/summary-D.csv` (braço D), `results/summary.csv` (B e C-R10
re-executados), `results/raw/*.csv`, `results/logs/*.log`. Não versionados
(`.gitignore`) — regenere com os comandos do topo deste documento.
