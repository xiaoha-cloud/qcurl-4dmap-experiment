package main

import "testing"

func TestPhase2OwnerLeaseLifecycle(t *testing.T) {
	sm := &ServerManager{
		phase2RunID: "run", phase2StateDir: t.TempDir(),
		phase2Enabled: true,
		phase2Owners:  make(map[string]phase2OwnerLease),
	}
	key := sm.phase2LeaseKey("live", "test")
	owner, err := sm.acquirePhase2Owner(key, "sub-1", "conn-1")
	if err != nil || !owner {
		t.Fatalf("first subscriber failed: owner=%t err=%v", owner, err)
	}
	owner, err = sm.acquirePhase2Owner(key, "sub-1", "conn-1")
	if err != nil || !owner {
		t.Fatalf("idempotent acquire failed: owner=%t err=%v", owner, err)
	}
	if _, err := sm.acquirePhase2Owner(key, "sub-2", "conn-2"); err == nil {
		t.Fatal("second subscriber acquired the same owner lease")
	}
	sm.releasePhase2Owner(key, "sub-2")
	if _, ok := sm.phase2Owners[key]; !ok {
		t.Fatal("non-owner released the lease")
	}
	sm.releasePhase2Owner(key, "sub-1")
	owner, err = sm.acquirePhase2Owner(key, "sub-2", "conn-2")
	if err != nil || !owner {
		t.Fatalf("owner handoff after release failed: owner=%t err=%v", owner, err)
	}
}
