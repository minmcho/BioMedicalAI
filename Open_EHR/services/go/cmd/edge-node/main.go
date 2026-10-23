// Command edge-node is the Go store-and-forward node for a clinic
// site: it captures HL7 / Observation / reading items over a local
// HTTP surface, queues them durably on disk in receive order, and
// syncs them to the FastAPI core through the existing offline
// raw-zone contract (POST /devices/offline/batches, success = 201).
//
// Usage:
//
//	edge-node             serve the capture HTTP surface + sync loop
//	edge-node sync-once   drain the queue once and exit:
//	                      0 = queue empty / everything synced,
//	                      1 = sync failed, items remain pending, or
//	                          a configuration / queue error.
//
// Configuration (environment; flags win where both are given):
//
//	EDGE_ADDR              capture HTTP listen address (default :8082)
//	EDGE_QUEUE_DIR         queue directory             (default ./edge-queue)
//	CORE_URL               FastAPI core base URL       (default http://127.0.0.1:8000)
//	OPENCARE_GATEWAY_TOKEN Bearer token for the core contract (no default)
//
// Delivery is at-least-once: an item is marked synced only after the
// core's 201. See services/go/README.md.
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	"opencare/services/internal/edge"
	"opencare/services/internal/redact"
)

func main() {
	cfg := edge.ConfigFromEnv()
	flag.StringVar(&cfg.Addr, "addr", cfg.Addr, "capture HTTP listen address (env EDGE_ADDR)")
	flag.StringVar(&cfg.QueueDir, "queue-dir", cfg.QueueDir, "queue directory (env EDGE_QUEUE_DIR)")
	flag.StringVar(&cfg.CoreURL, "core-url", cfg.CoreURL, "FastAPI core base URL (env CORE_URL)")
	flag.Parse()
	cfg.Token = os.Getenv("OPENCARE_GATEWAY_TOKEN")

	node, err := edge.New(cfg)
	if err != nil {
		log.Fatalf("edge-node: %s", redact.Redact(err.Error()))
	}

	if flag.Arg(0) == "sync-once" {
		ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
		defer cancel()
		res, err := node.SyncOnce(ctx)
		fmt.Printf("sync-once: attempted=%d synced=%d pending=%d\n", res.Attempted, res.Synced, res.Pending)
		if err != nil {
			fmt.Fprintf(os.Stderr, "sync-once: %v\n", err)
			os.Exit(1)
		}
		if res.Pending > 0 {
			os.Exit(1)
		}
		os.Exit(0)
	}

	if err := node.Start(); err != nil {
		log.Fatalf("edge-node: %s", redact.Redact(err.Error()))
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go node.SyncLoop(ctx)
	log.Printf("edge-node: capture HTTP on %s, queue %s, core %s", node.Addr(), cfg.QueueDir, cfg.CoreURL)

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	cancel()
	node.Close()
}
