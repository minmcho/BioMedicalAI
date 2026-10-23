// Package gateway is the Go MLLP gateway / device gateway tier.
//
// It sits IN FRONT OF the same FastAPI core raw-zone contract the
// in-core Python MLLP listener ultimately feeds — it does not replace
// that listener and it keeps no store of its own beyond its delivery
// spool:
//
//   - The Python MLLP listener (backend/app/mllp_server.py) remains
//     the IN-CORE listener: it calls app.interop.process_inbound in
//     the API's own process/database.
//   - This Go gateway is the EDGE / GATEWAY tier: it terminates MLLP
//     from analysers and devices at a site, and forwards every
//     accepted message into the core via the EXISTING offline
//     raw-zone contract, POST /devices/offline/batches with
//     {"items":[{"kind":"hl7","payload":"<raw HL7 text>"}]}, success
//     = HTTP 201 (backend/app/devices.py queue_offline_batch). The
//     core lands those items in its raw zone as held entries; no
//     second store and no invented endpoint exist on this side.
//
// ACK semantics (also in the README):
//
//   - Every well-formed message is FIRST durably spooled to disk,
//     then a drain is attempted in receive order. AA is returned only
//     after the payload has been forwarded (core answered 201, spool
//     entry removed) OR is durably spooled (fsynced envelope on
//     disk). Either way, an AA'd message is never lost by this
//     gateway: forwarded payloads are in the core's raw zone, spooled
//     payloads are retried with backoff, in receive order, until the
//     core answers 201.
//   - AR is returned for malformed frames: no complete MLLP frame
//     (including EOF with a partial frame, when the peer can still
//     be answered), a frame over the 4 MB guard (mirroring the
//     Python listener), or an unparseable MSH. AR'd payloads are
//     neither forwarded nor spooled as valid HL7.
//
// Delivery is therefore at-least-once: a crash between the core's
// 201 commit and the local spool removal can re-send one message.
// The core's raw zone tolerates this by design (held raw entries are
// replayed/governed downstream, not auto-curated).
package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"sync"
	"time"

	"opencare/services/internal/coreclient"
	"opencare/services/internal/hl7msg"
	"opencare/services/internal/queue"
	"opencare/services/internal/redact"
)

// Config is gateway configuration. Every value comes from the
// environment / flags in cmd/mllp-gateway — no secret is hardcoded.
type Config struct {
	MLLPAddr     string // MLLP_ADDR, default :2575
	HealthAddr   string // GATEWAY_HEALTH_ADDR, default :8081
	CoreURL      string // CORE_URL, default http://127.0.0.1:8000
	Token        string // OPENCARE_GATEWAY_TOKEN (Bearer token for the core contract)
	SpoolDir     string // GATEWAY_SPOOL_DIR, default ./mllp-spool
	DrainTimeout time.Duration
}

// ConfigFromEnv reads the configuration above from the environment.
func ConfigFromEnv() Config {
	get := func(key, def string) string {
		if v := os.Getenv(key); v != "" {
			return v
		}
		return def
	}
	return Config{
		MLLPAddr:   get("MLLP_ADDR", ":2575"),
		HealthAddr: get("GATEWAY_HEALTH_ADDR", ":8081"),
		CoreURL:    get("CORE_URL", "http://127.0.0.1:8000"),
		Token:      os.Getenv("OPENCARE_GATEWAY_TOKEN"),
		SpoolDir:   get("GATEWAY_SPOOL_DIR", "./mllp-spool"),
	}
}

// Gateway is one running MLLP gateway instance.
type Gateway struct {
	cfg    Config
	client *coreclient.Client
	spool  *queue.Store

	mllpLn    net.Listener
	healthLn  net.Listener
	healthSrv *http.Server

	drainMu   sync.Mutex // serialises drains: receive order is sacred
	wg        sync.WaitGroup
	stopCh    chan struct{}
	stopped   sync.Once
	lastErrMu sync.Mutex
	lastErr   string
}

// New opens the spool directory and builds a gateway (not yet
// listening). CoreURL/Token are taken from cfg.
func New(cfg Config) (*Gateway, error) {
	if cfg.DrainTimeout == 0 {
		cfg.DrainTimeout = 10 * time.Second
	}
	spool, err := queue.Open(cfg.SpoolDir)
	if err != nil {
		return nil, fmt.Errorf("opening spool dir %s: %w", cfg.SpoolDir, err)
	}
	return &Gateway{
		cfg:    cfg,
		client: coreclient.New(cfg.CoreURL, cfg.Token),
		spool:  spool,
		stopCh: make(chan struct{}),
	}, nil
}

// Spool returns the underlying spool store (health/tests).
func (g *Gateway) Spool() *queue.Store { return g.spool }

// Client returns the core client (health/tests).
func (g *Gateway) Client() *coreclient.Client { return g.client }

// Start binds the MLLP listener and the health listener and serves.
func (g *Gateway) Start() error {
	ln, err := net.Listen("tcp", g.cfg.MLLPAddr)
	if err != nil {
		return fmt.Errorf("MLLP listen %s: %w", g.cfg.MLLPAddr, err)
	}
	g.mllpLn = ln

	mux := http.NewServeMux()
	mux.HandleFunc("/health", g.handleHealth)
	// Phase 17 (BD-61): Prometheus text exposition on the health
	// port — spool depth + process start, hand-rolled (this module
	// stays dependency-free; the values are the gateway's own).
	mux.HandleFunc("/metrics", g.handleMetrics)
	g.healthSrv = &http.Server{Handler: mux}
	hln, err := net.Listen("tcp", g.cfg.HealthAddr)
	if err != nil {
		_ = ln.Close()
		return fmt.Errorf("health listen %s: %w", g.cfg.HealthAddr, err)
	}
	g.healthLn = hln

	g.wg.Add(2)
	go func() {
		defer g.wg.Done()
		_ = g.healthSrv.Serve(hln)
	}()
	go func() {
		defer g.wg.Done()
		g.drainLoop()
	}()
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return // listener closed
			}
			g.wg.Add(1)
			go func() {
				defer g.wg.Done()
				g.handleConn(conn)
			}()
		}
	}()
	return nil
}

// MLLPAddr returns the actual MLLP listen address (after Start).
func (g *Gateway) MLLPAddr() string {
	if g.mllpLn == nil {
		return g.cfg.MLLPAddr
	}
	return g.mllpLn.Addr().String()
}

// HealthAddr returns the actual health listen address (after Start).
func (g *Gateway) HealthAddr() string {
	if g.healthLn == nil {
		return g.cfg.HealthAddr
	}
	return g.healthLn.Addr().String()
}

// Close stops listeners and background work.
func (g *Gateway) Close() {
	g.stopped.Do(func() { close(g.stopCh) })
	if g.healthSrv != nil {
		_ = g.healthSrv.Close()
	}
	if g.mllpLn != nil {
		_ = g.mllpLn.Close()
	}
}

// handleMetrics serves Prometheus text format (BD-61).
func (g *Gateway) handleMetrics(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	spooled, err := g.spool.PendingCount()
	if err != nil {
		spooled = -1
	}
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	fmt.Fprintf(w, "# HELP opencare_gateway_spooled_messages MLLP messages durably spooled awaiting the core.\n")
	fmt.Fprintf(w, "# TYPE opencare_gateway_spooled_messages gauge\n")
	fmt.Fprintf(w, "opencare_gateway_spooled_messages %d\n", spooled)
	fmt.Fprintf(w, "# HELP opencare_gateway_up Health endpoint liveness (always 1 when served).\n")
	fmt.Fprintf(w, "# TYPE opencare_gateway_up gauge\n")
	fmt.Fprintf(w, "opencare_gateway_up 1\n")
}

func (g *Gateway) handleHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	spooled, err := g.spool.PendingCount()
	status := "ok"
	if err != nil {
		status = "degraded"
	}
	ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
	defer cancel()
	out := map[string]any{
		"status":         status,
		"spooled":        spooled,
		"core_reachable": g.client.Reachable(ctx),
		"service":        "mllp-gateway",
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(out)
}

// DrainOnce attempts to forward all spooled items, in receive order,
// in batches of up to 50. It stops at the first failed batch (order
// is preserved for the retry) and returns the number forwarded.
func (g *Gateway) DrainOnce(ctx context.Context) (int, error) {
	g.drainMu.Lock()
	defer g.drainMu.Unlock()
	forwarded := 0
	for {
		pending, err := g.spool.Pending()
		if err != nil {
			return forwarded, err
		}
		if len(pending) == 0 {
			return forwarded, nil
		}
		batch := pending
		if len(batch) > 50 {
			batch = batch[:50]
		}
		items := make([]coreclient.Item, len(batch))
		for i, env := range batch {
			items[i] = coreclient.Item{Kind: env.Kind, Payload: env.Payload}
		}
		if err := g.client.PostBatch(ctx, items); err != nil {
			g.setLastErr(err.Error())
			return forwarded, err
		}
		for _, env := range batch {
			if err := g.spool.Remove(env); err != nil {
				return forwarded, fmt.Errorf("core accepted batch but spool remove failed for seq %d: %w", env.Seq, err)
			}
			forwarded++
		}
		g.setLastErr("")
	}
}

func (g *Gateway) setLastErr(s string) {
	g.lastErrMu.Lock()
	defer g.lastErrMu.Unlock()
	g.lastErr = s
}

// LastError returns the most recent drain error, if any.
func (g *Gateway) LastError() string {
	g.lastErrMu.Lock()
	defer g.lastErrMu.Unlock()
	return g.lastErr
}

// drainLoop retries the spool with backoff until Close.
func (g *Gateway) drainLoop() {
	backoff := 200 * time.Millisecond
	for {
		select {
		case <-g.stopCh:
			return
		default:
		}
		ctx, cancel := context.WithTimeout(context.Background(), g.cfg.DrainTimeout)
		n, err := g.DrainOnce(ctx)
		cancel()
		if err == nil && n == 0 {
			// Nothing pending (or nothing to do): idle-poll slowly.
			select {
			case <-g.stopCh:
				return
			case <-time.After(500 * time.Millisecond):
			}
			continue
		}
		if err != nil {
			select {
			case <-g.stopCh:
				return
			case <-time.After(backoff):
			}
			backoff *= 2
			if backoff > 5*time.Second {
				backoff = 5 * time.Second
			}
			continue
		}
		backoff = 200 * time.Millisecond
	}
}

// handleMessage processes one unframed HL7 payload and returns the
// ACK text to frame back. It never loses a well-formed message.
func (g *Gateway) handleMessage(raw string) string {
	if _, err := hl7msg.ParseMSH(raw); err != nil {
		return hl7msg.BuildACK(raw, "AR", "Malformed HL7 message: "+err.Error())
	}
	payload, _ := json.Marshal(raw)
	if _, err := g.spool.Enqueue("hl7", payload); err != nil {
		// Cannot spool and cannot promise delivery: AE, not AA.
		// The error crosses the logging boundary redacted (BD-53).
		log.Printf("mllp-gateway: spool write failed: %s",
			redact.Redact(err.Error()))
		return hl7msg.BuildACK(raw, "AE", "Gateway spool write failed")
	}
	// Best-effort immediate drain (background loop keeps retrying).
	ctx, cancel := context.WithTimeout(context.Background(), g.cfg.DrainTimeout)
	_, _ = g.DrainOnce(ctx)
	cancel()
	// At this point the payload is either forwarded (201) or durably
	// spooled — AA is honest either way.
	return hl7msg.BuildACK(raw, "AA", "")
}

func (g *Gateway) handleConn(conn net.Conn) {
	defer conn.Close()
	var buffer []byte
	chunk := make([]byte, 32*1024)
	for {
		n, err := conn.Read(chunk)
		if n > 0 {
			buffer = append(buffer, chunk[:n]...)
			if len(buffer) > hl7msg.MaxFrameBytes {
				_, _ = conn.Write(hl7msg.Wrap(hl7msg.BuildACK("", "AR", "Frame exceeds maximum size")))
				return
			}
			for {
				msg, rest, complete := hl7msg.ExtractFrame(buffer)
				if !complete {
					buffer = rest
					break
				}
				buffer = rest
				ack := g.handleMessage(msg)
				if _, werr := conn.Write(hl7msg.Wrap(ack)); werr != nil {
					return
				}
				if len(buffer) > hl7msg.MaxFrameBytes {
					_, _ = conn.Write(hl7msg.Wrap(hl7msg.BuildACK("", "AR", "Frame exceeds maximum size")))
					return
				}
			}
		}
		if err != nil {
			if errors.Is(err, io.EOF) && len(buffer) > 0 {
				// Peer sent a partial frame and went away: there is
				// no complete MLLP frame, which is an AR by the
				// stated semantics. Best-effort answer (the peer may
				// already be gone; a half-close can still read it).
				_, _ = conn.Write(hl7msg.Wrap(hl7msg.BuildACK("", "AR", "No complete MLLP frame received")))
			}
			return
		}
	}
}
