// Package coreclient is the single HTTP client for the FastAPI
// core's EXISTING offline raw-zone contract:
//
//	POST {CORE_URL}/devices/offline/batches
//	Authorization: Bearer <OPENCARE_GATEWAY_TOKEN>   (when a token is set)
//	Content-Type: application/json
//	{"items":[{"kind":"hl7","payload":"<raw HL7 text>"},
//	          {"kind":"observation","payload":{...}},
//	          {"kind":"reading","payload":{...}}]}
//
// Success is exactly HTTP 201 (see queue_offline_batch in
// backend/app/devices.py). Item kinds are the core's own set:
// "hl7" (payload is the raw HL7 text, a JSON string), "observation"
// and "reading" (payload is a JSON object). This package invents no
// endpoints and keeps no store of its own: the core's raw zone is the
// only destination.
package coreclient

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// Item is one batch item in the core's contract shape.
type Item struct {
	Kind    string          `json:"kind"`
	Payload json.RawMessage `json:"payload"`
}

// HL7Item builds an hl7 item; the payload is the raw HL7 text exactly
// as received (JSON-encoded as a string, which the core decodes back
// to the identical bytes).
func HL7Item(raw string) Item {
	payload, _ := json.Marshal(raw)
	return Item{Kind: "hl7", Payload: payload}
}

// ObjectItem builds an observation/reading item from a JSON object.
func ObjectItem(kind string, object json.RawMessage) Item {
	return Item{Kind: kind, Payload: object}
}

// ValidKind reports whether kind is one of the core's accepted kinds.
func ValidKind(kind string) bool {
	return kind == "hl7" || kind == "observation" || kind == "reading"
}

// Client posts batches to one core base URL.
type Client struct {
	BaseURL string
	Token   string
	HTTP    *http.Client
}

// New builds a client. baseURL is configuration (CORE_URL), token is
// configuration (OPENCARE_GATEWAY_TOKEN); neither is a hardcoded
// secret — an empty token simply omits the Authorization header.
func New(baseURL, token string) *Client {
	return &Client{
		BaseURL: strings.TrimRight(baseURL, "/"),
		Token:   token,
		HTTP:    &http.Client{Timeout: 10 * time.Second},
	}
}

// PostBatch posts one batch. It returns nil only on HTTP 201; any
// other status or any transport error is a failure and the caller
// must retain the items (never drop).
func (c *Client) PostBatch(ctx context.Context, items []Item) error {
	if len(items) == 0 {
		return nil
	}
	body, err := json.Marshal(struct {
		Items []Item `json:"items"`
	}{Items: items})
	if err != nil {
		return err
	}
	url := c.BaseURL + "/devices/offline/batches"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	if c.Token != "" {
		req.Header.Set("Authorization", "Bearer "+c.Token)
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64*1024))
	if resp.StatusCode != http.StatusCreated {
		return fmt.Errorf("core POST /devices/offline/batches: HTTP %d (want 201)", resp.StatusCode)
	}
	return nil
}

// Reachable reports whether the core answers HTTP at all (any status
// counts: the transport succeeded). Used only for health/status
// reporting, never as the delivery decision — delivery is decided by
// PostBatch's 201.
func (c *Client) Reachable(ctx context.Context) bool {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.BaseURL+"/", nil)
	if err != nil {
		return false
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 1024))
	return true
}
