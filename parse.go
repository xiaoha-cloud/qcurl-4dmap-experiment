package main

import (
	"crypto/tls"
	"os"
	"strings"
	//"strings"
	//"fmt"
	"github.com/lucas-clemente/quic-go"
)

func parseCfg(multi bool, serverName string, insecureSkipVerify bool, sch string, red bool, iprio bool,
	utilityMode string, logControl bool, runID, experimentInput string) (*tls.Config, *quic.Config) {
	// var gquicvm = map[string]quic.VersionNumber{
	// 	"39": quic.VersionGQUIC39,
	// 	"43": quic.VersionGQUIC43,
	// 	"44": quic.VersionGQUIC44,
	// }

	// versions := []quic.VersionNumber{}
	// if version != "" {
	// 	vs := strings.Split(version, ",")
	// 	for _, v := range vs {
	// 		if vv, ok := gquicvm[v]; ok {
	// 			versions = append(versions, vv)
	// 		}
	// 	}
	// }
	//fmt.Print("sch:%s",sch)
	return &tls.Config{
			ServerName:             serverName,
			InsecureSkipVerify:     insecureSkipVerify,
			SessionTicketsDisabled: true}, &quic.Config{
			CreatePaths:         multi,
			SchedulerName:       sch,
			GenerateRedundancy:  red,
			IPriority:           iprio,
			UtilityMode:         utilityMode,
			LogControlActions:   logControl,
			ExperimentRunID:     runID,
			ExperimentInputFile: experimentInput,
			Phase2Enabled:       envEnabled("QACCESS_PHASE2_ENABLED"),
			Phase2Owner:         envEnabled("QACCESS_PHASE2_OWNER"),
			EndpointRole:        strings.TrimSpace(os.Getenv("QACCESS_ENDPOINT_ROLE")),
			Phase2StateDir:      strings.TrimSpace(os.Getenv("QACCESS_PHASE2_STATE_DIR")),
		}
	// NextProtos:             []string{"39", "43", "44"},
	//}, &quic.Config{Versions: versions}

}

func envEnabled(name string) bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(name))) {
	case "1", "true", "yes", "on":
		return true
	default:
		return false
	}
}
