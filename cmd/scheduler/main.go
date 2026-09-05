// Command scheduler is a drop-in replacement for kube-scheduler with the
// CostAware Score plugin compiled in.
//
// Build:
//
//	go build -o bin/costaware-scheduler ./cmd/scheduler
//
// It accepts every flag the upstream scheduler accepts, most importantly
// --config pointing at a KubeSchedulerConfiguration that enables the plugin.
package main

import (
	"os"

	"k8s.io/component-base/cli"
	"k8s.io/kubernetes/cmd/kube-scheduler/app"

	"github.com/camilla-m/k8s-costaware/pkg/costaware"
)

func main() {
	command := app.NewSchedulerCommand(
		app.WithPlugin(costaware.Name, costaware.New),
	)
	code := cli.Run(command)
	os.Exit(code)
}
