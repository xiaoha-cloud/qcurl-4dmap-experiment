package quic

import (
	"path/filepath"
	"testing"
)

func ownerTestIdentity(t *testing.T) Phase2SessionConfig {
	t.Helper()
	return Phase2SessionConfig{
		Enabled: true, Owner: true, EndpointRole: Phase2OwnerRole,
		StateDir: t.TempDir(), RunID: "run", RTMPSessionID: "sub-1", StreamKey: "live/test",
	}
}

func TestPhase2MissingConfigurationCreatesNoController(t *testing.T) {
	sch := &scheduler{config: &Config{UtilityMode: "qaccess_t"}}
	sch.setup()
	if sch.utilityController != nil {
		t.Fatal("missing Phase 2 identity created a controller")
	}
}

func TestPhase2OnlyServerDownlinkOwnerCreatesController(t *testing.T) {
	for _, role := range []string{"client_push_publisher", "client_pull_receiver", "server_publisher_ingress"} {
		t.Run(role, func(t *testing.T) {
			sch := &scheduler{config: &Config{UtilityMode: "qaccess_t"}}
			sch.setup()
			bad := ownerTestIdentity(t)
			bad.EndpointRole = role
			if err := sch.configurePhase2(bad, "conn-1"); err == nil {
				t.Fatal("non-owner role was accepted")
			}
			if sch.utilityController != nil {
				t.Fatal("non-owner role created a controller")
			}
		})
	}
	sch := &scheduler{config: &Config{UtilityMode: "qaccess_t"}}
	sch.setup()
	good := ownerTestIdentity(t)
	if err := sch.configurePhase2(good, "conn-1"); err != nil {
		t.Fatal(err)
	}
	if sch.utilityController == nil || !sch.utilityController.phase2MutationAllowed() {
		t.Fatal("server downlink owner did not create a mutable controller")
	}
	if got := sch.utilityController.phase2.runtimeSamples; got != filepath.Join(good.StateDir, "qaccess_runtime_samples.csv") {
		t.Fatalf("runtime path=%q", got)
	}
	if err := sch.configurePhase2(good, "conn-1"); err != nil {
		t.Fatalf("idempotent activation failed: %v", err)
	}
	conflict := good
	conflict.RTMPSessionID = "sub-2"
	if err := sch.configurePhase2(conflict, "conn-2"); err == nil {
		t.Fatal("conflicting activation was accepted")
	}
}
