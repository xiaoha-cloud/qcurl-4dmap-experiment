package quic

import (
	"fmt"
	"math"
	"sort"
	"time"

	"github.com/lucas-clemente/quic-go/internal/protocol"
)

const notAvailableForPreUpdateEvaluation = "NOT_AVAILABLE_FOR_PRE_UPDATE_EVALUATION"

type objectiveTriggerState struct {
	referenceSamples []float64
	reference        float64
	referenceReady   bool
	current          float64
	absoluteChange   float64
	relativeChange   float64
	triggerStreak    int
	recoveryStreak   int
	triggered        bool
	requestPending   bool
}

func objectiveTriggerModeForVariant(mode UtilityMode) string {
	switch mode {
	case ModeQAccessT:
		return "objective_t"
	case ModeQAccessD:
		return "objective_d"
	case ModeQAccessL:
		return "objective_l"
	default:
		return ""
	}
}

func (uc *UtilityController) objectiveTriggerEnabled() bool {
	return uc != nil && uc.phase2.triggerMode != "" && uc.phase2.triggerMode != defaultTriggerMode
}

func (uc *UtilityController) objectiveTriggerConfigurationValid() bool {
	if !uc.objectiveTriggerEnabled() {
		return uc == nil || uc.phase2.triggerMode == "" || uc.phase2.triggerMode == defaultTriggerMode
	}
	return uc.phase2.triggerMode == objectiveTriggerModeForVariant(uc.Mode)
}

func (uc *UtilityController) resetObjectiveTriggerState() {
	uc.objectiveTriggerStates = make(map[protocol.PathID]*objectiveTriggerState)
}

func medianFloat64(values []float64) float64 {
	if len(values) == 0 {
		return 0
	}
	ordered := append([]float64(nil), values...)
	sort.Float64s(ordered)
	mid := len(ordered) / 2
	if len(ordered)%2 == 1 {
		return ordered[mid]
	}
	return (ordered[mid-1] + ordered[mid]) / 2
}

func (uc *UtilityController) objectiveMetric(pm PathMetrics) float64 {
	switch uc.phase2.triggerMode {
	case "objective_t":
		return pm.BWbps
	case "objective_d":
		// OWDms is deliberately a delay proxy populated from SmoothedRTT()/2.
		return pm.OWDms
	case "objective_l":
		// LossRate is the sender's runtime losses/packets ratio, not tc percent.
		return pm.LossRate
	default:
		return 0
	}
}

func (uc *UtilityController) objectiveGateName() string {
	if uc.phase2.gateObjective != "" {
		return uc.phase2.gateObjective
	}
	switch uc.Mode {
	case ModeQAccessT:
		return "throughput"
	case ModeQAccessD:
		return "delay"
	case ModeQAccessL:
		return "loss"
	default:
		return ""
	}
}

func (uc *UtilityController) objectiveDecisionUnits() (triggerUnit, candidateUnit string) {
	switch uc.phase2.triggerMode {
	case "objective_t":
		return "bps", "bps"
	case "objective_d":
		return "ms", "ms"
	case "objective_l":
		return "ratio_0_to_1", "loss_risk_bytes"
	default:
		return "", ""
	}
}

func finiteObjectiveValue(value float64) float64 {
	if math.IsNaN(value) || math.IsInf(value, 0) {
		return 0
	}
	return value
}

func (uc *UtilityController) appendObjectiveDecisionAudit(now time.Time, pathID protocol.PathID, state *objectiveTriggerState, skipReason string) {
	if state == nil {
		state = &objectiveTriggerState{}
	}
	triggerUnit, candidateUnit := uc.objectiveDecisionUnits()
	appendTriggerAudit(uc.phase2.triggerAuditPath, map[string]interface{}{
		"timestamp_ms":              now.UnixNano() / 1e6,
		"event":                     "objective_trigger_decision",
		"decision_stage":            "deterioration_trigger",
		"variant":                   string(uc.Mode),
		"path_id":                   uint64(pathID),
		"trigger_mode":              uc.phase2.triggerMode,
		"gate_policy":               uc.phase2.gatePolicy,
		"gate_objective":            uc.objectiveGateName(),
		"reference_value":           finiteObjectiveValue(state.reference),
		"current_value":             finiteObjectiveValue(state.current),
		"absolute_change":           finiteObjectiveValue(state.absoluteChange),
		"relative_change":           finiteObjectiveValue(state.relativeChange),
		"trigger_streak":            state.triggerStreak,
		"triggered":                 state.triggered,
		"current_candidate_score":   nil,
		"best_candidate_score":      nil,
		"absolute_improvement":      nil,
		"relative_improvement":      nil,
		"gate_passed":               false,
		"actual_applied":            false,
		"skip_reason":               skipReason,
		"trigger_value_unit":        triggerUnit,
		"candidate_score_unit":      candidateUnit,
		"absolute_improvement_unit": candidateUnit,
		"secondary_guardrails":      notAvailableForPreUpdateEvaluation,
	})
}

func (uc *UtilityController) objectiveThresholdDescription() string {
	switch uc.phase2.triggerMode {
	case "objective_t":
		return fmt.Sprintf("abs_change_bps>=%.0f OR relative_change>=%.6g", uc.phase2.triggerTAbsBps, uc.phase2.triggerTRelative)
	case "objective_d":
		return fmt.Sprintf("increase_ms>=%.6g OR relative_increase>=%.6g", uc.phase2.triggerDAbsMs, uc.phase2.triggerDRelative)
	case "objective_l":
		return fmt.Sprintf("loss_ratio_increase>=%.6g", uc.phase2.triggerLRatio)
	default:
		return ""
	}
}

func (uc *UtilityController) objectiveDeteriorated(reference, current float64) (bool, float64, float64) {
	delta := current - reference
	relative := 0.0
	if math.Abs(reference) > 1e-12 {
		relative = delta / math.Abs(reference)
	}
	switch uc.phase2.triggerMode {
	case "objective_t":
		return math.Abs(delta) >= uc.phase2.triggerTAbsBps || math.Abs(relative) >= uc.phase2.triggerTRelative, delta, relative
	case "objective_d":
		return delta > 0 && (delta >= uc.phase2.triggerDAbsMs || relative >= uc.phase2.triggerDRelative), delta, relative
	case "objective_l":
		return delta >= uc.phase2.triggerLRatio, delta, relative
	default:
		return false, delta, relative
	}
}

func (uc *UtilityController) observeObjectiveTrigger(pm PathMetrics, now time.Time) {
	if !uc.objectiveTriggerEnabled() || !uc.objectiveTriggerConfigurationValid() {
		return
	}
	if uc.objectiveTriggerStates == nil {
		uc.resetObjectiveTriggerState()
	}
	state := uc.objectiveTriggerStates[pm.PathID]
	if state == nil {
		state = &objectiveTriggerState{}
		uc.objectiveTriggerStates[pm.PathID] = state
	}
	current := sanitizeMetric(uc.objectiveMetric(pm))
	state.current = current
	elapsed := experimentElapsedSeconds(uc.RunID, now)
	if elapsed >= uc.phase2.triggerReferenceStart && elapsed <= uc.phase2.triggerReferenceEnd {
		state.referenceSamples = append(state.referenceSamples, current)
		state.reference = medianFloat64(state.referenceSamples)
		state.referenceReady = true
	}
	if elapsed > uc.phase2.triggerReferenceEnd && !state.referenceReady {
		// A late-starting path establishes an observed reference before comparisons.
		state.referenceSamples = append(state.referenceSamples, current)
		state.reference = current
		state.referenceReady = true
	}
	if !state.referenceReady || elapsed <= uc.phase2.triggerReferenceEnd {
		uc.appendObjectiveDecisionAudit(now, pm.PathID, state, "reference_not_ready")
		return
	}

	deteriorated, absoluteChange, relativeChange := uc.objectiveDeteriorated(state.reference, current)
	state.current = current
	state.absoluteChange = absoluteChange
	state.relativeChange = relativeChange
	skipReason := "objective_trigger_not_satisfied"
	if deteriorated {
		state.recoveryStreak = 0
		if !state.triggered {
			state.triggerStreak++
			if state.triggerStreak >= uc.phase2.triggerActivateSamples {
				state.triggered = true
				state.requestPending = true
				skipReason = ""
			} else {
				skipReason = "trigger_streak_incomplete"
			}
		} else {
			skipReason = "trigger_already_active"
		}
	} else {
		state.triggerStreak = 0
		if state.triggered {
			state.recoveryStreak++
			if state.recoveryStreak >= uc.phase2.triggerRecoverySamples {
				state.triggered = false
				state.requestPending = false
				state.recoveryStreak = 0
			}
		}
	}
	uc.appendObjectiveDecisionAudit(now, pm.PathID, state, skipReason)
}

func (uc *UtilityController) maybeTriggerObjectiveUpdate(now time.Time) {
	if !uc.objectiveTriggerConfigurationValid() || uc.updateInProgress {
		return
	}
	if !uc.lastTriggerTime.IsZero() && now.Sub(uc.lastTriggerTime) < uc.phase2.triggerCooldown {
		for pathID, state := range uc.objectiveTriggerStates {
			if state != nil && state.requestPending && state.triggered {
				uc.appendObjectiveDecisionAudit(now, pathID, state, "cooldown_active")
			}
		}
		return
	}
	pathIDs := make([]protocol.PathID, 0, len(uc.objectiveTriggerStates))
	for pathID := range uc.objectiveTriggerStates {
		pathIDs = append(pathIDs, pathID)
	}
	sort.Slice(pathIDs, func(i, j int) bool { return pathIDs[i] < pathIDs[j] })
	for _, pathID := range pathIDs {
		state := uc.objectiveTriggerStates[pathID]
		if state == nil || !state.requestPending || !state.triggered {
			continue
		}
		if uc.runtimeExporter.pathBufferSize(pathID) < uc.phase2.minSamplesPerPath {
			continue
		}
		if uc.writePerPathTrigger(now, pathID, uc.phase2.triggerMode, false, map[string]interface{}{
			"trigger_mode":      uc.phase2.triggerMode,
			"gate_policy":       uc.phase2.gatePolicy,
			"gate_objective":    uc.objectiveGateName(),
			"reference_value":   state.reference,
			"current_value":     state.current,
			"trigger_threshold": uc.objectiveThresholdDescription(),
			"trigger_streak":    state.triggerStreak,
			"triggered":         state.triggered,
			"absolute_change":   state.absoluteChange,
			"relative_change":   state.relativeChange,
		}) {
			state.requestPending = false
		}
		return
	}
}
