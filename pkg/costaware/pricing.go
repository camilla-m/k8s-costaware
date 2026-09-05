package costaware

import (
	"strconv"
	"time"

	v1 "k8s.io/api/core/v1"
)

// InstancePrice carries the two economic parameters the model needs per node
// class: the operational cost alpha (USD/hour) and the boot latency that
// grounds the transition penalty delta.
//
// BootSeconds should be MEASURED, not assumed. See docs/measuring-delta.md:
// there is no published table of provisioning-to-Ready latency per instance
// type, and producing one is itself a contribution.
type InstancePrice struct {
	HourlyUSD   float64 `json:"hourlyUSD"`
	BootSeconds float64 `json:"bootSeconds"`
}

// instanceTypeLabels are checked in order; the first present wins.
var instanceTypeLabels = []string{
	"node.kubernetes.io/instance-type",
	"beta.kubernetes.io/instance-type",
	// escape hatch for kwok fake nodes and on-prem clusters
	"costaware.unirio.br/instance-type",
}

// priceOverrideAnnotation lets an operator (or the benchmark harness) pin an
// explicit hourly price on a node, bypassing the table entirely.
const (
	priceOverrideAnnotation = "costaware.unirio.br/hourly-usd"
	bootOverrideAnnotation  = "costaware.unirio.br/boot-seconds"
)

func (c *CostAware) priceFor(node *v1.Node) InstancePrice {
	p := c.args.DefaultPrice

	for _, key := range instanceTypeLabels {
		if it, ok := node.Labels[key]; ok {
			if tp, found := c.args.PriceTable[it]; found {
				p = tp
			}
			break
		}
	}

	// Spot capacity is materially cheaper; the karpenter/eks label is the
	// de-facto standard. We apply a flat discount only if no explicit
	// override is present.
	if ct, ok := node.Labels["karpenter.sh/capacity-type"]; ok && ct == "spot" {
		p.HourlyUSD *= 0.35
	} else if ct, ok := node.Labels["eks.amazonaws.com/capacityType"]; ok && ct == "SPOT" {
		p.HourlyUSD *= 0.35
	}

	if v, ok := node.Annotations[priceOverrideAnnotation]; ok {
		if f, err := parseFloat(v); err == nil {
			p.HourlyUSD = f
		}
	}
	if v, ok := node.Annotations[bootOverrideAnnotation]; ok {
		if f, err := parseFloat(v); err == nil {
			p.BootSeconds = f
		}
	}
	return p
}

// metaNowSub returns the node age in seconds.
func metaNowSub(node *v1.Node) float64 {
	return time.Since(node.CreationTimestamp.Time).Seconds()
}

func parseFloat(s string) (float64, error) {
	return strconv.ParseFloat(s, 64)
}

// DefaultPriceTable is a starting point ONLY. Before any published result,
// regenerate it from the live AWS Price List API for the target region and
// record the retrieval date — prices move, and a reviewer will ask.
//
// Boot latencies below are placeholders pending the measurement campaign.
// The GPU entries deliberately carry large BootSeconds: pulling a multi-GB
// model image is exactly the "cold start" regime the thesis targets.
func DefaultPriceTable() map[string]InstancePrice {
	return map[string]InstancePrice{
		// general purpose
		"m5.large":    {HourlyUSD: 0.096, BootSeconds: 150},
		"m5.xlarge":   {HourlyUSD: 0.192, BootSeconds: 150},
		"m5.2xlarge":  {HourlyUSD: 0.384, BootSeconds: 155},
		"m5.4xlarge":  {HourlyUSD: 0.768, BootSeconds: 160},
		"m6i.large":   {HourlyUSD: 0.095, BootSeconds: 145},
		"m6i.xlarge":  {HourlyUSD: 0.190, BootSeconds: 145},
		"m6i.2xlarge": {HourlyUSD: 0.380, BootSeconds: 150},

		// compute optimized (cheaper per core -> the price-aware win case)
		"c5.large":    {HourlyUSD: 0.085, BootSeconds: 145},
		"c5.xlarge":   {HourlyUSD: 0.170, BootSeconds: 145},
		"c5.2xlarge":  {HourlyUSD: 0.340, BootSeconds: 150},
		"c6i.4xlarge": {HourlyUSD: 0.680, BootSeconds: 155},

		// memory optimized (expensive per core -> bin-packing traps)
		"r5.large":   {HourlyUSD: 0.126, BootSeconds: 150},
		"r5.xlarge":  {HourlyUSD: 0.252, BootSeconds: 150},
		"r5.2xlarge": {HourlyUSD: 0.504, BootSeconds: 155},

		// GPU: the high-delta regime
		"g4dn.xlarge":  {HourlyUSD: 0.526, BootSeconds: 600},
		"g5.xlarge":    {HourlyUSD: 1.006, BootSeconds: 720},
		"p3.2xlarge":   {HourlyUSD: 3.060, BootSeconds: 900},
		"p4d.24xlarge": {HourlyUSD: 32.77, BootSeconds: 1200},
	}
}
