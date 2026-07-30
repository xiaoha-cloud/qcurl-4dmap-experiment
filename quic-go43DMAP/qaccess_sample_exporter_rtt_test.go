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
