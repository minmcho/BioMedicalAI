// Command mllp-gateway is the Go MLLP listener + device gateway:
// the edge/gateway tier in front of the FastAPI core's existing
// offline raw-zone contract (POST /devices/offline/batches).
//
// The Python MLLP listener (backend/app/mllp_server.py) remains the
// in-core listener; this gateway is for site/edge deployment in
// front of the same contract. See services/go/README.md for ACK and
// delivery semantics.
//
// Configuration (environment; flags win where both are given):
//
//	MLLP_ADDR              MLLP listen address        (default :2575)
//	GATEWAY_HEALTH_ADDR    health HTTP listen address (default :8081)
//	GATEWAY_SPOOL_DIR      durable spool directory    (default ./mllp-spool)
//	CORE_URL               FastAPI core base URL      (default http://127.0.0.1:8000)
//	OPENCARE_GATEWAY_TOKEN Bearer token for the core contract (no default)
package main

import (
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"

	"opencare/services/internal/gateway"
	"opencare/services/internal/redact"
)

func main() {
	cfg := gateway.ConfigFromEnv()
	flag.StringVar(&cfg.MLLPAddr, "mllp-addr", cfg.MLLPAddr, "MLLP listen address (env MLLP_ADDR)")
	flag.StringVar(&cfg.HealthAddr, "health-addr", cfg.HealthAddr, "health HTTP listen address (env GATEWAY_HEALTH_ADDR)")
	flag.StringVar(&cfg.SpoolDir, "spool-dir", cfg.SpoolDir, "durable spool directory (env GATEWAY_SPOOL_DIR)")
	flag.StringVar(&cfg.CoreURL, "core-url", cfg.CoreURL, "FastAPI core base URL (env CORE_URL)")
	flag.Parse()
	// The token is read from the environment only — never a flag, so
	// it cannot leak into shell history / process listings as easily.
	cfg.Token = os.Getenv("OPENCARE_GATEWAY_TOKEN")

	gw, err := gateway.New(cfg)
	if err != nil {
		log.Fatalf("mllp-gateway: %s", redact.Redact(err.Error()))
	}
	if err := gw.Start(); err != nil {
		log.Fatalf("mllp-gateway: %s", redact.Redact(err.Error()))
	}
	log.Printf("mllp-gateway: MLLP on %s, health on %s, core %s, spool %s",
		gw.MLLPAddr(), gw.HealthAddr(), cfg.CoreURL, cfg.SpoolDir)

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	gw.Close()
}
