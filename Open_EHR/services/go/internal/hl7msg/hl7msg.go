// Package hl7msg implements the small piece of HL7 v2 the Go gateway
// tier needs: MLLP framing, enough MSH parsing to build a proper ACK,
// and ACK construction mirroring backend/app/hl7.py (build_ack /
// wrap_mllp / extract_frame).
//
// ACK semantics for the MLLP gateway (see also services/go/README.md):
//
//   - AA (application accept) is returned ONLY after the payload has
//     been forwarded to the FastAPI core and the core answered 201 on
//     POST /devices/offline/batches, OR after the payload has been
//     durably written to the on-disk spool. A message is never
//     acknowledged as accepted before one of those two things is true.
//   - AE is reserved for ACK construction / internal errors.
//   - AR (application reject) is returned for malformed input: no
//     complete MLLP frame, a frame exceeding the maximum size, or a
//     payload whose MSH segment cannot be parsed. Rejected payloads
//     are not forwarded and not spooled as valid HL7.
package hl7msg

import (
	"crypto/rand"
	"fmt"
	"strings"
	"time"
)

// MLLP framing bytes (HL7 minimal lower layer protocol).
const (
	StartByte = 0x0B // VT
	EndByte1  = 0x1C // FS
	EndByte2  = 0x0D // CR
)

// MaxFrameBytes mirrors the Python MLLP listener
// (backend/app/mllp_server.py: MAX_FRAME_BYTES = 4 * 1024 * 1024).
const MaxFrameBytes = 4 * 1024 * 1024

// Wrap frames a message: VT <message> FS CR.
func Wrap(message string) []byte {
	out := make([]byte, 0, len(message)+3)
	out = append(out, StartByte)
	out = append(out, message...)
	out = append(out, EndByte1, EndByte2)
	return out
}

// ExtractFrame pulls one complete frame out of a receive buffer.
// It returns (message, rest, complete). complete is false until the
// FS CR terminator has arrived. A leading VT is stripped when present;
// unframed-but-terminated payloads are tolerated the same way the
// Python extract_frame tolerates them.
func ExtractFrame(buffer []byte) (string, []byte, bool) {
	end := -1
	for i := 0; i+1 < len(buffer); i++ {
		if buffer[i] == EndByte1 && buffer[i+1] == EndByte2 {
			end = i
			break
		}
	}
	if end == -1 {
		return "", buffer, false
	}
	frame := buffer[:end]
	rest := buffer[end+2:]
	if len(frame) > 0 && frame[0] == StartByte {
		frame = frame[1:]
	}
	return string(frame), rest, true
}

// MSHInfo is the subset of MSH the gateway needs.
type MSHInfo struct {
	Separator          byte
	SendingApplication string // MSH-3
	SendingFacility    string // MSH-4
	MessageType        string // MSH-9, e.g. "ORU^R01"
	ControlID          string // MSH-10
}

// ParseMSH parses the first segment of a raw HL7 message. It returns
// an error when the message does not start with a parseable MSH
// segment (including a missing message control id, MSH-10, which an
// ACK must echo).
func ParseMSH(raw string) (MSHInfo, error) {
	var info MSHInfo
	trimmed := strings.Trim(raw, "\x0b\x1c\r\n ")
	if trimmed == "" {
		return info, fmt.Errorf("empty message")
	}
	first := trimmed
	if idx := strings.IndexAny(first, "\r\n"); idx >= 0 {
		first = first[:idx]
	}
	if !strings.HasPrefix(first, "MSH") {
		return info, fmt.Errorf("message does not start with MSH")
	}
	if len(first) < 4 {
		return info, fmt.Errorf("MSH segment too short")
	}
	sep := first[3]
	info.Separator = sep
	parts := strings.Split(first, string(sep))
	// parts[0]="MSH", parts[1]=MSH-2 (encoding), parts[2]=MSH-3, ...
	field := func(n int) string { // 1-based HL7 field number
		if n-1 < len(parts) && n-1 >= 0 {
			return parts[n-1]
		}
		return ""
	}
	// With the split above, MSH-1 is the separator itself and is not a
	// slice element, so MSH-n for n>=2 lives at parts[n-1].
	info.SendingApplication = field(3)
	info.SendingFacility = field(4)
	info.MessageType = field(9)
	info.ControlID = field(10)
	if info.ControlID == "" {
		return info, fmt.Errorf("MSH has no message control id (MSH-10)")
	}
	if info.MessageType == "" {
		return info, fmt.Errorf("MSH has no message type (MSH-9)")
	}
	return info, nil
}

// esc applies HL7 field escaping, mirroring backend/app/hl7.py esc().
func esc(s string) string {
	var b strings.Builder
	for _, ch := range s {
		switch ch {
		case '|':
			b.WriteString(`\F\`)
		case '^':
			b.WriteString(`\S\`)
		case '~':
			b.WriteString(`\R\`)
		case '\\':
			b.WriteString(`\E\`)
		case '&':
			b.WriteString(`\T\`)
		default:
			b.WriteRune(ch)
		}
	}
	return b.String()
}

// segment joins fields the way backend/app/hl7.py segment() does:
// trailing empty fields are trimmed.
func segment(name string, fields ...string) string {
	vals := make([]string, len(fields))
	copy(vals, fields)
	for len(vals) > 0 && vals[len(vals)-1] == "" {
		vals = vals[:len(vals)-1]
	}
	if len(vals) == 0 {
		return name
	}
	return name + "|" + strings.Join(vals, "|")
}

func newControlID(prefix string) string {
	var raw [8]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return prefix + "FALLBACK"
	}
	return fmt.Sprintf("%s%X", prefix, raw[:])
}

// BuildACK builds an HL7 ACK for original, mirroring
// backend/app/hl7.py build_ack: the ACK's MSH swaps sender/receiver,
// MSA-1 carries ackCode, MSA-2 echoes the original MSH-10 control id,
// and an ERR segment rides along for AE/AR. A control id is echoed
// even for unparseable input when one could not be recovered it is
// simply empty (Python behaviour for the same case).
func BuildACK(original, ackCode, text string) string {
	info, _ := ParseMSH(original)
	ackType := "ACK"
	if strings.Contains(info.MessageType, "^") {
		ackType = "ACK^" + strings.SplitN(info.MessageType, "^", 2)[1]
	}
	dtm := time.Now().UTC().Format("20060102150405")
	msh := segment("MSH", `^~\&`, "OPENCARE", "OPENCARE",
		esc(info.SendingApplication), esc(info.SendingFacility),
		dtm, "", ackType, newControlID("ACK"), "P", "2.5.1")
	lines := []string{msh, segment("MSA", ackCode, info.ControlID, esc(text))}
	if ackCode == "AE" || ackCode == "AR" {
		lines = append(lines, segment("ERR", "", "",
			"207^Application Internal Error^HL70357", "E", "", esc(text)))
	}
	return strings.Join(lines, "\r")
}

// ParseACK is a test/helper convenience: it returns MSA-1 and MSA-2
// from an ACK message text (unframed).
func ParseACK(raw string) (code, controlID string, ok bool) {
	trimmed := strings.Trim(raw, "\x0b\x1c\r\n ")
	for _, line := range strings.FieldsFunc(trimmed, func(r rune) bool { return r == '\r' || r == '\n' }) {
		if strings.HasPrefix(line, "MSA|") {
			parts := strings.Split(line, "|")
			if len(parts) >= 3 {
				return parts[1], parts[2], true
			}
			if len(parts) == 2 {
				return parts[1], "", true
			}
			return "", "", false
		}
	}
	return "", "", false
}
