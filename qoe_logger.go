package main

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
	"github.com/q191201771/lal/pkg/httpflv"
)

type qoeEvent struct {
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

type qoeLogger struct {
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
	qoeOnce sync.Once
	qoeLog  *qoeLogger
)

func getQoELogger() *qoeLogger {
	qoeOnce.Do(func() {
		qoeLog = newQoELogger()
	})
	return qoeLog
}

func newQoELogger() *qoeLogger {
	if os.Getenv("QACCESS_ENABLE_QOE_LOG") != "1" {
		return &qoeLogger{}
	}

	dir := strings.TrimSpace(os.Getenv("QACCESS_QOE_LOG_DIR"))
	if dir == "" {
		dir = filepath.Join("logs", "qoe")
	}
	if err := os.MkdirAll(dir, 0755); err != nil {
		return &qoeLogger{}
	}

	role := sanitizeQoEValue(os.Getenv("QACCESS_QOE_ROLE"))
	sessionID := sanitizeQoEValue(os.Getenv("QACCESS_QOE_SESSION_ID"))
	if sessionID == "" {
		sessionID = sanitizeQoEValue(os.Getenv("RUN_ID"))
	}

	nameParts := []string{"qoe_events"}
	if sessionID != "" {
		nameParts = append(nameParts, sanitizeQoEFilename(sessionID))
	}
	if role != "" {
		nameParts = append(nameParts, sanitizeQoEFilename(role))
	}
	nameParts = append(nameParts, time.Now().UTC().Format("20060102T150405Z"), strconv.Itoa(os.Getpid()))
	path := filepath.Join(dir, strings.Join(nameParts, "_")+".csv")

	f, err := os.Create(path)
	if err != nil {
		return &qoeLogger{}
	}

	w := csv.NewWriter(f)
	l := &qoeLogger{
		enabled:       true,
		role:          role,
		sessionID:     sessionID,
		file:          f,
		writer:        w,
		logVideoEvery: parseQoEPositiveInt64(os.Getenv("QACCESS_QOE_LOG_VIDEO_EVERY_N"), 1),
		logAudio:      parseQoEBool(os.Getenv("QACCESS_QOE_LOG_AUDIO"), false),
		lastSync:      time.Now(),
	}
	l.writeHeader()
	return l
}

func (l *qoeLogger) writeHeader() {
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

func (l *qoeLogger) Close() {
	if l == nil || !l.enabled {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	l.writer.Flush()
	_ = l.file.Close()
	l.enabled = false
}

func logQoEEvent(ev qoeEvent) {
	l := getQoELogger()
	if l == nil || !l.enabled {
		return
	}

	now := strconv.FormatInt(time.Now().UnixNano()/int64(time.Millisecond), 10)
	role := sanitizeQoEValue(ev.role)
	if role == "" {
		role = l.role
	}
	if role == "" {
		role = "unknown"
	}
	sessionID := sanitizeQoEValue(ev.sessionID)
	if sessionID == "" {
		sessionID = l.sessionID
	}

	row := []string{
		now,
		role,
		sanitizeQoEValue(ev.event),
		sessionID,
		sanitizeQoEValue(ev.flvTimestampMS),
		sanitizeQoEValue(ev.physicalTimeMS),
		sanitizeQoEValue(ev.tagType),
		sanitizeQoEValue(ev.frameType),
		sanitizeQoEValue(ev.chunkSize),
		sanitizeQoEValue(ev.streamID),
		sanitizeQoEValue(ev.connectionID),
		sanitizeQoEValue(ev.localAddr),
		sanitizeQoEValue(ev.remoteAddr),
		sanitizeQoEValue(ev.note),
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

func qoeShouldLogVideo(seq int64) bool {
	l := getQoELogger()
	if l == nil || !l.enabled {
		return false
	}
	if l.logVideoEvery <= 1 {
		return true
	}
	return seq%l.logVideoEvery == 0
}

func qoeShouldLogAudio() bool {
	l := getQoELogger()
	return l != nil && l.enabled && l.logAudio
}

func qoeNowMS() string {
	return strconv.FormatInt(time.Now().UnixNano()/int64(time.Millisecond), 10)
}

func qoeUint32(v uint32) string {
	return strconv.FormatUint(uint64(v), 10)
}

func qoeInt(v int) string {
	return strconv.Itoa(v)
}

func qoeInt64(v int64) string {
	return strconv.FormatInt(v, 10)
}

func qoeFLVTagType(tag httpflv.Tag) string {
	switch tag.Header.Type {
	case httpflv.TagTypeVideo:
		return "video"
	case httpflv.TagTypeAudio:
		return "audio"
	case httpflv.TagTypeMetadata:
		return "script"
	default:
		return "unknown"
	}
}

func qoeFLVFrameType(tag httpflv.Tag) string {
	if len(tag.Raw) <= httpflv.TagHeaderSize+1 {
		return "unknown"
	}
	if tag.Header.Type == httpflv.TagTypeVideo {
		if tag.IsVideoKeyNALU() || tag.IsVideoKeySeqHeader() {
			return "keyframe"
		}
		return "interframe"
	}
	if tag.Header.Type == httpflv.TagTypeAudio {
		return "audio"
	}
	return "unknown"
}

func qoeRTMPTagType(msg base.RTMPMsg) string {
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

func qoeRTMPFrameType(msg base.RTMPMsg) string {
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

func sanitizeQoEValue(v string) string {
	v = strings.TrimSpace(v)
	v = strings.ReplaceAll(v, "\n", " ")
	v = strings.ReplaceAll(v, "\r", " ")
	return v
}

func sanitizeQoEFilename(v string) string {
	v = sanitizeQoEValue(v)
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

func qoeNote(format string, args ...interface{}) string {
	return fmt.Sprintf(format, args...)
}

func parseQoEPositiveInt64(v string, fallback int64) int64 {
	n, err := strconv.ParseInt(strings.TrimSpace(v), 10, 64)
	if err != nil || n <= 0 {
		return fallback
	}
	return n
}

func parseQoEBool(v string, fallback bool) bool {
	v = strings.TrimSpace(strings.ToLower(v))
	if v == "" {
		return fallback
	}
	return v == "1" || v == "true" || v == "yes" || v == "on"
}
