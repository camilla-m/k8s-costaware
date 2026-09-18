#!/usr/bin/env python3
"""
Gera o catalogo de instance types (JSON) que o Karpenter kwok cloud provider
le via INSTANCE_TYPES_FILE_PATH -- para que o Karpenter (de verdade, nucleo
sigs.k8s.io/karpenter, sem AWS) escolha entre os MESMOS tipos de no, aos
MESMOS precos reais, que os bracos A/B/C (bench/gen_nodes.py, mesma fonte:
docs/pricing/aws-on-demand-*.json).

Isto NAO restringe o Karpenter aos precos do trap/stable escolhidos por voce
-- ele recebe os dois capacity types (on-demand e spot, spot = on-demand x
0.35, a mesma aproximacao documentada em pkg/costaware/pricing.go) para TODOS
os quatro tipos, e escolhe pelo proprio algoritmo. E um comparativo mais justo
que restringir as opcoes dele.

Uso:
    python bench/gen_karpenter_instance_types.py --out deploy/karpenter/instance-types.json
"""

import argparse
import glob
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SPOT_DISCOUNT = 0.35  # mesma aproximacao de pkg/costaware/pricing.go:priceFor

# (instance_type, cpu, mem_gi) -- os mesmos 4 tipos de bench/gen_nodes.py
INSTANCE_SHAPES = [
    ("g4dn.xlarge", 4, 16),
    ("c5.2xlarge", 8, 16),
    ("m5.2xlarge", 8, 32),
    ("r5.xlarge", 4, 32),
]

ZONE = "test-zone-a"


def latest_price_snapshot():
    candidates = sorted(glob.glob(os.path.join(ROOT, "docs/pricing/aws-on-demand-*.json")))
    if not candidates:
        raise SystemExit(
            "nenhum snapshot em docs/pricing/ -- rode bench/fetch_aws_prices.py primeiro"
        )
    return candidates[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prices", default=None, help="snapshot de precos (default: o mais recente em docs/pricing/)")
    ap.add_argument("--out", default="deploy/karpenter/instance-types.json")
    args = ap.parse_args()

    price_file = args.prices or latest_price_snapshot()
    with open(price_file) as fh:
        snapshot = json.load(fh)
    prices = snapshot["prices_usd_per_hour"]

    def offering(capacity_type, price):
        # Schema real do KWOKOffering (kwok/cloudprovider/helpers.go): cada
        # offering carrega Requirements ([]corev1.NodeSelectorRequirement) com
        # a chave de capacity-type e de zona -- NAO campos soltos
        # "capacityType"/"zone" (o exemplo do proprio repo,
        # kwok/examples/instance_types.json, esta desatualizado nesse ponto;
        # confirmado lendo o parser em helpers.go:ConstructInstanceTypes).
        return {
            "Requirements": [
                {"key": "karpenter.sh/capacity-type", "operator": "In", "values": [capacity_type]},
                {"key": "topology.kubernetes.io/zone", "operator": "In", "values": [ZONE]},
            ],
            "Price": price,
        }

    types = []
    for name, cpu, mem_gi in INSTANCE_SHAPES:
        on_demand = prices[name]
        spot = round(on_demand * SPOT_DISCOUNT, 6)
        types.append({
            "name": name,
            "offerings": [
                offering("on-demand", on_demand),
                offering("spot", spot),
            ],
            "architecture": "amd64",
            "operatingSystems": ["linux"],
            "resources": {
                "cpu": str(cpu),
                "memory": f"{mem_gi}Gi",
            },
        })

    out_path = os.path.join(ROOT, args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump(types, fh, indent=2)
        fh.write("\n")

    print(f"fonte de precos : {price_file}")
    print(f"escrito         : {out_path}")
    for t in types:
        od = t["offerings"][0]["Price"]
        sp = t["offerings"][1]["Price"]
        print(f"  {t['name']:<14} on-demand=${od:<8} spot=${sp}")


if __name__ == "__main__":
    main()
