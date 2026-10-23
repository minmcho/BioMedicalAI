// Package redact is the Go services' PHI-redaction boundary for log
// lines — Phase 17, BD-53 (CMP-32).
//
// One rule spec, four runtimes: the source of truth is the FastAPI
// backend's app/logredact.py (spec version log-redaction-v1, governed
// surface Studio config integration/log-redaction). The Go services
// cannot read Studio config, so the pattern subset that can occur in
// Go log lines is mirrored here with the same names and the same
// replacement markers. Go log call sites never format message
// payloads by design (the gateway logs operational errors only), and
// every error value that reaches a log line now passes through
// Redact first — defence in depth, proven by the Phase 17 log scan
// over `go test` output and the emitted-log capture.
//
// The labelled rules from the Python spec (patient name / DOB / MRN /
// address after a label) are included so an error string that embeds
// a labelled value is caught the same way.
package redact

import (
	"regexp"
	"strings"
)

// SpecVersion mirrors backend/app/logredact.py SPEC_VERSION.
const SpecVersion = "log-redaction-v1"

type rule struct {
	name string
	re   *regexp.Regexp
	repl string
}

var rules = []rule{
	{"bearer-token", regexp.MustCompile(`(?i)\b(bearer\s+)[A-Za-z0-9._\-]{8,}`), "${1}[REDACTED-TOKEN]"},
	{"ssn", regexp.MustCompile(`\b\d{3}-\d{2}-\d{4}\b`), "[REDACTED-SSN]"},
	{"email", regexp.MustCompile(`\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b`), "[REDACTED-EMAIL]"},
	{"card", regexp.MustCompile(`\b\d{4}[- ]\d{4}[- ]\d{4}[- ]\d{4}\b`), "[REDACTED-CARD]"},
	{"phone", regexp.MustCompile(`\b(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b`), "[REDACTED-PHONE]"},
	{"labelled-name", regexp.MustCompile(`(?i)\b(patient name|full name)\s*[:=]\s*[A-Z][A-Za-z'\-]+(?: [A-Z][A-Za-z'\-]+){0,2}`), "${1}: [REDACTED-NAME]"},
	{"labelled-dob", regexp.MustCompile(`(?i)\b(dob|date of birth|birth ?date)\s*[:=]?\s*(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})`), "${1}: [REDACTED-DOB]"},
	{"labelled-mrn", regexp.MustCompile(`(?i)\b(mrn|medical record(?: number)?|chart(?: number)?)\s*[:#=]?\s*[A-Za-z0-9][A-Za-z0-9\-]{2,}`), "${1}: [REDACTED-MRN]"},
	{"labelled-ssn", regexp.MustCompile(`(?i)\b(ssn|social security(?: number)?)\s*[:#=]?\s*\d{3}[- ]?\d{2}[- ]?\d{4}`), "${1}: [REDACTED-SSN]"},
	{"labelled-address", regexp.MustCompile(`(?i)\b(address)\s*[:=]\s*[^\n;]+`), "${1}: [REDACTED-ADDRESS]"},
}

// Redact applies the rule set to one log-bound string.
func Redact(s string) string {
	out := s
	for _, r := range rules {
		out = r.re.ReplaceAllString(out, r.repl)
	}
	return out
}

// Scan counts unredacted PHI-pattern survivors per rule name — the
// proof metric used by the Phase 17 log scan (zero = clean). A match
// that IS a redaction marker is not a survivor.
func Scan(s string) map[string]int {
	counts := map[string]int{}
	for _, r := range rules {
		n := 0
		for _, m := range r.re.FindAllString(s, -1) {
			if !strings.Contains(m, "[REDACTED") {
				n++
			}
		}
		if n > 0 {
			counts[r.name] = n
		}
	}
	return counts
}
