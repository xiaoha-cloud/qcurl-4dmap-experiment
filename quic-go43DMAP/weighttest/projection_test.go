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

func TestLearnModeCompute(t *testing.T) {
	uc := quic.NewUtilityController(quic.ModeLearn)
	w := uc.GetWeights()
	if math.Abs(w.WT+w.WD+w.WL-1) > 1e-9 {
		t.Fatalf("initial sum != 1: %+v", w)
	}
	uc.BeginLearnRound()
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
	info := uc.EndLearnRound()
	if info.Reason != "init_baseline" {
		t.Fatalf("first round reason: %s", info.Reason)
	}
	w2 := uc.GetWeights()
	if math.Abs(w2.WT+w2.WD+w2.WL-1) > 1e-6 {
		t.Fatalf("after step sum != 1: %+v", w2)
	}
}

func TestInactivePathSkippedForLearn(t *testing.T) {
	uc := quic.NewUtilityController(quic.ModeLearn)
	wBefore := uc.GetWeights()
	uc.BeginLearnRound()
	sig := uc.Compute(quic.PathMetrics{PathID: protocol.PathID(2)})
	if sig.Active {
		t.Fatal("zero metrics should be inactive")
	}
	if sig.Gain != 1.0 || sig.Backoff != 1.0 {
		t.Fatalf("inactive should use neutral control: gain=%v backoff=%v", sig.Gain, sig.Backoff)
	}
	info := uc.EndLearnRound()
	if info.Reason != "no_active_path" {
		t.Fatalf("reason=%s", info.Reason)
	}
	wAfter := uc.GetWeights()
	if math.Abs(wAfter.WT-wBefore.WT) > 1e-9 {
		t.Fatal("inactive round should not change weights")
	}
}

func TestPathMetricsActiveInflight(t *testing.T) {
	pm := quic.PathMetrics{InflightBytes: 2048}
	if !quic.PathMetricsActive(pm) {
		t.Fatal("expected active via inflight")
	}
}
