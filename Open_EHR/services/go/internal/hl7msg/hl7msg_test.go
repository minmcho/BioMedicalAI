package hl7msg_test

import (
	"strings"
	"testing"

	"opencare/services/internal/hl7msg"
)

const oru = "MSH|^~\\&|ANALYZER-X|LAB|OPENCARE|OPENCARE|20261018120000||ORU^R01|CTRL-9|P|2.5.1\rPID|1||PAT1"

func TestParseMSHAndBuildACK(t *testing.T) {
	info, err := hl7msg.ParseMSH(oru)
	if err != nil {
		t.Fatalf("ParseMSH: %v", err)
	}
	if info.ControlID != "CTRL-9" || info.MessageType != "ORU^R01" || info.SendingApplication != "ANALYZER-X" {
		t.Fatalf("info = %+v", info)
	}
	ack := hl7msg.BuildACK(oru, "AA", "")
	if !strings.Contains(ack, "MSA|AA|CTRL-9") {
		t.Fatalf("ACK does not echo control id in MSA: %q", ack)
	}
	if !strings.Contains(ack, "ACK^R01") {
		t.Fatalf("ACK type should mirror the original event: %q", ack)
	}
	framed := hl7msg.Wrap(ack)
	if framed[0] != 0x0B || framed[len(framed)-2] != 0x1C || framed[len(framed)-1] != 0x0D {
		t.Fatalf("MLLP framing bytes wrong: %q", framed)
	}
}

func TestParseMSHRejectsGarbage(t *testing.T) {
	for _, bad := range []string{"", "hello", "MSH", "MSH|^~\\&|A|B|||||ORU^R01||P|2.5.1"} {
		if _, err := hl7msg.ParseMSH(bad); err == nil {
			t.Fatalf("ParseMSH(%q) succeeded, want error", bad)
		}
	}
}

func TestExtractFramePartial(t *testing.T) {
	framed := hl7msg.Wrap("MSH|test")
	if _, _, complete := hl7msg.ExtractFrame(framed[:5]); complete {
		t.Fatal("partial frame reported complete")
	}
	msg, rest, complete := hl7msg.ExtractFrame(append(framed, hl7msg.Wrap("NEXT")...))
	if !complete || msg != "MSH|test" {
		t.Fatalf("msg=%q complete=%v", msg, complete)
	}
	msg2, _, complete2 := hl7msg.ExtractFrame(rest)
	if !complete2 || msg2 != "NEXT" {
		t.Fatalf("pipelined frame: msg=%q complete=%v", msg2, complete2)
	}
}
