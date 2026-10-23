// Package edge is the Go store-and-forward edge node for a clinic
// site. It captures items locally (HL7 text, FHIR Observations,
// device readings), queues them durably on disk in receive order,
// and syncs them to the FastAPI core through the SAME existing
// contract the MLLP gateway uses:
//
//	POST {CORE_URL}/devices/offline/batches
//	Authorization: Bearer <OPENCARE_GATEWAY_TOKEN>   (when set)
//	{"items":[{"kind":"hl7","payload":"<raw HL7 text>"}, ...]}
//
// Success is exactly HTTP 201 (backend/app/devices.py
// queue_offline_batch). No endpoint is invented and no second store
// exists: the core's raw zone is where synced items land (as held
// entries), and this node's queue is only the site's outbox.
//
// Delivery semantics — AT-LEAST-ONCE, stated honestly:
//
//   - An item is marked synced ONLY after the core answers 201 for
//     the batch containing it. Marking = moving its envelope file
//     into the queue dir's synced/ subdir.
//   - On any failure (core down, non-201, timeout) the sync stops at
//     that batch, keeps every remaining item pending in receive
//     order, backs off, and retries later. Nothing is dropped.
//   - A crash between the core's 201 commit and the local mark can
//     re-send an item on the next sync. Duplicates are therefore
//     possible; the core's raw zone tolerates this by design (items
//     land as held raw entries and are replayed/governed downstream,
//     never auto-curated).
package edge

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"sync"
	"time"

	"opencare/services/internal/coreclient"
	"opencare/services/internal/queue"
)

// Config is edge-node configuration, all from the environment /
// flags in cmd/edge-node. No secret is hardcoded.
type Config struct {
	Addr         string // EDGE_ADDR, default :8082
	QueueDir     string // EDGE_QUEUE_DIR, default ./edge-queue
	CoreURL      string // CORE_URL, default http://127.0.0.1:8000
	Token        string // OPENCARE_GATEWAY_TOKEN
	SyncInterval time.Duration
	BatchSize    int // items per POST, default 50
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
		Addr:         get("EDGE_ADDR", ":8082"),
		QueueDir:     get("EDGE_QUEUE_DIR", "./edge-queue"),
		CoreURL:      get("CORE_URL", "http://127.0.0.1:8000"),
		Token:        os.Getenv("OPENCARE_GATEWAY_TOKEN"),
		SyncInterval: 5 * time.Second,
		BatchSize:    50,
	}
}

// NormalizePayload validates a capture payload against the core
// contract and returns the JSON to store/forward:
//
//   - kind "hl7": payload must be a JSON string (the raw HL7 text);
//     a bare object is rejected — the core requires HL7 text.
//   - kinds "observation"/"reading": payload must be a JSON object;
//     a JSON string that itself parses as an object is accepted and
//     unwrapped to the object (the core requires an object).
func NormalizePayload(kind string, raw json.RawMessage) (json.RawMessage, error) {
	if !coreclient.ValidKind(kind) {
		return nil, fmt.Errorf("kind must be one of hl7|observation|reading, got %q", kind)
	}
	if len(raw) == 0 {
		return nil, fmt.Errorf("payload is required")
	}
	if kind == "hl7" {
		var s string
		if err := json.Unmarshal(raw, &s); err != nil {
			return nil, fmt.Errorf("hl7 payload must be a string (the raw HL7 text)")
		}
		if s == "" {
			return nil, fmt.Errorf("hl7 payload must not be empty")
		}
		out, _ := json.Marshal(s)
		return out, nil
	}
	// observation / reading: must end up as a JSON object.
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(raw, &obj); err == nil && obj != nil {
		out, _ := json.Marshal(obj)
		return out, nil
	}
	var s string
	if err := json.Unmarshal(raw, &s); err == nil {
		if err := json.Unmarshal([]byte(s), &obj); err == nil && obj != nil {
			out, _ := json.Marshal(obj)
			return out, nil
		}
	}
	return nil, fmt.Errorf("%s payload must be a JSON object", kind)
}

// SyncResult is the outcome of one sync pass.
type SyncResult struct {
	Attempted int       `json:"attempted"`
	Synced    int       `json:"synced"`
	Pending   int       `json:"pending"`
	Error     string    `json:"error,omitempty"`
	At        time.Time `json:"at"`
}

// Node is one edge node instance.
type Node struct {
	cfg    Config
	client *coreclient.Client
	store  *queue.Store

	ln  net.Listener
	srv *http.Server

	mu       sync.Mutex
	lastSync SyncResult
	hasSync  bool
}

// New opens (creating if needed) the queue directory.
func New(cfg Config) (*Node, error) {
	if cfg.BatchSize <= 0 {
		cfg.BatchSize = 50
	}
	if cfg.SyncInterval <= 0 {
		cfg.SyncInterval = 5 * time.Second
	}
	store, err := queue.Open(cfg.QueueDir)
	if err != nil {
		return nil, fmt.Errorf("opening queue dir %s: %w", cfg.QueueDir, err)
	}
	return &Node{
		cfg:    cfg,
		client: coreclient.New(cfg.CoreURL, cfg.Token),
		store:  store,
	}, nil
}

// Store returns the underlying queue store (status/tests).
func (n *Node) Store() *queue.Store { return n.store }

// Client returns the core client.
func (n *Node) Client() *coreclient.Client { return n.client }

// Capture validates and durably queues one item, in receive order.
// The envelope is fsynced on disk before Capture returns.
func (n *Node) Capture(kind string, payload json.RawMessage) (queue.Envelope, error) {
	normalized, err := NormalizePayload(kind, payload)
	if err != nil {
		return queue.Envelope{}, err
	}
	return n.store.Enqueue(kind, normalized)
}

// SyncOnce drains the queue once: pending items are POSTed in
// receive order, in batches of up to BatchSize; an item is marked
// synced (moved to synced/) only after its batch gets a 201. On the
// first failure the pass stops, remaining items stay pending in
// order, and the returned error is non-nil.
func (n *Node) SyncOnce(ctx context.Context) (res SyncResult, retErr error) {
	res = SyncResult{At: time.Now().UTC()}
	defer func() {
		if pending, perr := n.store.PendingCount(); perr == nil {
			res.Pending = pending
		}
		n.mu.Lock()
		n.lastSync = res
		n.hasSync = true
		n.mu.Unlock()
	}()
	for {
		pending, err := n.store.Pending()
		if err != nil {
			res.Error = err.Error()
			return res, err
		}
		if len(pending) == 0 {
			return res, nil
		}
		batch := pending
		if len(batch) > n.cfg.BatchSize {
			batch = batch[:n.cfg.BatchSize]
		}
		items := make([]coreclient.Item, len(batch))
		for i, env := range batch {
			items[i] = coreclient.Item{Kind: env.Kind, Payload: env.Payload}
		}
		res.Attempted += len(batch)
		if err := n.client.PostBatch(ctx, items); err != nil {
			res.Error = err.Error()
			return res, err
		}
		for _, env := range batch {
			if err := n.store.MarkSynced(env); err != nil {
				err = fmt.Errorf("core accepted batch but marking seq %d synced failed: %w", env.Seq, err)
				res.Error = err.Error()
				return res, err
			}
			res.Synced++
		}
	}
}

// Status is the node's status snapshot (also GET /status).
func (n *Node) Status(ctx context.Context) map[string]any {
	pending, _ := n.store.PendingCount()
	synced, _ := n.store.SyncedCount()
	n.mu.Lock()
	last := n.lastSync
	has := n.hasSync
	n.mu.Unlock()
	out := map[string]any{
		"service":        "edge-node",
		"pending":        pending,
		"synced":         synced,
		"core_reachable": n.client.Reachable(ctx),
		"queue_dir":      n.cfg.QueueDir,
	}
	if has {
		out["last_sync"] = last
	} else {
		out["last_sync"] = nil
	}
	return out
}

// --- HTTP capture surface -------------------------------------------------

type captureRequest struct {
	Kind    string          `json:"kind"`
	Payload json.RawMessage `json:"payload"`
}

func (n *Node) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/capture", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var req captureRequest
		dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8*1024*1024))
		if err := dec.Decode(&req); err != nil {
			http.Error(w, "request body must be JSON {kind, payload}", http.StatusBadRequest)
			return
		}
		env, err := n.Capture(req.Kind, req.Payload)
		if err != nil {
			http.Error(w, err.Error(), http.StatusUnprocessableEntity)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(map[string]any{"id": env.ID, "seq": env.Seq})
	})
	mux.HandleFunc("/status", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
		defer cancel()
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(n.Status(ctx))
	})
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "ok", "service": "edge-node"})
	})
	// Phase 17 (BD-61): Prometheus text exposition — queue depth
	// from the node's own status, hand-rolled (dependency-free).
	mux.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
		defer cancel()
		st := n.Status(ctx)
		pending, _ := st["pending"].(int)
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		fmt.Fprintf(w, "# HELP opencare_edge_pending_items Captured items awaiting sync to the core.\n")
		fmt.Fprintf(w, "# TYPE opencare_edge_pending_items gauge\n")
		fmt.Fprintf(w, "opencare_edge_pending_items %d\n", pending)
		fmt.Fprintf(w, "# HELP opencare_edge_up Health endpoint liveness (always 1 when served).\n")
		fmt.Fprintf(w, "# TYPE opencare_edge_up gauge\n")
		fmt.Fprintf(w, "opencare_edge_up 1\n")
	})
	return mux
}

// Start binds the capture HTTP listener.
func (n *Node) Start() error {
	ln, err := net.Listen("tcp", n.cfg.Addr)
	if err != nil {
		return fmt.Errorf("edge listen %s: %w", n.cfg.Addr, err)
	}
	n.ln = ln
	n.srv = &http.Server{Handler: n.handler()}
	go func() { _ = n.srv.Serve(ln) }()
	return nil
}

// Addr returns the actual listen address (after Start).
func (n *Node) Addr() string {
	if n.ln == nil {
		return n.cfg.Addr
	}
	return n.ln.Addr().String()
}

// Close stops the HTTP listener.
func (n *Node) Close() {
	if n.srv != nil {
		_ = n.srv.Close()
	}
}

// SyncLoop syncs periodically with backoff until ctx is cancelled.
// Failures keep items pending (SyncOnce semantics); the loop only
// controls timing.
func (n *Node) SyncLoop(ctx context.Context) {
	backoff := time.Second
	for {
		_, err := n.SyncOnce(ctx)
		wait := n.cfg.SyncInterval
		if err != nil {
			wait = backoff
			backoff *= 2
			if backoff > 30*time.Second {
				backoff = 30 * time.Second
			}
		} else {
			backoff = time.Second
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(wait):
		}
	}
}
