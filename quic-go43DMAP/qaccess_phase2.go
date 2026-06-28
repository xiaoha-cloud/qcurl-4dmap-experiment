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

	"github.com/lucas-clemente/quic-go/internal/protocol"
	"github.com/lucas-clemente/quic-go/internal/utils"
)

const (
	defaultCoeffReloadIntervalMs = 5000
	defaultCoeffSmoothing        = 0.2
	defaultTriggerDropPct        = 5.0
	defaultTriggerCooldownMs     = 60000
	defaultRuntimeBufferSize     = 5000
	defaultMinSamplesPerPath     = 1
	defaultTriggerAuditPath      = "derived/qaccess_trigger_audit.jsonl"
	maxRoundBwHistory            = 32
)

type qaccessPhase2Config struct {
	enabled       bool
	owner         bool
	endpointRole  string
	stateDir      string
	connectionID  string
	rtmpSessionID string
	streamKey     string

	coeffReload         bool
	coeffReloadInterval time.Duration
	coeffSmoothing      float64
	coeffJSONPath       string

	runtimeExport     bool
	runtimeSamples    string
	runtimeBufferMax  int64
	minSamplesPerPath int64

	triggerUpdate           bool
	triggerOnBufferFull     bool
	triggerOnThroughputDrop bool
	triggerDropPct          float64
	triggerCooldown         time.Duration
	triggerPeriodicMs       int
	multipathShadowScoring  bool
	minSenderByteDelta      uint64
	updateRequestPath       string
	updateResponsePath      string
	triggerAuditPath        string
}

func loadQAccessPhase2Config() qaccessPhase2Config {
	stateDir := strings.TrimSpace(os.Getenv("QACCESS_PHASE2_STATE_DIR"))
	return qaccessPhase2Config{
		enabled:      envBool("QACCESS_PHASE2_ENABLED", false),
		owner:        envBool("QACCESS_PHASE2_OWNER", false),
		endpointRole: strings.TrimSpace(os.Getenv("QACCESS_ENDPOINT_ROLE")),
		stateDir:     stateDir,

		coeffReload:         envBool("QACCESS_COEFF_RELOAD", false),
		coeffReloadInterval: time.Duration(envInt("QACCESS_COEFF_RELOAD_INTERVAL_MS", defaultCoeffReloadIntervalMs)) * time.Millisecond,
		coeffSmoothing:      envFloat("QACCESS_COEFF_SMOOTHING", defaultCoeffSmoothing),
		coeffJSONPath:       resolveCoeffsJSONPath(),

		runtimeExport:     envBool("QACCESS_RUNTIME_SAMPLE_EXPORT", false),
		runtimeSamples:    resolveRuntimeSamplesCSVPath(),
		runtimeBufferMax:  int64(envInt("QACCESS_RUNTIME_BUFFER_SIZE", defaultRuntimeBufferSize)),
		minSamplesPerPath: int64(envInt("QACCESS_MIN_SAMPLES_PER_PATH", defaultMinSamplesPerPath)),

		triggerUpdate:           envBool("QACCESS_TRIGGER_UPDATE", false),
		triggerOnBufferFull:     envBool("QACCESS_TRIGGER_ON_BUFFER_FULL", true),
		triggerOnThroughputDrop: envBool("QACCESS_TRIGGER_ON_THROUGHPUT_DROP", false),
		triggerDropPct:          envFloat("QACCESS_TRIGGER_DROP_PCT", defaultTriggerDropPct),
		triggerCooldown:         time.Duration(envInt("QACCESS_TRIGGER_COOLDOWN_MS", defaultTriggerCooldownMs)) * time.Millisecond,
		triggerPeriodicMs:       envInt("QACCESS_TRIGGER_PERIODIC_MS", 0),
		multipathShadowScoring:  envBool("QACCESS_MULTIPATH_SHADOW_SCORING", false),
		minSenderByteDelta:      uint64(envInt("QACCESS_MIN_SENDER_BYTE_DELTA", 1)),
		updateRequestPath:       resolveUpdateRequestJSONPath(),
		updateResponsePath:      resolveUpdateResponseJSONPath(),
		triggerAuditPath:        resolveTriggerAuditPath(),
	}
}

func loadQAccessPhase2ConfigForSession(cfg Phase2SessionConfig, connectionID string) qaccessPhase2Config {
	p := loadQAccessPhase2Config()
	p.enabled = cfg.Enabled
	p.owner = cfg.Owner
	p.endpointRole = cfg.EndpointRole
	p.stateDir = cfg.StateDir
	p.connectionID = connectionID
	p.rtmpSessionID = cfg.RTMPSessionID
	p.streamKey = cfg.StreamKey
	p.coeffJSONPath = filepath.Join(cfg.StateDir, "qaccess_t_runtime_coefficients.json")
	p.runtimeSamples = filepath.Join(cfg.StateDir, "qaccess_runtime_samples.csv")
	p.updateRequestPath = filepath.Join(cfg.StateDir, "qaccess_update_request.json")
	p.updateResponsePath = filepath.Join(cfg.StateDir, "qaccess_update_response.json")
	p.triggerAuditPath = filepath.Join(cfg.StateDir, "qaccess_trigger_audit.jsonl")
	return p
}

func (uc *UtilityController) phase2MutationAllowed() bool {
	return uc != nil && isQAccessRuntimeMode(uc.Mode) && uc.phase2.enabled && uc.phase2.owner &&
		uc.phase2.endpointRole == Phase2OwnerRole && filepath.IsAbs(uc.phase2.stateDir)
}

func resolveTriggerAuditPath() string {
	if p := os.Getenv("QACCESS_TRIGGER_AUDIT_JSONL"); p != "" {
		return p
	}
	return defaultTriggerAuditPath
}

func appendTriggerAudit(path string, payload map[string]interface{}) {
	if path == "" {
		return
	}
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		return
	}
	data, err := json.Marshal(payload)
	if err != nil {
		return
	}
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0666)
	if err != nil {
		return
	}
	_, _ = f.Write(append(data, '\n'))
	_ = f.Chmod(0666)
	_ = f.Close()
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
	if !uc.phase2.coeffReload || !isQAccessRuntimeMode(uc.Mode) {
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

	uc.coeffsMu.RLock()
	oldPerPath := make(map[protocol.PathID]QAccessCoefficients, len(uc.perPathCoeffs))
	for pid, c := range uc.perPathCoeffs {
		oldPerPath[pid] = c
	}
	oldFallback := uc.coeffs
	uc.coeffsMu.RUnlock()

	doc, err := LoadQAccessCoeffsDocument(uc.phase2.coeffJSONPath)
	if err != nil {
		return
	}
	s := clamp(uc.phase2.coeffSmoothing, 0, 1)
	newPerPath := make(map[protocol.PathID]QAccessCoefficients)
	for key := range doc.Paths {
		pid64, err := strconv.ParseUint(key, 10, 8)
		if err != nil {
			continue
		}
		pid := protocol.PathID(pid64)
		loaded := ResolveCoefficientsForPath(doc, pid)
		if doc.Metric != "" {
			loaded.Metric = doc.Metric
		}
		prevC := loaded
		if oldC, ok := oldPerPath[pid]; ok {
			prevC = oldC
		}
		applied := QAccessCoefficients{
			Alpha:  prevC.Alpha*(1-s) + loaded.Alpha*s,
			Beta:   prevC.Beta*(1-s) + loaded.Beta*s,
			Gamma:  prevC.Gamma*(1-s) + loaded.Gamma*s,
			Source: loaded.Source,
			Metric: loaded.Metric,
		}
		newPerPath[pid] = applied
		utils.Infof(
			"[%s] reloaded path_id=%d alpha=%.4f beta=%.4f gamma=%.4f source=%s",
			uc.logPrefix(), pid, applied.Alpha, applied.Beta, applied.Gamma, applied.Source,
		)
	}
	fallback := ResolveCoefficientsForPath(doc, protocol.InitialPathID)
	if doc.Metric != "" {
		fallback.Metric = doc.Metric
	}
	if len(newPerPath) > 0 {
		uc.coeffsMu.Lock()
		uc.coeffsDoc = doc
		uc.perPathCoeffs = newPerPath
		uc.coeffs = fallback
		uc.coeffsMu.Unlock()
		return
	}
	if !validQAccessCoefficients(fallback) {
		return
	}
	oldC, applied := uc.updateCoefficientsSmoothly(fallback, s)
	utils.Infof(
		"[%s] reloaded coefficients alpha_old=%.4f beta_old=%.4f gamma_old=%.4f alpha_new=%.4f beta_new=%.4f gamma_new=%.4f alpha_applied=%.4f beta_applied=%.4f gamma_applied=%.4f",
		uc.logPrefix(), oldC.Alpha, oldC.Beta, oldC.Gamma,
		fallback.Alpha, fallback.Beta, fallback.Gamma,
		applied.Alpha, applied.Beta, applied.Gamma,
	)
	_ = oldFallback
}

func (uc *UtilityController) forceReloadCoefficients(now time.Time) {
	uc.lastCoeffCheck = time.Time{}
	uc.lastCoeffMtime = time.Time{}
	uc.maybeReloadCoefficients(now)
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
	if !uc.phase2MutationAllowed() {
		return ""
	}
	uc.requestSerial++
	return fmt.Sprintf("%s_%d_%d", uc.RunID, now.UnixNano()/1e6, uc.requestSerial)
}

func (uc *UtilityController) writePerPathTrigger(now time.Time, pathID protocol.PathID, reason string, globalBuffer bool, extra map[string]interface{}) bool {
	if !uc.phase2MutationAllowed() {
		return false
	}
	if uc.updateInProgress {
		return false
	}
	if _, err := os.Stat(uc.phase2.updateRequestPath); err == nil {
		return false
	}

	c := uc.getCoefficientsForPath(pathID)
	nSamples := int64(0)
	bufSize := int64(0)
	if uc.runtimeExporter != nil {
		nSamples = uc.runtimeExporter.pathBufferSize(pathID)
		bufSize = uc.runtimeExporter.bufferSize()
	}
	requestID := uc.newRequestID(now)
	req := map[string]interface{}{
		"request_id":           requestID,
		"path_id":              uint64(pathID),
		"timestamp_ms":         now.UnixNano() / 1e6,
		"reason":               reason,
		"n_samples":            nSamples,
		"runtime_buffer_size":  bufSize,
		"buffer_capacity":      uc.phase2.runtimeBufferMax,
		"current_alpha":        c.Alpha,
		"current_beta":         c.Beta,
		"current_gamma":        c.Gamma,
		"coeff_source":         c.Source,
		"run_id":               uc.RunID,
		"controller_variant":   string(uc.Mode),
		"experiment_elapsed_s": experimentElapsedSeconds(uc.RunID, now),
	}
	for key, value := range extra {
		req[key] = value
	}
	if err := writeJSONAtomic(uc.phase2.updateRequestPath, req); err != nil {
		return false
	}
	uc.updateInProgress = true
	uc.inflightRequestID = requestID
	uc.inflightPathID = pathID
	uc.inflightGlobalBuffer = globalBuffer
	uc.lastTriggerTime = now
	utils.Infof(
		"[%s] %s trigger request_id=%s path_id=%d n_samples=%d alpha=%.4f beta=%.4f gamma=%.4f source=%s",
		uc.logPrefix(), reason, requestID, pathID, nSamples, c.Alpha, c.Beta, c.Gamma, c.Source,
	)
	return true
}

func experimentElapsedSeconds(runID string, now time.Time) float64 {
	start, err := time.ParseInLocation("20060102_150405", runID, time.Local)
	if err != nil {
		return -1
	}
	return now.Sub(start).Seconds()
}

func pathIDsAsUint64(paths []protocol.PathID) []uint64 {
	out := make([]uint64, len(paths))
	for i, pathID := range paths {
		out[i] = uint64(pathID)
	}
	return out
}

func rowsByPathForJSON(rows map[protocol.PathID]int64) map[string]int64 {
	out := make(map[string]int64, len(rows))
	for pathID, count := range rows {
		out[strconv.FormatUint(uint64(pathID), 10)] = count
	}
	return out
}

func uint64ByPathForJSON(values map[protocol.PathID]uint64) map[string]uint64 {
	out := make(map[string]uint64, len(values))
	for pathID, value := range values {
		out[strconv.FormatUint(uint64(pathID), 10)] = value
	}
	return out
}

func stringsByPathForJSON(values map[protocol.PathID]string) map[string]string {
	out := make(map[string]string, len(values))
	for pathID, value := range values {
		out[strconv.FormatUint(uint64(pathID), 10)] = value
	}
	return out
}

func boolByPathForJSON(values map[protocol.PathID]bool) map[string]bool {
	out := make(map[string]bool, len(values))
	for pathID, value := range values {
		out[strconv.FormatUint(uint64(pathID), 10)] = value
	}
	return out
}

func (uc *UtilityController) logBufferDecision(now time.Time, decision string, snapshot qaccessBufferSnapshot, cooldownRemaining time.Duration) {
	if uc.lastBufferDecision == decision {
		return
	}
	uc.lastBufferDecision = decision
	rowsJSON, _ := json.Marshal(rowsByPathForJSON(snapshot.RowsByPath))
	appendTriggerAudit(uc.phase2.triggerAuditPath, map[string]interface{}{
		"timestamp_ms": now.UnixNano() / 1e6, "event": "buffer_trigger_eval", "trigger_decision": decision,
		"total_rows": snapshot.TotalRows, "valid_rows_total": snapshot.TotalRows,
		"rows_by_path": rowsByPathForJSON(snapshot.RowsByPath), "active_paths": pathIDsAsUint64(snapshot.ActivePaths),
		"eligible_paths": pathIDsAsUint64(snapshot.EligiblePaths), "selected_path": uint64(snapshot.SelectedPath),
		"sender_byte_delta_by_path":    uint64ByPathForJSON(snapshot.SenderByteDelta),
		"sender_bytes_first_by_path":   uint64ByPathForJSON(snapshot.SenderBytesFirst),
		"sender_bytes_last_by_path":    uint64ByPathForJSON(snapshot.SenderBytesLast),
		"sender_counter_reset_by_path": boolByPathForJSON(snapshot.SenderCounterReset),
		"endpoints_by_path":            stringsByPathForJSON(snapshot.Endpoints),
		"exclusion_reasons_by_path":    stringsByPathForJSON(snapshot.ExclusionReasons),
		"runtime_buffer_max":           uc.phase2.runtimeBufferMax, "min_samples_per_path": uc.phase2.minSamplesPerPath,
		"update_in_progress": uc.updateInProgress, "cooldown_remaining_ms": cooldownRemaining / time.Millisecond,
		"request_id": uc.inflightRequestID, "measurement_window_start_ms": snapshot.WindowStartMs,
		"measurement_window_end_ms": snapshot.WindowEndMs,
	})
	for _, pathID := range snapshot.ActivePaths {
		_, eligible := snapshot.ExclusionReasons[pathID]
		appendTriggerAudit(uc.phase2.triggerAuditPath, map[string]interface{}{
			"timestamp_ms": now.UnixNano() / 1e6, "event": "path_media_eligibility",
			"path_id": uint64(pathID), "endpoint": snapshot.Endpoints[pathID],
			"row_count": snapshot.RowsByPath[pathID], "sender_byte_delta": snapshot.SenderByteDelta[pathID],
			"sender_bytes_first": snapshot.SenderBytesFirst[pathID], "sender_bytes_last": snapshot.SenderBytesLast[pathID],
			"sender_counter_reset": snapshot.SenderCounterReset[pathID],
			"eligible":             !eligible, "exclusion_reason": snapshot.ExclusionReasons[pathID],
			"parent_trigger_decision": decision,
		})
	}
	utils.Infof(
		"[%s] buffer_trigger_eval timestamp_ms=%d decision=%s total_rows=%d rows_by_path=%s active_paths=%v eligible_paths=%v selected_path=%d runtime_buffer_max=%d min_samples_per_path=%d update_in_progress=%t cooldown_remaining_ms=%d measurement_window_start_ms=%d measurement_window_end_ms=%d",
		uc.logPrefix(), now.UnixNano()/1e6, decision, snapshot.TotalRows, string(rowsJSON), pathIDsAsUint64(snapshot.ActivePaths),
		pathIDsAsUint64(snapshot.EligiblePaths), uint64(snapshot.SelectedPath), uc.phase2.runtimeBufferMax,
		uc.phase2.minSamplesPerPath, uc.updateInProgress, cooldownRemaining/time.Millisecond,
		snapshot.WindowStartMs, snapshot.WindowEndMs,
	)
}

func (uc *UtilityController) writeBufferFullTrigger(now time.Time, snapshot qaccessBufferSnapshot) bool {
	return uc.writePerPathTrigger(now, snapshot.SelectedPath, "buffer_full", true, map[string]interface{}{
		"valid_rows_total":             snapshot.TotalRows,
		"rows_by_path":                 rowsByPathForJSON(snapshot.RowsByPath),
		"active_paths":                 pathIDsAsUint64(snapshot.ActivePaths),
		"eligible_paths":               pathIDsAsUint64(snapshot.EligiblePaths),
		"sender_byte_delta_by_path":    uint64ByPathForJSON(snapshot.SenderByteDelta),
		"sender_bytes_first_by_path":   uint64ByPathForJSON(snapshot.SenderBytesFirst),
		"sender_bytes_last_by_path":    uint64ByPathForJSON(snapshot.SenderBytesLast),
		"sender_counter_reset_by_path": boolByPathForJSON(snapshot.SenderCounterReset),
		"endpoints_by_path":            stringsByPathForJSON(snapshot.Endpoints),
		"exclusion_reasons_by_path":    stringsByPathForJSON(snapshot.ExclusionReasons),
		"selected_path":                uint64(snapshot.SelectedPath),
		"min_samples_per_path":         uc.phase2.minSamplesPerPath,
		"measurement_window_start_ms":  snapshot.WindowStartMs,
		"measurement_window_end_ms":    snapshot.WindowEndMs,
	})
}

func (uc *UtilityController) maybeCheckUpdateResponse(now time.Time) {
	if !uc.phase2MutationAllowed() {
		return
	}
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
	utils.Infof("[%s] update response request_id=%s status=%s", uc.logPrefix(), rid, status)
	appendTriggerAudit(uc.phase2.triggerAuditPath, map[string]interface{}{
		"timestamp_ms": now.UnixNano() / 1e6, "event": "response_consumed", "request_id": rid,
		"status": status, "path_id": uint64(uc.inflightPathID), "global_buffer": uc.inflightGlobalBuffer,
	})
	if status == "APPLIED_AGGREGATE" || status == "APPLIED" {
		uc.forceReloadCoefficients(now)
		appendTriggerAudit(uc.phase2.triggerAuditPath, map[string]interface{}{
			"timestamp_ms": now.UnixNano() / 1e6, "event": "coefficients_reloaded_after_response",
			"request_id": rid, "status": status,
		})
	}

	uc.updateInProgress = false
	uc.inflightRequestID = ""
	pathID := uc.inflightPathID
	uc.inflightPathID = 0
	globalBuffer := uc.inflightGlobalBuffer
	uc.inflightGlobalBuffer = false
	uc.lastBufferDecision = ""
	uc.lastTriggerTime = now

	if uc.runtimeExporter != nil {
		if globalBuffer {
			resetStatus := "ok"
			if err := uc.runtimeExporter.resetBuffer(); err != nil {
				resetStatus = "error"
				utils.Infof("[%s] runtime global buffer reset after response failed: %v", uc.logPrefix(), err)
			}
			appendTriggerAudit(uc.phase2.triggerAuditPath, map[string]interface{}{
				"timestamp_ms": now.UnixNano() / 1e6, "event": "buffer_reset", "request_id": rid,
				"status": resetStatus, "sampling_resumed": resetStatus == "ok",
			})
		} else {
			if err := uc.runtimeExporter.removePathRows(pathID); err != nil {
				utils.Infof("[%s] runtime buffer remove path_id=%d failed: %v", uc.logPrefix(), pathID, err)
			}
		}
	}
}

func (uc *UtilityController) maybeTriggerCoefficientUpdate(now time.Time) {
	if !uc.phase2MutationAllowed() || !uc.phase2.triggerUpdate {
		return
	}

	uc.maybeCheckUpdateResponse(now)

	if !uc.phase2.runtimeExport || uc.runtimeExporter == nil {
		return
	}

	snapshot := uc.runtimeExporter.triggerSnapshot(uc.phase2.minSamplesPerPath, uc.phase2.multipathShadowScoring, uc.phase2.minSenderByteDelta)
	bufSize := snapshot.TotalRows
	if !snapshot.AtCapacity {
		uc.logBufferDecision(now, "buffer_not_full", snapshot, 0)
	}
	if uc.updateInProgress {
		if snapshot.AtCapacity {
			uc.logBufferDecision(now, "blocked_update_in_progress", snapshot, 0)
		}
		return
	}

	cooldownRemaining := time.Duration(0)
	if !uc.lastTriggerTime.IsZero() {
		cooldownRemaining = uc.phase2.triggerCooldown - now.Sub(uc.lastTriggerTime)
		if cooldownRemaining < 0 {
			cooldownRemaining = 0
		}
	}
	inCooldown := cooldownRemaining > 0
	if inCooldown {
		if snapshot.AtCapacity {
			uc.logBufferDecision(now, "blocked_cooldown", snapshot, cooldownRemaining)
		}
		return
	}

	// Primary trigger: a bounded global window selects its best-sampled path.
	if uc.phase2.triggerOnBufferFull && snapshot.AtCapacity {
		if !snapshot.HasSelectedPath {
			uc.logBufferDecision(now, "buffer_full_no_eligible_path", snapshot, 0)
			return
		}
		if uc.writeBufferFullTrigger(now, snapshot) {
			uc.logBufferDecision(now, "request_written", snapshot, 0)
			return
		}
		uc.logBufferDecision(now, "request_write_failed", snapshot, 0)
		return
	}

	prevAvg, recentAvg, dropPct := uc.roundBwDropStats()

	// Optional periodic debug trigger (off by default).
	if uc.phase2.triggerPeriodicMs > 0 {
		interval := time.Duration(uc.phase2.triggerPeriodicMs) * time.Millisecond
		if uc.lastPeriodicTrigger.IsZero() || now.Sub(uc.lastPeriodicTrigger) >= interval {
			pathID := uc.pickLegacyTriggerPath()
			if uc.writeLegacyTriggerRequest(now, pathID, "periodic", prevAvg, recentAvg, dropPct, bufSize) {
				uc.lastPeriodicTrigger = now
				utils.Infof("[%s] periodic trigger (debug) runtime_buffer_size=%d", uc.logPrefix(), bufSize)
			}
		}
		return
	}

	// Optional throughput-drop trigger (off by default; not primary).
	if uc.phase2.triggerOnThroughputDrop {
		n := len(uc.roundBwHistory)
		if n >= 4 && prevAvg > 0 && dropPct >= uc.phase2.triggerDropPct {
			pathID := uc.pickLegacyTriggerPath()
			if uc.writeLegacyTriggerRequest(now, pathID, "throughput_drop", prevAvg, recentAvg, dropPct, bufSize) {
				utils.Infof(
					"[%s] throughput_drop trigger drop_pct=%.2f runtime_buffer_size=%d",
					uc.logPrefix(), dropPct, bufSize,
				)
			}
		}
	}
}

func (uc *UtilityController) pickLegacyTriggerPath() protocol.PathID {
	if uc.runtimeExporter == nil {
		return protocol.PathID(1)
	}
	var best protocol.PathID = 1
	var bestN int64
	snapshot := uc.runtimeExporter.triggerSnapshot(1, false, 0)
	for _, pid := range snapshot.EligiblePaths {
		n := uc.runtimeExporter.pathBufferSize(pid)
		if n > bestN {
			bestN = n
			best = pid
		}
	}
	if bestN > 0 {
		return best
	}
	return protocol.PathID(1)
}

func (uc *UtilityController) writeLegacyTriggerRequest(now time.Time, pathID protocol.PathID, reason string, prevAvg, recentAvg, dropPct float64, bufSize int64) bool {
	return uc.writePerPathTrigger(now, pathID, reason, false, map[string]interface{}{
		"previous_avg_bw_bps": prevAvg,
		"recent_avg_bw_bps":   recentAvg,
		"drop_pct":            dropPct,
	})
}
