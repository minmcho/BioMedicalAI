package termgrpc

import (
	"context"
	"encoding/json"
	"net"
	"testing"
	"time"

	v1 "opencare/services/gen/opencare/v1"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

// TestTerminologyContractOverGRPC is the BD-67 contract test: a real
// gRPC server (this tier) on a loopback socket, driven through the
// GENERATED client stubs, asserting the normalisation semantics the
// contract shares with the Python core and the Rust tier.
func TestTerminologyContractOverGRPC(t *testing.T) {
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	gs := grpc.NewServer()
	Register(gs)
	go func() { _ = gs.Serve(lis) }()
	defer gs.Stop()

	conn, err := grpc.NewClient(lis.Addr().String(),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()
	client := NewClient(conn)

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	resp, err := client.Normalise(ctx, &v1.NormaliseRequest{
		Mappings: []*v1.CodeMapEntry{
			{FromCode: "LOCAL-HBA1C", ToCode: "4548-4",
				ToSystem: "http://loinc.org"},
			{FromCode: "2339-0", FromSystem: "http://local/lab",
				ToCode: "2345-7", ToSystem: "http://loinc.org"},
		},
		RowsJson: []string{
			`{"code":"LOCAL-HBA1C","value":7.2}`,
			`{"code":"2339-0","system":"http://local/lab","value":91}`,
			`{"code":"2339-0","value":92}`,
			`{"code":"UNKNOWN-1","value":1}`,
			`{"code":"UNKNOWN-1","value":2}`,
			`{"value":0}`,
			`{"code":"","value":0}`,
		},
	})
	if err != nil {
		t.Fatalf("Normalise: %v", err)
	}
	if resp.GetMappedRows() != 3 {
		t.Errorf("mapped_rows = %d, want 3", resp.GetMappedRows())
	}
	if resp.GetUnmappedRows() != 2 {
		t.Errorf("unmapped_rows = %d, want 2", resp.GetUnmappedRows())
	}
	if len(resp.GetUnmappedCodes()) != 1 || resp.GetUnmappedCodes()[0] != "UNKNOWN-1" {
		t.Errorf("unmapped_codes = %v, want [UNKNOWN-1]", resp.GetUnmappedCodes())
	}
	rows := make([]map[string]interface{}, len(resp.GetRowsJson()))
	for i, r := range resp.GetRowsJson() {
		if err := json.Unmarshal([]byte(r), &rows[i]); err != nil {
			t.Fatalf("row %d not JSON: %v", i, err)
		}
	}
	// Generic hit: code + system rewritten, source_code preserved.
	if rows[0]["code"] != "4548-4" || rows[0]["system"] != "http://loinc.org" ||
		rows[0]["source_code"] != "LOCAL-HBA1C" || rows[0]["normalised"] != true {
		t.Errorf("generic-hit row wrong: %v", rows[0])
	}
	// System-specific hit.
	if rows[1]["code"] != "2345-7" || rows[1]["source_code"] != "2339-0" {
		t.Errorf("system-hit row wrong: %v", rows[1])
	}
	// No generic entry exists for 2339-0's system-less row? The map
	// DOES seed a generic from the first 2339-0 entry (setdefault
	// semantics shared with the other tiers), so the system-less row
	// maps through the generic entry too.
	if rows[2]["code"] != "2345-7" || rows[2]["normalised"] != true {
		t.Errorf("generic-seeded row wrong: %v", rows[2])
	}
	// Misses pass through unchanged.
	if rows[3]["code"] != "UNKNOWN-1" {
		t.Errorf("miss row changed: %v", rows[3])
	}
	if _, ok := rows[3]["normalised"]; ok {
		t.Errorf("miss row gained normalised flag: %v", rows[3])
	}
	// Falsy / missing codes pass through and are not reported.
	if _, ok := rows[5]["code"]; ok {
		t.Errorf("code-less row changed: %v", rows[5])
	}
	if rows[6]["code"] != "" {
		t.Errorf("empty-code row changed: %v", rows[6])
	}
}

// TestNormaliseDirect pins the pure function (same fixtures drive
// the wire test above).
func TestNormaliseDirect(t *testing.T) {
	out, err := Normalise(
		[]Entry{{FromCode: "A", ToCode: "B"}},
		[]json.RawMessage{json.RawMessage(`{"code":"A"}`),
			json.RawMessage(`{"code":"Z"}`)})
	if err != nil {
		t.Fatalf("Normalise: %v", err)
	}
	if out.MappedRows != 1 || out.UnmappedRows != 1 ||
		len(out.UnmappedCodes) != 1 || out.UnmappedCodes[0] != "Z" {
		t.Errorf("outcome wrong: %+v", out)
	}
}
