#!/usr/bin/env python3
"""
Braco D: Karpenter DE VERDADE (nucleo sigs.k8s.io/karpenter, cloud provider
kwok -- sem AWS, sem credenciais) como comparativo real de "por que nao
Karpenter?" (pergunta 1 da banca, README).

Diferente dos bracos A/B/C (bench/run_local.py): aqui nao ha scheduler
proprio nem pool fixo de 200 nos. O Karpenter parte de ZERO nos por seed e
PROVISIONA sob demanda (NodePool + KWOKNodeClass em deploy/karpenter/), com
o kube-scheduler padrao decidindo o placement entre os nos que ele cria. O
catalogo de instance types (deploy/karpenter/instance-types.json) usa os
MESMOS 4 tipos e precos reais de bench/gen_nodes.py.

Pre-requisito: ./hack/setup-karpenter-kwok.sh (cluster + controller + NodePool
no ar, contexto kind-karpenter-kwok).

Uso:
    python bench/run_karpenter_arm.py --repeats 8 --seed-base 1

    # rapido, 1 seed, horizonte curto:
    python bench/run_karpenter_arm.py --repeats 1 --pods 60 --slots 5 --slot-seconds 10
"""

import argparse
import csv
import os
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from run_local import aggregate, drain, wilcoxon  # noqa: E402

sys.stdout.reconfigure(line_buffering=True)

ARM_LABEL = "D"
NAMESPACE = "bench-d"


def wait_nodes_drained(context, timeout=450):
    """Espera o Karpenter desprovisionar os nos KWOK que sobraram do seed
    anterior (so o control-plane deve restar). O Karpenter processa a fila de
    disrupcao de forma SERIALIZADA (~25s por no, confirmado nos logs -- nao e
    paralelo), entao com ~15 nos ativos isso pode levar minutos. Isto e
    comportamento real do Karpenter, nao um artefato do harness; vale citar
    no texto se a campanha completa demorar mais que os outros bracos por
    causa disso. Sem isso, o proximo seed
    comecaria com nos 'de graca' de uma execucao passada."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(
            ["kubectl", "--context", context, "get", "nodes", "--no-headers"],
            capture_output=True, text=True, check=False,
        )
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        kwok_nodes = [ln for ln in lines if "control-plane" not in ln]
        if not kwok_nodes:
            return True
        time.sleep(3)
    print(f"  AVISO: {len(kwok_nodes)} no(s) KWOK nao desprovisionaram em {timeout}s "
          f"(consolidateAfter pode precisar de mais tempo)", file=sys.stderr)
    return False


def run_one(seed, args, raw_dir, log_dir):
    duration = args.slots * args.slot_seconds
    run_csv = os.path.join(raw_dir, f"run-{ARM_LABEL}-s{seed}.csv")
    arr_csv = os.path.join(raw_dir, f"arrivals-{ARM_LABEL}-s{seed}.csv")
    py = sys.executable

    print(f"  [D seed={seed}] pods={args.pods} slots={args.slots}x{args.slot_seconds}s "
          f"(Karpenter provisiona do zero)")
    drain([NAMESPACE], context=args.context)
    wait_nodes_drained(args.context)

    cost = subprocess.Popen(
        [py, "bench/cost.py", "--arm", ARM_LABEL, "--duration", str(duration),
         "--interval", str(args.slot_seconds), "--context", args.context,
         "--out", run_csv],
        cwd=ROOT,
        stderr=open(os.path.join(log_dir, f"cost-{ARM_LABEL}-s{seed}.log"), "w"),
    )
    time.sleep(1)
    wl = subprocess.run(
        [py, "bench/workload.py", "--pods", str(args.pods),
         "--slots", str(args.slots), "--slot-seconds", str(args.slot_seconds),
         "--scheduler-name", "default-scheduler", "--namespace", NAMESPACE,
         "--seed", str(seed), "--cpu-min", str(args.cpu_min),
         "--cpu-max", str(args.cpu_max), "--lifetime-slots", str(args.lifetime_slots),
         "--context", args.context, "--out", arr_csv],
        cwd=ROOT,
        stderr=open(os.path.join(log_dir, f"workload-{ARM_LABEL}-s{seed}.log"), "w"),
    )
    if wl.returncode != 0:
        cost.kill()
        raise RuntimeError(f"workload.py falhou (seed {seed}); veja o log")
    try:
        cost.wait(timeout=duration + 60)
    except subprocess.TimeoutExpired:
        cost.kill()

    drain([NAMESPACE], context=args.context)
    wait_nodes_drained(args.context)
    return aggregate(run_csv, ARM_LABEL)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=8)
    ap.add_argument("--seed-base", type=int, default=1)
    ap.add_argument("--context", default="kind-karpenter-kwok")
    ap.add_argument("--out-dir", default="results")
    ap.add_argument("--pods", type=int, default=150)
    ap.add_argument("--slots", type=int, default=10)
    ap.add_argument("--slot-seconds", type=int, default=18)
    ap.add_argument("--cpu-min", type=int, default=100)
    ap.add_argument("--cpu-max", type=int, default=1000)
    ap.add_argument("--lifetime-slots", type=int, default=6)
    args = ap.parse_args()

    out_dir = os.path.join(ROOT, args.out_dir)
    raw_dir = os.path.join(out_dir, "raw")
    log_dir = os.path.join(out_dir, "logs")
    for d in (raw_dir, log_dir):
        os.makedirs(d, exist_ok=True)

    r = subprocess.run(["kubectl", "--context", args.context, "get", "nodepool", "default"],
                        capture_output=True, check=False)
    if r.returncode != 0:
        sys.exit(f"NodePool 'default' nao encontrado no contexto {args.context} -- "
                  f"rode ./hack/setup-karpenter-kwok.sh primeiro")

    seeds = [args.seed_base + i for i in range(args.repeats)]
    results = []
    summary_path = os.path.join(out_dir, "summary-D.csv")
    with open(summary_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["arm", "seed", "total_cost_usd", "mean_slot_usd",
                    "sd_slot_usd", "activations", "active_nodes_mean",
                    "pending_max"])
        for seed in seeds:
            try:
                agg = run_one(seed, args, raw_dir, log_dir)
            except Exception as e:  # noqa: BLE001
                print(f"  ERRO seed {seed}: {e}", file=sys.stderr)
                continue
            agg["seed"] = seed
            results.append(agg)
            w.writerow([ARM_LABEL, seed, f"{agg['total_cost_usd']:.6f}",
                        f"{agg['mean_slot_usd']:.6f}", f"{agg['sd_slot_usd']:.6f}",
                        agg["activations"], f"{agg['active_nodes_mean']:.2f}",
                        agg["pending_max"]])
            fh.flush()

    if not results:
        sys.exit("nenhuma execucao completou")

    tc = [r["total_cost_usd"] for r in results]
    ac = [r["activations"] for r in results]
    nn = [r["active_nodes_mean"] for r in results]
    pm = max(r["pending_max"] for r in results)
    sd = statistics.pstdev(tc) if len(tc) > 1 else 0.0
    print("\n================ RESUMO (braco D, Karpenter) ================")
    print(f"n={len(results)}  custo_total$={statistics.mean(tc):.4f} +-{sd:.4f}  "
          f"ativacoes={statistics.mean(ac):.1f}  nos_ativos={statistics.mean(nn):.1f}  "
          f"pend_max={pm}")

    # cross-referencia com B/C-R10 se ja existirem em results/summary.csv
    # (da campanha bench/run_local.py, MESMOS defaults de pods/slots/slot-seconds)
    combined_path = os.path.join(out_dir, "summary.csv")
    if os.path.exists(combined_path):
        with open(combined_path) as fh:
            other = {}
            for row in csv.DictReader(fh):
                other.setdefault(row["arm"], {})[int(row["seed"])] = float(row["total_cost_usd"])
        by_seed_d = {r["seed"]: r["total_cost_usd"] for r in results}
        for baseline in ("B", "C-R10", "C"):
            if baseline not in other:
                continue
            common = sorted(set(by_seed_d) & set(other[baseline]))
            if len(common) < 2:
                continue
            d_vals = [by_seed_d[s] for s in common]
            b_vals = [other[baseline][s] for s in common]
            econ = 100 * (1 - statistics.mean(d_vals) / statistics.mean(b_vals))
            res = wilcoxon(d_vals, b_vals)
            print(f"\nD vs {baseline}, n={len(common)} seeds pareadas "
                  f"(cross-referenciado de {combined_path}, confira que os "
                  f"parametros de carga batem):")
            print(f"  D custa {econ:+.1f}% em relacao a {baseline} "
                  f"(negativo = D mais barato)")
            print(f"  Wilcoxon: W={res['W']:.1f}  p={res['p']:.4f}  ({res['method']})")

    print(f"\nsummary : {summary_path}")
    print(f"brutos  : {raw_dir}/  |  logs : {log_dir}/")


if __name__ == "__main__":
    main()
