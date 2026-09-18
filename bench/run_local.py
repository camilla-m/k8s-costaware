#!/usr/bin/env python3
"""
Orquestrador do benchmark local dos bracos A/B/C (+ varredura de R) contra o
cluster KWOK.

Para cada (variante, seed):
  1. sobe ./bin/costaware-scheduler como subprocesso, com um config renderizado
     (schedulerName dedicado, clientConnection.kubeconfig injetado -- com
     --config o kube-scheduler ignora o flag --kubeconfig -- e, para as
     variantes C-R<r>, transitionRatio sobrescrito no pluginConfig do
     CostAware).
  2. roda bench/cost.py (amostrador de custo) e bench/workload.py (driver de
     carga) em paralelo, com o MESMO slot-seconds e horizonte.
  3. derruba o scheduler, apaga o namespace e espera drenar.

No fim: agrega os CSVs, imprime media +- desvio por variante entre as seeds e,
se houver uma varredura de R, uma tabela R -> economia% vs B com Wilcoxon
pareado (exato via scipy quando disponivel; aproximacao normal como fallback).

Isto NAO substitui uma campanha de verdade: os precos vem de
docs/pricing/aws-on-demand-*.json (real, mas so On-Demand -- spot e
aproximado), e o KWOK nao mede latencia real de boot/pull de imagem.

Uso:
    # como antes: A/B/C, R=10 fixo (o do deploy/bench-C.yaml)
    python bench/run_local.py --repeats 10 --seed-base 1

    # varredura de sensibilidade: A, B, e C em R=0,1,10,100
    python bench/run_local.py --arms A,B,C --r-values 0,1,10,100 --repeats 8

    # rapido, uma variante so, sem regenerar os nos:
    python bench/run_local.py --arms C --r-values 0 --repeats 1 --skip-nodes
"""

import argparse
import csv
import os
import statistics
import subprocess
import sys
import tempfile
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

sys.stdout.reconfigure(line_buffering=True)  # progresso visivel mesmo redirecionado (nohup etc.)

# braco base -> (config, schedulerName)
ARMS = {
    "A": ("deploy/bench-A.yaml", "bench-a"),
    "B": ("deploy/bench-B.yaml", "bench-b"),
    "C": ("deploy/bench-C.yaml", "bench-c"),
}


def fmt_r(r):
    """0 -> '0', 10.0 -> '10', 0.5 -> '0-5' -- sem ponto, pra caber em nome de
    namespace/schedulerName (RFC 1123: so [a-z0-9-])."""
    if float(r).is_integer():
        return str(int(r))
    return str(r).replace(".", "-")


def build_variants(arm_labels, r_values):
    """Expande 'C' em C-R<r> para cada r de r_values; A/B passam direto."""
    variants = []  # (label, cfg_rel, sched_name, transition_ratio_or_None)
    for a in arm_labels:
        if a == "C" and r_values:
            for r in r_values:
                tag = fmt_r(r)
                variants.append((f"C-R{tag}", ARMS["C"][0], f"bench-c-r{tag}", r))
        else:
            cfg, name = ARMS[a]
            variants.append((a, cfg, name, None))
    return variants


# ---------------------------------------------------------------------------
# infra
# ---------------------------------------------------------------------------
def kubectl(*args, check=True, quiet=False, context=None):
    out = subprocess.DEVNULL if quiet else None
    cmd = ["kubectl"] + (["--context", context] if context else []) + list(args)
    return subprocess.run(cmd, check=check, stdout=out, stderr=out)


def render_config(src_rel, kubeconfig, scheduler_name=None, transition_ratio=None,
                   packing_weight=None):
    """Injeta clientConnection.kubeconfig, sobrescreve profiles[*].schedulerName
    (o rotulo da variante pode divergir do que esta gravado em deploy/bench-*.yaml
    -- e o caso de C-R<r>) e, se dado, transitionRatio/packingWeight do plugin
    CostAware. Config sem CostAware (arms A/B) ignora os dois silenciosamente."""
    with open(os.path.join(ROOT, src_rel)) as fh:
        doc = yaml.safe_load(fh)
    doc.setdefault("clientConnection", {})["kubeconfig"] = os.path.abspath(
        os.path.expanduser(kubeconfig)
    )
    for profile in doc.get("profiles", []):
        if scheduler_name is not None:
            profile["schedulerName"] = scheduler_name
        for pc in profile.get("pluginConfig", []):
            if pc.get("name") != "CostAware":
                continue
            args = pc.setdefault("args", {})
            if transition_ratio is not None:
                args["transitionRatio"] = float(transition_ratio)
            if packing_weight is not None:
                args["packingWeight"] = float(packing_weight)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", prefix="bench-cfg-", delete=False
    )
    yaml.safe_dump(doc, tmp)
    tmp.close()
    return tmp.name


class Scheduler:
    def __init__(self, binary, cfg_rel, kubeconfig, logpath, scheduler_name=None,
                 transition_ratio=None, packing_weight=None):
        self.binary = binary
        self.cfg_rel = cfg_rel
        self.kubeconfig = kubeconfig
        self.logpath = logpath
        self.scheduler_name = scheduler_name
        self.transition_ratio = transition_ratio
        self.packing_weight = packing_weight
        self.proc = None
        self._cfg = None
        self._log = None

    def __enter__(self):
        self._cfg = render_config(self.cfg_rel, self.kubeconfig, self.scheduler_name,
                                   self.transition_ratio, self.packing_weight)
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


def drain(namespaces, timeout=90, context=None):
    kubectl(
        "delete", "ns", *namespaces, "--ignore-not-found", "--wait=true",
        check=False, quiet=True, context=context,
    )
    cmd = ["kubectl"] + (["--context", context] if context else []) + ["get", "pods", "-A", "--no-headers"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if "bench-" not in r.stdout:
            return
        time.sleep(2)


# ---------------------------------------------------------------------------
# um run
# ---------------------------------------------------------------------------
def aggregate(csv_path, label):
    with open(csv_path) as fh:
        rows = list(csv.DictReader(fh))
    cost = [float(r["slot_cost_usd"]) for r in rows]
    return {
        "arm": label,
        "slots": len(rows),
        "total_cost_usd": sum(cost),
        "mean_slot_usd": statistics.mean(cost),
        "sd_slot_usd": statistics.pstdev(cost),
        "activations": sum(int(r["activations"]) for r in rows),
        "active_nodes_mean": statistics.mean(int(r["active_nodes"]) for r in rows),
        "pending_max": max(int(r["pending_pods"]) for r in rows),
    }


def run_one(variant, seed, args, env, raw_dir, log_dir):
    label, cfg_rel, sched_name, r_override = variant
    ns = "bench-" + label.lower().replace(".", "-")
    duration = args.slots * args.slot_seconds
    run_csv = os.path.join(raw_dir, f"run-{label}-s{seed}.csv")
    arr_csv = os.path.join(raw_dir, f"arrivals-{label}-s{seed}.csv")
    py = sys.executable

    r_note = f" R={r_override}" if r_override is not None else ""
    print(f"  [{label} seed={seed}] scheduler={sched_name}{r_note} "
          f"pods={args.pods} slots={args.slots}x{args.slot_seconds}s")
    drain([ns])

    sched_log = os.path.join(log_dir, f"sched-{label}-s{seed}.log")
    with Scheduler(args.scheduler_bin, cfg_rel, args.kubeconfig, sched_log,
                    sched_name, r_override, args.packing_weight):
        cost = subprocess.Popen(
            [py, "bench/cost.py", "--arm", label, "--duration", str(duration),
             "--interval", str(args.slot_seconds), "--out", run_csv],
            cwd=ROOT, env=env,
            stderr=open(os.path.join(log_dir, f"cost-{label}-s{seed}.log"), "w"),
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
            stderr=open(os.path.join(log_dir, f"workload-{label}-s{seed}.log"), "w"),
        )
        if wl.returncode != 0:
            cost.kill()
            raise RuntimeError(f"workload.py falhou ({label} seed {seed})")
        try:
            cost.wait(timeout=duration + 60)
        except subprocess.TimeoutExpired:
            cost.kill()

    drain([ns])
    return aggregate(run_csv, label)


# ---------------------------------------------------------------------------
# estatistica
# ---------------------------------------------------------------------------
def _wilcoxon_normal_approx(x, y):
    """Fallback sem scipy: aproximacao normal com correcao de continuidade e
    de empates. Descarta diferencas nulas (Wilcoxon classico)."""
    import math
    from collections import Counter

    diffs = [a - b for a, b in zip(x, y) if a - b != 0]
    n = len(diffs)
    if n == 0:
        return {"method": "aprox. normal (scipy ausente)", "n": 0, "W": 0.0, "z": 0.0, "p": 1.0}
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
    tie_term = sum(t ** 3 - t for t in Counter(abs(d) for d in diffs).values())
    var_w = (n * (n + 1) * (2 * n + 1) - tie_term / 2.0) / 24.0
    if var_w <= 0:
        return {"method": "aprox. normal (scipy ausente)", "n": n, "W": W, "z": 0.0, "p": 1.0}
    z = (W - mean_w + 0.5) / (var_w ** 0.5)
    p2 = math.erfc(abs(z) / math.sqrt(2))
    return {"method": "aprox. normal (scipy ausente)", "n": n, "W": W, "z": z, "p": p2}


def wilcoxon(x, y):
    """Wilcoxon signed-rank pareado. Usa scipy (exato quando aplicavel) se
    disponivel; senao cai na aproximacao normal manual acima."""
    try:
        from scipy.stats import wilcoxon as scipy_wilcoxon
    except ImportError:
        return _wilcoxon_normal_approx(x, y)

    diffs = [a - b for a, b in zip(x, y) if a - b != 0]
    if len(diffs) == 0:
        return {"method": "scipy", "n": 0, "W": 0.0, "z": None, "p": 1.0}
    try:
        res = scipy_wilcoxon(x, y, alternative="two-sided", method="auto")
        return {"method": "scipy (auto: exato se n pequeno e sem empates)",
                "n": len(diffs), "W": float(res.statistic), "z": None, "p": float(res.pvalue)}
    except ValueError as e:
        return {"method": f"scipy falhou ({e}); aprox. normal", **_wilcoxon_normal_approx(x, y)}


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="A,B,C", help="subconjunto de A,B,C")
    ap.add_argument("--r-values", default=None,
                    help="se dado, expande C em C-R<r> para cada valor (ex.: 0,1,10,100)")
    ap.add_argument("--packing-weight", type=float, default=None,
                    help="sobrescreve packingWeight do CostAware em todas as variantes C "
                         "(default: usa o que estiver em deploy/bench-C.yaml, 0.2)")
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

    arm_labels = [a.strip().upper() for a in args.arms.split(",") if a.strip()]
    for a in arm_labels:
        if a not in ARMS:
            sys.exit(f"braco invalido: {a}")
    r_values = None
    if args.r_values:
        r_values = [float(v) for v in args.r_values.split(",") if v.strip()]

    variants = build_variants(arm_labels, r_values)

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
    labels = [v[0] for v in variants]
    results = {label: [] for label in labels}
    total_runs = len(variants) * len(seeds)
    done = 0

    summary_path = os.path.join(out_dir, "summary.csv")
    with open(summary_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["arm", "seed", "total_cost_usd", "mean_slot_usd",
                    "sd_slot_usd", "activations", "active_nodes_mean",
                    "pending_max"])
        for seed in seeds:
            print(f">>> seed {seed}")
            for variant in variants:
                label = variant[0]
                try:
                    agg = run_one(variant, seed, args, env, raw_dir, log_dir)
                except Exception as e:  # noqa: BLE001
                    print(f"  ERRO {label} seed {seed}: {e}", file=sys.stderr)
                    done += 1
                    continue
                agg["seed"] = seed
                results[label].append(agg)
                w.writerow([label, seed, f"{agg['total_cost_usd']:.6f}",
                            f"{agg['mean_slot_usd']:.6f}", f"{agg['sd_slot_usd']:.6f}",
                            agg["activations"], f"{agg['active_nodes_mean']:.2f}",
                            agg["pending_max"]])
                fh.flush()
                done += 1
                print(f"    [{done}/{total_runs}] concluido")

    # ---- relatorio por variante ----
    print("\n================ RESUMO POR VARIANTE ================")
    print(f"{'variante':<10}{'n':>3}{'custo_total$ (media+-dp)':>28}"
          f"{'ativacoes':>12}{'nos_ativos':>12}{'pend_max':>10}")
    for label in labels:
        rs = results[label]
        if not rs:
            continue
        tc = [r["total_cost_usd"] for r in rs]
        ac = [r["activations"] for r in rs]
        nn = [r["active_nodes_mean"] for r in rs]
        pm = max(r["pending_max"] for r in rs)
        sd = statistics.pstdev(tc) if len(tc) > 1 else 0.0
        print(f"{label:<10}{len(rs):>3}{statistics.mean(tc):>18.4f} +-{sd:>7.4f}"
              f"{statistics.mean(ac):>12.1f}{statistics.mean(nn):>12.1f}{pm:>10d}")

    # ---- comparacao contra B (com ou sem varredura de R) ----
    def paired(label_a, label_b):
        by_a = {r["seed"]: r for r in results.get(label_a, [])}
        by_b = {r["seed"]: r for r in results.get(label_b, [])}
        common = sorted(set(by_a) & set(by_b))
        return (
            [by_a[s]["total_cost_usd"] for s in common],
            [by_b[s]["total_cost_usd"] for s in common],
            common,
        )

    c_labels = [label for label in labels if label == "C" or label.startswith("C-R")]
    if "B" in results and c_labels and results["B"]:
        print("\n================ C vs B (baseline forte) ================")
        print(f"{'variante':<10}{'n':>4}{'economia%':>12}{'W':>10}{'p':>12}  metodo")
        for label in c_labels:
            c, b, common = paired(label, "B")
            if len(common) < 2:
                continue
            econ = 100 * (1 - statistics.mean(c) / statistics.mean(b))
            res = wilcoxon(c, b)
            print(f"{label:<10}{len(common):>4}{econ:>11.1f}%{res['W']:>10.1f}"
                  f"{res['p']:>12.4f}  {res['method']}")

    print(f"\nsummary : {summary_path}")
    print(f"brutos  : {raw_dir}/  |  logs : {log_dir}/")


if __name__ == "__main__":
    main()
