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

var qaccessTrainCSVHeader = []string{
	"timestamp_ms", "run_id", "path_id",
	"bw_bps", "owd_ms", "delay_gradient_ms", "loss_rate",
	"lost_bytes_delta", "retrans_bytes_delta",
	"cwnd_bytes", "inflight_bytes", "cwnd_room",
	"alpha", "beta", "gamma", "utility", "gain", "backoff",
	"next_bw_bps", "next_goodput_bps", // next_goodput_bps: reserved for future receiver-side goodput labelling; empty in Phase 1
}

// qaccessTrainCollector exports labelled training rows for offline RFR training.
// It does not implement a separate monitor: it consumes PathMetrics built by the
// existing scheduler monitor (monitorUpdatePathState / monitorApplyUtility) and
// writes CSV samples only.
type qaccessTrainCollector struct {
	mu      sync.Mutex
	path    string
	runID   string
	opened  bool
	writer  *csv.Writer
	file    *os.File
	pending map[protocol.PathID]map[string]string
}

func newQAccessTrainCollector(runID string) *qaccessTrainCollector {
	return &qaccessTrainCollector{
		path:    resolveTrainingCSVPath(),
		runID:   runID,
		pending: make(map[protocol.PathID]map[string]string),
	}
}

// flushAllPending writes queued rows using nextBW(pathID) as the next_bw_bps label.
func (c *qaccessTrainCollector) flushAllPending(nextBW func(protocol.PathID) float64) error {
	if len(c.pending) == 0 {
		return nil
	}
	ids := make([]protocol.PathID, 0, len(c.pending))
	for pid := range c.pending {
		ids = append(ids, pid)
	}
	for _, pid := range ids {
		if err := c.flushPendingLabel(pid, nextBW(pid)); err != nil {
			return err
		}
	}
	return nil
}

func (c *qaccessTrainCollector) ensureOpen() error {
	if c.opened {
		return nil
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.opened {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(c.path), 0755); err != nil {
		return err
	}
	_, statErr := os.Stat(c.path)
	writeHeader := os.IsNotExist(statErr)
	f, err := os.OpenFile(c.path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		return err
	}
	c.file = f
	c.writer = csv.NewWriter(f)
	if writeHeader {
		if err := c.writer.Write(qaccessTrainCSVHeader); err != nil {
			return err
		}
	}
	c.opened = true
	return nil
}

func (c *qaccessTrainCollector) flushPendingLabel(pathID protocol.PathID, nextBWbps float64) error {
	row, ok := c.pending[pathID]
	if !ok {
		return nil
	}
	row["next_bw_bps"] = fmt.Sprintf("%.0f", nextBWbps)
	if err := c.ensureOpen(); err != nil {
		return err
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	vals := make([]string, len(qaccessTrainCSVHeader))
	for i, col := range qaccessTrainCSVHeader {
		vals[i] = row[col]
	}
	if err := c.writer.Write(vals); err != nil {
		return err
	}
	c.writer.Flush()
	delete(c.pending, pathID)
	return c.writer.Error()
}

// recordPending labels the previous row with next_bw_bps and queues the new row for this path.
func (c *qaccessTrainCollector) recordPending(row map[string]string, pathID protocol.PathID, nextBWbps float64) error {
	if err := c.flushPendingLabel(pathID, nextBWbps); err != nil {
		return err
	}
	c.pending[pathID] = row
	return nil
}

func buildTrainRow(runID string, pm PathMetrics, sig ControlSignal, alpha, beta, gamma float64) map[string]string {
	row := make(map[string]string, len(qaccessTrainCSVHeader))
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
	// next_goodput_bps: reserved, intentionally blank in Phase 1 (see train_qaccess_t.py).
	row["next_goodput_bps"] = ""
	return row
}
