#!/usr/bin/env python3
"""
Driver de carga para o benchmark.

Reproduz a dinamica de workload do artigo dentro de um cluster real (ou KWOK):
a cada slot t, o numero de pods ativos e |P_t| = floor(|P| * nu_t), com
nu_t ~ U[0.7, 1.3] no modo `uniform`. Pods excedentes sao removidos, pods
faltantes sao criados; pods tambem morrem naturalmente ao fim de seu lifetime.

Modos de chegada:
  uniform  -- perturbacao estocastica +-30% (o envelope usado no FTC/SAND)
  sine     -- onda senoidal diurna sobreposta a ruido
  trace    -- replay de um CSV com colunas (slot, pods_ativos), p.ex. extraido
              dos Alibaba cluster traces

IMPORTANTE: rode este driver e o bench/cost.py com o MESMO --slot-seconds e o
mesmo horizonte, senao a contabilidade fica desalinhada do workload.

Uso:
    python workload.py --pods 1000 --slots 20 --slot-seconds 60 \
        --scheduler-name default-scheduler --seed 42 --out arrivals-C.csv
"""

import argparse
import csv
import math
import random
import sys
import time
import uuid

from kubernetes import client, config
from kubernetes.client.rest import ApiException

KWOK_TOLERATION = client.V1Toleration(
    key="kwok.x-k8s.io/node", operator="Equal", value="fake", effect="NoSchedule"
)


def build_pod(name, cpu_milli, mem_mi, scheduler_name, namespace, gpu=False):
    labels = {"app": "costaware-bench", "bench/class": "gpu" if gpu else "cpu"}

    requests = {"cpu": f"{cpu_milli}m", "memory": f"{mem_mi}Mi"}
    limits = dict(requests)

    node_selector = {"type": "kwok"}
    if gpu:
        # forca a carga pesada para a classe trap, onde delta e alto.
        node_selector["costaware.unirio.br/class"] = "trap"

    return client.V1Pod(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace, labels=labels),
        spec=client.V1PodSpec(
            scheduler_name=scheduler_name,
            node_selector=node_selector,
            tolerations=[KWOK_TOLERATION],
            restart_policy="Never",
            termination_grace_period_seconds=0,
            containers=[
                client.V1Container(
                    name="app",
                    image="registry.k8s.io/pause:3.9",
                    resources=client.V1ResourceRequirements(
                        requests=requests, limits=limits
                    ),
                )
            ],
        ),
    )


def target_for_slot(mode, base, slot, total_slots, rng, trace):
    if mode == "uniform":
        return int(base * rng.uniform(0.7, 1.3))
    if mode == "sine":
        phase = 2 * math.pi * slot / max(total_slots, 1)
        diurnal = 1.0 + 0.3 * math.sin(phase)
        return int(base * diurnal * rng.uniform(0.9, 1.1))
    if mode == "trace":
        return int(trace.get(slot, base))
    raise ValueError(f"modo desconhecido: {mode}")


def load_trace(path):
    out = {}
    with open(path) as fh:
        for row in csv.DictReader(fh):
            out[int(row["slot"])] = int(row["pods_ativos"])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pods", type=int, default=1000, help="|P| nominal")
    ap.add_argument("--slots", type=int, default=20, help="|T|")
    ap.add_argument("--slot-seconds", type=int, default=60, help="duracao de cada slot")
    ap.add_argument("--scheduler-name", default="default-scheduler")
    ap.add_argument("--namespace", default="bench")
    ap.add_argument("--pattern", choices=["uniform", "sine", "trace"], default="uniform")
    ap.add_argument("--trace-file", default=None)
    ap.add_argument("--cpu-min", type=int, default=100, help="milli-cores")
    ap.add_argument("--cpu-max", type=int, default=2000)
    ap.add_argument("--gpu-fraction", type=float, default=0.0)
    ap.add_argument("--lifetime-slots", type=int, default=5, help="vida media de um pod")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="arrivals.csv")
    ap.add_argument("--cleanup", action="store_true", help="apaga todos os pods ao fim")
    ap.add_argument("--context", default=None, help="contexto kubectl (default: o atual)")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    trace = load_trace(args.trace_file) if args.pattern == "trace" else {}

    config.load_kube_config(context=args.context)
    v1 = client.CoreV1Api()

    # namespace dedicado, para que a limpeza seja trivial e o cost.py possa
    # filtrar somente a carga do experimento.
    try:
        v1.create_namespace(
            client.V1Namespace(metadata=client.V1ObjectMeta(name=args.namespace))
        )
    except ApiException as e:
        if e.status != 409:
            raise

    live = {}  # pod_name -> slot de expiracao
    rows = []

    for slot in range(args.slots):
        t0 = time.time()

        # 1. mortes naturais
        expired = [n for n, exp in live.items() if exp <= slot]
        for name in expired:
            try:
                v1.delete_namespaced_pod(
                    name, args.namespace, grace_period_seconds=0
                )
            except ApiException as e:
                if e.status != 404:
                    raise
            live.pop(name, None)

        # 2. ajuste ao alvo do slot
        target = target_for_slot(args.pattern, args.pods, slot, args.slots, rng, trace)
        created = 0
        deleted = len(expired)

        if len(live) > target:
            surplus = rng.sample(list(live.keys()), len(live) - target)
            for name in surplus:
                try:
                    v1.delete_namespaced_pod(
                        name, args.namespace, grace_period_seconds=0
                    )
                except ApiException as e:
                    if e.status != 404:
                        raise
                live.pop(name, None)
                deleted += 1
        else:
            for _ in range(target - len(live)):
                name = f"bench-{uuid.uuid4().hex[:10]}"
                gpu = rng.random() < args.gpu_fraction
                cpu = rng.randint(args.cpu_min, args.cpu_max)
                mem = cpu * 2  # 2 MiB por milli-core, razao fixa e documentada
                pod = build_pod(
                    name, cpu, mem, args.scheduler_name, args.namespace, gpu
                )
                try:
                    v1.create_namespaced_pod(args.namespace, pod)
                except ApiException as e:
                    print(f"falha ao criar {name}: {e.status}", file=sys.stderr)
                    continue
                # lifetime geometrico: media ~ lifetime_slots
                life = max(1, int(rng.expovariate(1.0 / args.lifetime_slots)))
                live[name] = slot + life
                created += 1

        rows.append(
            {
                "slot": slot,
                "target": target,
                "live": len(live),
                "created": created,
                "deleted": deleted,
            }
        )
        print(
            f"slot {slot:3d}  alvo={target:5d}  vivos={len(live):5d}  "
            f"+{created:4d}  -{deleted:4d}",
            file=sys.stderr,
        )

        elapsed = time.time() - t0
        if elapsed < args.slot_seconds:
            time.sleep(args.slot_seconds - elapsed)
        else:
            print(
                f"AVISO: slot {slot} levou {elapsed:.1f}s, mais que "
                f"--slot-seconds={args.slot_seconds}. O driver virou o gargalo; "
                "os resultados de latencia estao contaminados.",
                file=sys.stderr,
            )

    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    if args.cleanup:
        v1.delete_collection_namespaced_pod(args.namespace, grace_period_seconds=0)
        print("pods removidos", file=sys.stderr)


if __name__ == "__main__":
    main()
