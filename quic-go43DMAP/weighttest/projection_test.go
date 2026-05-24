package weighttest

import (
	"math"
	"testing"

	"github.com/lucas-clemente/quic-go"
	"github.com/lucas-clemente/quic-go/internal/protocol"
)

func TestProjectUnitSimplex3Corner(t *testing.T) {
	w0, w1, w2 := quic.ProjectUnitSimplex3(2, 0, 0)
	if math.Abs(w0-1) > 1e-9 || w1 > 1e-9 || w2 > 1e-9 {
		t.Fatalf("expected (1,0,0), got (%v,%v,%v)", w0, w1, w2)
	}
}

func TestProjectToBoundedSimplex3Floor(t *testing.T) {
	floor := 0.10
	w0, w1, w2 := quic.ProjectToBoundedSimplex3(0, 0, 0, floor)
	if w0 < floor-1e-9 || w1 < floor-1e-9 || w2 < floor-1e-9 {
		t.Fatalf("below floor: (%v,%v,%v)", w0, w1, w2)
	}
	if math.Abs(w0+w1+w2-1) > 1e-6 {
		t.Fatalf("sum should be 1, got %v", w0+w1+w2)
	}
}

func TestQAccessTCompute(t *testing.T) {
	uc := quic.NewUtilityController(quic.ModeQAccessT, "test")
	uc.BeginMonitorRound()
	uc.SetRoundGTotal(0.5)
	pm := quic.PathMetrics{
		PathID:   protocol.PathID(1),
		BWbps:    20e6,
		LossRate: 0,
		OWDms:    20,
	}
	sig := uc.Compute(pm)
	if !sig.Active {
		t.Fatal("expected active path")
	}
	if sig.Gain < 0.8 || sig.Gain > 1.2 {
		t.Fatalf("gain out of range: %v", sig.Gain)
	}
	c := uc.Coefficients()
	if c.Alpha <= 0 {
		t.Fatalf("expected default alpha > 0, got %v", c.Alpha)
	}
}

func TestQAccessCollectProbesCoefficients(t *testing.T) {
	uc := quic.NewUtilityController(quic.ModeQAccessCollect, "test")
	uc.BeginMonitorRound()
	pm := quic.PathMetrics{
		PathID: protocol.PathID(1),
		BWbps:  15e6,
		OWDms:  30,
	}
	sig1 := uc.Compute(pm)
	uc.BeginMonitorRound()
	sig2 := uc.Compute(pm)
	if sig1.Alpha == sig2.Alpha && sig1.Beta == sig2.Beta && sig1.Gamma == sig2.Gamma {
		t.Fatal("qaccess_collect should rotate candidate coefficients each monitor round")
	}
}

func TestInactivePathNeutralControl(t *testing.T) {
	uc := quic.NewUtilityController(quic.ModeQAccessT, "test")
	uc.BeginMonitorRound()
	sig := uc.Compute(quic.PathMetrics{PathID: protocol.PathID(2)})
	if sig.Active {
		t.Fatal("zero metrics should be inactive")
	}
	if sig.Gain != 1.0 || sig.Backoff != 1.0 {
		t.Fatalf("inactive should use neutral control: gain=%v backoff=%v", sig.Gain, sig.Backoff)
	}
}

func TestPathMetricsActiveInflight(t *testing.T) {
	pm := quic.PathMetrics{InflightBytes: 2048}
	if !quic.PathMetricsActive(pm) {
		t.Fatal("expected active via inflight")
	}
}
