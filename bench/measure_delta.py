#!/usr/bin/env python3
"""
Mede empiricamente as fases que compoem delta, em um cluster real com
provisionamento dinamico (Karpenter ou managed node group com scale-from-zero).

Metodo: cria um pod que EXIGE um tipo de instancia sem capacidade disponivel,
forcando o provisionamento de um no novo. Depois reconstroi a linha do tempo a
partir do objeto Node e dos Events do pod.

Fases medidas (ver docs/measuring-delta.md):
    decisao          pod criado          -> objeto Node aparece
    provisionamento  Node aparece        -> condicao Ready = True
    pull             evento Pulling      -> evento Pulled
    inicializacao    Pulled              -> pod Running
    total            pod criado          -> pod Running

ATENCAO: este script GASTA DINHEIRO. Ele provisiona e destroi maquinas reais.
Use --dry-run primeiro e confira o console do provedor ao terminar.

Uso:
    python bench/measure_delta.py --kubeconfig ~/.kube/config \
        --instance-types m5.2xlarge,g4dn.xlarge \
        --images registry.k8s.io/pause:3.9 \
        --repetitions 10 --out results/delta.csv
"""

import argparse
import csv
import datetime as dt
import statistics
import sys
import time
import uuid

from kubernetes import client, config
from kubernetes.client.rest import ApiException

NAMESPACE = "delta-measure"


def now():
    return dt.datetime.now(dt.timezone.utc)


def delta_seconds(a, b):
    if a is None or b is None:
        return None
    return round((b - a).total_seconds(), 2)


def ensure_namespace(v1):
    try:
        v1.create_namespace(
            client.V1Namespace(metadata=client.V1ObjectMeta(name=NAMESPACE))
        )
    except ApiException as e:
        if e.status != 409:
            raise


def build_probe_pod(name, instance_type, image, cpu_milli=1000):
    """
    Pod que so pode rodar no tipo de instancia alvo. Como nao ha no desse tipo
    no cluster, o autoscaler e obrigado a provisionar um.

    imagePullPolicy=Always e obrigatorio: sem isso, um no que ja tenha a imagem
    em cache responde em segundos e a fase de pull mede zero.
    """
    return client.V1Pod(
        metadata=client.V1ObjectMeta(
            name=name, namespace=NAMESPACE, labels={"role": "delta-probe"}
        ),
        spec=client.V1PodSpec(
            restart_policy="Never",
            termination_grace_period_seconds=0,
            node_selector={"node.kubernetes.io/instance-type": instance_type},
            containers=[
                client.V1Container(
                    name="probe",
                    image=image,
                    image_pull_policy="Always",
                    resources=client.V1ResourceRequirements(
                        requests={"cpu": f"{cpu_milli}m"},
                        limits={"cpu": f"{cpu_milli}m"},
                    ),
                )
            ],
        ),
    )


def pod_events(v1, pod_name):
    """Eventos do pod, ordenados. Usados para localizar Pulling/Pulled."""
    evts = v1.list_namespaced_event(
        NAMESPACE, field_selector=f"involvedObject.name={pod_name}"
    ).items
    out = []
    for e in evts:
        ts = e.event_time or e.last_timestamp or e.first_timestamp
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        out.append((ts, e.reason, e.message))
    return sorted(out, key=lambda x: x[0])


def node_ready_time(v1, node_name):
    try:
        node = v1.read_node(node_name)
    except ApiException:
        return None, None
    created = node.metadata.creation_timestamp
    if created and created.tzinfo is None:
        created = created.replace(tzinfo=dt.timezone.utc)
    ready = None
    for c in node.status.conditions or []:
        if c.type == "Ready" and c.status == "True":
            ready = c.last_transition_time
            if ready and ready.tzinfo is None:
                ready = ready.replace(tzinfo=dt.timezone.utc)
    return created, ready


def run_one(v1, instance_type, image, timeout, poll=3):
    name = f"probe-{uuid.uuid4().hex[:8]}"
    t_created = now()

    v1.create_namespaced_pod(NAMESPACE, build_probe_pod(name, instance_type, image))

    node_name = None
    t_running = None
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            pod = v1.read_namespaced_pod(name, NAMESPACE)
        except ApiException as e:
            if e.status == 404:
                time.sleep(poll)
                continue
            raise

        if pod.spec.node_name and node_name is None:
            node_name = pod.spec.node_name

        if pod.status.phase == "Running":
            t_running = now()
            break
        if pod.status.phase == "Failed":
            print(f"    pod falhou: {pod.status.reason}", file=sys.stderr)
            break

        time.sleep(poll)

    # linha do tempo
    t_node_created, t_ready = node_ready_time(v1, node_name) if node_name else (None, None)

    t_pulling = t_pulled = None
    cache_was_warm = True
    for ts, reason, _ in pod_events(v1, name):
        if reason == "Pulling" and t_pulling is None:
            t_pulling = ts
            cache_was_warm = False
        elif reason == "Pulled" and t_pulled is None:
            t_pulled = ts

    row = {
        "instance_type": instance_type,
        "image": image,
        "pod": name,
        "node": node_name or "",
        "timed_out": t_running is None,
        "cache_was_warm": cache_was_warm,
        "decisao_s": delta_seconds(t_created, t_node_created),
        "provisionamento_s": delta_seconds(t_node_created, t_ready),
        "pull_s": delta_seconds(t_pulling, t_pulled),
        "inicializacao_s": delta_seconds(t_pulled, t_running),
        "total_s": delta_seconds(t_created, t_running),
        "ts": t_created.isoformat(),
    }

    # limpeza: destruir o pod libera o no para o autoscaler remove-lo, o que e
    # essencial para garantir cache frio na proxima repeticao.
    try:
        v1.delete_namespaced_pod(name, NAMESPACE, grace_period_seconds=0)
    except ApiException:
        pass

    return row


def summarize(rows):
    """Mediana e IQR por (tipo, imagem). Media enganaria: a cauda e longa."""
    groups = {}
    for r in rows:
        if r["timed_out"] or r["total_s"] is None:
            continue
        groups.setdefault((r["instance_type"], r["image"]), []).append(r)

    print("\n=== RESUMO (mediana [IQR]) ===", file=sys.stderr)
    for (itype, image), rs in sorted(groups.items()):
        print(f"\n{itype}  |  {image}  (n={len(rs)})", file=sys.stderr)
        for phase in ("decisao_s", "provisionamento_s", "pull_s", "total_s"):
            vals = sorted(v for r in rs if (v := r[phase]) is not None)
            if not vals:
                continue
            med = statistics.median(vals)
            if len(vals) >= 4:
                q1 = statistics.median(vals[: len(vals) // 2])
                q3 = statistics.median(vals[(len(vals) + 1) // 2 :])
                print(f"  {phase:20s} {med:8.1f}s  [{q1:.1f} – {q3:.1f}]", file=sys.stderr)
            else:
                print(f"  {phase:20s} {med:8.1f}s  (n<4, sem IQR)", file=sys.stderr)

        warm = sum(1 for r in rs if r["cache_was_warm"])
        if warm:
            print(
                f"  AVISO: {warm}/{len(rs)} amostras com cache QUENTE "
                "(sem evento Pulling). Descarte-as: o no foi reutilizado.",
                file=sys.stderr,
            )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kubeconfig", required=True)
    ap.add_argument("--instance-types", required=True, help="lista separada por virgula")
    ap.add_argument("--images", default="registry.k8s.io/pause:3.9")
    ap.add_argument("--repetitions", type=int, default=10)
    ap.add_argument("--max-lifetime", type=int, default=900,
                    help="timeout por repeticao, em segundos; protege contra no orfao")
    ap.add_argument("--out", required=True)
    ap.add_argument("--dry-run", action="store_true",
                    help="lista o que seria executado e estima o custo, sem provisionar")
    args = ap.parse_args()

    types = [t.strip() for t in args.instance_types.split(",") if t.strip()]
    images = [i.strip() for i in args.images.split(",") if i.strip()]
    total_runs = len(types) * len(images) * args.repetitions

    print(f"{total_runs} provisionamentos: "
          f"{len(types)} tipos × {len(images)} imagens × {args.repetitions} repeticoes",
          file=sys.stderr)
    print(f"tempo maximo estimado: {total_runs * args.max_lifetime / 3600:.1f} h",
          file=sys.stderr)

    if args.dry_run:
        for t in types:
            for i in images:
                print(f"  {t:16s} {i}  ×{args.repetitions}", file=sys.stderr)
        print("\n--dry-run: nada foi provisionado.", file=sys.stderr)
        return

    config.load_kube_config(config_file=args.kubeconfig)
    v1 = client.CoreV1Api()
    ensure_namespace(v1)

    rows = []
    n = 0
    for itype in types:
        for image in images:
            for rep in range(args.repetitions):
                n += 1
                print(f"[{n}/{total_runs}] {itype} / {image} rep {rep + 1}",
                      file=sys.stderr)
                try:
                    row = run_one(v1, itype, image, args.max_lifetime)
                except Exception as e:  # noqa: BLE001
                    print(f"    ERRO: {e}", file=sys.stderr)
                    continue
                rows.append(row)
                print(f"    total={row['total_s']}s  "
                      f"provisionamento={row['provisionamento_s']}s  "
                      f"pull={row['pull_s']}s", file=sys.stderr)

                # deixa o autoscaler remover o no antes da proxima repeticao,
                # senao a proxima medicao pega cache quente.
                time.sleep(60)

    if not rows:
        print("nenhuma medicao valida", file=sys.stderr)
        sys.exit(1)

    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    summarize(rows)
    print(f"\nCSV: {args.out}", file=sys.stderr)
    print("CONFIRA O CONSOLE DO PROVEDOR: nenhum no deve ter sobrado ligado.",
          file=sys.stderr)


if __name__ == "__main__":
    main()
