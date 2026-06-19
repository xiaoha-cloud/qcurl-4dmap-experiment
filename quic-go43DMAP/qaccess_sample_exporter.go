package quic

import (
	"encoding/csv"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"sync"

	"github.com/lucas-clemente/quic-go/internal/protocol"
)

var qaccessSampleCSVHeader = []string{
	"timestamp_ms", "run_id", "path_id",
	"bw_bps", "owd_ms", "delay_gradient_ms", "loss_rate",
	"lost_bytes_delta", "retrans_bytes_delta",
	"cwnd_bytes", "inflight_bytes", "cwnd_room",
	"alpha", "beta", "gamma", "utility", "gain", "backoff",
	"next_bw_bps", "next_goodput_bps",
	"throughput_reward_term", "loss_penalty_term", "delay_penalty_term",
	"gain_raw", "gain_clamped", "gain_hit_min", "gain_hit_max",
	"retention_raw", "retention_clamped", "retention_hit_min", "retention_hit_max",
	"control_law", "delay_penalty_bounded", "prev_gain", "step_limited_gain",
}

// qaccessSampleExporter writes labelled rows from existing PathMetrics (no separate monitor).
type qaccessSampleExporter struct {
	mu                 sync.Mutex
	path               string
	runID              string
	maxRows            int64
	rowsWritten        int64
	rowsWrittenPerPath map[protocol.PathID]int64
	opened             bool
	writer             *csv.Writer
	file               *os.File
	pending            map[protocol.PathID]map[string]string
	windowStartMs      int64
	windowEndMs        int64
}

type qaccessBufferSnapshot struct {
	TotalRows       int64
	RowsByPath      map[protocol.PathID]int64
	ActivePaths     []protocol.PathID
	EligiblePaths   []protocol.PathID
	SelectedPath    protocol.PathID
	HasSelectedPath bool
	AtCapacity      bool
	WindowStartMs   int64
	WindowEndMs     int64
}

func newQAccessSampleExporter(csvPath, runID string, maxRows int64) *qaccessSampleExporter {
	return &qaccessSampleExporter{
		path:               csvPath,
		runID:              runID,
		maxRows:            maxRows,
		rowsWrittenPerPath: make(map[protocol.PathID]int64),
		pending:            make(map[protocol.PathID]map[string]string),
	}
}

func (e *qaccessSampleExporter) bufferSize() int64 {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.rowsWritten + int64(len(e.pending))
}

func (e *qaccessSampleExporter) pathBufferSize(pathID protocol.PathID) int64 {
	e.mu.Lock()
	defer e.mu.Unlock()
	n := e.rowsWrittenPerPath[pathID]
	if _, ok := e.pending[pathID]; ok {
		n++
	}
	return n
}

func (e *qaccessSampleExporter) triggerSnapshot(minSamplesPerPath int64) qaccessBufferSnapshot {
	e.mu.Lock()
	defer e.mu.Unlock()
	if minSamplesPerPath < 1 {
		minSamplesPerPath = 1
	}
	rowsByPath := make(map[protocol.PathID]int64, len(e.rowsWrittenPerPath)+len(e.pending))
	for pid, n := range e.rowsWrittenPerPath {
		rowsByPath[pid] = n
	}
	for pid := range e.pending {
		rowsByPath[pid]++
	}
	active := make([]protocol.PathID, 0, len(rowsByPath))
	eligible := make([]protocol.PathID, 0, len(rowsByPath))
	for pid, n := range rowsByPath {
		if n <= 0 {
			continue
		}
		active = append(active, pid)
		if n >= minSamplesPerPath {
			eligible = append(eligible, pid)
		}
	}
	sort.Slice(active, func(i, j int) bool { return active[i] < active[j] })
	sort.Slice(eligible, func(i, j int) bool {
		left, right := rowsByPath[eligible[i]], rowsByPath[eligible[j]]
		if left == right {
			return eligible[i] < eligible[j]
		}
		return left > right
	})
	total := e.rowsWritten + int64(len(e.pending))
	snapshot := qaccessBufferSnapshot{
		TotalRows: total, RowsByPath: rowsByPath, ActivePaths: active, EligiblePaths: eligible,
		AtCapacity:    e.maxRows > 0 && total >= e.maxRows,
		WindowStartMs: e.windowStartMs, WindowEndMs: e.windowEndMs,
	}
	if len(eligible) > 0 {
		snapshot.SelectedPath = eligible[0]
		snapshot.HasSelectedPath = true
	}
	return snapshot
}

func (e *qaccessSampleExporter) atCapacity() bool {
	if e.maxRows <= 0 {
		return false
	}
	return e.bufferSize() >= e.maxRows
}

func (e *qaccessSampleExporter) flushAllPending(nextBW func(protocol.PathID) float64) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	if len(e.pending) == 0 {
		return nil
	}
	ids := make([]protocol.PathID, 0, len(e.pending))
	for pid := range e.pending {
		ids = append(ids, pid)
	}
	for _, pid := range ids {
		if err := e.flushPendingLabelLocked(pid, nextBW(pid)); err != nil {
			return err
		}
	}
	return nil
}

func (e *qaccessSampleExporter) ensureOpen() error {
	if e.opened {
		return nil
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.ensureOpenLocked()
}

func (e *qaccessSampleExporter) ensureOpenLocked() error {
	if e.opened {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(e.path), 0755); err != nil {
		return err
	}
	_, statErr := os.Stat(e.path)
	writeHeader := os.IsNotExist(statErr)
	f, err := os.OpenFile(e.path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0666)
	if err != nil {
		return err
	}
	// Mininet runs 4dmap as root; Phase 2 worker runs as the normal user and must truncate this CSV.
	_ = f.Chmod(0666)
	e.file = f
	e.writer = csv.NewWriter(f)
	if writeHeader {
		if err := e.writer.Write(qaccessSampleCSVHeader); err != nil {
			return err
		}
	}
	e.opened = true
	return nil
}

func (e *qaccessSampleExporter) flushPendingLabel(pathID protocol.PathID, nextBWbps float64) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.flushPendingLabelLocked(pathID, nextBWbps)
}

func (e *qaccessSampleExporter) flushPendingLabelLocked(pathID protocol.PathID, nextBWbps float64) error {
	row, ok := e.pending[pathID]
	if !ok {
		return nil
	}
	row["next_bw_bps"] = fmt.Sprintf("%.0f", nextBWbps)
	if err := e.ensureOpenLocked(); err != nil {
		return err
	}
	vals := make([]string, len(qaccessSampleCSVHeader))
	for i, col := range qaccessSampleCSVHeader {
		vals[i] = row[col]
	}
	if err := e.writer.Write(vals); err != nil {
		return err
	}
	e.writer.Flush()
	delete(e.pending, pathID)
	if err := e.writer.Error(); err != nil {
		return err
	}
	e.rowsWritten++
	e.rowsWrittenPerPath[pathID]++
	return nil
}

// resetBuffer closes and removes the completed global window before sampling resumes.
func (e *qaccessSampleExporter) resetBuffer() error {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.writer != nil {
		e.writer.Flush()
	}
	if e.file != nil {
		if err := e.file.Close(); err != nil {
			return err
		}
	}
	if err := os.Remove(e.path); err != nil && !os.IsNotExist(err) {
		return err
	}
	e.file = nil
	e.writer = nil
	e.opened = false
	e.rowsWritten = 0
	e.rowsWrittenPerPath = make(map[protocol.PathID]int64)
	e.pending = make(map[protocol.PathID]map[string]string)
	e.windowStartMs = 0
	e.windowEndMs = 0
	return nil
}

// removePathRows deletes rows for one path_id from the on-disk CSV and resets that path's counters.
func (e *qaccessSampleExporter) removePathRows(pathID protocol.PathID) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	delete(e.pending, pathID)
	e.rowsWrittenPerPath[pathID] = 0

	if !e.opened && e.file == nil {
		if _, err := os.Stat(e.path); os.IsNotExist(err) {
			return nil
		}
	}
	if e.writer != nil {
		e.writer.Flush()
	}
	if e.file != nil {
		if err := e.file.Close(); err != nil {
			return err
		}
		e.file = nil
		e.writer = nil
		e.opened = false
	}

	f, err := os.Open(e.path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	r := csv.NewReader(f)
	records, err := r.ReadAll()
	_ = f.Close()
	if err != nil {
		return err
	}
	if len(records) == 0 {
		return nil
	}
	header := records[0]
	pathCol := -1
	for i, col := range header {
		if col == "path_id" {
			pathCol = i
			break
		}
	}
	if pathCol < 0 {
		return fmt.Errorf("path_id column missing in %s", e.path)
	}
	want := strconv.FormatUint(uint64(pathID), 10)
	kept := [][]string{header}
	removed := 0
	for _, row := range records[1:] {
		if pathCol < len(row) && row[pathCol] == want {
			removed++
			continue
		}
		kept = append(kept, row)
	}
	tmp := e.path + ".tmp"
	out, err := os.OpenFile(tmp, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0666)
	if err != nil {
		return err
	}
	w := csv.NewWriter(out)
	if err := w.WriteAll(kept); err != nil {
		_ = out.Close()
		return err
	}
	w.Flush()
	if err := w.Error(); err != nil {
		_ = out.Close()
		return err
	}
	if err := out.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmp, e.path); err != nil {
		return err
	}
	_ = os.Chmod(e.path, 0666)
	e.rowsWritten -= int64(removed)
	if e.rowsWritten < 0 {
		e.rowsWritten = 0
	}
	return nil
}

func (e *qaccessSampleExporter) recordPending(row map[string]string, pathID protocol.PathID, nextBWbps float64) error {
	if e.atCapacity() {
		return nil
	}
	if err := e.flushPendingLabel(pathID, nextBWbps); err != nil {
		return err
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	e.pending[pathID] = row
	if timestampMs, err := strconv.ParseInt(row["timestamp_ms"], 10, 64); err == nil {
		if e.windowStartMs == 0 || timestampMs < e.windowStartMs {
			e.windowStartMs = timestampMs
		}
		if timestampMs > e.windowEndMs {
			e.windowEndMs = timestampMs
		}
	}
	return nil
}

func buildTrainRow(runID string, pm PathMetrics, sig ControlSignal, alpha, beta, gamma float64, diag ControlLawDiagnostics) map[string]string {
	row := make(map[string]string, len(qaccessSampleCSVHeader))
	row["timestamp_ms"] = strconv.FormatInt(pm.Timestamp.UnixNano()/1e6, 10)
	row["run_id"] = runID
	row["path_id"] = strconv.FormatUint(uint64(pm.PathID), 10)
	row["bw_bps"] = fmt.Sprintf("%.0f", pm.BWbps)
	row["owd_ms"] = fmt.Sprintf("%.4f", pm.OWDms)
	row["delay_gradient_ms"] = fmt.Sprintf("%.4f", pm.DelayGradientMs)
	row["loss_rate"] = fmt.Sprintf("%.6f", pm.LossRate)
	row["lost_bytes_delta"] = strconv.FormatInt(pm.LostBytesDelta, 10)
	row["retrans_bytes_delta"] = strconv.FormatInt(pm.RetransBytesDelta, 10)
	row["cwnd_bytes"] = strconv.FormatInt(pm.CwndBytes, 10)
	row["inflight_bytes"] = strconv.FormatInt(pm.InflightBytes, 10)
	row["cwnd_room"] = fmt.Sprintf("%.0f", pm.CwndRoom)
	row["alpha"] = fmt.Sprintf("%.4f", alpha)
	row["beta"] = fmt.Sprintf("%.4f", beta)
	row["gamma"] = fmt.Sprintf("%.4f", gamma)
	row["utility"] = fmt.Sprintf("%.6f", sig.Utility)
	row["gain"] = fmt.Sprintf("%.4f", sig.Gain)
	row["backoff"] = fmt.Sprintf("%.4f", sig.Backoff)
	row["next_bw_bps"] = ""
	row["next_goodput_bps"] = ""
	row["throughput_reward_term"] = fmt.Sprintf("%.6f", diag.ThroughputRewardTerm)
	row["loss_penalty_term"] = fmt.Sprintf("%.6f", diag.LossPenaltyTerm)
	row["delay_penalty_term"] = fmt.Sprintf("%.6f", diag.DelayPenaltyTerm)
	row["gain_raw"] = fmt.Sprintf("%.6f", diag.GainRaw)
	row["gain_clamped"] = fmt.Sprintf("%.6f", diag.GainClamped)
	row["gain_hit_min"] = strconv.FormatBool(diag.GainHitMin)
	row["gain_hit_max"] = strconv.FormatBool(diag.GainHitMax)
	row["retention_raw"] = fmt.Sprintf("%.6f", diag.RetentionRaw)
	row["retention_clamped"] = fmt.Sprintf("%.6f", diag.RetentionClamped)
	row["retention_hit_min"] = strconv.FormatBool(diag.RetentionHitMin)
	row["retention_hit_max"] = strconv.FormatBool(diag.RetentionHitMax)
	row["control_law"] = diag.ControlLaw
	row["delay_penalty_bounded"] = fmt.Sprintf("%.6f", diag.DelayPenaltyBounded)
	if diag.PrevGain > 0 || diag.ControlLaw == string(ControlLawSafeTV1) {
		row["prev_gain"] = fmt.Sprintf("%.6f", diag.PrevGain)
	}
	row["step_limited_gain"] = fmt.Sprintf("%.6f", diag.StepLimitedGain)
	return row
}
