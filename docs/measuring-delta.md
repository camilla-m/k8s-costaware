# Medindo `delta`: protocolo experimental

## Por que isto existe

Em todos os artigos anteriores, `delta` foi um parametro **inventado**. No FTC/SAND
ele aparece como `delta = 5*alpha*R` para nos trap e `delta = 10` para nos stable —
numeros escolhidos para produzir o efeito desejado, nao medidos. Uma banca que
perguntar "de onde vem esse 600?" nao tem resposta hoje.

Pior: o resultado central da tese (economia sob alto `R`) e **monotonicamente
crescente em `delta`**. Se `delta` for arbitrario, o resultado tambem e. Toda a
contribuicao empirica repousa sobre este numero.

A boa noticia: **nao existe tabela publicada** de latencia de provisionamento ate
`Ready` por tipo de instancia, nem de tempo de pull de imagem de varios GB em
condicoes de cache frio. Produzir essa tabela e uma contribuicao autonoma,
provavelmente um artigo curto, alem de fechar o buraco metodologico da tese.

---

## Decomposicao de `delta`

`delta` nao e uma coisa so. O tempo entre "decidi usar um no novo" e "a carga
esta servindo" tem quatro fases observaveis, e elas se comportam de forma
muito diferente:

| Fase | Intervalo | O que domina | Ordem de grandeza esperada |
|------|-----------|--------------|----------------------------|
| **decisao** | pod criado → objeto Node/NodeClaim criado | latencia do autoscaler | 1–30 s |
| **provisionamento** | Node criado → `Ready` | boot da VM, join no cluster | 30–180 s |
| **pull** | `Pulling` → `Pulled` | tamanho da imagem, banda do registry | 2 s a 10+ min |
| **inicializacao** | `Pulled` → `Running` | startup da aplicacao | variavel |

Duas consequencias para a modelagem:

1. **Fases 1–2 dependem do tipo de instancia; fase 3 depende da imagem.** Isso
   significa que `delta_i` no modelo e, na verdade, `delta(tipo_i, imagem_j)`. A
   simplificacao para `delta_i` so e defensavel se voce fixar a imagem por classe
   de no — que e exatamente o que a classe `trap` (GPU + imagem de modelo) faz.
   **Diga isso explicitamente na tese.**

2. **A fase de pull e a unica que justifica `R = 100`.** Boot de VM raramente
   passa de 3 minutos; e o pull de uma imagem de 15 GB que produz cold starts de
   10+ minutos. Se a medicao mostrar que o pull nao domina, o regime critico do
   artigo perde sustentacao empirica e voce precisa reportar isso.

---

## Matriz de medicao

Minimo defensavel: **3 tipos × 2 imagens × 10 repeticoes = 60 provisionamentos.**

| Eixo | Niveis |
|------|--------|
| Tipo de instancia | `m5.2xlarge` (CPU pequeno), `c5.4xlarge` (CPU grande), `g4dn.xlarge` (GPU) |
| Imagem | `pause:3.9` (~700 KB, piso) e uma imagem de modelo real (~10–15 GB, teto) |
| Repeticoes | 10, com cache de registry frio a cada uma |
| Provedor | EKS obrigatorio; GKE se o orcamento permitir (fortalece a validade externa) |

Reporte **mediana e IQR**, nao media e desvio. A distribuicao de tempo de boot
tem cauda longa e assimetrica; media engana.

---

## Garantindo cache frio

Este e o ponto onde a medicao mais facilmente se corrompe. Um no que ja puxou a
imagem uma vez responde em segundos, e voce mede zero sem perceber.

- **Sempre destrua o no entre repeticoes.** Nunca reutilize.
- Nao use `imagePullPolicy: IfNotPresent`. Force `Always`.
- Se o cluster tiver um registry mirror ou cache regional, **registre isso** —
  muda a validade externa completamente.
- Verifique nos eventos que houve `Pulling` seguido de `Pulled`. Se so aparecer
  `Pulled`, o cache estava quente e a amostra deve ser descartada.

---

## Custo estimado

Grosseiramente, para 60 provisionamentos com ~10 min de vida cada:

| Tipo | $/h | 20 execucoes × 10 min | Subtotal |
|------|-----|----------------------|----------|
| m5.2xlarge | 0.384 | 3.3 h | ~$1.30 |
| c5.4xlarge | 0.680 | 3.3 h | ~$2.30 |
| g4dn.xlarge | 0.526 | 3.3 h | ~$1.80 |
| Control plane EKS | 0.10 | ~8 h | ~$0.80 |

Total na ordem de **US$ 10–20**, mais transferencia de dados do pull das imagens
grandes. E barato — o risco real nao e financeiro, e esquecer um nodegroup
ligado. Use `--max-lifetime` no script e confira o console ao fim do dia.

---

## Como converter a medicao em `delta` do modelo

O modelo cobra `delta_i` como um custo escalar. A ponte:

```
delta_i = alpha_i * (T_provisionamento + T_pull) / 3600
```

ou seja, o custo da janela em que voce paga pelo no sem receber trabalho util.
No plugin, `pkg/costaware/pricing.go` guarda `BootSeconds` e faz exatamente essa
amortizacao em `computePhi`.

Isso da uma interpretacao **fisica** para `R`: `R = 1` significa cobrar
literalmente a janela ociosa. `R > 1` significa que voce atribui ao cold start um
custo maior que o aluguel — o que se justifica por violacao de SLO, nao por
dinheiro. **Deixe essa distincao explicita na tese**, porque `R = 100` nao e
"cem vezes mais caro em dolares", e sim uma penalidade de qualidade de servico.

---

## Ameacas a validade

- **Uma unica regiao, um unico dia.** Latencia de provisionamento varia com a
  pressao de capacidade da regiao. Repita em dois dias diferentes.
- **Spot vs on-demand.** Spot pode ter latencia de alocacao muito maior sob
  contencao, ou falhar. Meca separadamente; nao agregue.
- **Warm pools.** EKS e GKE oferecem pools pre-aquecidos que reduzem drasticamente
  a fase 2. Se estiverem ligados, sua medicao subestima `delta` para quem nao os
  usa — e se estiverem desligados, superestima para quem os usa. Registre a
  configuracao.
- **Tamanho da amostra.** 10 repeticoes dao um IC largo para uma distribuicao de
  cauda longa. Se o IQR sair grande, aumente para 20 antes de publicar.

---

## Execucao

```bash
python bench/measure_delta.py \
    --kubeconfig ~/.kube/config \
    --instance-types m5.2xlarge,c5.4xlarge,g4dn.xlarge \
    --images registry.k8s.io/pause:3.9,SEU_REGISTRY/modelo:latest \
    --repetitions 10 \
    --max-lifetime 900 \
    --out results/delta-eks-us-east-1.csv
```

O resultado alimenta `DefaultPriceTable()` em `pkg/costaware/pricing.go`,
substituindo os placeholders. **Registre a data e a regiao no proprio codigo** —
precos e latencias mudam, e um revisor vai perguntar.
