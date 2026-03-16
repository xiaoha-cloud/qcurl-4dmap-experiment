package main

import (
	"crypto/tls"
	"fmt"
	"os"

	"github.com/lucas-clemente/quic-go"
)

func h1OverQUIC(network, local, addr, rawurl string, tlsCfg *tls.Config, cfg *quic.Config, buffer []byte, dst *os.File) {
	fmt.Println("h1OverQUIC is disabled for RTMP-only experiment")
}

func h2OverQUIC(network, local, addr, rawurl string, tlsCfg *tls.Config, cfg *quic.Config, buffer []byte, dst *os.File) {
	fmt.Println("h2OverQUIC is disabled for RTMP-only experiment")
}
