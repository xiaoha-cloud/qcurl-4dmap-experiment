package quic

import (
	"encoding/json"
	"io/ioutil"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/lucas-clemente/quic-go/internal/protocol"
)

func newObjectiveTriggerTestController(mode UtilityMode, triggerMode string) (*UtilityController, time.Time) {
	start := time.Date(2026, 1, 2, 3, 4, 5, 0, time.Local)
	return &UtilityController{
		Mode:  mode,
		RunID: start.Format("20060102_150405"),
		phase2: qaccessPhase2Config{
			triggerMode:            triggerMode,
			gatePolicy:             "objective_aware",
			triggerReferenceStart:  10,
			triggerReferenceEnd:    30,
			triggerActivateSamples: 3,
			triggerRecoverySamples: 5,
			triggerTAbsBps:         500000,
			triggerTRelative:       0.05,
			triggerDAbsMs:          10,
			triggerDRelative:       0.25,
			triggerLRatio:          0.002,
		},
		objectiveTriggerStates: make(map[protocol.PathID]*objectiveTriggerState),
	}, start
}

func enableObjectiveAudit(t *testing.T, uc *UtilityController) string {
	t.Helper()
	dir, err := ioutil.TempDir("", "qaccess-objective-audit-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	path := filepath.Join(dir, "objective_decisions.jsonl")
	uc.phase2.triggerAuditPath = path
	return path
}

func readObjectiveAudit(t *testing.T, path string) []map[string]interface{} {
	t.Helper()
	data, err := ioutil.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var rows []map[string]interface{}
	for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
		var row map[string]interface{}
		if err := json.Unmarshal([]byte(line), &row); err != nil {
			t.Fatalf("invalid decision JSON %q: %v", line, err)
		}
		rows = append(rows, row)
	}
	return rows
}

func observeObjectiveReference(uc *UtilityController, start time.Time, pathID protocol.PathID, value float64) {
	for _, second := range []int{10, 20, 30} {
		pm := PathMetrics{PathID: pathID, Timestamp: start.Add(time.Duration(second) * time.Second)}
		switch uc.phase2.triggerMode {
		case "objective_t":
			pm.BWbps = value
		case "objective_d":
			pm.OWDms = value
		case "objective_l":
			pm.LossRate = value
		}
		uc.observeObjectiveTrigger(pm, pm.Timestamp)
	}
}

func observeObjectiveValue(uc *UtilityController, start time.Time, pathID protocol.PathID, second int, value float64) {
	pm := PathMetrics{PathID: pathID, Timestamp: start.Add(time.Duration(second) * time.Second)}
	switch uc.phase2.triggerMode {
	case "objective_t":
		pm.BWbps = value
	case "objective_d":
		pm.OWDms = value
	case "objective_l":
		pm.LossRate = value
	}
	uc.observeObjectiveTrigger(pm, pm.Timestamp)
}

func TestObjectiveTIncreaseAndDecreaseTrigger(t *testing.T) {
	for _, changed := range []float64{21000000, 19000000} {
		uc, start := newObjectiveTriggerTestController(ModeQAccessT, "objective_t")
		observeObjectiveReference(uc, start, 3, 20000000)
		for second := 31; second <= 33; second++ {
			observeObjectiveValue(uc, start, 3, second, changed)
		}
		if !uc.objectiveTriggerStates[3].triggered {
			t.Fatalf("T change to %.0f did not trigger", changed)
		}
	}
}

func TestObjectiveTSmallChangesDoNotTrigger(t *testing.T) {
	uc, start := newObjectiveTriggerTestController(ModeQAccessT, "objective_t")
	observeObjectiveReference(uc, start, 3, 20000000)
	for second := 31; second <= 40; second++ {
		observeObjectiveValue(uc, start, 3, second, 20400000)
	}
	if uc.objectiveTriggerStates[3].triggered {
		t.Fatal("T change below both thresholds triggered")
	}
}

func TestObjectiveDIncreaseOnly(t *testing.T) {
	uc, start := newObjectiveTriggerTestController(ModeQAccessD, "objective_d")
	observeObjectiveReference(uc, start, 3, 40)
	for second := 31; second <= 33; second++ {
		observeObjectiveValue(uc, start, 3, second, 55)
	}
	if !uc.objectiveTriggerStates[3].triggered {
		t.Fatal("D delay increase did not trigger")
	}

	uc, start = newObjectiveTriggerTestController(ModeQAccessD, "objective_d")
	observeObjectiveReference(uc, start, 3, 40)
	for second := 31; second <= 35; second++ {
		observeObjectiveValue(uc, start, 3, second, 20)
	}
	if uc.objectiveTriggerStates[3].triggered {
		t.Fatal("D delay decrease must not trigger")
	}
}

func TestObjectiveDOneSpikeAndRecovery(t *testing.T) {
	uc, start := newObjectiveTriggerTestController(ModeQAccessD, "objective_d")
	observeObjectiveReference(uc, start, 3, 40)
	observeObjectiveValue(uc, start, 3, 31, 55)
	if uc.objectiveTriggerStates[3].triggered {
		t.Fatal("one D spike triggered")
	}
	for second := 32; second <= 34; second++ {
		observeObjectiveValue(uc, start, 3, second, 55)
	}
	for second := 35; second <= 39; second++ {
		observeObjectiveValue(uc, start, 3, second, 40)
	}
	if uc.objectiveTriggerStates[3].triggered {
		t.Fatal("D recovery did not clear trigger")
	}
}

func TestObjectiveLLossRatioTrigger(t *testing.T) {
	uc, start := newObjectiveTriggerTestController(ModeQAccessL, "objective_l")
	observeObjectiveReference(uc, start, 3, 0.001)
	for second := 31; second <= 33; second++ {
		observeObjectiveValue(uc, start, 3, second, 0.003)
	}
	if !uc.objectiveTriggerStates[3].triggered {
		t.Fatal("L ratio increase of 0.002 did not trigger")
	}
}

func TestObjectiveLOneSpikeRatioAndRecovery(t *testing.T) {
	uc, start := newObjectiveTriggerTestController(ModeQAccessL, "objective_l")
	observeObjectiveReference(uc, start, 3, 0.001)
	observeObjectiveValue(uc, start, 3, 31, 0.003)
	if uc.objectiveTriggerStates[3].triggered {
		t.Fatal("one L spike triggered")
	}
	for second := 32; second <= 34; second++ {
		observeObjectiveValue(uc, start, 3, second, 0.003)
	}
	state := uc.objectiveTriggerStates[3]
	if !state.triggered || state.absoluteChange < 0.001999 || state.absoluteChange > 0.002001 {
		t.Fatalf("L must use 0-1 ratio; state=%+v", state)
	}
	for second := 35; second <= 39; second++ {
		observeObjectiveValue(uc, start, 3, second, 0.001)
	}
	if state.triggered {
		t.Fatal("L recovery did not clear trigger")
	}
}

func TestObjectiveTriggerNeedsThreeSamplesAndRecoveryClears(t *testing.T) {
	uc, start := newObjectiveTriggerTestController(ModeQAccessT, "objective_t")
	observeObjectiveReference(uc, start, 3, 20000000)
	observeObjectiveValue(uc, start, 3, 31, 21000000)
	if uc.objectiveTriggerStates[3].triggered {
		t.Fatal("one spike triggered")
	}
	observeObjectiveValue(uc, start, 3, 32, 20000000)
	for second := 33; second <= 35; second++ {
		observeObjectiveValue(uc, start, 3, second, 21000000)
	}
	if !uc.objectiveTriggerStates[3].triggered {
		t.Fatal("three consecutive samples did not trigger")
	}
	for second := 36; second <= 40; second++ {
		observeObjectiveValue(uc, start, 3, second, 20000000)
	}
	if uc.objectiveTriggerStates[3].triggered {
		t.Fatal("five recovery samples did not clear trigger")
	}
}

func TestObjectiveTriggerStateResetBetweenLegs(t *testing.T) {
	uc, start := newObjectiveTriggerTestController(ModeQAccessT, "objective_t")
	observeObjectiveReference(uc, start, 3, 20000000)
	for second := 31; second <= 33; second++ {
		observeObjectiveValue(uc, start, 3, second, 21000000)
	}
	uc.resetObjectiveTriggerState()
	if len(uc.objectiveTriggerStates) != 0 {
		t.Fatal("objective trigger state was not reset")
	}
}

func TestObjectiveTriggerStateIsIndependentAcrossPaths(t *testing.T) {
	uc, start := newObjectiveTriggerTestController(ModeQAccessT, "objective_t")
	observeObjectiveReference(uc, start, 1, 20000000)
	observeObjectiveReference(uc, start, 3, 20000000)
	for second := 31; second <= 33; second++ {
		observeObjectiveValue(uc, start, 3, second, 21000000)
		observeObjectiveValue(uc, start, 1, second, 20000000)
	}
	if !uc.objectiveTriggerStates[3].triggered || uc.objectiveTriggerStates[1].triggered {
		t.Fatalf("path state leaked: %+v", uc.objectiveTriggerStates)
	}
}

func TestObjectiveTriggerStateIsIndependentAcrossControllersAndRuns(t *testing.T) {
	first, start := newObjectiveTriggerTestController(ModeQAccessT, "objective_t")
	second, secondStart := newObjectiveTriggerTestController(ModeQAccessT, "objective_t")
	second.RunID = secondStart.Add(time.Hour).Format("20060102_150405")
	secondStart = secondStart.Add(time.Hour)
	observeObjectiveReference(first, start, 3, 20000000)
	observeObjectiveReference(second, secondStart, 3, 20000000)
	for secondValue := 31; secondValue <= 33; secondValue++ {
		observeObjectiveValue(first, start, 3, secondValue, 21000000)
	}
	if !first.objectiveTriggerStates[3].triggered || second.objectiveTriggerStates[3].triggered {
		t.Fatal("controller or experiment-run state leaked")
	}
}

func TestObjectiveTriggerCooldownPreventsImmediateRepeat(t *testing.T) {
	uc, requestPath, _ := newTriggerTestController(t, 100, 1)
	uc.phase2.triggerMode = "objective_t"
	uc.phase2.gatePolicy = "objective_aware"
	uc.phase2.gateObjective = "throughput"
	uc.objectiveTriggerStates = map[protocol.PathID]*objectiveTriggerState{
		3: {reference: 20000000, referenceReady: true, current: 21000000, absoluteChange: 1000000, relativeChange: 0.05, triggerStreak: 3, triggered: true, requestPending: true},
	}
	now := time.Unix(100, 0)
	uc.lastTriggerTime = now.Add(-30 * time.Second)
	uc.maybeTriggerObjectiveUpdate(now)
	if _, err := os.Stat(requestPath); !os.IsNotExist(err) {
		t.Fatalf("cooldown should block request, stat err=%v", err)
	}
	rows := readObjectiveAudit(t, uc.phase2.triggerAuditPath)
	if rows[len(rows)-1]["skip_reason"] != "cooldown_active" {
		t.Fatalf("missing cooldown decision: %+v", rows[len(rows)-1])
	}
}

func TestObjectiveTriggerRejectsVariantMismatch(t *testing.T) {
	uc, _ := newObjectiveTriggerTestController(ModeQAccessD, "objective_t")
	if uc.objectiveTriggerConfigurationValid() {
		t.Fatal("objective_t must not be valid for qaccess_d")
	}
}

func TestObjectiveTStableSyntheticDataRemainsInactive(t *testing.T) {
	uc, start := newObjectiveTriggerTestController(ModeQAccessT, "objective_t")
	observeObjectiveReference(uc, start, 3, 20000000)
	for second := 31; second <= 50; second++ {
		observeObjectiveValue(uc, start, 3, second, 20000000)
	}
	if uc.objectiveTriggerStates[3].triggered {
		t.Fatal("stable T data triggered")
	}
}

func TestObjectiveDecisionLogsRequiredFieldsUnitsAndFiniteValues(t *testing.T) {
	uc, start := newObjectiveTriggerTestController(ModeQAccessT, "objective_t")
	path := enableObjectiveAudit(t, uc)
	observeObjectiveValue(uc, start, 3, 5, 0)
	observeObjectiveReference(uc, start, 3, 0)
	observeObjectiveValue(uc, start, 3, 31, 1000000)
	observeObjectiveValue(uc, start, 3, 32, 0)
	for second := 33; second <= 35; second++ {
		observeObjectiveValue(uc, start, 3, second, 1000000)
	}
	rows := readObjectiveAudit(t, path)
	required := []string{
		"variant", "path_id", "trigger_mode", "gate_policy", "gate_objective",
		"reference_value", "current_value", "absolute_change", "relative_change",
		"trigger_streak", "triggered", "current_candidate_score", "best_candidate_score",
		"absolute_improvement", "relative_improvement", "gate_passed", "actual_applied", "skip_reason",
	}
	seen := map[string]bool{}
	for _, row := range rows {
		for _, key := range required {
			if _, ok := row[key]; !ok {
				t.Fatalf("missing %s in %+v", key, row)
			}
		}
		seen[row["skip_reason"].(string)] = true
		if row["trigger_value_unit"] != "bps" || row["candidate_score_unit"] != "bps" {
			t.Fatalf("wrong T units: %+v", row)
		}
		if row["secondary_guardrails"] != notAvailableForPreUpdateEvaluation {
			t.Fatalf("unavailable guardrail misreported: %+v", row)
		}
	}
	encoded, _ := json.Marshal(rows)
	if strings.Contains(string(encoded), "NaN") || strings.Contains(string(encoded), "Inf") {
		t.Fatalf("non-finite value written: %s", encoded)
	}
	for _, reason := range []string{"reference_not_ready", "trigger_streak_incomplete", "objective_trigger_not_satisfied", ""} {
		if !seen[reason] {
			t.Fatalf("missing skip reason %q in %+v", reason, seen)
		}
	}
}

func TestObjectiveDecisionUnitsForDelayAndLoss(t *testing.T) {
	cases := []struct {
		mode          UtilityMode
		triggerMode   string
		triggerUnit   string
		candidateUnit string
	}{
		{ModeQAccessD, "objective_d", "ms", "ms"},
		{ModeQAccessL, "objective_l", "ratio_0_to_1", "loss_risk_bytes"},
	}
	for _, tc := range cases {
		uc, _ := newObjectiveTriggerTestController(tc.mode, tc.triggerMode)
		triggerUnit, candidateUnit := uc.objectiveDecisionUnits()
		if triggerUnit != tc.triggerUnit || candidateUnit != tc.candidateUnit {
			t.Fatalf("%s units=%s/%s", tc.triggerMode, triggerUnit, candidateUnit)
		}
	}
}
