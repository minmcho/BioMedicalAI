// Package queue is the durable on-disk store-and-forward queue
// shared by the MLLP gateway (as its spool) and the edge node (as its
// capture queue). It is deliberately boring:
//
//   - One JSON envelope file per captured item in the queue directory,
//     named by zero-padded sequence so directory order == receive
//     order: 00000000000000000001-<uuid>.json
//   - Envelope: {id (uuid), seq, received_at, kind, payload}.
//   - Writes are atomic: write to a .tmp-* file in the same
//     directory, fsync, rename, fsync the directory.
//   - The next sequence number is persisted in a state file
//     (seq.state, also written atomically) AND recovered by scanning
//     existing envelopes (pending + synced/), so receive order and
//     sequence monotonicity survive restarts even if one of the two
//     records is lost.
//   - An item is removed (gateway spool) or moved to synced/
//     (edge node) ONLY by an explicit call after the core has
//     answered 201. Nothing in this package ever discards a pending
//     envelope on its own.
//
// Delivery semantics built on top of this package are at-least-once:
// see services/go/README.md.
package queue

import (
	"crypto/rand"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Envelope is one queued item, exactly as stored on disk.
type Envelope struct {
	ID         string          `json:"id"`
	Seq        uint64          `json:"seq"`
	ReceivedAt time.Time       `json:"received_at"`
	Kind       string          `json:"kind"`
	Payload    json.RawMessage `json:"payload"`
}

const (
	seqStateFile = "seq.state"
	syncedDir    = "synced"
)

// Store is a queue directory. Safe for concurrent use in one process.
type Store struct {
	Dir string

	mu      sync.Mutex
	nextSeq uint64
}

// NewUUID returns a random RFC 4122 v4 UUID string.
func NewUUID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		// crypto/rand failure is vanishingly unlikely; fall back to a
		// time-based unique-enough id rather than failing a capture.
		return fmt.Sprintf("t%d", time.Now().UnixNano())
	}
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

// Open creates (if needed) the queue directory and its synced/
// subdir, and recovers the next sequence number from the state file
// and/or the envelopes already on disk.
func Open(dir string) (*Store, error) {
	if err := os.MkdirAll(filepath.Join(dir, syncedDir), 0o755); err != nil {
		return nil, err
	}
	s := &Store{Dir: dir, nextSeq: 1}
	maxSeq := uint64(0)
	// Recover from persisted state file.
	if data, err := os.ReadFile(filepath.Join(dir, seqStateFile)); err == nil {
		if n, perr := strconv.ParseUint(strings.TrimSpace(string(data)), 10, 64); perr == nil {
			maxSeq = n
		}
	}
	// Recover from envelopes on disk (pending and synced).
	for _, sub := range []string{dir, filepath.Join(dir, syncedDir)} {
		entries, err := os.ReadDir(sub)
		if err != nil {
			return nil, err
		}
		for _, e := range entries {
			if e.IsDir() || !strings.HasSuffix(e.Name(), ".json") {
				continue
			}
			if env, err := readEnvelope(filepath.Join(sub, e.Name())); err == nil {
				if env.Seq > maxSeq {
					maxSeq = env.Seq
				}
			}
		}
	}
	s.nextSeq = maxSeq + 1
	return s, nil
}

func envelopeName(env Envelope) string {
	return fmt.Sprintf("%020d-%s.json", env.Seq, env.ID)
}

func readEnvelope(path string) (Envelope, error) {
	var env Envelope
	data, err := os.ReadFile(path)
	if err != nil {
		return env, err
	}
	err = json.Unmarshal(data, &env)
	return env, err
}

func fsyncDir(dir string) {
	if f, err := os.Open(dir); err == nil {
		_ = f.Sync()
		_ = f.Close()
	}
}

// writeFileAtomic writes data to dir/name via tmp+rename+fsync.
func writeFileAtomic(dir, name string, data []byte) error {
	tmp, err := os.CreateTemp(dir, ".tmp-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName) // no-op after a successful rename
	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmpName, filepath.Join(dir, name)); err != nil {
		return err
	}
	fsyncDir(dir)
	return nil
}

// Enqueue durably appends one item and returns its envelope. The
// item is on disk (fsynced) before Enqueue returns — callers may ACK
// / answer 201 on the strength of this call alone.
func (s *Store) Enqueue(kind string, payload json.RawMessage) (Envelope, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	env := Envelope{
		ID:         NewUUID(),
		Seq:        s.nextSeq,
		ReceivedAt: time.Now().UTC(),
		Kind:       kind,
		Payload:    payload,
	}
	data, err := json.Marshal(env)
	if err != nil {
		return Envelope{}, err
	}
	if err := writeFileAtomic(s.Dir, envelopeName(env), data); err != nil {
		return Envelope{}, err
	}
	// Persist the sequence high-water mark (atomic as well).
	seqData := []byte(strconv.FormatUint(env.Seq, 10) + "\n")
	if err := writeFileAtomic(s.Dir, seqStateFile, seqData); err != nil {
		return Envelope{}, fmt.Errorf("envelope written (seq %d) but seq state persist failed: %w", env.Seq, err)
	}
	s.nextSeq++
	return env, nil
}

// Pending lists pending envelopes in receive order (ascending seq).
func (s *Store) Pending() ([]Envelope, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.listLocked(s.Dir)
}

// Synced lists synced envelopes in receive order (edge node).
func (s *Store) Synced() ([]Envelope, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.listLocked(filepath.Join(s.Dir, syncedDir))
}

func (s *Store) listLocked(dir string) ([]Envelope, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	var out []Envelope
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".json") {
			continue
		}
		env, err := readEnvelope(filepath.Join(dir, e.Name()))
		if err != nil {
			return nil, fmt.Errorf("reading queued envelope %s: %w", e.Name(), err)
		}
		out = append(out, env)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Seq < out[j].Seq })
	return out, nil
}

// PendingCount is Pending() length without decoding every payload
// field beyond the envelope JSON.
func (s *Store) PendingCount() (int, error) {
	p, err := s.Pending()
	return len(p), err
}

// SyncedCount is Synced() length.
func (s *Store) SyncedCount() (int, error) {
	p, err := s.Synced()
	return len(p), err
}

func (s *Store) pendingPath(env Envelope) string {
	return filepath.Join(s.Dir, envelopeName(env))
}

// Remove deletes a pending envelope. Call ONLY after the core has
// confirmed receipt (HTTP 201) — this is the gateway spool drain.
func (s *Store) Remove(env Envelope) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := os.Remove(s.pendingPath(env)); err != nil && !os.IsNotExist(err) {
		return err
	}
	fsyncDir(s.Dir)
	return nil
}

// MarkSynced moves a pending envelope into synced/. Call ONLY after
// the core has confirmed receipt (HTTP 201) — this is the edge
// node's synced marking. The envelope content is unchanged.
func (s *Store) MarkSynced(env Envelope) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	dst := filepath.Join(s.Dir, syncedDir, envelopeName(env))
	if err := os.Rename(s.pendingPath(env), dst); err != nil {
		if os.IsNotExist(err) {
			// Already gone (e.g. a duplicate mark): if it is in
			// synced/ the desired end state already holds.
			if _, serr := os.Stat(dst); serr == nil {
				return nil
			}
		}
		return err
	}
	fsyncDir(s.Dir)
	fsyncDir(filepath.Join(s.Dir, syncedDir))
	return nil
}
