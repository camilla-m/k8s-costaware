#!/usr/bin/env python3
"""
Busca precos on-demand REAIS de EC2 na AWS Price List Bulk API -- publica,
sem credenciais, sem conta AWS. Responde a pergunta nº2 da banca ("de onde
vem alpha?") com uma fonte citavel: URL, regiao e data de coleta.

O arquivo por regiao tem ~400-500 MB (todo SO x tenancy x licenca), entao isto
baixa uma vez para um cache local e filtra por streaming (ijson), sem carregar
tudo em memoria.

NAO cobre spot: a AWS nao publica preco de spot no Price List API (isso exige
DescribeSpotPriceHistory, que pede credenciais de uma conta real). O desconto
de spot usado no resto do repo (`karpenter.sh/capacity-type: spot` -> x0.35 em
pkg/costaware/pricing.go) continua sendo uma aproximacao documentada, nao um
numero medido -- deixe isso explicito em qualquer texto que cite estes dados.

Uso:
    python bench/fetch_aws_prices.py --region us-east-1 --out docs/pricing

    # tipos de instancia customizados:
    python bench/fetch_aws_prices.py --instance-types m5.large,c5.xlarge
"""

import argparse
import datetime
import json
import os
import sys
import urllib.request

import ijson

PRICING_INDEX = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/region_index.json"

# uniao dos instance types usados em pkg/costaware/pricing.go e bench/gen_nodes.py
DEFAULT_TYPES = [
    "m5.large", "m5.xlarge", "m5.2xlarge", "m5.4xlarge",
    "m6i.large", "m6i.xlarge", "m6i.2xlarge",
    "c5.large", "c5.xlarge", "c5.2xlarge", "c6i.4xlarge",
    "r5.large", "r5.xlarge", "r5.2xlarge",
    "g4dn.xlarge", "g5.xlarge", "p3.2xlarge", "p4d.24xlarge",
]


def region_offer_url(region):
    with urllib.request.urlopen(PRICING_INDEX) as r:
        idx = json.load(r)
    entry = idx["regions"].get(region)
    if not entry:
        raise SystemExit(f"regiao desconhecida no Price List API: {region}")
    return "https://pricing.us-east-1.amazonaws.com" + entry["currentVersionUrl"]


def download(url, dest):
    print(f">>> baixando {url}", file=sys.stderr)
    urllib.request.urlretrieve(url, dest)
    print(f"    salvo em {dest} ({os.path.getsize(dest) / 1e6:.0f} MB)", file=sys.stderr)


def extract(path, targets):
    targets = set(targets)
    print(">>> passo 1/2: varrendo 'products' por SKU", file=sys.stderr)
    sku_to_type = {}
    with open(path, "rb") as f:
        for sku, prod in ijson.kvitems(f, "products"):
            a = prod.get("attributes", {})
            it = a.get("instanceType")
            if it not in targets:
                continue
            if (a.get("operatingSystem"), a.get("tenancy"), a.get("preInstalledSw"),
                    a.get("capacitystatus")) != ("Linux", "Shared", "NA", "Used"):
                continue
            if a.get("licenseModel") not in (None, "No License required"):
                continue
            sku_to_type[sku] = it
    print(f"    {len(sku_to_type)} SKUs candidatos para {len(targets)} tipos", file=sys.stderr)

    print(">>> passo 2/2: varrendo 'terms.OnDemand' pelos SKUs encontrados", file=sys.stderr)
    result = {}
    with open(path, "rb") as f:
        for sku, term in ijson.kvitems(f, "terms.OnDemand"):
            if sku not in sku_to_type:
                continue
            it = sku_to_type[sku]
            for offer in term.values():
                for pd in offer.get("priceDimensions", {}).values():
                    if pd.get("unit") != "Hrs":
                        continue
                    usd = pd.get("pricePerUnit", {}).get("USD")
                    if usd is None:
                        continue
                    price = float(usd)
                    if price > 0 and (it not in result or price < result[it]):
                        result[it] = price

    missing = targets - set(result)
    if missing:
        print(f"AVISO: sem preco para {sorted(missing)}", file=sys.stderr)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--instance-types", default=",".join(DEFAULT_TYPES))
    ap.add_argument("--cache", default="/tmp/aws-ec2-pricing-cache.json",
                    help="onde guardar o download bruto (~450MB) entre execucoes")
    ap.add_argument("--force-download", action="store_true")
    ap.add_argument("--out", default="docs/pricing",
                    help="diretorio onde salvar o snapshot datado (JSON pequeno)")
    args = ap.parse_args()

    targets = [t.strip() for t in args.instance_types.split(",") if t.strip()]

    if args.force_download or not os.path.exists(args.cache):
        url = region_offer_url(args.region)
        download(url, args.cache)
    else:
        print(f">>> usando cache existente: {args.cache}", file=sys.stderr)

    prices = extract(args.cache, targets)

    date = datetime.date.today().isoformat()
    snapshot = {
        "source": "AWS Price List Bulk API (publica, sem credenciais)",
        "region": args.region,
        "retrieved": date,
        "unit": "USD/hora, On-Demand, Linux, tenancy=Shared",
        "note": (
            "Nao inclui spot -- a AWS nao publica spot no Price List API "
            "(precisa de DescribeSpotPriceHistory com credenciais). O desconto "
            "de spot usado no repo (x0.35) e uma aproximacao documentada."
        ),
        "prices_usd_per_hour": dict(sorted(prices.items())),
    }

    os.makedirs(args.out, exist_ok=True)
    out_path = os.path.join(args.out, f"aws-on-demand-{args.region}-{date}.json")
    with open(out_path, "w") as fh:
        json.dump(snapshot, fh, indent=2, sort_keys=True)
        fh.write("\n")

    print(f"\n{out_path}")
    print(json.dumps(snapshot["prices_usd_per_hour"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
