package quic

import (
	"math"
	"sync"
	"time"

	"github.com/lucas-clemente/quic-go/internal/protocol"
	"github.com/lucas-clemente/quic-go/internal/utils"
)

// UtilityMode selects Q-ACCeSS / baseline behavior (see interface.go Config.UtilityMode).
type UtilityMode string

const (
	// ModeBaseline: controller disabled in scheduler (original 4D-MAP without utility control).
	ModeBaseline UtilityMode = "baseline"

	// ModeQAccessT: Q-ACCeSS-T — RFR-optimized (alpha,beta,gamma) at runtime (JSON from Python).
	ModeQAccessT UtilityMode = "qaccess_t"

	// ModeQAccessCollect: generic Q-ACCeSS data collection (probes alpha/beta/gamma, exports CSV).
	ModeQAccessCollect UtilityMode = "qaccess_collect"
)

// Experiment-aligned normalization (Fig.7-style, up to 30 Mbps steps).
const (
	bwRefBps          = 30 * 1000 * 1000
	delayRefMs        = 100.0
	delayTrendRefMs   = 50.0
	lossRef           = 0.01
	inflightActiveMin = 1024
)

// PathMetrics is the per-path snapshot passed into UtilityController.Compute.
type PathMetrics struct {
	PathID            protocol.PathID
	BWbps             float64
	OWDms             float64
	DelayGradientMs   float64
	LossRate          float64
	LostBytesDelta    int64
	RetransBytesDelta int64
	CwndBytes         int64
	InflightBytes     int64
	CwndRoom          float64
	Alpha             float64
	Beta              float64
	Gamma             float64
	Gain              float64
	Backoff           float64
	Utility           float64
	Timestamp         time.Time
}

type PathState struct {
	LastOWDms     float64
	LastBWbps     float64
	LastLoss      float64
	LastLostBytes int64 // cumulative lost bytes (for delta in training CSV)
	LastTime      time.Time
}

// ControlSignal is applied via sentPacketHandler.SetUtilityControl (OLIA secondary paths only).
type ControlSignal struct {
	PathID      protocol.PathID
	Mode        UtilityMode
	Utility     float64
	Gain        float64
	Backoff     float64
	NormG       float64
	NormD       float64
	NormL       float64
	GTotal      float64
	Alpha       float64
	Beta        float64
	Gamma       float64
	DelayTrend  float64
	Active      bool
}

// UtilityController implements Q-ACCeSS-T utility → gain/backoff for OLIA secondary paths.
type UtilityController struct {
	Mode   UtilityMode
	RunID  string
	Prev   map[protocol.PathID]*PathState
	MinGain, MaxGain       float64
	MinBackoff, MaxBackoff float64

	// Per monitor-cycle aggregate (set before per-path Compute).
	roundGTotal float64

	// Q-ACCeSS-T runtime coefficients (protected by coeffsMu for Phase 2 reload).
	coeffsMu sync.RWMutex
	coeffs   QAccessCoefficients

	// Phase 2 (qaccess_t only; disabled when env flags are 0).
	phase2               qaccessPhase2Config
	lastCoeffCheck       time.Time
	lastCoeffMtime       time.Time
	lastTriggerTime      time.Time
	roundBwHistory       []float64
	currentRoundTotalBwBps float64
	currentRoundActivePaths int
	lastRoundActivePaths    int

	// qaccess_collect
	collectIdx     int
	trainCollector *qaccessTrainCollector

	// qaccess_t runtime sample export (separate CSV from collect).
	runtimeExporter *qaccessSampleExporter
}

func NewUtilityController(mode UtilityMode, runID string) *UtilityController {
	uc := &UtilityController{
		Mode:   mode,
		RunID:  runID,
		Prev:   make(map[protocol.PathID]*PathState),
		MinGain:    0.80,
		MaxGain:    1.20,
		MinBackoff: 0.90,
		MaxBackoff: 1.10,
		coeffs: defaultQAccessTCoefficients(),
	}
	switch mode {
	case ModeQAccessT:
		uc.phase2 = loadQAccessPhase2Config()
		jsonPath := uc.phase2.coeffJSONPath
		if c, err := LoadQAccessTCoefficients(jsonPath); err == nil {
			uc.coeffs = c
			utils.Infof("[qaccess_t] loaded coefficients alpha=%.2f beta=%.2f gamma=%.2f source=%s",
				c.Alpha, c.Beta, c.Gamma, c.Source)
		} else {
			utils.Infof("[qaccess_t] loaded coefficients alpha=%.2f beta=%.2f gamma=%.2f source=%s (json=%s err=%v)",
				uc.coeffs.Alpha, uc.coeffs.Beta, uc.coeffs.Gamma, uc.coeffs.Source, jsonPath, err)
		}
		if uc.phase2.runtimeExport {
			uc.runtimeExporter = newQAccessSampleExporter(
				uc.phase2.runtimeSamples, runID, uc.phase2.runtimeBufferMax,
			)
			if err := uc.runtimeExporter.ensureOpen(); err != nil {
				utils.Infof("[qaccess_t] runtime sample export open failed path=%s err=%v",
					uc.phase2.runtimeSamples, err)
			}
		}
	case ModeQAccessCollect:
		uc.trainCollector = newQAccessTrainCollector(runID)
		if err := uc.trainCollector.ensureOpen(); err != nil {
			utils.Infof("[qaccess_collect] failed to open training csv %s: %v", resolveTrainingCSVPath(), err)
		} else {
			utils.Infof("[qaccess_collect] training csv=%s", resolveTrainingCSVPath())
		}
	}
	return uc
}

func (uc *UtilityController) SetMode(mode UtilityMode) { uc.Mode = mode }

func (uc *UtilityController) Coefficients() QAccessCoefficients { return uc.getCoefficients() }

// BeginMonitorRound resets per-tick state (call once per scheduler monitor cycle).
func (uc *UtilityController) BeginMonitorRound() {
	now := time.Now()
	if uc.Mode == ModeQAccessT {
		uc.finalizeMonitorRoundThroughput()
		uc.maybeReloadCoefficients(now)
		uc.maybeTriggerCoefficientUpdate(now)
	}
	if uc.Mode == ModeQAccessCollect && uc.trainCollector != nil {
		_ = uc.trainCollector.flushAllPending(func(pid protocol.PathID) float64 {
			if prev, ok := uc.Prev[pid]; ok {
				return prev.LastBWbps
			}
			return 0
		})
		uc.collectIdx++
	}
	if uc.Mode == ModeQAccessT && uc.runtimeExporter != nil {
		_ = uc.runtimeExporter.flushAllPending(func(pid protocol.PathID) float64 {
			if prev, ok := uc.Prev[pid]; ok {
				return prev.LastBWbps
			}
			return 0
		})
	}
	uc.roundGTotal = 0
}

// SetRoundGTotal sets normalized aggregate throughput across active paths for this tick.
func (uc *UtilityController) SetRoundGTotal(gTotal float64) {
	uc.roundGTotal = clamp(sanitizeMetric(gTotal), 0, 1)
}

func PathMetricsActive(pm PathMetrics) bool {
	if sanitizeMetric(pm.BWbps) > 0 || sanitizeMetric(pm.OWDms) > 0 {
		return true
	}
	if pm.InflightBytes > inflightActiveMin {
		return true
	}
	return false
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

func sanitizeMetric(v float64) float64 {
	if math.IsNaN(v) || math.IsInf(v, 0) || v < 0 {
		return 0
	}
	return v
}

func (uc *UtilityController) normalizeG(bwBps float64) float64 {
	return clamp(sanitizeMetric(bwBps)/bwRefBps, 0, 1)
}

func (uc *UtilityController) normalizeL(loss float64) float64 {
	return clamp(sanitizeMetric(loss)/lossRef, 0, 1)
}

func (uc *UtilityController) normalizeD(owdMs, delayTrendMs float64) float64 {
	delayLevel := clamp(sanitizeMetric(owdMs)/delayRefMs, 0, 1)
	trend := 0.0
	if delayTrendMs > 0 {
		trend = clamp(sanitizeMetric(delayTrendMs)/delayTrendRefMs, 0, 1)
	}
	return clamp(0.7*delayLevel+0.3*trend, 0, 1)
}

// qaccessUtility: U = GTotal^alpha - beta*GTotal*L - gamma*GTotal*D (ACCeSS-style structure).
func qaccessUtility(gTotal, normD, normL, alpha, beta, gamma float64) float64 {
	if gTotal <= 0 {
		gTotal = 1e-9
	}
	reward := math.Pow(gTotal, alpha)
	penalty := beta*gTotal*normL + gamma*gTotal*normD
	return reward - penalty
}

func (uc *UtilityController) qaccessGainBackoff(gTotal, normD, normL, alpha, beta, gamma float64) (float64, float64) {
	u := qaccessUtility(gTotal, normD, normL, alpha, beta, gamma)
	gain := 1.0 + 0.20*math.Pow(gTotal, alpha) - 0.10*beta*5*normL - 0.05*gamma*5*normD
	backoff := 1.0 - 0.08*math.Pow(gTotal, alpha) + 0.05*beta*5*normL + 0.03*gamma*5*normD
	_ = u
	gain = clamp(gain, uc.MinGain, uc.MaxGain)
	backoff = clamp(backoff, uc.MinBackoff, uc.MaxBackoff)
	return gain, backoff
}

func (uc *UtilityController) collectCoefficients() (alpha, beta, gamma float64) {
	idx := uc.collectIdx % qaccessCandidateCount()
	return QAccessCandidateAt(idx)
}

func (uc *UtilityController) Compute(pm PathMetrics) ControlSignal {
	now := pm.Timestamp
	if now.IsZero() {
		now = time.Now()
	}
	pm.BWbps = sanitizeMetric(pm.BWbps)
	pm.LossRate = sanitizeMetric(pm.LossRate)
	pm.OWDms = sanitizeMetric(pm.OWDms)

	active := PathMetricsActive(pm)

	if uc.Mode == ModeQAccessT {
		uc.noteActivePathThroughput(pm)
	}

	prev, ok := uc.Prev[pm.PathID]
	if !ok {
		prev = &PathState{LastOWDms: pm.OWDms, LastBWbps: pm.BWbps, LastLoss: pm.LossRate, LastTime: now}
		uc.Prev[pm.PathID] = prev
	}
	if pm.DelayGradientMs == 0 {
		pm.DelayGradientMs = pm.OWDms - prev.LastOWDms
	}

	normG := uc.normalizeG(pm.BWbps)
	normL := uc.normalizeL(pm.LossRate)
	normD := uc.normalizeD(pm.OWDms, pm.DelayGradientMs)
	gTotal := uc.roundGTotal
	if gTotal <= 0 && active {
		gTotal = normG
	}

	coeffs := uc.getCoefficients()
	alpha, beta, gamma := coeffs.Alpha, coeffs.Beta, coeffs.Gamma
	gain, backoff := 1.0, 1.0
	var u float64

	switch uc.Mode {
	case ModeQAccessT, ModeQAccessCollect:
		if uc.Mode == ModeQAccessCollect {
			alpha, beta, gamma = uc.collectCoefficients()
		}
		if active {
			u = qaccessUtility(gTotal, normD, normL, alpha, beta, gamma)
			gain, backoff = uc.qaccessGainBackoff(gTotal, normD, normL, alpha, beta, gamma)
		}
	}

	sig := ControlSignal{
		PathID:     pm.PathID,
		Mode:       uc.Mode,
		Utility:    math.Round(u*10000) / 10000,
		Gain:       math.Round(gain*10000) / 10000,
		Backoff:    math.Round(backoff*10000) / 10000,
		NormG:      math.Round(normG*10000) / 10000,
		NormD:      math.Round(normD*10000) / 10000,
		NormL:      math.Round(normL*10000) / 10000,
		GTotal:     math.Round(gTotal*10000) / 10000,
		Alpha:      alpha,
		Beta:       beta,
		Gamma:      gamma,
		DelayTrend: math.Round(pm.DelayGradientMs*10000) / 10000,
		Active:     active,
	}

	if uc.Mode == ModeQAccessCollect && active && uc.trainCollector != nil {
		pm.Timestamp = now
		pm.Alpha, pm.Beta, pm.Gamma = alpha, beta, gamma
		row := buildTrainRow(uc.RunID, pm, sig, alpha, beta, gamma)
		if err := uc.trainCollector.recordPending(row, pm.PathID, pm.BWbps); err != nil {
			utils.Infof("[qaccess_collect] csv write error path=%v: %v", pm.PathID, err)
		}
	}

	if uc.Mode == ModeQAccessT && active && uc.runtimeExporter != nil {
		pm.Timestamp = now
		pm.Alpha, pm.Beta, pm.Gamma = alpha, beta, gamma
		row := buildTrainRow(uc.RunID, pm, sig, alpha, beta, gamma)
		if err := uc.runtimeExporter.recordPending(row, pm.PathID, pm.BWbps); err != nil {
			utils.Infof("[qaccess_t] runtime sample export error path=%v: %v", pm.PathID, err)
		}
	}

	prev.LastOWDms = pm.OWDms
	prev.LastBWbps = pm.BWbps
	prev.LastLoss = pm.LossRate
	prev.LastTime = now

	return sig
}

// ComputeRoundGTotal returns clamped sum of per-path normalized throughput / bwRef over active paths.
func ComputeRoundGTotal(paths []PathMetrics) float64 {
	var sum float64
	for _, pm := range paths {
		if !PathMetricsActive(pm) {
			continue
		}
		sum += sanitizeMetric(pm.BWbps) / bwRefBps
	}
	return clamp(sum, 0, 1)
}
