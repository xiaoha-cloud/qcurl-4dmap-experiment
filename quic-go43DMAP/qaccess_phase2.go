package quic

import (
	"encoding/json"
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
	defaultTriggerDropPct        = 15.0
	defaultTriggerCooldownMs     = 30000
	defaultTriggerMinSamples     = 100
	defaultRuntimeBufferSize   = 10000
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

	triggerUpdate      bool
	triggerDropPct     float64
	triggerCooldown    time.Duration
	triggerMinSamples  int64
	updateRequestPath  string
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

		triggerUpdate:     envBool("QACCESS_TRIGGER_UPDATE", false),
		triggerDropPct:    envFloat("QACCESS_TRIGGER_DROP_PCT", defaultTriggerDropPct),
		triggerCooldown:   time.Duration(envInt("QACCESS_TRIGGER_COOLDOWN_MS", defaultTriggerCooldownMs)) * time.Millisecond,
		triggerMinSamples: int64(envInt("QACCESS_TRIGGER_MIN_SAMPLES", defaultTriggerMinSamples)),
		updateRequestPath: resolveUpdateRequestJSONPath(),
	}
}

func resolveRuntimeSamplesCSVPath() string {
	if p := os.Getenv("QACCESS_RUNTIME_SAMPLES_CSV"); p != "" {
		return p
	}
	return "derived/qaccess_runtime_samples.csv"
}

func resolveUpdateRequestJSONPath() string {
	if p := os.Getenv("QACCESS_UPDATE_REQUEST_JSON"); p != "" {
		return p
	}
	return "derived/qaccess_update_request.json"
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
	if err := os.WriteFile(tmp, data, 0644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
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

func (uc *UtilityController) maybeTriggerCoefficientUpdate(now time.Time) {
	if !uc.phase2.triggerUpdate || uc.Mode != ModeQAccessT {
		return
	}
	if uc.runtimeExporter != nil && uc.runtimeExporter.bufferSize() < uc.phase2.triggerMinSamples {
		return
	}
	if !uc.lastTriggerTime.IsZero() && now.Sub(uc.lastTriggerTime) < uc.phase2.triggerCooldown {
		return
	}
	n := len(uc.roundBwHistory)
	if n < 4 {
		return
	}
	split := n / 2
	var prevSum, recentSum float64
	for i := 0; i < split; i++ {
		prevSum += uc.roundBwHistory[i]
	}
	for i := split; i < n; i++ {
		recentSum += uc.roundBwHistory[i]
	}
	prevAvg := prevSum / float64(split)
	recentAvg := recentSum / float64(n-split)
	if prevAvg <= 0 {
		return
	}
	dropPct := (prevAvg - recentAvg) / prevAvg * 100.0
	if dropPct < uc.phase2.triggerDropPct {
		return
	}

	c := uc.getCoefficients()
	bufSize := int64(0)
	if uc.runtimeExporter != nil {
		bufSize = uc.runtimeExporter.bufferSize()
	}
	req := map[string]interface{}{
		"timestamp_ms":        now.UnixNano() / 1e6,
		"reason":              "throughput_drop",
		"previous_avg_bw_bps": prevAvg,
		"recent_avg_bw_bps":   recentAvg,
		"drop_pct":            dropPct,
		"current_alpha":       c.Alpha,
		"current_beta":        c.Beta,
		"current_gamma":       c.Gamma,
		"active_paths":        uc.lastRoundActivePaths,
		"runtime_buffer_size": bufSize,
	}
	if err := writeJSONAtomic(uc.phase2.updateRequestPath, req); err != nil {
		return
	}
	uc.lastTriggerTime = now
	utils.Infof(
		"[qaccess_t] trigger update reason=throughput_drop previous_avg_bw=%.0f recent_avg_bw=%.0f drop_pct=%.2f",
		prevAvg, recentAvg, dropPct,
	)
}
