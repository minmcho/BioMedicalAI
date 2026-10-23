package gateway_test

import (
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"opencare/services/internal/gateway"
	"opencare/services/internal/hl7msg"
)

// stubCore implements the core's POST /devices/offline/batches
// contract (backend/app/devices.py queue_offline_batch): 201 on
// success, items recorded in arrival order. fail toggles a 500.
type stubCore struct {
	mu       sync.Mutex
	fail     bool
	batches  int
	items    []stubItem
	authSeen string
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
		s.authSeen = r.Header.Get("Authorization")
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
		s.batches++
		s.items = append(s.items, body.Items...)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(map[string]any{"queued": len(body.Items)})
	})
	return mux
}

func (s *stubCore) setFail(f bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.fail = f
}

func (s *stubCore) snapshot() ([]stubItem, string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]stubItem, len(s.items))
	copy(out, s.items)
	return out, s.authSeen
}

const oruMessage = "MSH|^~\\&|ANALYZER-X|LAB|OPENCARE|OPENCARE|20261018120000||ORU^R01|CTRL-GW-1|P|2.5.1\r" +
	"PID|1||PAT123^^^OPENCARE^FI||Doe^John||19800101|M\r" +
	"OBR|1|ORD123||80061^Lipid Panel^http://loinc.org\r" +
	"OBX|1|NM|2339-0^Glucose^http://loinc.org||95|mg/dL|70-99||||F"

func oruWithControl(id string) string {
	return strings.Replace(oruMessage, "CTRL-GW-1", id, 1)
}

func startGateway(t *testing.T, coreURL string) *gateway.Gateway {
	t.Helper()
	gw, err := gateway.New(gateway.Config{
		MLLPAddr:   "127.0.0.1:0",
		HealthAddr: "127.0.0.1:0",
		CoreURL:    coreURL,
		Token:      "test-token",
		SpoolDir:   t.TempDir(),
	})
	if err != nil {
		t.Fatalf("gateway.New: %v", err)
	}
	if err := gw.Start(); err != nil {
		t.Fatalf("gateway.Start: %v", err)
	}
	t.Cleanup(gw.Close)
	return gw
}

func mllpRoundTrip(t *testing.T, addr, payload string) string {
	t.Helper()
	conn, err := net.DialTimeout("tcp", addr, 3*time.Second)
	if err != nil {
		t.Fatalf("dial gateway: %v", err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
	if _, err := conn.Write(hl7msg.Wrap(payload)); err != nil {
		t.Fatalf("write frame: %v", err)
	}
	var buf []byte
	chunk := make([]byte, 4096)
	for {
		n, err := conn.Read(chunk)
		if n > 0 {
			buf = append(buf, chunk[:n]...)
			if msg, _, complete := hl7msg.ExtractFrame(buf); complete {
				return msg
			}
		}
		if err != nil {
			t.Fatalf("reading ACK (got %q so far): %v", buf, err)
		}
	}
}

func TestMLLPRoundTrip(t *testing.T) {
	stub := &stubCore{}
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()
	gw := startGateway(t, srv.URL)

	ack := mllpRoundTrip(t, gw.MLLPAddr(), oruMessage)
	code, controlID, ok := hl7msg.ParseACK(ack)
	if !ok {
		t.Fatalf("ACK has no MSA segment: %q", ack)
	}
	if code != "AA" {
		t.Fatalf("MSA-1 = %q, want AA (ack=%q)", code, ack)
	}
	if controlID != "CTRL-GW-1" {
		t.Fatalf("MSA-2 = %q, want echoed MSH-10 CTRL-GW-1", controlID)
	}
	items, auth := stub.snapshot()
	if len(items) != 1 {
		t.Fatalf("stub received %d items, want exactly 1", len(items))
	}
	if items[0].Kind != "hl7" {
		t.Fatalf("item kind = %q, want hl7", items[0].Kind)
	}
	var payload string
	if err := json.Unmarshal(items[0].Payload, &payload); err != nil {
		t.Fatalf("item payload is not a JSON string: %v", err)
	}
	if payload != oruMessage {
		t.Fatalf("payload not byte-identical:\n got %q\nwant %q", payload, oruMessage)
	}
	if auth != "Bearer test-token" {
		t.Fatalf("Authorization header = %q, want Bearer test-token", auth)
	}
	if n, err := gw.Spool().PendingCount(); err != nil || n != 0 {
		t.Fatalf("spool pending = %d, %v; want 0 after a 201", n, err)
	}

	// Health endpoint: spooled count + core reachability.
	resp, err := http.Get("http://" + gw.HealthAddr() + "/health")
	if err != nil {
		t.Fatalf("health GET: %v", err)
	}
	defer resp.Body.Close()
	var health map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&health); err != nil {
		t.Fatalf("health decode: %v", err)
	}
	if health["status"] != "ok" || health["core_reachable"] != true {
		t.Fatalf("health = %v, want status ok + core_reachable true", health)
	}
	if health["spooled"].(float64) != 0 {
		t.Fatalf("health spooled = %v, want 0", health["spooled"])
	}
}

func TestCoreDownSpoolsAndDrainsInOrder(t *testing.T) {
	stub := &stubCore{fail: true}
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()
	gw := startGateway(t, srv.URL)

	msg1 := oruWithControl("CTRL-DOWN-1")
	msg2 := oruWithControl("CTRL-DOWN-2")
	for i, msg := range []string{msg1, msg2} {
		ack := mllpRoundTrip(t, gw.MLLPAddr(), msg)
		code, _, ok := hl7msg.ParseACK(ack)
		if !ok || code != "AA" {
			t.Fatalf("message %d: ACK = %q (ok=%v), want AA (durably spooled)", i, ack, ok)
		}
	}
	if n, _ := gw.Spool().PendingCount(); n != 2 {
		t.Fatalf("spool pending = %d, want 2 (nothing dropped while core is down)", n)
	}
	if items, _ := stub.snapshot(); len(items) != 0 {
		t.Fatalf("stub received %d items while failing, want 0", len(items))
	}

	stub.setFail(false)
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		items, _ := stub.snapshot()
		pending, _ := gw.Spool().PendingCount()
		if len(items) == 2 && pending == 0 {
			var got []string
			for _, it := range items {
				var p string
				if err := json.Unmarshal(it.Payload, &p); err != nil {
					t.Fatalf("payload decode: %v", err)
				}
				got = append(got, p)
			}
			if got[0] != msg1 || got[1] != msg2 {
				t.Fatalf("spool drained out of order:\n got %q, %q\nwant %q, %q", got[0], got[1], msg1, msg2)
			}
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
	items, _ := stub.snapshot()
	pending, _ := gw.Spool().PendingCount()
	t.Fatalf("spool did not drain after core recovery: stub items=%d spool pending=%d", len(items), pending)
}

func TestCoreUnreachableSpools(t *testing.T) {
	// A port that is (almost certainly) closed.
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	dead := fmt.Sprintf("http://%s", ln.Addr().String())
	_ = ln.Close()

	gw := startGateway(t, dead)
	ack := mllpRoundTrip(t, gw.MLLPAddr(), oruMessage)
	code, _, ok := hl7msg.ParseACK(ack)
	if !ok || code != "AA" {
		t.Fatalf("ACK = %q, want AA (spooled) with an unreachable core", ack)
	}
	if n, _ := gw.Spool().PendingCount(); n != 1 {
		t.Fatalf("spool pending = %d, want 1", n)
	}
}

func TestMalformedFrameRejected(t *testing.T) {
	stub := &stubCore{}
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()
	gw := startGateway(t, srv.URL)

	for name, payload := range map[string]string{
		"not hl7 at all": "GARBAGE NOT HL7",
		"MSH too short":  "MSH",
		"no control id":  "MSH|^~\\&|APP|FAC|||||ORU^R01||P|2.5.1",
	} {
		ack := mllpRoundTrip(t, gw.MLLPAddr(), payload)
		code, _, ok := hl7msg.ParseACK(ack)
		if !ok || code != "AR" {
			t.Fatalf("%s: ACK = %q, want AR", name, ack)
		}
	}
	if items, _ := stub.snapshot(); len(items) != 0 {
		t.Fatalf("malformed frames were forwarded: %d items", len(items))
	}
	if n, _ := gw.Spool().PendingCount(); n != 0 {
		t.Fatalf("malformed frames were spooled: %d", n)
	}
}
