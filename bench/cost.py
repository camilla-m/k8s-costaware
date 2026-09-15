#!/usr/bin/env python3
"""
Contabiliza o custo real de uma execucao do benchmark.

Amostra o cluster a cada intervalo e computa, por slot:
  - custo_ativo:  soma do preco/hora dos nos com >=1 pod de workload
  - nos_ativos:   cardinalidade desse conjunto
  - ativacoes:    nos que estavam vazios no slot anterior e passaram a ter carga
                  (esta e a metrica que materializa delta -- e a que separa
                   o Arm C do Arm B; se ela nao cair, a tese nao se sustenta)
  - pendentes:    pods em Pending (custo de qualidade de servico)

Saida: CSV por slot + um resumo agregado.

Uso:
    python cost.py --arm C --duration 3600 --interval 60 --out run-C.csv
"""

import argparse
import csv
import statistics
import sys
import time

from kubernetes import client, config

PRICE_ANNOTATION = "costaware.unirio.br/hourly-usd"


def is_workload_pod(pod) -> bool:
    """Exclui DaemonSets, mirror pods e pods terminados."""
    meta = pod.metadata
    if meta.annotations and "kubernetes.io/config.mirror" in meta.annotations:
        return False
    for ref in meta.owner_references or []:
        if ref.kind == "DaemonSet":
            return False
    return pod.status.phase in ("Running", "Pending")


def node_price(node) -> float:
    ann = node.metadata.annotations or {}
    if PRICE_ANNOTATION in ann:
        try:
            return float(ann[PRICE_ANNOTATION])
        except ValueError:
            pass
    return 0.0


def sample(v1):
    nodes = {n.metadata.name: n for n in v1.list_node().items}
    pods = v1.list_pod_for_all_namespaces().items

    occupied = set()
    pending = 0
    for p in pods:
        if not is_workload_pod(p):
            continue
        if p.status.phase == "Pending" or not p.spec.node_name:
            pending += 1
            continue
        occupied.add(p.spec.node_name)

    cost = sum(node_price(nodes[n]) for n in occupied if n in nodes)
    return occupied, cost, pending, len(nodes)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True, help="rotulo do braco experimental")
    ap.add_argument("--duration", type=int, default=3600, help="segundos")
    ap.add_argument("--interval", type=int, default=60, help="segundos por slot")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    config.load_kube_config()
    v1 = client.CoreV1Api()

    rows = []
    prev_occupied = set()
    slots = args.duration // args.interval

    for slot in range(slots):
        occupied, cost, pending, total_nodes = sample(v1)
        activations = len(occupied - prev_occupied)
        rows.append(
            {
                "arm": args.arm,
                "slot": slot,
                "ts": time.time(),
                "active_nodes": len(occupied),
                "total_nodes": total_nodes,
                "hourly_cost_usd": round(cost, 4),
                "slot_cost_usd": round(cost * args.interval / 3600.0, 6),
                "activations": activations,
                "pending_pods": pending,
            }
        )
        print(
            f"[{args.arm}] slot {slot:3d}  ativos={len(occupied):4d}  "
            f"$/h={cost:8.3f}  ativacoes={activations:3d}  pendentes={pending}",
            file=sys.stderr,
        )
        prev_occupied = occupied
        time.sleep(args.interval)

    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    costs = [r["slot_cost_usd"] for r in rows]
    total = sum(costs)
    print("\n=== RESUMO ===", file=sys.stderr)
    print(f"custo total do horizonte : USD {total:.4f}", file=sys.stderr)
    print(f"custo medio por slot     : USD {statistics.mean(costs):.6f}", file=sys.stderr)
    print(
        f"desvio padrao por slot   : USD {statistics.pstdev(costs):.6f}  "
        "(estabilidade operacional)",
        file=sys.stderr,
    )
    print(
        f"ativacoes totais         : {sum(r['activations'] for r in rows)}",
        file=sys.stderr,
    )
    print(f"nos ativos (media)       : {statistics.mean(r['active_nodes'] for r in rows):.1f}", file=sys.stderr)


if __name__ == "__main__":
    main()
