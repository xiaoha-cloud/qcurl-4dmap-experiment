package rtmp

import (
	"encoding/csv"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/q191201771/lal/pkg/base"
)

type rtmpQoEEvent struct {
	role           string
	event          string
	sessionID      string
	flvTimestampMS string
	physicalTimeMS string
	tagType        string
	frameType      string
	chunkSize      string
	streamID       string
	connectionID   string
	localAddr      string
	remoteAddr     string
	note           string
}

type rtmpQoELogger struct {
	enabled       bool
	role          string
	sessionID     string
	file          *os.File
	writer        *csv.Writer
	logVideoEvery int64
	logAudio      bool
	rowsSinceSync int
	lastSync      time.Time
	mu            sync.Mutex
}

var (
	rtmpQoEOnce sync.Once
	rtmpQoELog  *rtmpQoELogger
)

func getRTMPQoELogger() *rtmpQoELogger {
	rtmpQoEOnce.Do(func() {
		rtmpQoELog = newRTMPQoELogger()
	})
	return rtmpQoELog
}

func newRTMPQoELogger() *rtmpQoELogger {
	if os.Getenv("QACCESS_ENABLE_QOE_LOG") != "1" {
		return &rtmpQoELogger{}
	}

	dir := strings.TrimSpace(os.Getenv("QACCESS_QOE_LOG_DIR"))
	if dir == "" {
		dir = filepath.Join("logs", "qoe")
	}
	if err := os.MkdirAll(dir, 0755); err != nil {
		return &rtmpQoELogger{}
	}

	role := sanitizeRTMPQoEValue(os.Getenv("QACCESS_QOE_ROLE"))
	if role == "" {
		role = "server"
	}
	sessionID := sanitizeRTMPQoEValue(os.Getenv("QACCESS_QOE_SESSION_ID"))
	if sessionID == "" {
		sessionID = sanitizeRTMPQoEValue(os.Getenv("RUN_ID"))
	}
	if sessionID == "" {
		sessionID = sanitizeRTMPQoEValue(os.Getenv("QACCESS_EXPERIMENT_RUN_ID"))
	}

	nameParts := []string{"qoe_events"}
	if sessionID != "" {
		nameParts = append(nameParts, sanitizeRTMPQoEFilename(sessionID))
	}
	if role != "" {
		nameParts = append(nameParts, sanitizeRTMPQoEFilename(role))
	}
	nameParts = append(nameParts, time.Now().UTC().Format("20060102T150405Z"), strconv.Itoa(os.Getpid()))
	path := filepath.Join(dir, strings.Join(nameParts, "_")+".csv")

	f, err := os.Create(path)
	if err != nil {
		return &rtmpQoELogger{}
	}

	l := &rtmpQoELogger{
		enabled:       true,
		role:          role,
		sessionID:     sessionID,
		file:          f,
		writer:        csv.NewWriter(f),
		logVideoEvery: parseRTMPQoEPositiveInt64(os.Getenv("QACCESS_QOE_LOG_VIDEO_EVERY_N"), 1),
		logAudio:      parseRTMPQoEBool(os.Getenv("QACCESS_QOE_LOG_AUDIO"), false),
		lastSync:      time.Now(),
	}
	l.writeHeader()
	return l
}

func (l *rtmpQoELogger) writeHeader() {
	if l == nil || !l.enabled {
		return
	}
	_ = l.writer.Write([]string{
		"timestamp_ms",
		"role",
		"event",
		"session_id",
		"flv_timestamp_ms",
		"physical_time_ms",
		"tag_type",
		"frame_type",
		"chunk_size",
		"stream_id",
		"connection_id",
		"local_addr",
		"remote_addr",
		"note",
	})
	l.writer.Flush()
}

func logRTMPQoEEvent(ev rtmpQoEEvent) {
	l := getRTMPQoELogger()
	if l == nil || !l.enabled {
		return
	}

	now := strconv.FormatInt(time.Now().UnixNano()/int64(time.Millisecond), 10)
	role := sanitizeRTMPQoEValue(ev.role)
	if role == "" {
		role = l.role
	}
	if role == "" {
		role = "unknown"
	}
	sessionID := sanitizeRTMPQoEValue(ev.sessionID)
	if sessionID == "" {
		sessionID = l.sessionID
	}

	row := []string{
		now,
		role,
		sanitizeRTMPQoEValue(ev.event),
		sessionID,
		sanitizeRTMPQoEValue(ev.flvTimestampMS),
		sanitizeRTMPQoEValue(ev.physicalTimeMS),
		sanitizeRTMPQoEValue(ev.tagType),
		sanitizeRTMPQoEValue(ev.frameType),
		sanitizeRTMPQoEValue(ev.chunkSize),
		sanitizeRTMPQoEValue(ev.streamID),
		sanitizeRTMPQoEValue(ev.connectionID),
		sanitizeRTMPQoEValue(ev.localAddr),
		sanitizeRTMPQoEValue(ev.remoteAddr),
		sanitizeRTMPQoEValue(ev.note),
	}

	l.mu.Lock()
	defer l.mu.Unlock()
	if err := l.writer.Write(row); err != nil {
		return
	}
	l.rowsSinceSync++
	if l.rowsSinceSync >= 100 || time.Since(l.lastSync) >= time.Second {
		l.writer.Flush()
		l.rowsSinceSync = 0
		l.lastSync = time.Now()
	}
}

func rtmpQoEShouldLogVideo(seq int64) bool {
	l := getRTMPQoELogger()
	if l == nil || !l.enabled {
		return false
	}
	if l.logVideoEvery <= 1 {
		return true
	}
	return seq%l.logVideoEvery == 0
}

func rtmpQoEShouldLogAudio() bool {
	l := getRTMPQoELogger()
	return l != nil && l.enabled && l.logAudio
}

func rtmpQoENowMS() string {
	return strconv.FormatInt(time.Now().UnixNano()/int64(time.Millisecond), 10)
}

func rtmpQoEUint32(v uint32) string {
	return strconv.FormatUint(uint64(v), 10)
}

func rtmpQoEInt(v int) string {
	return strconv.Itoa(v)
}

func rtmpQoETagType(msg base.RTMPMsg) string {
	switch msg.Header.MsgTypeID {
	case base.RTMPTypeIDVideo:
		return "video"
	case base.RTMPTypeIDAudio:
		return "audio"
	case base.RTMPTypeIDMetadata:
		return "script"
	default:
		return "unknown"
	}
}

func rtmpQoEFrameType(msg base.RTMPMsg) string {
	if len(msg.Payload) < 2 {
		return "unknown"
	}
	if msg.Header.MsgTypeID == base.RTMPTypeIDVideo {
		if msg.IsVideoKeyNALU() || msg.IsVideoKeySeqHeader() {
			return "keyframe"
		}
		return "interframe"
	}
	if msg.Header.MsgTypeID == base.RTMPTypeIDAudio {
		return "audio"
	}
	return "unknown"
}

func sanitizeRTMPQoEValue(v string) string {
	v = strings.TrimSpace(v)
	v = strings.ReplaceAll(v, "\n", " ")
	v = strings.ReplaceAll(v, "\r", " ")
	return v
}

func sanitizeRTMPQoEFilename(v string) string {
	v = sanitizeRTMPQoEValue(v)
	if v == "" {
		return "unknown"
	}
	var b strings.Builder
	for _, r := range v {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') || r == '-' || r == '_' || r == '.' {
			b.WriteRune(r)
			continue
		}
		b.WriteByte('_')
	}
	out := b.String()
	if out == "" {
		return "unknown"
	}
	return out
}

func rtmpQoENote(format string, args ...interface{}) string {
	return fmt.Sprintf(format, args...)
}

func parseRTMPQoEPositiveInt64(v string, fallback int64) int64 {
	n, err := strconv.ParseInt(strings.TrimSpace(v), 10, 64)
	if err != nil || n <= 0 {
		return fallback
	}
	return n
}

func parseRTMPQoEBool(v string, fallback bool) bool {
	v = strings.TrimSpace(strings.ToLower(v))
	if v == "" {
		return fallback
	}
	return v == "1" || v == "true" || v == "yes" || v == "on"
}
