package quic

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"

	"github.com/lucas-clemente/quic-go/internal/protocol"
	"github.com/lucas-clemente/quic-go/internal/utils"
)

// QAccessCoefficients are utility function coefficients (alpha, beta, gamma) for Q-ACCeSS variants.
type QAccessCoefficients struct {
	Alpha  float64 `json:"alpha"`
	Beta   float64 `json:"beta"`
	Gamma  float64 `json:"gamma"`
	Source string  `json:"source,omitempty"`
	Metric string  `json:"metric,omitempty"`
}

// QAccessCoeffsDocument is the version-1 per-subflow runtime coefficient file.
type QAccessCoeffsDocument struct {
	Version int                            `json:"version"`
	Default QAccessCoeffEntry              `json:"default"`
	Paths   map[string]QAccessCoeffEntry `json:"paths"`
	// Legacy flat fields (backward compatibility when version != 1).
	Alpha  float64 `json:"alpha,omitempty"`
	Beta   float64 `json:"beta,omitempty"`
	Gamma  float64 `json:"gamma,omitempty"`
	Source string  `json:"source,omitempty"`
	Metric string  `json:"metric,omitempty"`
}

type QAccessCoeffEntry struct {
	Alpha  float64 `json:"alpha"`
	Beta   float64 `json:"beta"`
	Gamma  float64 `json:"gamma"`
}

const (
	defaultQAccessAlpha = 0.70
	defaultQAccessBeta  = 0.10
	defaultQAccessGamma = 0.10
	qaccessCoeffsVersion = 1
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
		Source: "builtin",
		Metric: "predicted_next_bw_bps",
	}
}

func defaultQAccessCoeffsDocument() QAccessCoeffsDocument {
	return QAccessCoeffsDocument{
		Version: qaccessCoeffsVersion,
		Default: QAccessCoeffEntry{Alpha: 0.6, Beta: 0.3, Gamma: 0.1},
		Paths:   make(map[string]QAccessCoeffEntry),
	}
}

func validCoeffEntry(alpha, beta, gamma float64) bool {
	return finitePositive(alpha, 2.0) && finiteNonNeg(beta, 1.0) && finiteNonNeg(gamma, 1.0)
}

func entryFromMapping(alpha, beta, gamma float64) (QAccessCoeffEntry, bool) {
	if !validCoeffEntry(alpha, beta, gamma) {
		return QAccessCoeffEntry{}, false
	}
	return QAccessCoeffEntry{Alpha: alpha, Beta: beta, Gamma: gamma}, true
}

func normalizeCoeffsDocument(doc *QAccessCoeffsDocument) {
	if doc == nil {
		return
	}
	if doc.Version == qaccessCoeffsVersion {
		if !validCoeffEntry(doc.Default.Alpha, doc.Default.Beta, doc.Default.Gamma) {
			doc.Default = defaultQAccessCoeffsDocument().Default
		}
		if doc.Paths == nil {
			doc.Paths = make(map[string]QAccessCoeffEntry)
		}
		clean := make(map[string]QAccessCoeffEntry, len(doc.Paths))
		for k, v := range doc.Paths {
			if validCoeffEntry(v.Alpha, v.Beta, v.Gamma) {
				clean[k] = v
			}
		}
		doc.Paths = clean
		return
	}
	if e, ok := entryFromMapping(doc.Alpha, doc.Beta, doc.Gamma); ok {
		doc.Version = qaccessCoeffsVersion
		doc.Default = e
		doc.Paths = make(map[string]QAccessCoeffEntry)
	}
}

// LoadQAccessCoeffsDocument reads version-1 or legacy flat JSON.
func LoadQAccessCoeffsDocument(path string) (QAccessCoeffsDocument, error) {
	doc := defaultQAccessCoeffsDocument()
	if path == "" {
		path = defaultCoeffsJSON
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return doc, err
	}
	if err := json.Unmarshal(data, &doc); err != nil {
		return doc, err
	}
	normalizeCoeffsDocument(&doc)
	return doc, nil
}

// ResolveCoefficientsForPath resolves coefficients for a subflow:
// paths[str(path_id)] → default → legacy flat → builtin defaults.
func ResolveCoefficientsForPath(doc QAccessCoeffsDocument, pathID protocol.PathID) QAccessCoefficients {
	normalizeCoeffsDocument(&doc)
	key := strconv.FormatUint(uint64(pathID), 10)
	metric := doc.Metric
	if metric == "" {
		metric = "unknown"
	}
	if e, ok := doc.Paths[key]; ok && validCoeffEntry(e.Alpha, e.Beta, e.Gamma) {
		return QAccessCoefficients{Alpha: e.Alpha, Beta: e.Beta, Gamma: e.Gamma, Source: "per_path", Metric: metric}
	}
	if validCoeffEntry(doc.Default.Alpha, doc.Default.Beta, doc.Default.Gamma) {
		return QAccessCoefficients{Alpha: doc.Default.Alpha, Beta: doc.Default.Beta, Gamma: doc.Default.Gamma, Source: "default", Metric: metric}
	}
	if doc.Version != qaccessCoeffsVersion {
		if e, ok := entryFromMapping(doc.Alpha, doc.Beta, doc.Gamma); ok {
			return QAccessCoefficients{Alpha: e.Alpha, Beta: e.Beta, Gamma: e.Gamma, Source: "legacy", Metric: doc.Metric}
		}
	}
	c := defaultQAccessTCoefficients()
	return c
}

// LoadQAccessTCoefficients reads JSON and returns legacy global coefficients (default entry).
func LoadQAccessTCoefficients(path string) (QAccessCoefficients, error) {
	doc, err := LoadQAccessCoeffsDocument(path)
	if err != nil {
		return defaultQAccessTCoefficients(), err
	}
	c := ResolveCoefficientsForPath(doc, protocol.InitialPathID)
	if doc.Metric != "" {
		c.Metric = doc.Metric
	}
	if doc.Source != "" && c.Source == "default" {
		c.Source = doc.Source
	}
	return c, nil
}

func (uc *UtilityController) getCoefficientsForPath(pathID protocol.PathID) QAccessCoefficients {
	uc.coeffsMu.RLock()
	defer uc.coeffsMu.RUnlock()
	if uc.perPathCoeffs != nil {
		if c, ok := uc.perPathCoeffs[pathID]; ok {
			return c
		}
	}
	if uc.coeffsDoc.Version == qaccessCoeffsVersion {
		return ResolveCoefficientsForPath(uc.coeffsDoc, pathID)
	}
	return uc.coeffs
}

func (uc *UtilityController) reloadCoefficientsFromDisk() {
	if uc.phase2.coeffJSONPath == "" {
		return
	}
	doc, err := LoadQAccessCoeffsDocument(uc.phase2.coeffJSONPath)
	if err != nil {
		uc.coeffsMu.Lock()
		uc.coeffs = defaultQAccessTCoefficients()
		uc.perPathCoeffs = nil
		uc.coeffsMu.Unlock()
		return
	}
	perPath := make(map[protocol.PathID]QAccessCoefficients)
	for key := range doc.Paths {
		pid64, err := strconv.ParseUint(key, 10, 8)
		if err != nil {
			continue
		}
		pid := protocol.PathID(pid64)
		c := ResolveCoefficientsForPath(doc, pid)
		if doc.Metric != "" {
			c.Metric = doc.Metric
		}
		perPath[pid] = c
		utils.Infof(
			"[qaccess_t] path coeffs path_id=%d alpha=%.4f beta=%.4f gamma=%.4f source=%s",
			pid, c.Alpha, c.Beta, c.Gamma, c.Source,
		)
	}
	fallback := ResolveCoefficientsForPath(doc, protocol.InitialPathID)
	if doc.Metric != "" {
		fallback.Metric = doc.Metric
	}
	if doc.Source != "" && fallback.Source == "default" {
		fallback.Source = doc.Source
	}
	uc.coeffsMu.Lock()
	uc.coeffsDoc = doc
	uc.perPathCoeffs = perPath
	uc.coeffs = fallback
	uc.coeffsMu.Unlock()
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

// formatPathID returns the string key used in coefficient JSON paths map.
func formatPathID(pathID protocol.PathID) string {
	return fmt.Sprintf("%d", pathID)
}
