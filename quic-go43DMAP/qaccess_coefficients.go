package quic

import (
	"encoding/json"
	"os"
	"path/filepath"
)

// QAccessCoefficients are utility function coefficients (alpha, beta, gamma) for Q-ACCeSS variants.
type QAccessCoefficients struct {
	Alpha  float64 `json:"alpha"`
	Beta   float64 `json:"beta"`
	Gamma  float64 `json:"gamma"`
	Source string  `json:"source,omitempty"`
	Metric string  `json:"metric,omitempty"`
}

const (
	defaultQAccessAlpha = 0.70
	defaultQAccessBeta  = 0.10
	defaultQAccessGamma = 0.10
	// Phase 1 optimize / static qaccess_t default; Phase 2 dynamic runs should set
	// QACCESS_COEFFS_JSON=derived/qaccess_t_runtime_coefficients.json (see reset_qaccess_phase2_runtime.sh).
	defaultCoeffsJSON = "derived/qaccess_t_best_coefficients.json"
)

var qaccessAlphaCandidates = []float64{0.60, 0.70, 0.80, 0.90}
var qaccessBetaCandidates = []float64{0.05, 0.10, 0.20, 0.30}
var qaccessGammaCandidates = []float64{0.05, 0.10, 0.20, 0.30}

func defaultQAccessTCoefficients() QAccessCoefficients {
	return QAccessCoefficients{
		Alpha:  defaultQAccessAlpha,
		Beta:   defaultQAccessBeta,
		Gamma:  defaultQAccessGamma,
		Source: "runtime_default",
		Metric: "predicted_next_bw_bps",
	}
}

// LoadQAccessTCoefficients reads JSON produced by optimize_qaccess_t_coefficients.py.
func LoadQAccessTCoefficients(path string) (QAccessCoefficients, error) {
	c := defaultQAccessTCoefficients()
	if path == "" {
		path = defaultCoeffsJSON
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return c, err
	}
	var parsed QAccessCoefficients
	if err := json.Unmarshal(data, &parsed); err != nil {
		return c, err
	}
	if parsed.Alpha > 0 {
		c.Alpha = parsed.Alpha
	}
	if parsed.Beta >= 0 {
		c.Beta = parsed.Beta
	}
	if parsed.Gamma >= 0 {
		c.Gamma = parsed.Gamma
	}
	if parsed.Source != "" {
		c.Source = parsed.Source
	}
	if parsed.Metric != "" {
		c.Metric = parsed.Metric
	}
	return c, nil
}

// QAccessCandidateAt returns the idx-th (alpha, beta, gamma) in row-major candidate order (64 total).
func QAccessCandidateAt(idx int) (alpha, beta, gamma float64) {
	nBeta := len(qaccessBetaCandidates)
	nGamma := len(qaccessGammaCandidates)
	if idx < 0 {
		idx = 0
	}
	max := len(qaccessAlphaCandidates) * nBeta * nGamma
	if idx >= max {
		idx = idx % max
	}
	ai := idx / (nBeta * nGamma)
	rem := idx % (nBeta * nGamma)
	bi := rem / nGamma
	gi := rem % nGamma
	return qaccessAlphaCandidates[ai], qaccessBetaCandidates[bi], qaccessGammaCandidates[gi]
}

func qaccessCandidateCount() int {
	return len(qaccessAlphaCandidates) * len(qaccessBetaCandidates) * len(qaccessGammaCandidates)
}

func resolveTrainingCSVPath() string {
	if p := os.Getenv("QACCESS_TRAINING_CSV"); p != "" {
		return p
	}
	return filepath.Join("derived", "qaccess_training_samples.csv")
}

func resolveCoeffsJSONPath() string {
	if p := os.Getenv("QACCESS_COEFFS_JSON"); p != "" {
		return p
	}
	return defaultCoeffsJSON
}
