package edge_test

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"opencare/services/internal/edge"
)

type stubCore struct {
	mu    sync.Mutex
	fail  bool
	items []stubItem
}

type stubItem struct {
	Kind    string          `json:"kind"`
	Payload json.RawMessage `json:"payload"`
}

func (s *stubCore) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/devices/offline/batches", func(w http.ResponseWriter, r *http.Request) {
		s.mu.Lock()
		defer s.mu.Unlock()
		if s.fail {
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		var body struct {
			Items []stubItem `json:"items"`
		}
		data, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(data, &body); err != nil {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		s.items = append(s.items, body.Items...)
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(map[string]any{"queued": len(body.Items)})
	})
	return mux
}

func (s *stubCore) snapshot() []stubItem {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]stubItem, len(s.items))
	copy(out, s.items)
	return out
}

func newNode(t *testing.T, dir, coreURL string) *edge.Node {
	t.Helper()
	n, err := edge.New(edge.Config{
		Addr:     "127.0.0.1:0",
		QueueDir: dir,
		CoreURL:  coreURL,
		Token:    "test-token",
	})
	if err != nil {
		t.Fatalf("edge.New: %v", err)
	}
	return n
}

func mustJSON(t *testing.T, v any) json.RawMessage {
	t.Helper()
	data, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

const hl7Payload = "MSH|^~\\&|ANALYZER-X|LAB|OPENCARE|OPENCARE|20261018120000||ORU^R01|CTRL-EDGE-1|P|2.5.1\rPID|1||PAT123^^^OPENCARE^FI"

func TestCaptureSyncOnceOrderAndEmptyQueue(t *testing.T) {
	stub := &stubCore{}
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()
	node := newNode(t, t.TempDir(), srv.URL)

	kinds := []string{"hl7", "observation", "reading"}
	payloads := []json.RawMessage{
		mustJSON(t, hl7Payload),
		mustJSON(t, map[string]any{"resourceType": "Observation", "id": "obs-1", "status": "final"}),
		mustJSON(t, map[string]any{"device_key": "fridge-1", "metric": "temperature", "value": 4.2}),
	}
	for i := range kinds {
		env, err := node.Capture(kinds[i], payloads[i])
		if err != nil {
			t.Fatalf("capture %d: %v", i, err)
		}
		if env.Seq != uint64(i+1) {
			t.Fatalf("capture %d seq = %d, want %d", i, env.Seq, i+1)
		}
		if env.ID == "" {
			t.Fatalf("capture %d has empty id", i)
		}
	}

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	res, err := node.SyncOnce(ctx)
	if err != nil {
		t.Fatalf("SyncOnce: %v (result %+v)", err, res)
	}
	if res.Synced != 3 || res.Pending != 0 {
		t.Fatalf("result = %+v, want synced=3 pending=0", res)
	}
	items := stub.snapshot()
	if len(items) != 3 {
		t.Fatalf("stub received %d items, want 3", len(items))
	}
	for i := range kinds {
		if items[i].Kind != kinds[i] {
			t.Fatalf("stub item %d kind = %q, want %q (receive order broken)", i, items[i].Kind, kinds[i])
		}
		// Payloads must round-trip to the same JSON value.
		var got, want any
		if err := json.Unmarshal(items[i].Payload, &got); err != nil {
			t.Fatalf("stub item %d payload decode: %v", i, err)
		}
		if err := json.Unmarshal(payloads[i], &want); err != nil {
			t.Fatal(err)
		}
		gotJSON, _ := json.Marshal(got)
		wantJSON, _ := json.Marshal(want)
		if string(gotJSON) != string(wantJSON) {
			t.Fatalf("stub item %d payload = %s, want %s", i, gotJSON, wantJSON)
		}
	}
	if pending, _ := node.Store().PendingCount(); pending != 0 {
		t.Fatalf("pending = %d, want queue empty after sync", pending)
	}
	if synced, _ := node.Store().SyncedCount(); synced != 3 {
		t.Fatalf("synced = %d, want 3 (items moved to synced/)", synced)
	}
}

func TestRestartPersistencePreservesOrder(t *testing.T) {
	dir := t.TempDir()
	stub := &stubCore{}
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()

	first := newNode(t, dir, srv.URL)
	if _, err := first.Capture("hl7", mustJSON(t, hl7Payload)); err != nil {
		t.Fatal(err)
	}
	if _, err := first.Capture("reading", mustJSON(t, map[string]any{"device_key": "fridge-1", "metric": "temperature", "value": 3.9})); err != nil {
		t.Fatal(err)
	}

	// A brand-new node object over the same directory (a restart).
	second := newNode(t, dir, srv.URL)
	pending, err := second.Store().Pending()
	if err != nil {
		t.Fatal(err)
	}
	if len(pending) != 2 || pending[0].Seq != 1 || pending[1].Seq != 2 {
		t.Fatalf("after restart pending = %+v, want seqs [1 2]", pending)
	}
	env, err := second.Capture("observation", mustJSON(t, map[string]any{"resourceType": "Observation", "id": "obs-2"}))
	if err != nil {
		t.Fatal(err)
	}
	if env.Seq != 3 {
		t.Fatalf("post-restart capture seq = %d, want 3 (sequence must survive restarts)", env.Seq)
	}
	pending, _ = second.Store().Pending()
	if len(pending) != 3 || pending[2].Seq != 3 || pending[2].Kind != "observation" {
		t.Fatalf("pending after third capture = %+v", pending)
	}
}

func TestSyncFailureLeavesItemsPendingInOrder(t *testing.T) {
	stub := &stubCore{fail: true}
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()
	node := newNode(t, t.TempDir(), srv.URL)

	for i := 0; i < 3; i++ {
		if _, err := node.Capture("reading", mustJSON(t, map[string]any{"device_key": "fridge-1", "metric": "temperature", "value": float64(i)})); err != nil {
			t.Fatal(err)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	res, err := node.SyncOnce(ctx)
	if err == nil {
		t.Fatalf("SyncOnce against a failing core returned nil error (result %+v)", res)
	}
	if res.Synced != 0 || res.Pending != 3 {
		t.Fatalf("result = %+v, want synced=0 pending=3", res)
	}
	pending, _ := node.Store().Pending()
	if len(pending) != 3 {
		t.Fatalf("pending = %d, want 3", len(pending))
	}
	for i, env := range pending {
		if env.Seq != uint64(i+1) {
			t.Fatalf("pending[%d].Seq = %d, want %d (order must survive a failed sync)", i, env.Seq, i+1)
		}
	}
	if synced, _ := node.Store().SyncedCount(); synced != 0 {
		t.Fatalf("synced = %d, want 0 after a failed sync", synced)
	}
}

func TestCaptureHTTPSurfaceAndStatus(t *testing.T) {
	stub := &stubCore{}
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()
	node := newNode(t, t.TempDir(), srv.URL)
	if err := node.Start(); err != nil {
		t.Fatalf("node.Start: %v", err)
	}
	defer node.Close()
	base := "http://" + node.Addr()

	resp, err := http.Post(base+"/capture", "application/json",
		strings.NewReader(`{"kind":"hl7","payload":"MSH|^~\\&|A|B|||||ORU^R01|X1|P|2.5.1"}`))
	if err != nil {
		t.Fatalf("POST /capture: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("POST /capture status = %d, want 201", resp.StatusCode)
	}
	var capBody map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&capBody); err != nil {
		t.Fatal(err)
	}
	if capBody["id"] == "" || capBody["seq"].(float64) != 1 {
		t.Fatalf("capture response = %v, want {id, seq:1}", capBody)
	}

	resp2, err := http.Get(base + "/status")
	if err != nil {
		t.Fatalf("GET /status: %v", err)
	}
	defer resp2.Body.Close()
	var status map[string]any
	if err := json.NewDecoder(resp2.Body).Decode(&status); err != nil {
		t.Fatal(err)
	}
	if status["pending"].(float64) != 1 || status["synced"].(float64) != 0 || status["core_reachable"] != true {
		t.Fatalf("status = %v, want pending=1 synced=0 core_reachable=true", status)
	}
}
