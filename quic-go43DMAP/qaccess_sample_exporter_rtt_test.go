package quic

import (
	"testing"
	"time"

	"github.com/lucas-clemente/quic-go/internal/protocol"
)

func TestBuildTrainRowIncludesSenderRTTMetrics(t *testing.T) {
	row := buildTrainRow("run", PathMetrics{
		PathID:        protocol.PathID(3),
		Timestamp:     time.Unix(1, 0),
		RTTLatestMs:   81.25,
		RTTSmoothedMs: 79.5,
		RTTMinMs:      40.125,
	}, ControlSignal{}, 0.6, 0.3, 0.1, ControlLawDiagnostics{})

	want := map[string]string{
		"rtt_latest_ms":   "81.2500",
		"rtt_smoothed_ms": "79.5000",
		"rtt_min_ms":      "40.1250",
	}
	for field, expected := range want {
		if got := row[field]; got != expected {
			t.Fatalf("%s=%q, want %q", field, got, expected)
		}
	}
}

func TestRuntimeSampleIntervalIsOptInAndPerPath(t *testing.T) {
	uc := &UtilityController{
		phase2:            qaccessPhase2Config{runtimeSampleInterval: 100 * time.Millisecond},
		lastRuntimeSample: make(map[protocol.PathID]time.Time),
	}
	start := time.Unix(100, 0)
	if !uc.shouldExportRuntimeSample(1, start) {
		t.Fatal("first Path 1 sample should be exported")
	}
	if uc.shouldExportRuntimeSample(1, start.Add(99*time.Millisecond)) {
		t.Fatal("Path 1 sample inside interval should be suppressed")
	}
	if !uc.shouldExportRuntimeSample(3, start.Add(50*time.Millisecond)) {
		t.Fatal("sampling interval state must be independent per path")
	}
	if !uc.shouldExportRuntimeSample(1, start.Add(100*time.Millisecond)) {
		t.Fatal("Path 1 sample at interval boundary should be exported")
	}

	legacy := &UtilityController{lastRuntimeSample: make(map[protocol.PathID]time.Time)}
	if !legacy.shouldExportRuntimeSample(1, start) || !legacy.shouldExportRuntimeSample(1, start) {
		t.Fatal("default zero interval must preserve legacy unthrottled export")
	}
}
