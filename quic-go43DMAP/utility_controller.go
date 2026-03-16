package quic

import (
	"math"
	"time"

	"github.com/lucas-clemente/quic-go/internal/protocol"
)

type UtilityMode string

const (
	ModeT UtilityMode = "T" // throughput-first
	ModeD UtilityMode = "D" // delay-first
	ModeL UtilityMode = "L" // loss-first
)

type PathMetrics struct {
	PathID    protocol.PathID
	BWbps     float64   // bandwidth estimate in bps
	LossRate  float64   // cumulative / current loss rate
	OWDms     float64   // one-way delay approximation in ms
	CwndRoom  float64   // cwnd - inflight
	Timestamp time.Time
}

type PathState struct {
	LastOWDms float64
	LastBWbps float64
	LastLoss  float64
	LastTime  time.Time
}

type ControlSignal struct {
	PathID     protocol.PathID
	Mode       UtilityMode
	Utility    float64
	Gain       float64
	Backoff    float64
	NormG      float64
	NormD      float64
	NormL      float64
	DelayTrend float64
}

type UtilityWeights struct {
	WT float64
	WD float64
	WL float64
}

type UtilityController struct {
	Mode UtilityMode

	// Previous state for trend calculation
	Prev map[protocol.PathID]*PathState

	// Normalization caps to keep first version stable
	MaxBWbps    float64
	MaxOWDms    float64
	MaxLossRate float64

	// Optional bounds for control output
	MinGain    float64
	MaxGain    float64
	MinBackoff float64
	MaxBackoff float64
}

func NewUtilityController(mode UtilityMode) *UtilityController {
	return &UtilityController{
		Mode: mode,
		Prev: make(map[protocol.PathID]*PathState),

		// first version: fixed normalization ranges
		MaxBWbps:    100 * 1000 * 1000, // 100 Mbps
		MaxOWDms:    500.0,             // 500 ms
		MaxLossRate: 0.20,              // 20%

		MinGain:    0.80,
		MaxGain:    1.20,
		MinBackoff: 0.90,
		MaxBackoff: 1.10,
	}
}

func (uc *UtilityController) SetMode(mode UtilityMode) {
	uc.Mode = mode
}

func (uc *UtilityController) GetWeights() UtilityWeights {
	switch uc.Mode {
	case ModeT:
		return UtilityWeights{WT: 0.60, WD: 0.20, WL: 0.20}
	case ModeD:
		return UtilityWeights{WT: 0.20, WD: 0.60, WL: 0.20}
	case ModeL:
		return UtilityWeights{WT: 0.20, WD: 0.20, WL: 0.60}
	default:
		return UtilityWeights{WT: 0.34, WD: 0.33, WL: 0.33}
	}
}

func clamp(x, lo, hi float64) float64 {
	if x < lo {
		return lo
	}
	if x > hi {
		return hi
	}
	return x
}

func safeDiv(a, b float64) float64 {
	if b == 0 {
		return 0
	}
	return a / b
}

func (uc *UtilityController) normalizeG(bwBps float64) float64 {
	return clamp(safeDiv(bwBps, uc.MaxBWbps), 0.0, 1.0)
}

func (uc *UtilityController) normalizeL(loss float64) float64 {
	return clamp(safeDiv(loss, uc.MaxLossRate), 0.0, 1.0)
}

func (uc *UtilityController) normalizeD(owdMs float64, delayTrendMs float64) float64 {
	// first version:
	// combine current delay level and delay trend lightly
	delayLevel := clamp(safeDiv(owdMs, uc.MaxOWDms), 0.0, 1.0)

	// only positive trend is penalized
	trendPenalty := 0.0
	if delayTrendMs > 0 {
		trendPenalty = clamp(delayTrendMs/100.0, 0.0, 1.0)
	}

	// weighted mix: mostly delay level, some trend
	return clamp(0.7*delayLevel+0.3*trendPenalty, 0.0, 1.0)
}

func (uc *UtilityController) Compute(pm PathMetrics) ControlSignal {
	now := pm.Timestamp
	if now.IsZero() {
		now = time.Now()
	}

	prev, ok := uc.Prev[pm.PathID]
	if !ok {
		prev = &PathState{
			LastOWDms: pm.OWDms,
			LastBWbps: pm.BWbps,
			LastLoss:  pm.LossRate,
			LastTime:  now,
		}
		uc.Prev[pm.PathID] = prev
	}

	delayTrend := pm.OWDms - prev.LastOWDms

	normG := uc.normalizeG(pm.BWbps)
	normL := uc.normalizeL(pm.LossRate)
	normD := uc.normalizeD(pm.OWDms, delayTrend)

	w := uc.GetWeights()

	// U = wT * G - wD * D - wL * L
	u := w.WT*normG - w.WD*normD - w.WL*normL

	// First version: simple rule-based control mapping
	gain := 1.0
	backoff := 1.0

	switch uc.Mode {
	case ModeT:
		// be more aggressive if bandwidth looks good and loss is not high
		gain = 1.0 + 0.20*normG - 0.10*normL - 0.05*normD
		backoff = 1.0 - 0.08*normG + 0.05*normL + 0.03*normD

	case ModeD:
		// reduce aggressiveness if delay is building up
		gain = 1.0 - 0.20*normD - 0.05*normL + 0.05*normG
		backoff = 1.0 + 0.08*normD + 0.03*normL

	case ModeL:
		// become conservative when loss is high
		gain = 1.0 - 0.20*normL - 0.08*normD + 0.03*normG
		backoff = 1.0 + 0.10*normL + 0.03*normD
	}

	gain = clamp(gain, uc.MinGain, uc.MaxGain)
	backoff = clamp(backoff, uc.MinBackoff, uc.MaxBackoff)

	prev.LastOWDms = pm.OWDms
	prev.LastBWbps = pm.BWbps
	prev.LastLoss = pm.LossRate
	prev.LastTime = now

	return ControlSignal{
		PathID:     pm.PathID,
		Mode:       uc.Mode,
		Utility:    math.Round(u*10000) / 10000,
		Gain:       math.Round(gain*10000) / 10000,
		Backoff:    math.Round(backoff*10000) / 10000,
		NormG:      math.Round(normG*10000) / 10000,
		NormD:      math.Round(normD*10000) / 10000,
		NormL:      math.Round(normL*10000) / 10000,
		DelayTrend: math.Round(delayTrend*10000) / 10000,
	}
}
