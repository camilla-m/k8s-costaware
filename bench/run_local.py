#!/usr/bin/env python3
"""
Orquestrador do benchmark local dos tres bracos (A/B/C) contra o cluster KWOK.

Para cada (braco, seed):
  1. sobe ./bin/costaware-scheduler como subprocesso, com o config do braco e
     um schedulerName dedicado (bench-a/-b/-c). O kubeconfig e injetado em
     clientConnection.kubeconfig num arquivo temporario -- porque, com --config,
     o kube-scheduler IGNORA o flag --kubeconfig.
  2. roda bench/cost.py (amostrador de custo) e bench/workload.py (driver de
     carga) em paralelo, com o MESMO slot-seconds e horizonte.
  3. derruba o scheduler, apaga o namespace e espera drenar.

No fim: agrega os CSVs, imprime media +- desvio por braco entre as seeds e um
Wilcoxon pareado C vs B sobre o custo total por seed.

Isto NAO substitui uma campanha de verdade: os precos vem da tabela chutada do
repo (nao da AWS), o KWOK nao mede latencia real e o Wilcoxon aqui e a
aproximacao normal. Para publicar, use precos reais e scipy.stats.wilcoxon exato.

Uso:
    python bench/run_local.py --repeats 10 --seed-base 1 \
        --scheduler-bin ./bin/costaware-scheduler --kubeconfig ~/.kube/config

    # rapido, um braco so, sem regenerar os nos:
    python bench/run_local.py --arms C --repeats 1 --skip-nodes
"""

import argparse
import os
import statistics
import subprocess
import sys
import tempfile
import time
import csv

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# braco -> (config, schedulerName)
ARMS = {
    "A": ("deploy/bench-A.yaml", "bench-a"),
    "B": ("deploy/bench-B.yaml", "bench-b"),
    "C": ("deploy/bench-C.yaml", "bench-c"),
}


# ---------------------------------------------------------------------------
# infra
# ---------------------------------------------------------------------------
def kubectl(*args, check=True, quiet=False):
    out = subprocess.DEVNULL if quiet else None
    return subprocess.run(["kubectl", *args], check=check, stdout=out, stderr=out)


def render_config(src_rel, kubeconfig):
    """Injeta clientConnection.kubeconfig e devolve o caminho do arquivo temp."""
    with open(os.path.join(ROOT, src_rel)) as fh:
        doc = yaml.safe_load(fh)
    doc.setdefault("clientConnection", {})["kubeconfig"] = os.path.abspath(
        os.path.expanduser(kubeconfig)
    )
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", prefix="bench-cfg-", delete=False
    )
    yaml.safe_dump(doc, tmp)
    tmp.close()
    return tmp.name


class Scheduler:
    def __init__(self, binary, cfg_rel, kubeconfig, logpath):
        self.binary = binary
        self.cfg_rel = cfg_rel
        self.kubeconfig = kubeconfig
        self.logpath = logpath
        self.proc = None
        self._cfg = None
        self._log = None

    def __enter__(self):
        self._cfg = render_config(self.cfg_rel, self.kubeconfig)
        self._log = open(self.logpath, "w")
        self.proc = subprocess.Popen(
            [self.binary, "--config", self._cfg, "--secure-port=0", "--v=2"],
            stdout=subprocess.DEVNULL,
            stderr=self._log,
            cwd=ROOT,
        )
        # readiness: espera os informers popularem ou 25s
        deadline = time.time() + 25
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"scheduler morreu ao subir; veja {self.logpath}"
                )
            try:
                with open(self.logpath) as fh:
                    if "Caches populated for *v1.Pod" in fh.read():
                        break
            except FileNotFoundError:
                pass
            time.sleep(0.5)
        time.sleep(2)
        return self

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._log:
            self._log.close()
        if self._cfg and os.path.exists(self._cfg):
            os.unlink(self._cfg)


def drain(namespaces, timeout=90):
    kubectl(
        "delete", "ns", *namespaces, "--ignore-not-found", "--wait=true",
        check=False, quiet=True,
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(
            ["kubectl", "get", "pods", "-A", "--no-headers"],
            capture_output=True, text=True, check=False,
        )
        if "bench-" not in r.stdout:
            return
        time.sleep(2)


# ---------------------------------------------------------------------------
# um run
# ---------------------------------------------------------------------------
def aggregate(csv_path, arm):
    with open(csv_path) as fh:
        rows = list(csv.DictReader(fh))
    cost = [float(r["slot_cost_usd"]) for r in rows]
    return {
        "arm": arm,
        "slots": len(rows),
        "total_cost_usd": sum(cost),
        "mean_slot_usd": statistics.mean(cost),
        "sd_slot_usd": statistics.pstdev(cost),
        "activations": sum(int(r["activations"]) for r in rows),
        "active_nodes_mean": statistics.mean(int(r["active_nodes"]) for r in rows),
        "pending_max": max(int(r["pending_pods"]) for r in rows),
    }


def run_one(arm, seed, args, env, raw_dir, log_dir):
    cfg_rel, sched_name = ARMS[arm]
    ns = f"bench-{arm.lower()}"
    duration = args.slots * args.slot_seconds
    run_csv = os.path.join(raw_dir, f"run-{arm}-s{seed}.csv")
    arr_csv = os.path.join(raw_dir, f"arrivals-{arm}-s{seed}.csv")
    py = sys.executable

    print(f"  [{arm} seed={seed}] scheduler={sched_name} "
          f"pods={args.pods} slots={args.slots}x{args.slot_seconds}s")
    drain([ns])

    sched_log = os.path.join(log_dir, f"sched-{arm}-s{seed}.log")
    with Scheduler(args.scheduler_bin, cfg_rel, args.kubeconfig, sched_log):
        cost = subprocess.Popen(
            [py, "bench/cost.py", "--arm", arm, "--duration", str(duration),
             "--interval", str(args.slot_seconds), "--out", run_csv],
            cwd=ROOT, env=env,
            stderr=open(os.path.join(log_dir, f"cost-{arm}-s{seed}.log"), "w"),
        )
        time.sleep(1)
        wl = subprocess.run(
            [py, "bench/workload.py", "--pods", str(args.pods),
             "--slots", str(args.slots), "--slot-seconds", str(args.slot_seconds),
             "--scheduler-name", sched_name, "--namespace", ns,
             "--seed", str(seed), "--cpu-min", str(args.cpu_min),
             "--cpu-max", str(args.cpu_max), "--lifetime-slots",
             str(args.lifetime_slots), "--out", arr_csv],
            cwd=ROOT, env=env,
            stderr=open(os.path.join(log_dir, f"workload-{arm}-s{seed}.log"), "w"),
        )
        if wl.returncode != 0:
            cost.kill()
            raise RuntimeError(f"workload.py falhou (arm {arm} seed {seed})")
        try:
            cost.wait(timeout=duration + 60)
        except subprocess.TimeoutExpired:
            cost.kill()

    drain([ns])
    return aggregate(run_csv, arm)


# ---------------------------------------------------------------------------
# estatistica
# ---------------------------------------------------------------------------
def wilcoxon_signed_rank(x, y):
    """Wilcoxon pareado, aproximacao normal com correcao de continuidade e de
    empates. Descarta diferencas nulas (Wilcoxon classico). Devolve (W, z, p2)."""
    diffs = [a - b for a, b in zip(x, y) if a - b != 0]
    n = len(diffs)
    if n == 0:
        return 0.0, 0.0, 1.0
    order = sorted(range(n), key=lambda i: abs(diffs[i]))
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(diffs[order[j + 1]]) == abs(diffs[order[i]]):
            j += 1
        avg = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    w_plus = sum(r for d, r in zip(diffs, ranks) if d > 0)
    w_minus = sum(r for d, r in zip(diffs, ranks) if d < 0)
    W = min(w_plus, w_minus)
    mean_w = n * (n + 1) / 4.0
    # correcao de empates
    from collections import Counter
    tie_term = sum(t ** 3 - t for t in Counter(abs(d) for d in diffs).values())
    var_w = (n * (n + 1) * (2 * n + 1) - tie_term / 2.0) / 24.0
    if var_w <= 0:
        return W, 0.0, 1.0
    z = (W - mean_w + 0.5) / (var_w ** 0.5)
    # p bicaudal via erfc
    import math
    p2 = math.erfc(abs(z) / math.sqrt(2))
    return W, z, p2


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="A,B,C", help="subconjunto de A,B,C")
    ap.add_argument("--repeats", type=int, default=10)
    ap.add_argument("--seed-base", type=int, default=1)
    ap.add_argument("--scheduler-bin", default="./bin/costaware-scheduler")
    ap.add_argument("--kubeconfig", default=os.path.expanduser("~/.kube/config"))
    ap.add_argument("--out-dir", default="results")
    # carga -- padrao conservador para nao saturar uma VM local
    ap.add_argument("--pods", type=int, default=150)
    ap.add_argument("--slots", type=int, default=10)
    ap.add_argument("--slot-seconds", type=int, default=18)
    ap.add_argument("--cpu-min", type=int, default=100)
    ap.add_argument("--cpu-max", type=int, default=1000)
    ap.add_argument("--lifetime-slots", type=int, default=6)
    # nos
    ap.add_argument("--nodes", type=int, default=200)
    ap.add_argument("--trap-fraction", type=float, default=0.5)
    ap.add_argument("--ratio", type=float, default=10.0)
    ap.add_argument("--skip-nodes", action="store_true",
                    help="assume que os nos KWOK ja estao aplicados")
    args = ap.parse_args()

    arms = [a.strip().upper() for a in args.arms.split(",") if a.strip()]
    for a in arms:
        if a not in ARMS:
            sys.exit(f"braco invalido: {a}")

    if not os.path.isabs(args.scheduler_bin):
        args.scheduler_bin = os.path.join(ROOT, args.scheduler_bin)
    if not os.path.exists(args.scheduler_bin):
        sys.exit(f"binario nao encontrado: {args.scheduler_bin} (rode `make build`)")

    env = dict(os.environ, KUBECONFIG=os.path.abspath(os.path.expanduser(args.kubeconfig)))
    out_dir = os.path.join(ROOT, args.out_dir)
    raw_dir = os.path.join(out_dir, "raw")
    log_dir = os.path.join(out_dir, "logs")
    for d in (raw_dir, log_dir):
        os.makedirs(d, exist_ok=True)

    if not args.skip_nodes:
        nodes_yaml = os.path.join(raw_dir, "nodes.yaml")
        print(f">>> gerando {args.nodes} nos KWOK (ratio={args.ratio})")
        with open(nodes_yaml, "w") as fh:
            subprocess.run(
                [sys.executable, "bench/gen_nodes.py", "--nodes", str(args.nodes),
                 "--trap-fraction", str(args.trap_fraction), "--ratio",
                 str(args.ratio), "--out", "-"],
                cwd=ROOT, check=True, stdout=fh,
            )
        kubectl("apply", "-f", nodes_yaml, quiet=True)

    seeds = [args.seed_base + i for i in range(args.repeats)]
    results = {a: [] for a in arms}
    summary_path = os.path.join(out_dir, "summary.csv")
    with open(summary_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["arm", "seed", "total_cost_usd", "mean_slot_usd",
                    "sd_slot_usd", "activations", "active_nodes_mean",
                    "pending_max"])
        for seed in seeds:
            print(f">>> seed {seed}")
            for arm in arms:
                try:
                    agg = run_one(arm, seed, args, env, raw_dir, log_dir)
                except Exception as e:  # noqa: BLE001
                    print(f"  ERRO {arm} seed {seed}: {e}", file=sys.stderr)
                    continue
                agg["seed"] = seed
                results[arm].append(agg)
                w.writerow([arm, seed, f"{agg['total_cost_usd']:.6f}",
                            f"{agg['mean_slot_usd']:.6f}", f"{agg['sd_slot_usd']:.6f}",
                            agg["activations"], f"{agg['active_nodes_mean']:.2f}",
                            agg["pending_max"]])
                fh.flush()

    # ---- relatorio ----
    print("\n================ RESUMO ================")
    print(f"{'arm':<4}{'n':>3}{'custo_total$ (media+-dp)':>28}"
          f"{'ativacoes':>12}{'nos_ativos':>12}{'pend_max':>10}")
    for arm in arms:
        rs = results[arm]
        if not rs:
            continue
        tc = [r["total_cost_usd"] for r in rs]
        ac = [r["activations"] for r in rs]
        nn = [r["active_nodes_mean"] for r in rs]
        pm = max(r["pending_max"] for r in rs)
        sd = statistics.pstdev(tc) if len(tc) > 1 else 0.0
        print(f"{arm:<4}{len(rs):>3}{statistics.mean(tc):>18.4f} +-{sd:>7.4f}"
              f"{statistics.mean(ac):>12.1f}{statistics.mean(nn):>12.1f}{pm:>10d}")

    if "B" in results and "C" in results and results["B"] and results["C"]:
        by_seed_b = {r["seed"]: r for r in results["B"]}
        by_seed_c = {r["seed"]: r for r in results["C"]}
        common = sorted(set(by_seed_b) & set(by_seed_c))
        b = [by_seed_b[s]["total_cost_usd"] for s in common]
        c = [by_seed_c[s]["total_cost_usd"] for s in common]
        if len(common) >= 2:
            econ = 100 * (1 - statistics.mean(c) / statistics.mean(b))
            W, z, p = wilcoxon_signed_rank(c, b)
            print(f"\nC vs B (baseline forte), n={len(common)} seeds pareadas:")
            print(f"  economia de custo media : {econ:.1f}%")
            print(f"  Wilcoxon signed-rank    : W={W:.1f}  z={z:.2f}  p(2-tail)~{p:.4f}")
            print("  (aprox. normal; para publicar use scipy.stats.wilcoxon exato)")

    print(f"\nsummary : {summary_path}")
    print(f"brutos  : {raw_dir}/  |  logs : {log_dir}/")


if __name__ == "__main__":
    main()
