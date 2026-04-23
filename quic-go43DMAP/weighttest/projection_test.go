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
	floor := 0.05
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
	pm := quic.PathMetrics{
		PathID:    protocol.PathID(1),
		BWbps:     50e6,
		LossRate:  0,
		OWDms:     10,
	}
	_ = uc.Compute(pm)
	w2 := uc.GetWeights()
	if math.Abs(w2.WT+w2.WD+w2.WL-1) > 1e-6 {
		t.Fatalf("after step sum != 1: %+v", w2)
	}
}
