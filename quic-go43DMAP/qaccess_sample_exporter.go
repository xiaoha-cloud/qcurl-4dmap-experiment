package quic

import (
	"encoding/csv"
	"fmt"
	"os"
	"path/filepath"
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
}

// qaccessSampleExporter writes labelled rows from existing PathMetrics (no separate monitor).
type qaccessSampleExporter struct {
	mu          sync.Mutex
	path        string
	runID       string
	maxRows     int64
	rowsWritten int64
	opened      bool
	writer      *csv.Writer
	file        *os.File
	pending     map[protocol.PathID]map[string]string
}

func newQAccessSampleExporter(csvPath, runID string, maxRows int64) *qaccessSampleExporter {
	return &qaccessSampleExporter{
		path:    csvPath,
		runID:   runID,
		maxRows: maxRows,
		pending: make(map[protocol.PathID]map[string]string),
	}
}

func (e *qaccessSampleExporter) bufferSize() int64 {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.rowsWritten + int64(len(e.pending))
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
	f, err := os.OpenFile(e.path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		return err
	}
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
	return nil
}

// resetBuffer closes the CSV and clears in-memory counters after the worker archives a full buffer.
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
	e.file = nil
	e.writer = nil
	e.opened = false
	e.rowsWritten = 0
	e.pending = make(map[protocol.PathID]map[string]string)
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
	return nil
}

func buildTrainRow(runID string, pm PathMetrics, sig ControlSignal, alpha, beta, gamma float64) map[string]string {
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
	return row
}
