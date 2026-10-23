// Package termgrpc is the Go implementation of the versioned
// terminology gRPC contract (services/contracts/opencare/v1,
// BD-67). Its Normalise semantics are a port of the SAME governed
// normalisation the Python core (pipelines._step_normalise) and the
// Rust tier (services/rust/src/term.rs) implement — see the .proto
// for the normative rules. Rows are JSON objects handled as raw
// field maps so value types survive exactly as they do on the
// JSON contracts of the other tiers.
package termgrpc

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
)

// Entry is one code-map mapping (the proto CodeMapEntry shape).
type Entry struct {
	FromCode   string
	FromSystem string
	ToCode     string
	ToSystem   string
}

// Outcome is one Normalise result.
type Outcome struct {
	Rows          []json.RawMessage
	UnmappedCodes []string
	UnmappedRows  int
	MappedRows    int
}

func truthy(raw json.RawMessage) bool {
	if len(raw) == 0 {
		return false
	}
	var v interface{}
	if err := json.Unmarshal(raw, &v); err != nil {
		return false
	}
	switch t := v.(type) {
	case nil:
		return false
	case bool:
		return t
	case float64:
		return t != 0
	case string:
		return t != ""
	case []interface{}:
		return len(t) > 0
	case map[string]interface{}:
		return len(t) > 0
	}
	return true
}

// pyStr renders a scalar the way Python str() would for the shapes a
// code/system field realistically carries (mirrors term.rs).
func pyStr(raw json.RawMessage) string {
	var v interface{}
	if err := json.Unmarshal(raw, &v); err != nil {
		return string(raw)
	}
	switch t := v.(type) {
	case string:
		return t
	case bool:
		if t {
			return "True"
		}
		return "False"
	case nil:
		return "None"
	case float64:
		if t == float64(int64(t)) {
			return fmt.Sprintf("%d", int64(t))
		}
		return fmt.Sprintf("%v", t)
	}
	b, _ := json.Marshal(v)
	return string(b)
}

type tableKey struct{ system, code string }

// Normalise applies the contract semantics to rows (each a JSON
// object) under the given code-map entries.
func Normalise(mappings []Entry, rows []json.RawMessage) (Outcome, error) {
	table := map[tableKey]Entry{}
	for _, e := range mappings {
		if e.FromCode == "" {
			continue
		}
		table[tableKey{e.FromSystem, e.FromCode}] = e
		if e.FromSystem != "" {
			generic := tableKey{"", e.FromCode}
			if _, seen := table[generic]; !seen {
				table[generic] = e
			}
		}
	}
	out := Outcome{Rows: make([]json.RawMessage, 0, len(rows))}
	unmapped := map[string]bool{}
	for _, raw := range rows {
		var obj map[string]json.RawMessage
		if err := json.Unmarshal(raw, &obj); err != nil || obj == nil {
			// Not an object: pass through unchanged (the other tiers
			// only ever receive object rows; a non-object is a
			// contract violation surfaced as an error instead).
			return Outcome{}, fmt.Errorf("row is not a JSON object: %s", string(raw))
		}
		codeRaw, hasCode := obj["code"]
		if !hasCode || !truthy(codeRaw) {
			out.Rows = append(out.Rows, raw)
			continue
		}
		code := pyStr(codeRaw)
		var entry *Entry
		if sysRaw, ok := obj["system"]; ok && truthy(sysRaw) {
			if e, hit := table[tableKey{pyStr(sysRaw), code}]; hit {
				ee := e
				entry = &ee
			}
		}
		if entry == nil {
			if e, hit := table[tableKey{"", code}]; hit {
				ee := e
				entry = &ee
			}
		}
		if entry == nil {
			unmapped[code] = true
			out.UnmappedRows++
			out.Rows = append(out.Rows, raw)
			continue
		}
		obj["source_code"] = codeRaw
		toCode := entry.ToCode
		if toCode == "" {
			toCode = code
		}
		b, _ := json.Marshal(toCode)
		obj["code"] = b
		if entry.ToSystem != "" {
			sb, _ := json.Marshal(entry.ToSystem)
			obj["system"] = sb
		}
		obj["normalised"] = json.RawMessage("true")
		merged, err := marshalObject(obj)
		if err != nil {
			return Outcome{}, err
		}
		out.MappedRows++
		out.Rows = append(out.Rows, merged)
	}
	for code := range unmapped {
		out.UnmappedCodes = append(out.UnmappedCodes, code)
	}
	sort.Strings(out.UnmappedCodes)
	return out, nil
}

// marshalObject marshals a field map with sorted keys (stable output
// for tests and diffing; JSON object order is not semantic).
func marshalObject(obj map[string]json.RawMessage) (json.RawMessage, error) {
	keys := make([]string, 0, len(obj))
	for k := range obj {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var sb strings.Builder
	sb.WriteString("{")
	for i, k := range keys {
		if i > 0 {
			sb.WriteString(",")
		}
		kb, _ := json.Marshal(k)
		sb.Write(kb)
		sb.WriteString(":")
		sb.Write(obj[k])
	}
	sb.WriteString("}")
	return json.RawMessage(sb.String()), nil
}
