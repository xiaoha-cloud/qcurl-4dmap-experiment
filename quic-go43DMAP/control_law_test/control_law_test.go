package control_law_test

import (
	"math"
	"os"
	"testing"

	quic "github.com/lucas-clemente/quic-go"
	"github.com/lucas-clemente/quic-go/internal/protocol"
)

func TestLegacyControlLawReproducesGainBackoff(t *testing.T) {
	cases := []struct {
		gTotal, normD, normL, alpha, beta, gamma float64
	}{
		{0.75, 0.2, 0.0, 0.6, 0.3, 0.1},
		{0.5, 1.0, 0.1, 0.6, 0.3, 0.12},
	}
	for _, c := range cases {
		gain, backoff := quic.ComputeGainBackoffForTest(c.gTotal, c.normD, c.normL, c.alpha, c.beta, c.gamma)
		gainRaw := 1.0 + 0.20*math.Pow(c.gTotal, c.alpha) - 0.10*c.beta*5*c.normL - 0.05*c.gamma*5*c.normD
		backoffRaw := 1.0 - 0.08*math.Pow(c.gTotal, c.alpha) + 0.05*c.beta*5*c.normL + 0.03*c.gamma*5*c.normD
		wantGain := clamp(gainRaw, 0.80, 1.20)
		wantBackoff := clamp(backoffRaw, 0.90, 1.10)
		if math.Abs(gain-wantGain) > 1e-9 || math.Abs(backoff-wantBackoff) > 1e-9 {
			t.Fatalf("legacy mismatch: gain=%v backoff=%v", gain, backoff)
		}
	}
}

func TestSafeTV1DelayPenaltyCapRespected(t *testing.T) {
	in := quic.ControlLawTermsInput{
		GTotal: 0.8, NormD: 1.0, NormL: 0.0, Alpha: 0.6, Beta: 0.3, Gamma: 0.3,
	}
	_, _, diag := quic.ApplySafeTV1GainForTest(protocol.PathID(1), 1.0, in)
	if diag.DelayPenaltyBounded < -0.02-1e-9 {
		t.Fatalf("delay penalty not capped: %v", diag.DelayPenaltyBounded)
	}
}

func TestSafeTV1GainWithinBoundsAndStepLimited(t *testing.T) {
	prev := 1.0
	for i := 0; i < 20; i++ {
		in := quic.ControlLawTermsInput{
			GTotal: 0.9, NormD: 1.0, NormL: 0.1, Alpha: 0.6, Beta: 0.3, Gamma: 0.2,
		}
		gain, _, diag := quic.ApplySafeTV1GainForTest(protocol.PathID(1), prev, in)
		if gain < 0.95-1e-9 || gain > 1.10+1e-9 {
			t.Fatalf("gain out of range: %v", gain)
		}
		if math.Abs(gain-prev) > 0.02+1e-9 {
			t.Fatalf("step too large: %v -> %v", prev, gain)
		}
		_ = diag
		prev = gain
	}
}

func TestSafeTV1RetentionUnchangedFromLegacy(t *testing.T) {
	in := quic.ControlLawTermsInput{GTotal: 0.7, NormD: 0.5, NormL: 0.2, Alpha: 0.6, Beta: 0.3, Gamma: 0.15}
	_, legacyRet, _ := quic.LegacyGainBackoffDiagnostics(in.GTotal, in.NormD, in.NormL, in.Alpha, in.Beta, in.Gamma)
	_, safeRet, _ := quic.ApplySafeTV1GainForTest(protocol.PathID(1), 1.0, in)
	if math.Abs(legacyRet-safeRet) > 1e-9 {
		t.Fatalf("retention changed: %v vs %v", legacyRet, safeRet)
	}
}

func TestSafeTV1IndependentPrevGainPerPath(t *testing.T) {
	in := quic.ControlLawTermsInput{GTotal: 0.9, NormD: 1.0, NormL: 0.0, Alpha: 0.6, Beta: 0.3, Gamma: 0.25}
	g1, _, _ := quic.ApplySafeTV1GainForTest(protocol.PathID(1), 1.05, in)
	g3, _, _ := quic.ApplySafeTV1GainForTest(protocol.PathID(3), 0.98, in)
	if math.Abs(g1-g3) < 1e-6 {
		t.Fatal("paths should diverge with different prev gain")
	}
}

func TestUtilityControllerLegacyDefault(t *testing.T) {
	os.Unsetenv("QACCESS_CONTROL_LAW")
	uc := quic.NewUtilityController(quic.ModeQAccessT, "test")
	uc.BeginMonitorRound()
	uc.SetRoundGTotal(0.75)
	sig := uc.Compute(quic.PathMetrics{PathID: protocol.PathID(1), BWbps: 20e6, OWDms: 200})
	wantGain, wantBackoff := quic.ComputeGainBackoffForTest(0.75, sig.NormD, sig.NormL, sig.Alpha, sig.Beta, sig.Gamma)
	if math.Abs(sig.Gain-wantGain) > 0.0001 || math.Abs(sig.Backoff-wantBackoff) > 0.0001 {
		t.Fatalf("legacy mismatch")
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
