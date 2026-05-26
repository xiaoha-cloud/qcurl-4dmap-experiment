package quic

// qaccessTrainCollector wraps qaccessSampleExporter for qaccess_collect training CSV only.
type qaccessTrainCollector struct {
	*qaccessSampleExporter
}

func newQAccessTrainCollector(runID string) *qaccessTrainCollector {
	return &qaccessTrainCollector{
		qaccessSampleExporter: newQAccessSampleExporter(resolveTrainingCSVPath(), runID, 0),
	}
}
