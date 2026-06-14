package quic

import (
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/lucas-clemente/quic-go/internal/utils"
)

const (
	defaultCoeffReloadIntervalMs = 5000
	defaultCoeffSmoothing        = 0.2
	defaultTriggerDropPct        = 5.0
	defaultTriggerCooldownMs     = 60000
	defaultRuntimeBufferSize     = 5000
	maxRoundBwHistory            = 32
)

type qaccessPhase2Config struct {
	coeffReload         bool
	coeffReloadInterval time.Duration
	coeffSmoothing      float64
	coeffJSONPath       string

	runtimeExport    bool
	runtimeSamples   string
	runtimeBufferMax int64

	triggerUpdate           bool
	triggerOnBufferFull     bool
	triggerOnThroughputDrop bool
	triggerDropPct          float64
	triggerCooldown         time.Duration
	triggerPeriodicMs       int
	updateRequestPath       string
	updateResponsePath      string
}

func loadQAccessPhase2Config() qaccessPhase2Config {
	return qaccessPhase2Config{
		coeffReload:         envBool("QACCESS_COEFF_RELOAD", false),
		coeffReloadInterval: time.Duration(envInt("QACCESS_COEFF_RELOAD_INTERVAL_MS", defaultCoeffReloadIntervalMs)) * time.Millisecond,
		coeffSmoothing:      envFloat("QACCESS_COEFF_SMOOTHING", defaultCoeffSmoothing),
		coeffJSONPath:       resolveCoeffsJSONPath(),

		runtimeExport:    envBool("QACCESS_RUNTIME_SAMPLE_EXPORT", false),
		runtimeSamples:   resolveRuntimeSamplesCSVPath(),
		runtimeBufferMax: int64(envInt("QACCESS_RUNTIME_BUFFER_SIZE", defaultRuntimeBufferSize)),

		triggerUpdate:           envBool("QACCESS_TRIGGER_UPDATE", false),
		triggerOnBufferFull:     envBool("QACCESS_TRIGGER_ON_BUFFER_FULL", true),
		triggerOnThroughputDrop: envBool("QACCESS_TRIGGER_ON_THROUGHPUT_DROP", false),
		triggerDropPct:          envFloat("QACCESS_TRIGGER_DROP_PCT", defaultTriggerDropPct),
		triggerCooldown:         time.Duration(envInt("QACCESS_TRIGGER_COOLDOWN_MS", defaultTriggerCooldownMs)) * time.Millisecond,
		triggerPeriodicMs:       envInt("QACCESS_TRIGGER_PERIODIC_MS", 0),
		updateRequestPath:       resolveUpdateRequestJSONPath(),
		updateResponsePath:      resolveUpdateResponseJSONPath(),
	}
}

func resolveRuntimeSamplesCSVPath() string {
	if p := os.Getenv("QACCESS_RUNTIME_SAMPLES_CSV"); p != "" {
		return p
	}
	return filepath.Join("derived", "qaccess_runtime_samples.csv")
}

func resolveUpdateRequestJSONPath() string {
	if p := os.Getenv("QACCESS_UPDATE_REQUEST_JSON"); p != "" {
		return p
	}
	return filepath.Join("derived", "qaccess_update_request.json")
}

func resolveUpdateResponseJSONPath() string {
	if p := os.Getenv("QACCESS_UPDATE_RESPONSE_JSON"); p != "" {
		return p
	}
	return filepath.Join("derived", "qaccess_update_response.json")
}

func envBool(key string, def bool) bool {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return def
	}
	switch strings.ToLower(v) {
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	default:
		return def
	}
}

func envInt(key string, def int) int {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return def
	}
	return n
}

func envFloat(key string, def float64) float64 {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return def
	}
	f, err := strconv.ParseFloat(v, 64)
	if err != nil {
		return def
	}
	return f
}

func validQAccessCoefficients(c QAccessCoefficients) bool {
	if !finitePositive(c.Alpha, 2.0) {
		return false
	}
	if !finiteNonNeg(c.Beta, 1.0) {
		return false
	}
	if !finiteNonNeg(c.Gamma, 1.0) {
		return false
	}
	return true
}

func finitePositive(v, max float64) bool {
	if math.IsNaN(v) || math.IsInf(v, 0) || v <= 0 || v > max {
		return false
	}
	return true
}

func finiteNonNeg(v, max float64) bool {
	if math.IsNaN(v) || math.IsInf(v, 0) || v < 0 || v > max {
		return false
	}
	return true
}

func writeJSONAtomic(path string, payload interface{}) error {
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		return err
	}
	data, err := json.MarshalIndent(payload, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0666); err != nil {
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		return err
	}
	// Worker (non-root) must read/unlink request JSON and read response after sudo Mininet runs.
	_ = os.Chmod(path, 0666)
	return nil
}

func (uc *UtilityController) getCoefficients() QAccessCoefficients {
	uc.coeffsMu.RLock()
	defer uc.coeffsMu.RUnlock()
	return uc.coeffs
}

func (uc *UtilityController) updateCoefficientsSmoothly(newC QAccessCoefficients, smoothing float64) (oldC, applied QAccessCoefficients) {
	s := clamp(smoothing, 0, 1)
	uc.coeffsMu.Lock()
	defer uc.coeffsMu.Unlock()
	oldC = uc.coeffs
	applied = QAccessCoefficients{
		Alpha:  oldC.Alpha*(1-s) + newC.Alpha*s,
		Beta:   oldC.Beta*(1-s) + newC.Beta*s,
		Gamma:  oldC.Gamma*(1-s) + newC.Gamma*s,
		Source: newC.Source,
		Metric: newC.Metric,
	}
	uc.coeffs = applied
	return oldC, applied
}

func (uc *UtilityController) maybeReloadCoefficients(now time.Time) {
	if !uc.phase2.coeffReload || uc.Mode != ModeQAccessT {
		return
	}
	if !uc.lastCoeffCheck.IsZero() && now.Sub(uc.lastCoeffCheck) < uc.phase2.coeffReloadInterval {
		return
	}
	uc.lastCoeffCheck = now

	info, err := os.Stat(uc.phase2.coeffJSONPath)
	if err != nil {
		return
	}
	if !uc.lastCoeffMtime.IsZero() && !info.ModTime().After(uc.lastCoeffMtime) {
		return
	}
	uc.lastCoeffMtime = info.ModTime()

	newC, err := LoadQAccessTCoefficients(uc.phase2.coeffJSONPath)
	if err != nil || !validQAccessCoefficients(newC) {
		return
	}
	oldC, applied := uc.updateCoefficientsSmoothly(newC, uc.phase2.coeffSmoothing)
	utils.Infof(
		"[qaccess_t] reloaded coefficients alpha_old=%.4f beta_old=%.4f gamma_old=%.4f alpha_new=%.4f beta_new=%.4f gamma_new=%.4f alpha_applied=%.4f beta_applied=%.4f gamma_applied=%.4f",
		oldC.Alpha, oldC.Beta, oldC.Gamma,
		newC.Alpha, newC.Beta, newC.Gamma,
		applied.Alpha, applied.Beta, applied.Gamma,
	)
}

func (uc *UtilityController) finalizeMonitorRoundThroughput() {
	if uc.currentRoundTotalBwBps > 0 {
		uc.roundBwHistory = append(uc.roundBwHistory, uc.currentRoundTotalBwBps)
		if len(uc.roundBwHistory) > maxRoundBwHistory {
			uc.roundBwHistory = uc.roundBwHistory[len(uc.roundBwHistory)-maxRoundBwHistory:]
		}
		uc.lastRoundActivePaths = uc.currentRoundActivePaths
	}
	uc.currentRoundTotalBwBps = 0
	uc.currentRoundActivePaths = 0
}

func (uc *UtilityController) noteActivePathThroughput(pm PathMetrics) {
	if !PathMetricsActive(pm) {
		return
	}
	uc.currentRoundTotalBwBps += sanitizeMetric(pm.BWbps)
	uc.currentRoundActivePaths++
}

func (uc *UtilityController) roundBwDropStats() (prevAvg, recentAvg, dropPct float64) {
	n := len(uc.roundBwHistory)
	if n < 4 {
		return 0, 0, 0
	}
	split := n / 2
	var prevSum, recentSum float64
	for i := 0; i < split; i++ {
		prevSum += uc.roundBwHistory[i]
	}
	for i := split; i < n; i++ {
		recentSum += uc.roundBwHistory[i]
	}
	prevAvg = prevSum / float64(split)
	recentAvg = recentSum / float64(n-split)
	if prevAvg <= 0 {
		return prevAvg, recentAvg, 0
	}
	dropPct = (prevAvg - recentAvg) / prevAvg * 100.0
	return prevAvg, recentAvg, dropPct
}

func (uc *UtilityController) runtimeBufferSize() int64 {
	if uc.runtimeExporter == nil {
		return 0
	}
	return uc.runtimeExporter.bufferSize()
}

func (uc *UtilityController) newRequestID(now time.Time) string {
	uc.requestSerial++
	return fmt.Sprintf("%s_%d_%d", uc.RunID, now.UnixNano()/1e6, uc.requestSerial)
}

func (uc *UtilityController) writeBufferFullTrigger(now time.Time, bufSize int64) bool {
	if uc.updateInProgress {
		return false
	}
	if _, err := os.Stat(uc.phase2.updateRequestPath); err == nil {
		return false
	}

	c := uc.getCoefficients()
	requestID := uc.newRequestID(now)
	req := map[string]interface{}{
		"request_id":          requestID,
		"timestamp_ms":        now.UnixNano() / 1e6,
		"reason":              "buffer_full",
		"runtime_buffer_size": bufSize,
		"buffer_capacity":     uc.phase2.runtimeBufferMax,
		"current_alpha":       c.Alpha,
		"current_beta":        c.Beta,
		"current_gamma":       c.Gamma,
		"run_id":              uc.RunID,
	}
	if err := writeJSONAtomic(uc.phase2.updateRequestPath, req); err != nil {
		return false
	}
	uc.updateInProgress = true
	uc.inflightRequestID = requestID
	uc.lastTriggerTime = now
	utils.Infof(
		"[qaccess_t] buffer_full trigger request_id=%s runtime_buffer_size=%d capacity=%d alpha=%.4f beta=%.4f gamma=%.4f",
		requestID, bufSize, uc.phase2.runtimeBufferMax, c.Alpha, c.Beta, c.Gamma,
	)
	return true
}

func (uc *UtilityController) maybeCheckUpdateResponse(now time.Time) {
	if !uc.updateInProgress || uc.inflightRequestID == "" {
		return
	}
	data, err := os.ReadFile(uc.phase2.updateResponsePath)
	if err != nil {
		return
	}
	var resp map[string]interface{}
	if err := json.Unmarshal(data, &resp); err != nil {
		return
	}
	rid, _ := resp["request_id"].(string)
	if rid == "" || rid != uc.inflightRequestID {
		return
	}
	status, _ := resp["status"].(string)
	utils.Infof("[qaccess_t] update response request_id=%s status=%s", rid, status)

	uc.updateInProgress = false
	uc.inflightRequestID = ""
	uc.lastTriggerTime = now

	if uc.runtimeExporter != nil {
		if err := uc.runtimeExporter.resetBuffer(); err != nil {
			utils.Infof("[qaccess_t] runtime buffer reset after response failed: %v", err)
		}
	}
}

func (uc *UtilityController) maybeTriggerCoefficientUpdate(now time.Time) {
	if !uc.phase2.triggerUpdate || uc.Mode != ModeQAccessT {
		return
	}

	uc.maybeCheckUpdateResponse(now)
	if uc.updateInProgress {
		return
	}

	if !uc.phase2.runtimeExport || uc.runtimeExporter == nil {
		return
	}

	bufSize := uc.runtimeBufferSize()
	threshold := uc.phase2.runtimeBufferMax
	if threshold <= 0 {
		threshold = defaultRuntimeBufferSize
	}

	inCooldown := !uc.lastTriggerTime.IsZero() && now.Sub(uc.lastTriggerTime) < uc.phase2.triggerCooldown

	// Primary trigger: full runtime training buffer (ACCeSS-like).
	if uc.phase2.triggerOnBufferFull && bufSize >= threshold && !inCooldown {
		uc.writeBufferFullTrigger(now, bufSize)
		return
	}

	if inCooldown {
		return
	}

	prevAvg, recentAvg, dropPct := uc.roundBwDropStats()

	// Optional periodic debug trigger (off by default).
	if uc.phase2.triggerPeriodicMs > 0 {
		interval := time.Duration(uc.phase2.triggerPeriodicMs) * time.Millisecond
		if uc.lastPeriodicTrigger.IsZero() || now.Sub(uc.lastPeriodicTrigger) >= interval {
			if uc.writeLegacyTriggerRequest(now, "periodic", prevAvg, recentAvg, dropPct, bufSize) {
				uc.lastPeriodicTrigger = now
				utils.Infof("[qaccess_t] periodic trigger (debug) runtime_buffer_size=%d", bufSize)
			}
		}
		return
	}

	// Optional throughput-drop trigger (off by default; not primary).
	if uc.phase2.triggerOnThroughputDrop {
		n := len(uc.roundBwHistory)
		if n >= 4 && prevAvg > 0 && dropPct >= uc.phase2.triggerDropPct {
			if uc.writeLegacyTriggerRequest(now, "throughput_drop", prevAvg, recentAvg, dropPct, bufSize) {
				utils.Infof(
					"[qaccess_t] throughput_drop trigger drop_pct=%.2f runtime_buffer_size=%d",
					dropPct, bufSize,
				)
			}
		}
	}
}

func (uc *UtilityController) writeLegacyTriggerRequest(now time.Time, reason string, prevAvg, recentAvg, dropPct float64, bufSize int64) bool {
	if uc.updateInProgress {
		return false
	}
	if _, err := os.Stat(uc.phase2.updateRequestPath); err == nil {
		return false
	}
	c := uc.getCoefficients()
	requestID := uc.newRequestID(now)
	req := map[string]interface{}{
		"request_id":          requestID,
		"timestamp_ms":        now.UnixNano() / 1e6,
		"reason":              reason,
		"previous_avg_bw_bps": prevAvg,
		"recent_avg_bw_bps":   recentAvg,
		"drop_pct":            dropPct,
		"current_alpha":       c.Alpha,
		"current_beta":        c.Beta,
		"current_gamma":       c.Gamma,
		"runtime_buffer_size": bufSize,
		"run_id":              uc.RunID,
	}
	if err := writeJSONAtomic(uc.phase2.updateRequestPath, req); err != nil {
		return false
	}
	uc.updateInProgress = true
	uc.inflightRequestID = requestID
	uc.lastTriggerTime = now
	return true
}
