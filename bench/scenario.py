#!/usr/bin/env python3
"""
Teste de cenario deterministico: a ponte entre o teste unitario de Phi e o
benchmark agregado.

O teste unitario responde "Phi calcula o que o artigo diz?".
O benchmark responde "quanto economiza?".
Falta a pergunta do meio: "o plugin, montado dentro do kube-scheduler real,
coloca o pod no no que a teoria preve?" -- e isso e o que este script afere.

Ele sobe o scheduler compilado como subprocesso, com um schedulerName
dedicado (costaware-scenario), de modo que o scheduler do cluster ignora os
pods do cenario e nada precisa ser derrubado.

Cada caso e uma assercao sobre o NOME EXATO do no escolhido, derivada
analiticamente de deploy/scenario-nodes.yaml.

Pre-requisitos:
    cluster kind + KWOK no ar, nos do cenario aplicados:
        kubectl apply -f deploy/scenario-nodes.yaml
    binario compilado:
        go build -o bin/costaware-scheduler ./cmd/scheduler

Uso:
    python bench/scenario.py --scheduler-bin ./bin/costaware-scheduler \
        --kubeconfig ~/.kube/config
"""

import argparse
import os
import subprocess
import sys
import tempfile
import time
import uuid

import yaml
from kubernetes import client, config
from kubernetes.client.rest import ApiException

NAMESPACE = "costaware-scenario"
SCHEDULER_NAME = "costaware-scenario"

TOLERATION = client.V1Toleration(
    key="kwok.x-k8s.io/node", operator="Equal", value="fake", effect="NoSchedule"
)


# ---------------------------------------------------------------------------
# Os casos. A coluna "porque" e o que voce vai ler na banca quando perguntarem
# de onde saiu a expectativa.
# ---------------------------------------------------------------------------
CASES = [
    {
        "name": "R=0 escolhe o no mais barato por core, mesmo frio",
        "config": "deploy/scenario-R0.yaml",
        "candidates": ["trap-cold", "stable-warm"],
        "pod_cpu": 1000,
        "expect": "trap-cold",
        "why": (
            "sem inercia, Phi = alpha. alpha_trap=4.60e-5 < alpha_stable=4.80e-5, "
            "entao o trap vence apesar do cold start de 600s."
        ),
    },
    {
        "name": "R=10 inverte: o no quente e caro vence",
        "config": "deploy/scenario-R10.yaml",
        "candidates": ["trap-cold", "stable-warm"],
        "pod_cpu": 1000,
        "expect": "stable-warm",
        "why": (
            "delta_trap = 10*0.184*(600/3600)/4000 = 7.67e-5 domina a diferenca "
            "de 0.20e-5 em alpha. E a Condicao 2 do Teorema 2 em execucao."
        ),
    },
    {
        "name": "entre dois nos quentes, vence o mais barato por core",
        "config": "deploy/scenario-R10.yaml",
        "candidates": ["trap-warm", "stable-warm"],
        "pod_cpu": 1000,
        "expect": "trap-warm",
        "why": "ambos quentes => delta=0 nos dois, Phi reduz a alpha.",
    },
    {
        "name": "entre dois nos frios, vence o de menor cold start amortizado",
        "config": "deploy/scenario-R10.yaml",
        "candidates": ["trap-cold", "stable-cold"],
        "pod_cpu": 1000,
        "expect": "stable-cold",
        "why": (
            "delta_stable = 10*0.384*(150/3600)/8000 = 2.00e-5 contra "
            "delta_trap = 7.67e-5."
        ),
    },
    {
        "name": "a mistura de producao preserva a ordenacao",
        "config": "deploy/scenario-blend.yaml",
        "candidates": ["trap-cold", "stable-warm"],
        "pod_cpu": 1000,
        "expect": "stable-warm",
        "why": (
            "com packingWeight=0.2 e NodeResourcesFit peso 3, o termo de custo "
            "ainda deve dominar. Se ESTE caso falhar e o R=10 isolado passar, "
            "o problema esta nos pesos do config, nao em Phi."
        ),
    },
    {
        "name": "pod que nao cabe em lugar nenhum fica Pending",
        "config": "deploy/scenario-R10.yaml",
        "candidates": ["trap-cold", "trap-warm"],
        "pod_cpu": 16000,  # 16 cores, nenhum trap tem
        "expect": None,
        "why": "o Filter nativo continua soberano; Score nunca cria viabilidade.",
    },
]

# Ocupacao previa que define quais nos sao "quentes".
WARM_NODES = {"stable-warm": 2000, "trap-warm": 1000}


def ensure_namespace(v1):
    try:
        v1.create_namespace(
            client.V1Namespace(metadata=client.V1ObjectMeta(name=NAMESPACE))
        )
    except ApiException as e:
        if e.status != 409:
            raise


def make_pod(name, cpu_milli, candidates, scheduler_name=SCHEDULER_NAME, node_name=None):
    spec = client.V1PodSpec(
        tolerations=[TOLERATION],
        restart_policy="Never",
        termination_grace_period_seconds=0,
        containers=[
            client.V1Container(
                name="app",
                image="registry.k8s.io/pause:3.9",
                resources=client.V1ResourceRequirements(
                    requests={"cpu": f"{cpu_milli}m"},
                    limits={"cpu": f"{cpu_milli}m"},
                ),
            )
        ],
    )
    if node_name:
        # bypassa o scheduler: e assim que criamos os nos "quentes"
        spec.node_name = node_name
    else:
        spec.scheduler_name = scheduler_name
        # restringe o conjunto viavel aos candidatos do caso, via affinity.
        spec.affinity = client.V1Affinity(
            node_affinity=client.V1NodeAffinity(
                required_during_scheduling_ignored_during_execution=client.V1NodeSelector(
                    node_selector_terms=[
                        client.V1NodeSelectorTerm(
                            match_expressions=[
                                client.V1NodeSelectorRequirement(
                                    key="kubernetes.io/hostname",
                                    operator="In",
                                    values=candidates,
                                )
                            ]
                        )
                    ]
                )
            )
        )
    return client.V1Pod(
        metadata=client.V1ObjectMeta(name=name, namespace=NAMESPACE),
        spec=spec,
    )


def place_warm_pods(v1):
    """Cria a ocupacao previa que torna certos nos 'quentes'."""
    for node, cpu in WARM_NODES.items():
        name = f"warm-{node}"
        try:
            v1.create_namespaced_pod(
                NAMESPACE, make_pod(name, cpu, [], node_name=node)
            )
        except ApiException as e:
            if e.status != 409:
                raise
    # espera o snapshot do scheduler enxergar a ocupacao
    time.sleep(3)


def clear_probe_pods(v1):
    try:
        v1.delete_collection_namespaced_pod(
            NAMESPACE, label_selector="role=probe", grace_period_seconds=0
        )
    except ApiException:
        pass
    time.sleep(2)


def wait_for_binding(v1, name, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            pod = v1.read_namespaced_pod(name, NAMESPACE)
        except ApiException as e:
            if e.status == 404:
                time.sleep(0.5)
                continue
            raise
        if pod.spec.node_name:
            return pod.spec.node_name
        time.sleep(0.5)
    return None


class Scheduler:
    """Sobe o binario do scheduler com um config e o derruba ao sair."""

    def __init__(self, binary, cfg, kubeconfig):
        self.binary = binary
        self.cfg = cfg
        self.kubeconfig = os.path.abspath(os.path.expanduser(kubeconfig))
        self.proc = None
        self._tmp_cfg = None

    def _render_config(self):
        """Injeta clientConnection.kubeconfig no KubeSchedulerConfiguration.

        Com --config, o kube-scheduler IGNORA o flag --kubeconfig (fica so o
        caminho in-cluster, que nao existe aqui). O caminho do kubeconfig tem
        de estar dentro do proprio config, em clientConnection.kubeconfig.
        """
        with open(self.cfg) as f:
            doc = yaml.safe_load(f)
        doc.setdefault("clientConnection", {})["kubeconfig"] = self.kubeconfig
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", prefix="costaware-cfg-", delete=False
        )
        yaml.safe_dump(doc, tmp)
        tmp.close()
        self._tmp_cfg = tmp.name
        return tmp.name

    def __enter__(self):
        cmd = [self.binary, "--config", self._render_config(), "--v=3"]
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        time.sleep(6)  # informers sincronizando
        if self.proc.poll() is not None:
            err = self.proc.stderr.read().decode()[-2000:]
            raise RuntimeError(f"scheduler morreu ao subir:\n{err}")
        return self

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._tmp_cfg and os.path.exists(self._tmp_cfg):
            os.unlink(self._tmp_cfg)


def run_case(v1, case, binary, kubeconfig):
    probe = f"probe-{uuid.uuid4().hex[:8]}"
    pod = make_pod(probe, case["pod_cpu"], case["candidates"])
    pod.metadata.labels = {"role": "probe"}

    with Scheduler(binary, case["config"], kubeconfig):
        place_warm_pods(v1)
        v1.create_namespaced_pod(NAMESPACE, pod)
        got = wait_for_binding(v1, probe, timeout=30)

    clear_probe_pods(v1)

    expected = case["expect"]
    if got == expected:
        return True, got
    return False, got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scheduler-bin", default="./bin/costaware-scheduler")
    ap.add_argument("--kubeconfig", required=True)
    ap.add_argument("--only", help="roda apenas casos cujo nome contenha esta string")
    args = ap.parse_args()

    config.load_kube_config(config_file=args.kubeconfig)
    v1 = client.CoreV1Api()
    ensure_namespace(v1)

    cases = CASES
    if args.only:
        cases = [c for c in CASES if args.only.lower() in c["name"].lower()]

    failures = 0
    for case in cases:
        print(f"\n>>> {case['name']}")
        print(f"    {case['why']}")
        try:
            ok, got = run_case(v1, case, args.scheduler_bin, args.kubeconfig)
        except Exception as e:  # noqa: BLE001
            print(f"    ERRO: {e}")
            failures += 1
            continue

        want = case["expect"] or "Pending (nenhum no)"
        got_str = got or "Pending (nenhum no)"
        if ok:
            print(f"    OK   -> {got_str}")
        else:
            print(f"    FALHA -> esperado {want}, obtido {got_str}")
            failures += 1

    # limpeza dos pods quentes
    try:
        v1.delete_collection_namespaced_pod(NAMESPACE, grace_period_seconds=0)
    except ApiException:
        pass

    print(f"\n{len(cases) - failures}/{len(cases)} casos passaram")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
