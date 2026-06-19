package quic

import (
	"encoding/json"
	"io/ioutil"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/lucas-clemente/quic-go/internal/protocol"
)

func newTriggerTestController(t *testing.T, maxRows, minSamples int64) (*UtilityController, string, string) {
	t.Helper()
	dir, err := ioutil.TempDir("", "qaccess-trigger-test-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	requestPath := filepath.Join(dir, "request.json")
	responsePath := filepath.Join(dir, "response.json")
	uc := &UtilityController{
		Mode:   ModeQAccessT,
		RunID:  "trigger-test",
		coeffs: defaultQAccessTCoefficients(),
		phase2: qaccessPhase2Config{
			runtimeExport:       true,
			runtimeBufferMax:    maxRows,
			minSamplesPerPath:   minSamples,
			triggerUpdate:       true,
			triggerOnBufferFull: true,
			triggerCooldown:     time.Minute,
			updateRequestPath:   requestPath,
			updateResponsePath:  responsePath,
			triggerAuditPath:    filepath.Join(dir, "trigger_audit.jsonl"),
		},
	}
	uc.runtimeExporter = newQAccessSampleExporter(filepath.Join(dir, "samples.csv"), uc.RunID, maxRows)
	return uc, requestPath, responsePath
}

func setTriggerTestRows(uc *UtilityController, rows map[protocol.PathID]int64) {
	uc.runtimeExporter.rowsWrittenPerPath = make(map[protocol.PathID]int64, len(rows))
	uc.runtimeExporter.rowsWritten = 0
	uc.runtimeExporter.pending = make(map[protocol.PathID]map[string]string)
	for pathID, count := range rows {
		uc.runtimeExporter.rowsWrittenPerPath[pathID] = count
		uc.runtimeExporter.rowsWritten += count
	}
	uc.runtimeExporter.windowStartMs = 1000
	uc.runtimeExporter.windowEndMs = 2000
}

func readTriggerTestRequest(t *testing.T, path string) map[string]interface{} {
	t.Helper()
	data, err := ioutil.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var request map[string]interface{}
	if err := json.Unmarshal(data, &request); err != nil {
		t.Fatal(err)
	}
	return request
}

func TestQAccessBufferGlobalCapacitySelectsLargestPath(t *testing.T) {
	uc, _, _ := newTriggerTestController(t, 3000, 1)
	setTriggerTestRows(uc, map[protocol.PathID]int64{0: 999, 1: 1000, 3: 1001})
	snapshot := uc.runtimeExporter.triggerSnapshot(uc.phase2.minSamplesPerPath)
	if !snapshot.AtCapacity || snapshot.TotalRows != 3000 {
		t.Fatalf("expected full global buffer, got %+v", snapshot)
	}
	if !snapshot.HasSelectedPath || snapshot.SelectedPath != protocol.PathID(3) {
		t.Fatalf("expected path 3, got %+v", snapshot)
	}
}

func TestQAccessBufferRemainsGloballyBounded(t *testing.T) {
	uc, _, _ := newTriggerTestController(t, 3, 1)
	for i := 0; i < 4; i++ {
		pathID := protocol.PathID(i % 2)
		row := map[string]string{"timestamp_ms": "1000"}
		if err := uc.runtimeExporter.recordPending(row, pathID, 1); err != nil {
			t.Fatal(err)
		}
		if err := uc.runtimeExporter.flushAllPending(func(protocol.PathID) float64 { return 1 }); err != nil {
			t.Fatal(err)
		}
	}
	if size := uc.runtimeExporter.bufferSize(); size != 3 {
		t.Fatalf("global buffer exceeded capacity: %d", size)
	}
}

func TestQAccessBufferFullWithoutEligiblePath(t *testing.T) {
	uc, requestPath, _ := newTriggerTestController(t, 3000, 1002)
	setTriggerTestRows(uc, map[protocol.PathID]int64{0: 999, 1: 1000, 3: 1001})
	snapshot := uc.runtimeExporter.triggerSnapshot(uc.phase2.minSamplesPerPath)
	if !snapshot.AtCapacity || snapshot.HasSelectedPath || len(snapshot.EligiblePaths) != 0 {
		t.Fatalf("expected full buffer with no eligible path, got %+v", snapshot)
	}
	uc.maybeTriggerCoefficientUpdate(time.Unix(10, 0))
	if _, err := os.Stat(requestPath); !os.IsNotExist(err) {
		t.Fatalf("ineligible buffer should not create request, stat err=%v", err)
	}
}

func TestQAccessBufferGlobalSplitWritesAuditableRequest(t *testing.T) {
	uc, requestPath, _ := newTriggerTestController(t, 3000, 1)
	setTriggerTestRows(uc, map[protocol.PathID]int64{0: 999, 1: 1000, 3: 1001})
	uc.maybeTriggerCoefficientUpdate(time.Unix(10, 0))
	request := readTriggerTestRequest(t, requestPath)
	if request["reason"] != "buffer_full" || int(request["path_id"].(float64)) != 3 {
		t.Fatalf("unexpected request: %+v", request)
	}
	if int(request["runtime_buffer_size"].(float64)) != 3000 {
		t.Fatalf("missing global buffer context: %+v", request)
	}
	rows := request["rows_by_path"].(map[string]interface{})
	if int(rows["0"].(float64)) != 999 || int(rows["1"].(float64)) != 1000 || int(rows["3"].(float64)) != 1001 {
		t.Fatalf("unexpected rows_by_path: %+v", rows)
	}
}

func TestQAccessBufferUpdateInProgressBlocksRequest(t *testing.T) {
	uc, requestPath, _ := newTriggerTestController(t, 3000, 1)
	setTriggerTestRows(uc, map[protocol.PathID]int64{0: 1500, 1: 1500})
	uc.updateInProgress = true
	uc.inflightRequestID = "existing"
	uc.maybeTriggerCoefficientUpdate(time.Unix(10, 0))
	if _, err := os.Stat(requestPath); !os.IsNotExist(err) {
		t.Fatalf("request should be blocked, stat err=%v", err)
	}
}

func TestQAccessBufferCooldownBlocksRequest(t *testing.T) {
	uc, requestPath, _ := newTriggerTestController(t, 3000, 1)
	setTriggerTestRows(uc, map[protocol.PathID]int64{0: 1500, 1: 1500})
	now := time.Unix(100, 0)
	uc.lastTriggerTime = now.Add(-30 * time.Second)
	uc.maybeTriggerCoefficientUpdate(now)
	if _, err := os.Stat(requestPath); !os.IsNotExist(err) {
		t.Fatalf("request should be blocked by cooldown, stat err=%v", err)
	}
}

func TestQAccessBufferResponseResumesAndAllowsSecondCycle(t *testing.T) {
	uc, requestPath, responsePath := newTriggerTestController(t, 3000, 1)
	setTriggerTestRows(uc, map[protocol.PathID]int64{0: 1500, 1: 1500})
	firstNow := time.Unix(100, 0)
	uc.maybeTriggerCoefficientUpdate(firstNow)
	first := readTriggerTestRequest(t, requestPath)
	if !uc.updateInProgress {
		t.Fatal("first request did not set updateInProgress")
	}
	if err := writeJSONAtomic(responsePath, map[string]interface{}{
		"request_id": first["request_id"], "status": "skipped",
	}); err != nil {
		t.Fatal(err)
	}
	responseNow := firstNow.Add(5 * time.Second)
	uc.maybeCheckUpdateResponse(responseNow)
	if uc.updateInProgress || uc.runtimeExporter.bufferSize() != 0 {
		t.Fatalf("response did not reset global cycle: in_progress=%t size=%d", uc.updateInProgress, uc.runtimeExporter.bufferSize())
	}
	audit, err := ioutil.ReadFile(uc.phase2.triggerAuditPath)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(audit), `"event":"response_consumed"`) ||
		!strings.Contains(string(audit), `"event":"buffer_reset"`) ||
		!strings.Contains(string(audit), `"sampling_resumed":true`) {
		t.Fatalf("missing response/reset audit markers: %s", audit)
	}
	if err := uc.runtimeExporter.recordPending(
		map[string]string{"timestamp_ms": "106000"}, protocol.PathID(1), 1,
	); err != nil {
		t.Fatal(err)
	}
	if uc.runtimeExporter.bufferSize() != 1 {
		t.Fatalf("sampling did not resume after response, size=%d", uc.runtimeExporter.bufferSize())
	}

	if err := os.Remove(requestPath); err != nil {
		t.Fatal(err)
	}
	_ = os.Remove(responsePath)
	setTriggerTestRows(uc, map[protocol.PathID]int64{0: 1400, 1: 1600})
	uc.maybeTriggerCoefficientUpdate(responseNow.Add(61 * time.Second))
	second := readTriggerTestRequest(t, requestPath)
	if second["request_id"] == first["request_id"] || int(second["path_id"].(float64)) != 1 {
		t.Fatalf("second trigger cycle failed: first=%+v second=%+v", first, second)
	}
}
