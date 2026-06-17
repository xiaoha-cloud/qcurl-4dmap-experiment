package quic

import (
	"math"
	"os"
	"strings"

	"github.com/lucas-clemente/quic-go/internal/protocol"
	"github.com/lucas-clemente/quic-go/internal/utils"
)

// ControlLawMode selects utility → gain/retention mapping (QACCESS_CONTROL_LAW).
type ControlLawMode string

const (
	ControlLawLegacy  ControlLawMode = "legacy"
	ControlLawSafeTV1 ControlLawMode = "safe_t_v1"

	legacyGainMin    = 0.80
	legacyGainMax    = 1.20
	legacyRetMin     = 0.90
	legacyRetMax     = 1.10
	safeTV1GainMin   = 0.95
	safeTV1GainMax   = 1.10
	safeTV1MaxGainStep = 0.02
	safeTV1MaxDelayPen = 0.02
)

// ControlLawDiagnostics breaks down gain/retention mapping for runtime CSV export.
type ControlLawDiagnostics struct {
	ThroughputRewardTerm float64
	LossPenaltyTerm      float64
	DelayPenaltyTerm     float64
	GainRaw              float64
	GainClamped          float64
	GainHitMin           bool
	GainHitMax           bool
	RetentionRaw         float64
	RetentionClamped     float64
	RetentionHitMin      bool
	RetentionHitMax      bool
	ControlLaw           string
	DelayPenaltyBounded  float64
	PrevGain             float64
	StepLimitedGain      float64
}

type controlLawTermsInput struct {
	GTotal float64
	NormD  float64
	NormL  float64
	Alpha  float64
	Beta   float64
	Gamma  float64
}

func resolveControlLawMode() ControlLawMode {
	v := strings.TrimSpace(strings.ToLower(os.Getenv("QACCESS_CONTROL_LAW")))
	switch v {
	case string(ControlLawSafeTV1):
		return ControlLawSafeTV1
	default:
		return ControlLawLegacy
	}
}

func gTotalPowAlpha(gTotal, alpha float64) float64 {
	if gTotal <= 0 {
		return 0
	}
	return math.Pow(gTotal, alpha)
}

// computeGainMappingTerms returns throughput reward and penalty contributions to raw gain.
func computeGainMappingTerms(in controlLawTermsInput) (throughputReward, lossPenalty, delayPenalty, gainRaw float64) {
	gPow := gTotalPowAlpha(in.GTotal, in.Alpha)
	throughputReward = 0.20 * gPow
	lossPenalty = -0.10 * in.Beta * 5.0 * in.NormL
	delayPenalty = -0.05 * in.Gamma * 5.0 * in.NormD
	gainRaw = 1.0 + throughputReward + lossPenalty + delayPenalty
	return
}

// computeRetentionMappingTerms returns terms for raw retention (utilityBackoff field).
func computeRetentionMappingTerms(in controlLawTermsInput) (throughputTerm, lossTerm, delayTerm, retentionRaw float64) {
	gPow := gTotalPowAlpha(in.GTotal, in.Alpha)
	throughputTerm = -0.08 * gPow
	lossTerm = 0.05 * in.Beta * 5.0 * in.NormL
	delayTerm = 0.03 * in.Gamma * 5.0 * in.NormD
	retentionRaw = 1.0 + throughputTerm + lossTerm + delayTerm
	return
}

func clampGainRetention(gainRaw, retRaw float64, gainMin, gainMax, retMin, retMax float64) (gain, retention float64, gHitMin, gHitMax, rHitMin, rHitMax bool) {
	gain = clamp(gainRaw, gainMin, gainMax)
	retention = clamp(retRaw, retMin, retMax)
	gHitMin = gain <= gainMin+1e-12
	gHitMax = gain >= gainMax-1e-12
	rHitMin = retention <= retMin+1e-12
	rHitMax = retention >= retMax-1e-12
	return
}

func (uc *UtilityController) computeLegacyGainBackoffWithDiagnostics(in controlLawTermsInput) (gain, retention float64, diag ControlLawDiagnostics) {
	tp, lossPen, delayPen, gainRaw := computeGainMappingTerms(in)
	_, _, _, retRaw := computeRetentionMappingTerms(in)

	gainMin, gainMax := uc.MinGain, uc.MaxGain
	retMin, retMax := uc.MinBackoff, uc.MaxBackoff
	gain, retention, gMin, gMax, rMin, rMax := clampGainRetention(gainRaw, retRaw, gainMin, gainMax, retMin, retMax)

	diag = ControlLawDiagnostics{
		ThroughputRewardTerm: tp,
		LossPenaltyTerm:      lossPen,
		DelayPenaltyTerm:     delayPen,
		GainRaw:              gainRaw,
		GainClamped:          gain,
		GainHitMin:           gMin,
		GainHitMax:           gMax,
		RetentionRaw:         retRaw,
		RetentionClamped:       retention,
		RetentionHitMin:        rMin,
		RetentionHitMax:        rMax,
		ControlLaw:           string(ControlLawLegacy),
		DelayPenaltyBounded:  delayPen,
		StepLimitedGain:      gain,
	}
	return gain, retention, diag
}

func boundDelayPenaltyForGain(delayPenalty float64) float64 {
	// delayPenalty is non-positive; cap how much delay can reduce gain.
	if delayPenalty < -safeTV1MaxDelayPen {
		return -safeTV1MaxDelayPen
	}
	return delayPenalty
}

func stepLimitGain(prev, target float64) float64 {
	delta := target - prev
	delta = clamp(delta, -safeTV1MaxGainStep, safeTV1MaxGainStep)
	return prev + delta
}

func (uc *UtilityController) computeSafeTV1GainBackoff(pathID protocol.PathID, in controlLawTermsInput) (gain, retention float64, diag ControlLawDiagnostics) {
	tp, lossPen, delayPen, gainRawLegacy := computeGainMappingTerms(in)
	_ = gainRawLegacy
	delayBounded := boundDelayPenaltyForGain(delayPen)
	gainRaw := 1.0 + tp + lossPen + delayBounded

	gainClamped := clamp(gainRaw, safeTV1GainMin, safeTV1GainMax)
	prev := uc.prevAppliedGain[pathID]
	if prev == 0 {
		prev = 1.0
	}
	applied := stepLimitGain(prev, gainClamped)
	uc.prevAppliedGain[pathID] = applied

	// Retention/backoff: legacy formula unchanged in safe_t_v1.
	_, _, _, retRaw := computeRetentionMappingTerms(in)
	_, retention, _, _, rMin, rMax := clampGainRetention(0, retRaw, legacyGainMin, legacyGainMax, legacyRetMin, legacyRetMax)

	gain = applied

	diag = ControlLawDiagnostics{
		ThroughputRewardTerm: tp,
		LossPenaltyTerm:      lossPen,
		DelayPenaltyTerm:     delayPen,
		GainRaw:              gainRaw,
		GainClamped:          gainClamped,
		GainHitMin:           gainClamped <= safeTV1GainMin+1e-12,
		GainHitMax:           gainClamped >= safeTV1GainMax-1e-12,
		RetentionRaw:         retRaw,
		RetentionClamped:     retention,
		RetentionHitMin:      rMin,
		RetentionHitMax:      rMax,
		ControlLaw:           string(ControlLawSafeTV1),
		DelayPenaltyBounded:  delayBounded,
		PrevGain:             prev,
		StepLimitedGain:      applied,
	}

	utils.Infof("[control_law] path=%v law=%s gain_raw=%.4f delay_pen_bounded=%.4f gain_clamped=%.4f prev_gain=%.4f gain_applied=%.4f retention=%.4f",
		pathID, diag.ControlLaw, diag.GainRaw, diag.DelayPenaltyBounded, diag.GainClamped, diag.PrevGain, diag.StepLimitedGain, retention)

	return gain, retention, diag
}

// BoundDelayPenaltyForGain caps how much delay may reduce ACK gain (safe_t_v1).
func BoundDelayPenaltyForGain(delayPenalty float64) float64 {
	return boundDelayPenaltyForGain(delayPenalty)
}

// StepLimitGain applies per-round gain delta cap (safe_t_v1).
func StepLimitGain(prev, target float64) float64 {
	return stepLimitGain(prev, target)
}

// LegacyGainBackoffDiagnostics computes legacy mapping terms for tests/replay alignment.
func LegacyGainBackoffDiagnostics(gTotal, normD, normL, alpha, beta, gamma float64) (gain, retention float64, diag ControlLawDiagnostics) {
	uc := &UtilityController{
		MinGain:    legacyGainMin,
		MaxGain:    legacyGainMax,
		MinBackoff: legacyRetMin,
		MaxBackoff: legacyRetMax,
	}
	return uc.computeLegacyGainBackoffWithDiagnostics(controlLawTermsInput{
		GTotal: gTotal, NormD: normD, NormL: normL, Alpha: alpha, Beta: beta, Gamma: gamma,
	})
}

// ApplySafeTV1GainForTest runs one safe_t_v1 gain step with explicit previous gain state.
func ApplySafeTV1GainForTest(pathID protocol.PathID, prevGain float64, in controlLawTermsInput) (gain, retention float64, diag ControlLawDiagnostics) {
	uc := &UtilityController{
		MinGain:         legacyGainMin,
		MaxGain:         legacyGainMax,
		MinBackoff:      legacyRetMin,
		MaxBackoff:      legacyRetMax,
		controlLawMode:  ControlLawSafeTV1,
		prevAppliedGain: map[protocol.PathID]float64{pathID: prevGain},
	}
	return uc.computeSafeTV1GainBackoff(pathID, in)
}

// ControlLawTermsInput is exported for tests.
type ControlLawTermsInput = controlLawTermsInput

// ComputeGainBackoffForTest exposes legacy mapping for unit tests (legacy mode only).
func ComputeGainBackoffForTest(gTotal, normD, normL, alpha, beta, gamma float64) (gain, backoff float64) {
	gain, backoff, _ = LegacyGainBackoffDiagnostics(gTotal, normD, normL, alpha, beta, gamma)
	return
}
