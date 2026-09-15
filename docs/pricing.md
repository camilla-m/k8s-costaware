# De onde vem `alpha`

Resposta a pergunta nº2 da banca (README, "Perguntas que a banca vai fazer").

## Fonte

**AWS Price List Bulk API**, publica, **sem necessidade de conta ou
credenciais AWS** — importante porque este projeto foi desenvolvido sem
orcamento de nuvem.

- Endpoint: `https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/<version>/<region>/index.json`
- Regiao: `us-east-1`
- Coletado em: **2026-09-14**
- Filtro: On-Demand, Linux, `tenancy=Shared`, `preInstalledSw=NA`,
  `capacitystatus=Used`, `licenseModel=No License required`
- Snapshot versionado: [`pricing/aws-on-demand-us-east-1-2026-09-14.json`](pricing/aws-on-demand-us-east-1-2026-09-14.json)

Script (`bench/fetch_aws_prices.py`): baixa o arquivo da regiao (~450-500 MB,
uma vez, cacheado em `/tmp`) e extrai por streaming (`ijson`, sem carregar o
JSON inteiro em memoria) os precos dos instance types usados no repo.

```bash
python bench/fetch_aws_prices.py --region us-east-1 --out docs/pricing
```

## O que isto NAO cobre: spot

A AWS **nao publica preco de spot** no Price List API — spot e um mercado
dinamico, e o preco historico so sai da API `DescribeSpotPriceHistory`, que
exige uma conta AWS credenciada. Sem orcamento para manter essa conta ativa,
o repo usa uma **aproximacao documentada**: spot = 0.35 x on-demand
(`karpenter.sh/capacity-type: spot` em `pkg/costaware/pricing.go:priceFor`).
Isto e consistente com a ordem de grandeza tipica de desconto de spot para as
familias usadas aqui (m5/c5/r5/g4dn), mas **e uma suposicao, nao uma medicao**
— diga isso explicitamente na banca se perguntarem.

## Tabela (On-Demand, us-east-1, 2026-09-14)

| instance type | USD/hora | uso no repo |
|---|---|---|
| m5.large | 0.096 | `DefaultPriceTable` |
| m5.xlarge | 0.192 | `DefaultPriceTable` |
| m5.2xlarge | 0.384 | `DefaultPriceTable`, `gen_nodes.py` STABLE |
| m5.4xlarge | 0.768 | `DefaultPriceTable` |
| m6i.large | 0.096 | `DefaultPriceTable` |
| m6i.xlarge | 0.192 | `DefaultPriceTable` |
| m6i.2xlarge | 0.384 | `DefaultPriceTable` |
| c5.large | 0.085 | `DefaultPriceTable` |
| c5.xlarge | 0.170 | `DefaultPriceTable` |
| c5.2xlarge | 0.340 | `DefaultPriceTable`, `gen_nodes.py` TRAP (x0.35 spot) |
| c6i.4xlarge | 0.680 | `DefaultPriceTable` |
| r5.large | 0.126 | `DefaultPriceTable` |
| r5.xlarge | 0.252 | `DefaultPriceTable`, `gen_nodes.py` STABLE |
| r5.2xlarge | 0.504 | `DefaultPriceTable` |
| g4dn.xlarge | 0.526 | `DefaultPriceTable`, `gen_nodes.py` TRAP (x0.35 spot) |
| g5.xlarge | 1.006 | `DefaultPriceTable` |
| p3.2xlarge | 3.060 | `DefaultPriceTable` |
| p4d.24xlarge | **21.957642** | `DefaultPriceTable` |

## O que mudou em relacao a tabela anterior (chutada, sem fonte)

A maioria dos valores ja batia com o preco real (quem escreveu a tabela
original claramente consultou a pagina de precos da AWS em algum momento, so
nao registrou fonte nem data). Duas diferencas reais:

- `m6i.large`/`m6i.xlarge`/`m6i.2xlarge`: ajuste de ~0.001-0.004 USD/h
  (variacao de preco entre a coleta antiga e agora).
- **`p4d.24xlarge`: 32.77 -> 21.957642 USD/h, uma diferenca de ~33%.** Se
  algum resultado antigo usou este instance type num calculo de custo
  absoluto (nao so relativo entre bracos), ele precisa ser refeito.

## Pendente

`delta` (boot-to-`Ready`, pull de imagem) continua **nao medido** — isso exige
um cluster real rodando por um tempo (EKS/GKE), e por enquanto nao ha
orcamento para isso. Ver `docs/measuring-delta.md` para o metodo, pronto para
quando houver credito de nuvem (academico ou nao).
