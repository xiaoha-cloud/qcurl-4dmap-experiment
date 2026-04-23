package quic

import (
	"math"
	"sort"
)

// ProjectUnitSimplex3 Euclidean-projects (x0,x1,x2) onto the unit simplex
// {w : w_i >= 0, w0+w1+w2 = 1} (Duchi et al. style construction).
func ProjectUnitSimplex3(x0, x1, x2 float64) (w0, w1, w2 float64) {
	x := []float64{x0, x1, x2}
	u := make([]float64, 3)
	copy(u, x)
	sort.Slice(u, func(i, j int) bool { return u[i] > u[j] })

	n := 3
	cssv := make([]float64, n)
	s := 0.0
	for j := 0; j < n; j++ {
		s += u[j]
		cssv[j] = s - 1.0
	}

	// rho = largest (1-based) index j such that u[j-1] - cssv[j-1]/j > 0 (Numpy: last True)
	rho := 0
	for j := 0; j < n; j++ {
		ind := float64(j + 1)
		if u[j]-cssv[j]/ind > 0 {
			rho = j + 1
		}
	}
	if rho < 1 {
		rho = 1
	}
	theta := cssv[rho-1] / float64(rho)
	w0 = math.Max(x[0]-theta, 0)
	w1 = math.Max(x[1]-theta, 0)
	w2 = math.Max(x[2]-theta, 0)
	return w0, w1, w2
}

// ProjectToBoundedSimplex3 projects v onto {w: sum(w)=1, w[i] >= floor} with floor in [0,1/3).
func ProjectToBoundedSimplex3(v0, v1, v2, floor float64) (w0, w1, w2 float64) {
	if floor < 0 || floor >= 1.0/3.0 {
		floor = 0.05
	}
	ssum := 1.0 - 3.0*floor
	if ssum <= 0 {
		return 1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0
	}
	p0, p1, p2 := ProjectUnitSimplex3((v0-floor)/ssum, (v1-floor)/ssum, (v2-floor)/ssum)
	return p0*ssum + floor, p1*ssum + floor, p2*ssum + floor
}
