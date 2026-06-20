package main

import (
	"encoding/json"
	"flag"
	"fmt"
	quic "github.com/lucas-clemente/quic-go"
	"github.com/q191201771/lal/pkg/base"
	"github.com/q191201771/lal/pkg/logic"
	"github.com/q191201771/lal/pkg/rtmp"
	"os"
	"path/filepath"
	"sync"
	"time"
	//"github.com/q191201771/naza/pkg/nazalog"
	//quic "github.com/lucas-clemente/quic-go"
)

type pushProxy struct {
	isPushing   bool
	pushSession *rtmp.PushSession
}
type pullProxy struct {
	isPulling   bool
	pullSession *rtmp.PullSession
}

type ServerManager struct {
	rtmpServer *rtmp.Server
	exitChan   chan struct{}

	mutex          sync.Mutex
	groupMap       map[string]*logic.Group // TODO chef: with appName
	phase2RunID    string
	phase2StateDir string
	phase2Enabled  bool
	phase2Owners   map[string]phase2OwnerLease
}

type phase2OwnerLease struct {
	RTMPSessionID string
	ConnectionID  string
}

func main() {
	var (
		protocol   = flag.String("protocol", "quic", "network")
		hasau      = flag.Bool("au", true, "has au?")
		rtmpserver *rtmp.Server
		addr       string
	)
	flag.Parse()

	conffile := "lalserver.conf.json"
	addr = "0.0.0.0:1935"
	logic.Init(conffile)
	m := &ServerManager{
		groupMap:       make(map[string]*logic.Group),
		exitChan:       make(chan struct{}),
		phase2RunID:    os.Getenv("QACCESS_EXPERIMENT_RUN_ID"),
		phase2StateDir: os.Getenv("QACCESS_PHASE2_STATE_DIR"),
		phase2Enabled:  os.Getenv("QACCESS_PHASE2_ENABLED") == "1",
		phase2Owners:   make(map[string]phase2OwnerLease),
	}
	rtmpserver = rtmp.NewServer(m, addr, *protocol, *hasau)
	fmt.Println(*protocol, *hasau)

	// if err := rtmpserver.Listen(); err != nil {
	// 	return err
	// }
	if *hasau {
		if err := rtmpserver.ListenAU(); err != nil {
			fmt.Println("rtmpserver.ListenAU failed:", err)
			return
		}
		if err := rtmpserver.RunLoopAU(); err != nil {
			fmt.Println("rtmpserver.RunLoopAU failed:", err)
			return
		}
	}
	//rtmpserver.Listen()

	//rtmpserver.RunLoop()

	if err := rtmpserver.Listen(); err != nil {
		fmt.Println("rtmpserver.Listen failed:", err)
		return
	}
	fmt.Println("rtmpserver.Listen ok")

	if err := rtmpserver.RunLoop(); err != nil {
		fmt.Println("rtmpserver.RunLoop failed:", err)
		return
	}

	// go func() {
	// 	if err := rtmpserver.RunLoop(); err != nil {
	// 		nazalog.Error(err)
	// 	}
	// }()
}

// ServerObserver of rtmp.Server
func (sm *ServerManager) OnRTMPConnect(session *rtmp.ServerSession, opa rtmp.ObjectPairArray) {
	sm.mutex.Lock()
	defer sm.mutex.Unlock()

	var info base.RTMPConnectInfo
	info.ServerID = "111"
	info.SessionID = session.UniqueKey()
	//info.RemoteAddr = session.GetStat().RemoteAddr
	if app, err := opa.FindString("app"); err == nil {
		info.App = app
	}
	if flashVer, err := opa.FindString("flashVer"); err == nil {
		info.FlashVer = flashVer
	}
	if tcURL, err := opa.FindString("tcUrl"); err == nil {
		info.TCURL = tcURL
	}
	logic.HttpNotify.OnRTMPConnect(info)
}

func (sm *ServerManager) getGroup(appName string, streamName string) *logic.Group {
	group, exist := sm.groupMap[streamName]
	if !exist {
		return nil
	}
	return group
}

// ServerObserver of rtmp.Server
func (sm *ServerManager) OnDelRTMPPubSession(session *rtmp.ServerSession) {
	sm.mutex.Lock()
	defer sm.mutex.Unlock()
	group := sm.getGroup(session.AppName(), session.StreamName())
	if group == nil {
		return
	}

	group.DelRTMPPubSession(session)

	var info base.PubStopInfo
	//info.ServerID = config.ServerID
	info.ServerID = "111"
	info.Protocol = base.ProtocolRTMP
	info.URL = session.URL()
	info.AppName = session.AppName()
	info.StreamName = session.StreamName()
	info.URLParam = session.RawQuery()
	info.SessionID = session.UniqueKey()
	//info.RemoteAddr = session.GetStat().RemoteAddr
	info.HasInSession = group.HasInSession()
	info.HasOutSession = group.HasOutSession()
	logic.HttpNotify.OnPubStop(info)
}

// ServerObserver of rtmp.Server
func (sm *ServerManager) OnNewRTMPSubSession(session *rtmp.ServerSession) bool {
	sm.mutex.Lock()
	defer sm.mutex.Unlock()
	leaseKey := sm.phase2LeaseKey(session.AppName(), session.StreamName())
	if !sm.phase2Enabled {
		_ = session.DisablePhase2()
		sm.writePhase2Audit(session, false, false, "downlink_disabled", "")
	} else {
		owner, err := sm.acquirePhase2Owner(leaseKey, session.UniqueKey(), session.Phase2ConnectionID())
		if err != nil {
			sm.writePhase2Audit(session, false, false, "lease_conflict", err.Error())
			_ = session.DisablePhase2()
		} else if owner {
			cfg := quic.Phase2SessionConfig{
				Enabled: true, Owner: true, EndpointRole: quic.Phase2OwnerRole,
				StateDir: sm.phase2StateDir, RunID: sm.phase2RunID,
				RTMPSessionID: session.UniqueKey(), StreamKey: session.AppName() + "/" + session.StreamName(),
			}
			if err := session.ConfigurePhase2(cfg); err != nil {
				sm.releasePhase2Owner(leaseKey, session.UniqueKey())
				sm.writePhase2Audit(session, false, false, "controller_create_failed", err.Error())
				return false
			}
			sm.writePhase2Audit(session, true, true, "owner_acquired", "")
		} else {
			_ = session.DisablePhase2()
			sm.writePhase2Audit(session, false, false, "non_owner_subscriber", "")
		}
	}
	group := sm.getOrCreateGroup(session.AppName(), session.StreamName())
	group.AddRTMPSubSession(session)

	var info base.SubStartInfo
	info.ServerID = "111"
	info.Protocol = base.ProtocolRTMP
	info.Protocol = session.URL()
	info.AppName = session.AppName()
	info.StreamName = session.StreamName()
	info.URLParam = session.RawQuery()
	info.SessionID = session.UniqueKey()
	//info.RemoteAddr = session.GetStat().RemoteAddr
	info.HasInSession = group.HasInSession()
	info.HasOutSession = group.HasOutSession()
	logic.HttpNotify.OnSubStart(info)

	return true
}

func (sm *ServerManager) OnNewRTMPPubSession(session *rtmp.ServerSession) bool {
	sm.mutex.Lock()
	defer sm.mutex.Unlock()
	_ = session.DisablePhase2()
	sm.writePhase2Audit(session, false, false, "publisher_ingress_disabled", "")
	group := sm.getOrCreateGroup(session.AppName(), session.StreamName())
	res := group.AddRTMPPubSession(session)

	// TODO chef: res值为false时，可以考虑不回调
	// TODO chef: 每次赋值都逐个拼，代码冗余，考虑直接用ISession抽离一下代码
	var info base.PubStartInfo
	info.ServerID = "111"
	info.Protocol = base.ProtocolRTMP
	info.URL = session.URL()
	info.AppName = session.AppName()
	info.StreamName = session.StreamName()
	info.URLParam = session.RawQuery()
	info.SessionID = session.UniqueKey()
	//info.RemoteAddr = session.GetStat().RemoteAddr
	info.HasInSession = group.HasInSession()
	info.HasOutSession = group.HasOutSession()
	logic.HttpNotify.OnPubStart(info)
	return res
}

// ServerObserver of rtmp.Server
func (sm *ServerManager) OnDelRTMPSubSession(session *rtmp.ServerSession) {
	sm.mutex.Lock()
	defer sm.mutex.Unlock()
	sm.releasePhase2Owner(sm.phase2LeaseKey(session.AppName(), session.StreamName()), session.UniqueKey())
	group := sm.getGroup(session.AppName(), session.StreamName())
	if group == nil {
		return
	}

	group.DelRTMPSubSession(session)

	var info base.SubStopInfo
	info.ServerID = "111"
	info.Protocol = base.ProtocolRTMP
	info.AppName = session.AppName()
	info.StreamName = session.StreamName()
	info.URLParam = session.RawQuery()
	info.SessionID = session.UniqueKey()
	//info.RemoteAddr = session.GetStat().RemoteAddr
	info.HasInSession = group.HasInSession()
	info.HasOutSession = group.HasOutSession()
	logic.HttpNotify.OnSubStop(info)
}

func (sm *ServerManager) phase2LeaseKey(app, stream string) string {
	return sm.phase2RunID + "/" + app + "/" + stream
}

func (sm *ServerManager) acquirePhase2Owner(key, sessionID, connectionID string) (bool, error) {
	if sm.phase2RunID == "" || !filepath.IsAbs(sm.phase2StateDir) {
		return false, fmt.Errorf("Phase 2 requires run ID and absolute state dir")
	}
	if current, ok := sm.phase2Owners[key]; ok {
		if current.RTMPSessionID == sessionID && current.ConnectionID == connectionID {
			return true, nil
		}
		return false, fmt.Errorf("owner already held by rtmp_session_id=%s connection_id=%s", current.RTMPSessionID, current.ConnectionID)
	}
	sm.phase2Owners[key] = phase2OwnerLease{RTMPSessionID: sessionID, ConnectionID: connectionID}
	return true, nil
}

func (sm *ServerManager) releasePhase2Owner(key, sessionID string) {
	if current, ok := sm.phase2Owners[key]; ok && current.RTMPSessionID == sessionID {
		delete(sm.phase2Owners, key)
	}
}

func (sm *ServerManager) writePhase2Audit(session *rtmp.ServerSession, owner, controller bool, decision, errorText string) {
	if !filepath.IsAbs(sm.phase2StateDir) {
		return
	}
	_ = os.MkdirAll(sm.phase2StateDir, 0755)
	role := quic.Phase2OwnerRole
	if decision == "publisher_ingress_disabled" {
		role = "server_publisher_ingress"
	}
	payload := map[string]interface{}{
		"timestamp_ms": time.Now().UnixNano() / int64(time.Millisecond), "pid": os.Getpid(),
		"controller_pid": os.Getpid(),
		"endpoint_role":  role,
		"phase2_enabled": owner, "phase2_owner": owner, "controller_created": controller,
		"phase2_state_dir": sm.phase2StateDir, "run_id": sm.phase2RunID,
		"rtmp_session_id": session.UniqueKey(), "connection_id": session.Phase2ConnectionID(),
		"stream_key": session.AppName() + "/" + session.StreamName(), "lease_decision": decision,
	}
	if errorText != "" {
		payload["error"] = errorText
	}
	b, _ := json.Marshal(payload)
	f, err := os.OpenFile(filepath.Join(sm.phase2StateDir, "qaccess_owner_audit.jsonl"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0666)
	if err == nil {
		_, _ = f.Write(append(b, '\n'))
		_ = f.Close()
	}
}

func (sm *ServerManager) getOrCreateGroup(appName string, streamName string) *logic.Group {
	group, exist := sm.groupMap[streamName]
	if !exist {
		// pullURL := fmt.Sprintf("rtmp://%s/%s/%s", config.RelayPullConfig.Addr, appName, streamName)
		// group = logic.NewGroup(appName, streamName, config.RelayPullConfig.Enable, pullURL)
		pullURL := fmt.Sprintf("rtmp://%s/%s/%s", "", appName, streamName)
		group = logic.NewGroup(appName, streamName, false, pullURL)
		sm.groupMap[streamName] = group

		go group.RunLoop()
	}
	return group
}
