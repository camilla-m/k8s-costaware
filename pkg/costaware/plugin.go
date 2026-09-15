// Package costaware implements a cost-aware, temporally-sticky Score plugin
// for the Kubernetes scheduler.
//
// It is the in-cluster realization of the perceived-cost criterion
//
//	Phi_i = alpha_i + I(i not in S_{t-1}) * delta_i
//
// from the Temporal Greedy Constructive Heuristic (TGCH), adapted to the
// pod-at-a-time nature of kube-scheduler:
//
//   - alpha_i  -> hourly price of node i, normalized per allocatable CPU
//     ($/core-hour), so that big-cheap and small-expensive nodes
//     are comparable.
//   - delta_i  -> startup / stickiness penalty, charged only when the node is
//     "cold". A node is cold when it currently hosts no
//     non-DaemonSet pod (placing a pod there blocks scale-down and
//     therefore commits the cluster to paying for it), or when it
//     is NotReady, or younger than ColdNodeAgeSeconds.
//   - packing -> a MostAllocated-style term, so that among equally priced
//     warm nodes we still consolidate.
//
// IMPORTANT: the scheduler framework API is not stable across minor releases.
// This file targets k8s.io/kubernetes v1.31.x, where ScorePlugin.Score
// receives a nodeName string. On v1.32+ the signature takes *framework.NodeInfo
// instead; adjust Score() accordingly and pin the version in go.mod.
package costaware

import (
	"context"
	"fmt"
	"math"
	"sync"

	v1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/klog/v2"
	"k8s.io/kubernetes/pkg/scheduler/framework"
	frameworkruntime "k8s.io/kubernetes/pkg/scheduler/framework/runtime"
)

const (
	// Name is the plugin name used in the KubeSchedulerConfiguration.
	Name = "CostAware"

	// stateKey caches the per-cycle min/max of Phi for normalization.
	stateKey framework.StateKey = "CostAwareScores"
)

// Args is the plugin configuration, supplied via pluginConfig in the
// KubeSchedulerConfiguration.
type Args struct {
	// TransitionRatio (R) multiplies the startup penalty relative to the
	// operational cost, mirroring the sensitivity parameter of the paper.
	// R = 0 disables stickiness (pure price-aware scheduling).
	TransitionRatio float64 `json:"transitionRatio"`

	// PackingWeight in [0,1] blends the consolidation term into Phi.
	// 0 = pure cost, 1 = pure MostAllocated.
	PackingWeight float64 `json:"packingWeight"`

	// ColdNodeAgeSeconds: nodes younger than this are always considered cold,
	// approximating the boot latency window.
	ColdNodeAgeSeconds int64 `json:"coldNodeAgeSeconds"`

	// PriceTable maps an instance type to its hourly on-demand price (USD)
	// and its measured boot-to-Ready latency in seconds. If a node's
	// instance type is absent, DefaultPrice is used.
	PriceTable map[string]InstancePrice `json:"priceTable"`

	// DefaultPrice is the fallback for unknown instance types.
	DefaultPrice InstancePrice `json:"defaultPrice"`
}

// CostAware is the plugin implementation.
type CostAware struct {
	handle framework.Handle
	args   Args
}

var (
	_ framework.PreScorePlugin  = &CostAware{}
	_ framework.ScorePlugin     = &CostAware{}
	_ framework.ScoreExtensions = &CostAware{}
)

// New is the plugin factory registered with the scheduler command.
func New(_ context.Context, obj runtime.Object, h framework.Handle) (framework.Plugin, error) {
	args := defaultArgs()
	if obj != nil {
		if err := frameworkruntime.DecodeInto(obj, &args); err != nil {
			return nil, fmt.Errorf("decoding %s args: %w", Name, err)
		}
	}
	if args.PackingWeight < 0 || args.PackingWeight > 1 {
		return nil, fmt.Errorf("packingWeight must be in [0,1], got %v", args.PackingWeight)
	}
	klog.InfoS("CostAware plugin initialized",
		"transitionRatio", args.TransitionRatio,
		"packingWeight", args.PackingWeight,
		"priceTableSize", len(args.PriceTable))
	return &CostAware{handle: h, args: args}, nil
}

func defaultArgs() Args {
	return Args{
		TransitionRatio:    1.0,
		PackingWeight:      0.2,
		ColdNodeAgeSeconds: 300,
		DefaultPrice:       InstancePrice{HourlyUSD: 0.10, BootSeconds: 180},
		PriceTable:         DefaultPriceTable(),
	}
}

func (c *CostAware) Name() string { return Name }

// perNodeScore holds the raw (lower-is-better) perceived cost for a node.
// The framework wants higher-is-better, so NormalizeScore inverts it.
type perNodeScore struct {
	phi float64
}

// cycleState caches the per-node raw Phi within a single scheduling cycle.
//
// The scheduler runs Score for every feasible node in PARALLEL goroutines
// (framework parallelize), so every access to raw must hold mu. Without it
// the concurrent map writes crash the whole scheduler process with
// "fatal error: concurrent map writes".
type cycleState struct {
	mu  sync.Mutex
	raw map[string]float64
}

func (s *cycleState) set(node string, phi float64) {
	s.mu.Lock()
	s.raw[node] = phi
	s.mu.Unlock()
}

func (s *cycleState) get(node string) (float64, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	v, ok := s.raw[node]
	return v, ok
}

func (s *cycleState) Clone() framework.StateData {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := &cycleState{raw: make(map[string]float64, len(s.raw))}
	for k, v := range s.raw {
		out.raw[k] = v
	}
	return out
}

// PreScore runs once per cycle, before the parallel Score fan-out, and is the
// only place the cycle state is created. Doing it here (single-threaded) avoids
// a create race between Score goroutines.
func (c *CostAware) PreScore(_ context.Context, state *framework.CycleState, _ *v1.Pod, _ []*framework.NodeInfo) *framework.Status {
	state.Write(stateKey, &cycleState{raw: make(map[string]float64)})
	return nil
}

// Score computes the raw perceived cost Phi for a candidate node.
//
// We return the value scaled into int64 here only as a placeholder; the real
// ranking happens in NormalizeScore, which min-max normalizes Phi across the
// feasible set. This mirrors the TGCH sort step, which is also relative.
func (c *CostAware) Score(ctx context.Context, state *framework.CycleState, pod *v1.Pod, nodeName string) (int64, *framework.Status) {
	nodeInfo, err := c.handle.SnapshotSharedLister().NodeInfos().Get(nodeName)
	if err != nil {
		return 0, framework.AsStatus(fmt.Errorf("getting node %q: %w", nodeName, err))
	}
	node := nodeInfo.Node()
	if node == nil {
		return 0, framework.AsStatus(fmt.Errorf("node %q not found", nodeName))
	}

	if nodeInfo.Allocatable.MilliCPU <= 0 {
		return 0, framework.NewStatus(framework.Error, "node has no allocatable CPU")
	}

	phi := computePhi(phiInput{
		Price:           c.priceFor(node),
		AllocatableCPU:  float64(nodeInfo.Allocatable.MilliCPU),
		RequestedCPU:    float64(nodeInfo.Requested.MilliCPU),
		IncomingPodCPU:  float64(podCPURequest(pod)),
		Cold:            isCold(nodeInfo, c.args.ColdNodeAgeSeconds),
		TransitionRatio: c.args.TransitionRatio,
		PackingWeight:   c.args.PackingWeight,
	})

	st, err := getOrCreateState(state)
	if err != nil {
		return 0, framework.AsStatus(err)
	}
	st.set(nodeName, phi)

	return 0, nil
}

// phiInput carries everything computePhi needs, deliberately free of any
// Kubernetes type, so the economic core of the plugin can be unit tested
// without a scheduler Handle, a snapshot lister or a running cluster.
type phiInput struct {
	Price           InstancePrice
	AllocatableCPU  float64 // milli-cores
	RequestedCPU    float64 // milli-cores already requested on the node
	IncomingPodCPU  float64 // milli-cores of the pod being scheduled
	Cold            bool
	TransitionRatio float64 // R
	PackingWeight   float64 // in [0,1]
}

// computePhi is the perceived cost of placing a pod on a node. Lower is better.
//
//	Phi = (1-w) * (alpha + [cold] * delta)  -  w * utilization * alpha
//
// where alpha is USD per milli-core-hour and delta amortizes the boot window
// over the node's capacity, scaled by the transition ratio R.
//
// Setting R = 0 removes the stickiness term entirely, which is the ablation
// that separates "price awareness" from "temporal inertia".
func computePhi(in phiInput) float64 {
	if in.AllocatableCPU <= 0 {
		return math.Inf(1)
	}

	alpha := in.Price.HourlyUSD / in.AllocatableCPU

	var delta float64
	if in.Cold {
		delta = in.TransitionRatio * in.Price.HourlyUSD *
			(in.Price.BootSeconds / 3600.0) / in.AllocatableCPU
	}

	util := math.Min((in.RequestedCPU+in.IncomingPodCPU)/in.AllocatableCPU, 1.0)

	return (1.0-in.PackingWeight)*(alpha+delta) - in.PackingWeight*util*alpha
}

func (c *CostAware) ScoreExtensions() framework.ScoreExtensions { return c }

// NormalizeScore converts the raw perceived costs into the framework's
// [0, MaxNodeScore] higher-is-better space via min-max inversion.
func (c *CostAware) NormalizeScore(ctx context.Context, state *framework.CycleState, pod *v1.Pod, scores framework.NodeScoreList) *framework.Status {
	st, err := getOrCreateState(state)
	if err != nil {
		return framework.AsStatus(err)
	}

	minPhi, maxPhi := math.Inf(1), math.Inf(-1)
	for _, s := range scores {
		v, ok := st.get(s.Name)
		if !ok {
			continue
		}
		minPhi = math.Min(minPhi, v)
		maxPhi = math.Max(maxPhi, v)
	}

	span := maxPhi - minPhi
	for i := range scores {
		v, ok := st.get(scores[i].Name)
		if !ok {
			scores[i].Score = 0
			continue
		}
		if span <= 0 {
			scores[i].Score = framework.MaxNodeScore
			continue
		}
		// invert: cheapest perceived cost -> MaxNodeScore
		norm := 1.0 - (v-minPhi)/span
		scores[i].Score = int64(math.Round(norm * float64(framework.MaxNodeScore)))
	}
	return nil
}

// isCold reports whether placing a pod on this node constitutes a state
// transition that the cluster would otherwise not pay for.
func isCold(nodeInfo *framework.NodeInfo, coldAgeSeconds int64) bool {
	node := nodeInfo.Node()

	// Recently created nodes are still within their boot window.
	if coldAgeSeconds > 0 {
		age := int64(metaNowSub(node))
		if age < coldAgeSeconds {
			return true
		}
	}

	// NotReady nodes are, by definition, in transition.
	for _, cond := range node.Status.Conditions {
		if cond.Type == v1.NodeReady && cond.Status != v1.ConditionTrue {
			return true
		}
	}

	// Empty node: no workload pod is running, so the node is a scale-down
	// candidate. Scheduling here commits the cluster to keeping it alive.
	for _, p := range nodeInfo.Pods {
		if isWorkloadPod(p.Pod) {
			return false
		}
	}
	return true
}

// isWorkloadPod excludes DaemonSet and static/mirror pods, which are present
// on every node and therefore carry no information about whether the node is
// actually being used.
func isWorkloadPod(p *v1.Pod) bool {
	if p == nil {
		return false
	}
	if _, isMirror := p.Annotations[v1.MirrorPodAnnotationKey]; isMirror {
		return false
	}
	for _, or := range p.OwnerReferences {
		if or.Kind == "DaemonSet" {
			return false
		}
	}
	if p.Status.Phase == v1.PodSucceeded || p.Status.Phase == v1.PodFailed {
		return false
	}
	return true
}

func podCPURequest(pod *v1.Pod) int64 {
	var total int64
	for _, ctr := range pod.Spec.Containers {
		if q, ok := ctr.Resources.Requests[v1.ResourceCPU]; ok {
			total += q.MilliValue()
		}
	}
	return total
}

// stateCreateMu serializes the lazy-create fallback below. In the normal path
// PreScore has already installed the state single-threaded and this lock is
// never contended; it only matters if PreScore was skipped for some profile,
// where two parallel Score goroutines could otherwise both create and race the
// CycleState write.
var stateCreateMu sync.Mutex

func getOrCreateState(state *framework.CycleState) (*cycleState, error) {
	if raw, err := state.Read(stateKey); err == nil {
		st, ok := raw.(*cycleState)
		if !ok {
			return nil, fmt.Errorf("unexpected state type %T", raw)
		}
		return st, nil
	}

	stateCreateMu.Lock()
	defer stateCreateMu.Unlock()
	// Re-check: another goroutine may have created it while we waited.
	if raw, err := state.Read(stateKey); err == nil {
		if st, ok := raw.(*cycleState); ok {
			return st, nil
		}
	}
	st := &cycleState{raw: make(map[string]float64)}
	state.Write(stateKey, st)
	return st, nil
}
