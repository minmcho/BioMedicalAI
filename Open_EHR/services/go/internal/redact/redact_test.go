package redact

import "testing"

func TestRedactCatchesPhiShapes(t *testing.T) {
	cases := []string{
		"patient ssn 123-45-6789 flagged",
		"contact jane.doe@example.org now",
		"call (415) 555-0132 back",
		"card 4111 1111 1111 1111 declined",
		"Patient Name: Jane Doe admitted",
		"DOB: 1980-04-12 on file",
		"MRN: SYN-88213 verified",
		"Authorization: Bearer abcdefgh12345678",
	}
	for _, in := range cases {
		out := Redact(in)
		if hits := Scan(out); len(hits) > 0 {
			t.Errorf("Redact(%q) = %q, survivors %v", in, out, hits)
		}
	}
}

func TestRedactLeavesOperationalTextAlone(t *testing.T) {
	in := "mllp-gateway: spool write failed: open /var/spool/x: no space left on device; BP 120/80 dose 5 mg"
	if out := Redact(in); out != in {
		t.Errorf("operational text changed: %q -> %q", in, out)
	}
}

func TestScanCountsSurvivors(t *testing.T) {
	hits := Scan("two ssns 123-45-6789 and 987-65-4321")
	if hits["ssn"] != 2 {
		t.Errorf("expected 2 ssn hits, got %v", hits)
	}
	if got := Scan("nothing here"); len(got) != 0 {
		t.Errorf("expected clean scan, got %v", got)
	}
}
