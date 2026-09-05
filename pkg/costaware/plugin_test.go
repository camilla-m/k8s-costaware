package costaware

import (
	"context"
	"math"
	"testing"
	"time"

	"fmt"
	v1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/kubernetes/pkg/scheduler/framework"
	"sync"
)

// ---------------------------------------------------------------------------
// fixtures
//
// Two node classes taken from bench/gen_nodes.py, so the unit tests and the
// KWOK experiment exercise the same economics:
//
//	trap   -- g4dn.xlarge: cheaper per core, but a 600s cold start (big image)
//	stable -- m5.2xlarge:  pricier per core, but a 150s cold start
//
// The trap node is genuinely cheaper per core-hour, so a price-only scheduler
// prefers it. This is precisely Condition 2 of Theorem 2 (startup-cost
// heterogeneity), and the flip test below is its empirical counterpart.
// ---------------------------------------------------------------------------

var (
	trapPrice   = InstancePrice{HourlyUSD: 0.184, BootSeconds: 600}
	stablePrice = InstancePrice{HourlyUSD: 0.384, BootSeconds: 150}
)

const (
	trapCPU   = 4000.0 // milli-cores
	stableCPU = 8000.0
)

func TestComputePhi_TrapIsCheaperPerCore(t *testing.T) {
	// Sanity check on the fixtures themselves: if this ever stops holding,
	// the flip test below becomes vacuous and would pass for the wrong reason.
	alphaTrap := trapPrice.HourlyUSD / trapCPU
	alphaStable := stablePrice.HourlyUSD / stableCPU

	if alphaTrap >= alphaStable {
		t.Fatalf("fixture broken: trap must be cheaper per core-hour, got trap=%g stable=%g",
			alphaTrap, alphaStable)
	}
}

// TestComputePhi_TransitionRatioFlipsRanking is the central test of the plugin.
//
// Same two candidate nodes, same pod, same everything except R:
//
//	R = 0  -> stickiness disabled, the cheaper-per-core cold trap node wins
//	R = 10 -> the amortized cold-start penalty dominates, the warm stable
//	          node wins
//
// If this flip does not happen, Phi has collapsed into plain price-aware
// bin-packing and the temporal contribution of the thesis is not being
// exercised by the artifact.
func TestComputePhi_TransitionRatioFlipsRanking(t *testing.T) {
	coldTrap := func(r float64) phiInput {
		return phiInput{
			Price:           trapPrice,
			AllocatableCPU:  trapCPU,
			RequestedCPU:    0, // empty -> cold
			IncomingPodCPU:  1000,
			Cold:            true,
			TransitionRatio: r,
			PackingWeight:   0, // isolate the cost terms
		}
	}
	warmStable := func(r float64) phiInput {
		return phiInput{
			Price:           stablePrice,
			AllocatableCPU:  stableCPU,
			RequestedCPU:    2000, // already carrying load -> warm
			IncomingPodCPU:  1000,
			Cold:            false,
			TransitionRatio: r,
			PackingWeight:   0,
		}
	}

	t.Run("R=0 prefers the cheaper cold node", func(t *testing.T) {
		trap := computePhi(coldTrap(0))
		stable := computePhi(warmStable(0))
		if trap >= stable {
			t.Errorf("with R=0 the trap node should win (lower Phi), got trap=%g stable=%g",
				trap, stable)
		}
	})

	t.Run("R=10 prefers the warm node despite higher price", func(t *testing.T) {
		trap := computePhi(coldTrap(10))
		stable := computePhi(warmStable(10))
		if stable >= trap {
			t.Errorf("with R=10 the stable warm node should win (lower Phi), got trap=%g stable=%g",
				trap, stable)
		}
	})

	t.Run("the flip has a finite crossover point", func(t *testing.T) {
		// Locate the R at which the ranking inverts, and assert it is a
		// sensible interior value rather than a degenerate 0 or infinity.
		var crossover float64 = -1
		for r := 0.0; r <= 20.0; r += 0.05 {
			if computePhi(coldTrap(r)) > computePhi(warmStable(r)) {
				crossover = r
				break
			}
		}
		if crossover <= 0 {
			t.Fatal("no crossover found in R in [0,20]; stickiness term is inert")
		}
		t.Logf("ranking inverts at R = %.2f", crossover)
	})
}

func TestComputePhi_ColdPenaltyIsMonotoneInR(t *testing.T) {
	base := phiInput{
		Price:           trapPrice,
		AllocatableCPU:  trapCPU,
		IncomingPodCPU:  1000,
		Cold:            true,
		PackingWeight:   0,
		TransitionRatio: 0,
	}

	prev := math.Inf(-1)
	for _, r := range []float64{0, 1, 10, 100} {
		in := base
		in.TransitionRatio = r
		phi := computePhi(in)
		if phi <= prev {
			t.Errorf("Phi must increase with R, got %g at R=%g after %g", phi, r, prev)
		}
		prev = phi
	}
}

func TestComputePhi_WarmNodeIgnoresR(t *testing.T) {
	base := phiInput{
		Price:          stablePrice,
		AllocatableCPU: stableCPU,
		RequestedCPU:   2000,
		IncomingPodCPU: 1000,
		Cold:           false,
		PackingWeight:  0,
	}

	in0, in100 := base, base
	in0.TransitionRatio = 0
	in100.TransitionRatio = 100

	if computePhi(in0) != computePhi(in100) {
		t.Error("R must not affect a warm node: the startup penalty is only paid on transition")
	}
}

func TestComputePhi_PackingConsolidatesAmongEqualPrices(t *testing.T) {
	// Two identical warm nodes differing only in current occupancy. With a
	// non-zero packing weight, the fuller one must win, reproducing
	// MostAllocated behaviour as a tie-breaker.
	mk := func(requested float64) phiInput {
		return phiInput{
			Price:           stablePrice,
			AllocatableCPU:  stableCPU,
			RequestedCPU:    requested,
			IncomingPodCPU:  1000,
			Cold:            false,
			TransitionRatio: 10,
			PackingWeight:   0.2,
		}
	}

	fuller := computePhi(mk(6000))
	emptier := computePhi(mk(1000))

	if fuller >= emptier {
		t.Errorf("the fuller node should win to consolidate, got fuller=%g emptier=%g",
			fuller, emptier)
	}
}

func TestComputePhi_PackingWeightZeroIgnoresOccupancy(t *testing.T) {
	mk := func(requested float64) phiInput {
		return phiInput{
			Price:           stablePrice,
			AllocatableCPU:  stableCPU,
			RequestedCPU:    requested,
			IncomingPodCPU:  1000,
			Cold:            false,
			TransitionRatio: 10,
			PackingWeight:   0,
		}
	}
	if computePhi(mk(6000)) != computePhi(mk(1000)) {
		t.Error("with packingWeight=0, occupancy must not influence Phi")
	}
}

func TestComputePhi_ZeroCapacityIsInfeasible(t *testing.T) {
	phi := computePhi(phiInput{Price: stablePrice, AllocatableCPU: 0})
	if !math.IsInf(phi, 1) {
		t.Errorf("a node with no allocatable CPU must be infinitely costly, got %g", phi)
	}
}

// ---------------------------------------------------------------------------
// isCold
// ---------------------------------------------------------------------------

func newNode(name string, ageSeconds int64, ready bool) *v1.Node {
	status := v1.ConditionTrue
	if !ready {
		status = v1.ConditionFalse
	}
	return &v1.Node{
		ObjectMeta: metav1.ObjectMeta{
			Name:              name,
			CreationTimestamp: metav1.NewTime(time.Now().Add(-time.Duration(ageSeconds) * time.Second)),
			Labels:            map[string]string{},
			Annotations:       map[string]string{},
		},
		Status: v1.NodeStatus{
			Conditions: []v1.NodeCondition{{Type: v1.NodeReady, Status: status}},
			Allocatable: v1.ResourceList{
				v1.ResourceCPU: *resource.NewMilliQuantity(8000, resource.DecimalSI),
			},
		},
	}
}

func newPod(name string, milliCPU int64, ownerKind string) *v1.Pod {
	pod := &v1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "default"},
		Spec: v1.PodSpec{
			Containers: []v1.Container{{
				Name: "c",
				Resources: v1.ResourceRequirements{
					Requests: v1.ResourceList{
						v1.ResourceCPU: *resource.NewMilliQuantity(milliCPU, resource.DecimalSI),
					},
				},
			}},
		},
		Status: v1.PodStatus{Phase: v1.PodRunning},
	}
	if ownerKind != "" {
		pod.OwnerReferences = []metav1.OwnerReference{{Kind: ownerKind, Name: "owner"}}
	}
	return pod
}

func nodeInfoWith(node *v1.Node, pods ...*v1.Pod) *framework.NodeInfo {
	ni := framework.NewNodeInfo(pods...)
	ni.SetNode(node)
	return ni
}

func TestIsCold(t *testing.T) {
	const coldAge = 300

	cases := []struct {
		name string
		info *framework.NodeInfo
		want bool
		why  string
	}{
		{
			name: "empty node is cold",
			info: nodeInfoWith(newNode("empty", 3600, true)),
			want: true,
			why:  "scheduling here blocks scale-down, which is exactly what delta prices",
		},
		{
			name: "node with only DaemonSet pods is cold",
			info: nodeInfoWith(newNode("ds-only", 3600, true), newPod("ds", 100, "DaemonSet")),
			want: true,
			why:  "DaemonSets run everywhere and carry no signal about actual usage",
		},
		{
			name: "node with a workload pod is warm",
			info: nodeInfoWith(newNode("busy", 3600, true), newPod("app", 1000, "ReplicaSet")),
			want: false,
		},
		{
			name: "NotReady node is cold even when occupied",
			info: nodeInfoWith(newNode("notready", 3600, false), newPod("app", 1000, "ReplicaSet")),
			want: true,
			why:  "a NotReady node is by definition in transition",
		},
		{
			name: "freshly created node is cold even when occupied",
			info: nodeInfoWith(newNode("young", 10, true), newPod("app", 1000, "ReplicaSet")),
			want: true,
			why:  "still inside the boot window",
		},
		{
			name: "node with a succeeded pod only is cold",
			info: func() *framework.NodeInfo {
				p := newPod("done", 1000, "Job")
				p.Status.Phase = v1.PodSucceeded
				return nodeInfoWith(newNode("finished", 3600, true), p)
			}(),
			want: true,
			why:  "terminated pods do not keep a node alive",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := isCold(tc.info, coldAge)
			if got != tc.want {
				t.Errorf("isCold = %v, want %v. %s", got, tc.want, tc.why)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// priceFor
// ---------------------------------------------------------------------------

func TestPriceFor(t *testing.T) {
	c := &CostAware{args: defaultArgs()}

	t.Run("known instance type comes from the table", func(t *testing.T) {
		n := newNode("n", 3600, true)
		n.Labels["node.kubernetes.io/instance-type"] = "m5.2xlarge"
		if got := c.priceFor(n).HourlyUSD; got != 0.384 {
			t.Errorf("HourlyUSD = %g, want 0.384", got)
		}
	})

	t.Run("unknown instance type falls back to default", func(t *testing.T) {
		n := newNode("n", 3600, true)
		n.Labels["node.kubernetes.io/instance-type"] = "does.not.exist"
		if got := c.priceFor(n).HourlyUSD; got != c.args.DefaultPrice.HourlyUSD {
			t.Errorf("HourlyUSD = %g, want default %g", got, c.args.DefaultPrice.HourlyUSD)
		}
	})

	t.Run("spot capacity is discounted", func(t *testing.T) {
		n := newNode("n", 3600, true)
		n.Labels["node.kubernetes.io/instance-type"] = "m5.2xlarge"
		n.Labels["karpenter.sh/capacity-type"] = "spot"
		want := 0.384 * 0.35
		if got := c.priceFor(n).HourlyUSD; math.Abs(got-want) > 1e-9 {
			t.Errorf("HourlyUSD = %g, want %g", got, want)
		}
	})

	t.Run("annotation overrides everything", func(t *testing.T) {
		n := newNode("n", 3600, true)
		n.Labels["node.kubernetes.io/instance-type"] = "m5.2xlarge"
		n.Labels["karpenter.sh/capacity-type"] = "spot"
		n.Annotations[priceOverrideAnnotation] = "1.2345"
		n.Annotations[bootOverrideAnnotation] = "999"
		p := c.priceFor(n)
		if p.HourlyUSD != 1.2345 {
			t.Errorf("HourlyUSD = %g, want 1.2345", p.HourlyUSD)
		}
		if p.BootSeconds != 999 {
			t.Errorf("BootSeconds = %g, want 999", p.BootSeconds)
		}
	})

	t.Run("malformed annotation is ignored, not fatal", func(t *testing.T) {
		n := newNode("n", 3600, true)
		n.Labels["node.kubernetes.io/instance-type"] = "m5.2xlarge"
		n.Annotations[priceOverrideAnnotation] = "not-a-number"
		if got := c.priceFor(n).HourlyUSD; got != 0.384 {
			t.Errorf("HourlyUSD = %g, want the table value 0.384", got)
		}
	})
}

// ---------------------------------------------------------------------------
// NormalizeScore
// ---------------------------------------------------------------------------

func TestNormalizeScore_InvertsAndSpansTheRange(t *testing.T) {
	c := &CostAware{args: defaultArgs()}
	state := framework.NewCycleState()

	if status := c.PreScore(context.Background(), state, nil, nil); !status.IsSuccess() {
		t.Fatalf("PreScore failed: %v", status)
	}
	st, err := c.getOrCreateState(state)
	if err != nil {
		t.Fatal(err)
	}
	st.set("cheap", 1.0)
	st.set("mid", 2.0)
	st.set("expensive", 3.0)

	scores := framework.NodeScoreList{
		{Name: "cheap"}, {Name: "mid"}, {Name: "expensive"},
	}

	if status := c.NormalizeScore(context.Background(), state, nil, scores); !status.IsSuccess() {
		t.Fatalf("NormalizeScore failed: %v", status)
	}

	byName := map[string]int64{}
	for _, s := range scores {
		byName[s.Name] = s.Score
	}

	if byName["cheap"] != framework.MaxNodeScore {
		t.Errorf("lowest Phi must get MaxNodeScore, got %d", byName["cheap"])
	}
	if byName["expensive"] != 0 {
		t.Errorf("highest Phi must get 0, got %d", byName["expensive"])
	}
	if byName["mid"] <= 0 || byName["mid"] >= framework.MaxNodeScore {
		t.Errorf("mid score should be interior, got %d", byName["mid"])
	}
}

func TestNormalizeScore_AllEqualGetsMaxScore(t *testing.T) {
	c := &CostAware{args: defaultArgs()}
	state := framework.NewCycleState()

	c.PreScore(context.Background(), state, nil, nil)
	st, _ := c.getOrCreateState(state)
	st.set("a", 5.0)
	st.set("b", 5.0)

	scores := framework.NodeScoreList{{Name: "a"}, {Name: "b"}}
	if status := c.NormalizeScore(context.Background(), state, nil, scores); !status.IsSuccess() {
		t.Fatalf("NormalizeScore failed: %v", status)
	}
	for _, s := range scores {
		if s.Score != framework.MaxNodeScore {
			t.Errorf("with zero span every node should tie at MaxNodeScore, %s got %d",
				s.Name, s.Score)
		}
	}
}

func TestNormalizeScore_UnknownNodeScoresZero(t *testing.T) {
	c := &CostAware{args: defaultArgs()}
	state := framework.NewCycleState()

	c.PreScore(context.Background(), state, nil, nil)
	st, _ := c.getOrCreateState(state)
	st.set("known", 1.0)

	scores := framework.NodeScoreList{{Name: "known"}, {Name: "never-scored"}}
	if status := c.NormalizeScore(context.Background(), state, nil, scores); !status.IsSuccess() {
		t.Fatalf("NormalizeScore failed: %v", status)
	}
	for _, s := range scores {
		if s.Name == "never-scored" && s.Score != 0 {
			t.Errorf("a node absent from the cycle state should score 0, got %d", s.Score)
		}
	}
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

func TestPodCPURequest(t *testing.T) {
	pod := newPod("p", 1500, "ReplicaSet")
	pod.Spec.Containers = append(pod.Spec.Containers, v1.Container{
		Name: "sidecar",
		Resources: v1.ResourceRequirements{
			Requests: v1.ResourceList{
				v1.ResourceCPU: *resource.NewMilliQuantity(500, resource.DecimalSI),
			},
		},
	})
	if got := podCPURequest(pod); got != 2000 {
		t.Errorf("podCPURequest = %d, want 2000 (sum across containers)", got)
	}
}

func TestPodCPURequest_NoRequestsIsZero(t *testing.T) {
	pod := &v1.Pod{Spec: v1.PodSpec{Containers: []v1.Container{{Name: "c"}}}}
	if got := podCPURequest(pod); got != 0 {
		t.Errorf("podCPURequest = %d, want 0", got)
	}
}

// TestCycleState_ConcurrentWritesKeepEveryNode is the regression test for the
// bug that made the scenario suite flaky: the framework fans Score out over the
// nodes in parallel, all writing the same cycleState. Unguarded, the map write
// races -- Go can panic, and a lost write drops a node from raw, which
// NormalizeScore then scores 0. Run with -race.
func TestCycleState_ConcurrentWritesKeepEveryNode(t *testing.T) {
	const nodes = 200

	c := &CostAware{args: defaultArgs()}
	state := framework.NewCycleState()
	if status := c.PreScore(context.Background(), state, nil, nil); !status.IsSuccess() {
		t.Fatalf("PreScore failed: %v", status)
	}
	st, err := c.getOrCreateState(state)
	if err != nil {
		t.Fatal(err)
	}

	var wg sync.WaitGroup
	for i := 0; i < nodes; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			st.set(fmt.Sprintf("node-%03d", i), float64(i))
		}(i)
	}
	wg.Wait()

	for i := 0; i < nodes; i++ {
		name := fmt.Sprintf("node-%03d", i)
		got, ok := st.get(name)
		if !ok {
			t.Fatalf("%s was lost from the cycle state", name)
		}
		if got != float64(i) {
			t.Errorf("%s: got Phi %v, want %v", name, got, float64(i))
		}
	}
}
