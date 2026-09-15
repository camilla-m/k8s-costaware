#!/usr/bin/env python3
"""
Gera nos falsos KWOK com heterogeneidade de preco e de penalidade de startup.

O ponto do experimento e criar exatamente a condicao do Teorema 2 (Startup-Cost
Heterogeneity): existe um par de nos que podem servir a mesma carga, mas com
delta muito diferente. Sem isso, Phi degenera para bin-packing e o Arm C empata
com o Arm B -- o que, alias, e um resultado valido e deve ser reportado.

Duas classes, herdadas da parametrizacao do artigo:
  trap    -> barato por hora, mas delta alto  (ex.: GPU spot, imagem gigante)
  stable  -> caro por hora, mas delta baixo   (ex.: CPU on-demand quente)

Uso:
    python gen_nodes.py --nodes 200 --trap-fraction 0.5 --out nodes.yaml
    kubectl apply -f nodes.yaml
"""

import argparse
import random
import sys

import yaml

NODE_TEMPLATE = {
    "apiVersion": "v1",
    "kind": "Node",
    "metadata": {
        "annotations": {
            "node.alpha.kubernetes.io/ttl": "0",
            "kwok.x-k8s.io/node": "fake",
        },
        "labels": {
            "beta.kubernetes.io/arch": "amd64",
            "beta.kubernetes.io/os": "linux",
            "kubernetes.io/arch": "amd64",
            "kubernetes.io/os": "linux",
            "kubernetes.io/role": "agent",
            "type": "kwok",
        },
    },
    "spec": {
        "taints": [
            {
                "effect": "NoSchedule",
                "key": "kwok.x-k8s.io/node",
                "value": "fake",
            }
        ]
    },
    "status": {
        "allocatable": {"cpu": "32", "memory": "128Gi", "pods": "110"},
        "capacity": {"cpu": "32", "memory": "128Gi", "pods": "110"},
        "nodeInfo": {
            "architecture": "amd64",
            "containerRuntimeVersion": "kwok",
            "kubeletVersion": "kwok",
            "operatingSystem": "linux",
        },
        "phase": "Running",
        "conditions": [
            {"type": "Ready", "status": "True", "reason": "KubeletReady"},
        ],
    },
}

# (instance_type, cpu, mem_gi, hourly_usd, boot_seconds)
#
# hourly_usd: STABLE_CLASSES sao preco On-Demand real (us-east-1, ver
# docs/pricing/aws-on-demand-us-east-1-2026-09-14.json, obtido via
# bench/fetch_aws_prices.py, sem credenciais). TRAP_CLASSES sao esse mesmo
# preco On-Demand x 0.35 (o desconto de spot documentado em
# pkg/costaware/pricing.go) -- a AWS nao publica spot no Price List API, entao
# isto e uma aproximacao, nao uma medicao.
TRAP_CLASSES = [
    ("g4dn.xlarge", 4, 16, 0.184, 600),      # 0.526 on-demand x 0.35 spot, imagem gigante
    ("c5.2xlarge", 8, 16, 0.119, 480),       # 0.340 on-demand x 0.35 spot
]
STABLE_CLASSES = [
    ("m5.2xlarge", 8, 32, 0.384, 150),       # on-demand
    ("r5.xlarge", 4, 32, 0.252, 150),        # on-demand
]


def make_node(idx: int, cls, capacity_type: str, ratio: float):
    itype, cpu, mem, price, boot = cls
    node = yaml.safe_load(yaml.safe_dump(NODE_TEMPLATE))  # deep copy
    node["metadata"]["name"] = f"kwok-node-{idx:04d}"
    node["metadata"]["labels"]["node.kubernetes.io/instance-type"] = itype
    node["metadata"]["labels"]["karpenter.sh/capacity-type"] = capacity_type
    node["metadata"]["labels"]["costaware.unirio.br/class"] = (
        "trap" if capacity_type == "spot" else "stable"
    )
    # delta escalado pelo ratio R do estudo de sensibilidade
    node["metadata"]["annotations"]["costaware.unirio.br/hourly-usd"] = f"{price:.4f}"
    node["metadata"]["annotations"]["costaware.unirio.br/boot-seconds"] = (
        f"{boot * ratio:.1f}"
    )
    node["status"]["allocatable"]["cpu"] = str(cpu)
    node["status"]["capacity"]["cpu"] = str(cpu)
    node["status"]["allocatable"]["memory"] = f"{mem}Gi"
    node["status"]["capacity"]["memory"] = f"{mem}Gi"
    return node


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes", type=int, default=200)
    ap.add_argument("--trap-fraction", type=float, default=0.5)
    ap.add_argument(
        "--ratio",
        type=float,
        default=1.0,
        help="multiplicador de boot-seconds; reproduz o parametro R",
    )
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="-")
    args = ap.parse_args()

    random.seed(args.seed)
    docs = []
    for i in range(args.nodes):
        if random.random() < args.trap_fraction:
            cls = random.choice(TRAP_CLASSES)
            docs.append(make_node(i, cls, "spot", args.ratio))
        else:
            cls = random.choice(STABLE_CLASSES)
            docs.append(make_node(i, cls, "on-demand", args.ratio))

    out = yaml.safe_dump_all(docs, sort_keys=False)
    if args.out == "-":
        sys.stdout.write(out)
    else:
        with open(args.out, "w") as fh:
            fh.write(out)
        print(f"wrote {args.nodes} nodes to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
