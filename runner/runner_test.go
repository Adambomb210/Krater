package main

import (
	"context"
	"crypto/md5"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeKrater serves the runner API and a presigned-URL "storage" from one test server.
type fakeKrater struct {
	t         *testing.T
	server    *httptest.Server
	token     string
	command   string
	resumed   bool
	partBytes int64
	stopAfter int // heartbeats before answering stop (0 = never)

	mu         sync.Mutex
	objects    map[string][]byte // "area/name" -> content
	multipart  map[string]map[int][]byte
	heartbeats []map[string]any
	exitCodes  []int
	starts     int
}

func newFakeKrater(t *testing.T) *fakeKrater {
	f := &fakeKrater{t: t, token: "tok", partBytes: 64 << 20, objects: map[string][]byte{}, multipart: map[string]map[int][]byte{}}
	f.server = httptest.NewServer(http.HandlerFunc(f.handle))
	t.Cleanup(f.server.Close)
	return f
}

func (f *fakeKrater) cfg(t *testing.T) Config {
	dir := t.TempDir()
	return Config{
		RunnerURL:     f.server.URL,
		Token:         f.token,
		CheckpointDir: filepath.Join(dir, "checkpoints"),
		OutputDir:     filepath.Join(dir, "outputs"),
	}
}

func (f *fakeKrater) writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

func (f *fakeKrater) handle(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	path := r.URL.Path
	if strings.HasPrefix(path, "/storage/") {
		f.storage(w, r, strings.TrimPrefix(path, "/storage/"))
		return
	}
	if r.Header.Get("Authorization") != "Bearer "+f.token {
		w.WriteHeader(http.StatusForbidden)
		f.writeJSON(w, map[string]string{"error": "bad token"})
		return
	}
	var body map[string]any
	_ = json.NewDecoder(r.Body).Decode(&body)
	switch {
	case path == "/runner/v1/start":
		f.starts++
		f.writeJSON(w, map[string]any{"job_id": "j1", "attempt": 1, "resumed": f.resumed, "command": f.command,
			"kind": "job", "heartbeat_seconds": 1, "checkpoint_sync_seconds": 1})
	case path == "/runner/v1/heartbeat":
		f.heartbeats = append(f.heartbeats, body)
		f.writeJSON(w, map[string]any{"stop": f.stopAfter > 0 && len(f.heartbeats) >= f.stopAfter})
	case path == "/runner/v1/exit":
		f.exitCodes = append(f.exitCodes, int(body["exit_code"].(float64)))
		f.writeJSON(w, map[string]any{"state": "done"})
	case strings.HasSuffix(path, "/list"):
		area := strings.Split(path, "/")[4]
		files := []map[string]any{}
		for key, data := range f.objects {
			if strings.HasPrefix(key, area+"/") {
				name := strings.TrimPrefix(key, area+"/")
				files = append(files, map[string]any{"name": name, "size": len(data), "url": f.server.URL + "/storage/" + key})
			}
		}
		f.writeJSON(w, map[string]any{"files": files})
	case strings.HasSuffix(path, "/upload"):
		area := strings.Split(path, "/")[4]
		name := body["name"].(string)
		size := int64(body["size"].(float64))
		key := area + "/" + name
		if size <= f.partBytes {
			f.writeJSON(w, map[string]any{"name": name, "url": f.server.URL + "/storage/" + key})
			return
		}
		uploadID := fmt.Sprintf("u%d", len(f.multipart)+1)
		f.multipart[uploadID] = map[int][]byte{}
		parts := []string{}
		for n := int64(1); (n-1)*f.partBytes < size; n++ {
			parts = append(parts, fmt.Sprintf("%s/storage/part/%s/%d", f.server.URL, uploadID, n))
		}
		f.writeJSON(w, map[string]any{"name": name, "upload_id": uploadID, "part_bytes": f.partBytes, "parts": parts})
	case strings.HasSuffix(path, "/complete"):
		area := strings.Split(path, "/")[4]
		uploadID := body["upload_id"].(string)
		parts := f.multipart[uploadID]
		var numbers []int
		for n := range parts {
			numbers = append(numbers, n)
		}
		sort.Ints(numbers)
		etags := body["etags"].([]any)
		var data []byte
		for i, n := range numbers {
			sum := md5.Sum(parts[n])
			if etags[i].(string) != `"`+hex.EncodeToString(sum[:])+`"` {
				f.t.Errorf("part %d ETag mismatch", n)
			}
			data = append(data, parts[n]...)
		}
		f.objects[area+"/"+body["name"].(string)] = data
		delete(f.multipart, uploadID)
		f.writeJSON(w, map[string]any{"ok": true})
	default:
		http.NotFound(w, r)
	}
}

func (f *fakeKrater) storage(w http.ResponseWriter, r *http.Request, key string) {
	if r.Header.Get("Authorization") != "" {
		f.t.Errorf("storage request carried an Authorization header")
	}
	switch r.Method {
	case http.MethodGet:
		data, ok := f.objects[key]
		if !ok {
			http.NotFound(w, r)
			return
		}
		_, _ = w.Write(data)
	case http.MethodPut:
		data, _ := io.ReadAll(r.Body)
		if strings.HasPrefix(key, "part/") {
			bits := strings.Split(key, "/")
			var n int
			fmt.Sscan(bits[2], &n)
			f.multipart[bits[1]][n] = data
		} else {
			f.objects[key] = data
		}
		sum := md5.Sum(data)
		w.Header().Set("ETag", `"`+hex.EncodeToString(sum[:])+`"`)
	}
}

func noGPU() (float64, bool) { return 0, false }

func TestRunReportsTheExitCodeAndUploadsCheckpointsAndOutputs(t *testing.T) {
	f := newFakeKrater(t)
	f.command = `echo step > "$KRATER_CHECKPOINT_DIR/ckpt.txt"; mkdir -p "$KRATER_OUTPUT_DIR/eval"; ` +
		`echo 0.93 > "$KRATER_OUTPUT_DIR/eval/score.txt"; test "$KRATER_RESUMED" = 0; exit 3`
	cfg := f.cfg(t)

	code, err := Run(context.Background(), cfg, http.DefaultClient, http.DefaultClient, noGPU)

	if err != nil || code != 3 {
		t.Fatalf("Run = %d, %v; want 3, nil", code, err)
	}
	if fmt.Sprint(f.exitCodes) != "[3]" {
		t.Errorf("exit codes reported: %v", f.exitCodes)
	}
	if string(f.objects["checkpoints/ckpt.txt"]) != "step\n" || string(f.objects["outputs/eval/score.txt"]) != "0.93\n" {
		t.Errorf("objects: %v", keys(f.objects))
	}
}

func TestRunRestoresCheckpointsAndSaysItResumed(t *testing.T) {
	f := newFakeKrater(t)
	f.resumed = true
	f.objects["checkpoints/model/step-100.pt"] = []byte("weights")
	f.command = `test "$KRATER_RESUMED" = 1 && test "$(cat "$KRATER_CHECKPOINT_DIR/model/step-100.pt")" = weights`
	cfg := f.cfg(t)

	code, err := Run(context.Background(), cfg, http.DefaultClient, http.DefaultClient, noGPU)

	if err != nil || code != 0 {
		t.Fatalf("Run = %d, %v; want 0, nil", code, err)
	}
}

func TestBigFilesGoUpInParts(t *testing.T) {
	f := newFakeKrater(t)
	f.partBytes = 10
	cfg := f.cfg(t)
	os.MkdirAll(cfg.CheckpointDir, 0o755)
	content := strings.Repeat("0123456789", 3) + "xyz" // 33 bytes: 4 parts
	os.WriteFile(filepath.Join(cfg.CheckpointDir, "big.bin"), []byte(content), 0o644)
	os.WriteFile(filepath.Join(cfg.CheckpointDir, "half-written.pt.tmp"), []byte("x"), 0o644)
	api := &API{BaseURL: f.server.URL, Token: f.token, HTTP: http.DefaultClient}
	s := NewSyncer(api, "checkpoints", cfg.CheckpointDir, http.DefaultClient)

	n, err := s.Push(context.Background())

	if err != nil || n != 1 {
		t.Fatalf("Push = %d, %v; want 1, nil", n, err)
	}
	if string(f.objects["checkpoints/big.bin"]) != content {
		t.Errorf("stored %q", f.objects["checkpoints/big.bin"])
	}
	// Unchanged files aren't uploaded again.
	if n, _ := s.Push(context.Background()); n != 0 {
		t.Errorf("second push uploaded %d files", n)
	}
}

func TestKraterCanStopTheJob(t *testing.T) {
	f := newFakeKrater(t)
	f.stopAfter = 2
	f.command = "sleep 60"
	cfg := f.cfg(t)
	start := time.Now()

	code, err := Run(context.Background(), cfg, http.DefaultClient, http.DefaultClient, func() (float64, bool) { return 87, true })

	if err != nil {
		t.Fatal(err)
	}
	if code != 128+15 {
		t.Errorf("exit code %d, want SIGTERM's 143", code)
	}
	if time.Since(start) > 20*time.Second {
		t.Errorf("stopping took %s", time.Since(start))
	}
	if f.heartbeats[0]["gpu_util"] != 87.0 {
		t.Errorf("heartbeat: %v", f.heartbeats[0])
	}
}

func TestADeniedTokenGivesUpAtOnce(t *testing.T) {
	f := newFakeKrater(t)
	cfg := f.cfg(t)
	cfg.Token = "wrong"

	_, err := Run(context.Background(), cfg, http.DefaultClient, http.DefaultClient, noGPU)

	if err == nil || !strings.Contains(err.Error(), "refused") {
		t.Fatalf("err = %v", err)
	}
}

func TestStoredNamesCannotEscapeTheDirectory(t *testing.T) {
	s := NewSyncer(nil, "checkpoints", "/krater/checkpoints", nil)
	for _, name := range []string{"../x", "a/../../x", "/etc/passwd", ".."} {
		if _, err := s.localPath(name); err == nil {
			t.Errorf("%q was allowed", name)
		}
	}
	if p, err := s.localPath("a/b.pt"); err != nil || p != filepath.Join("/krater/checkpoints", "a", "b.pt") {
		t.Errorf("a/b.pt -> %q, %v", p, err)
	}
}

func TestMeanOfNvidiaSMILines(t *testing.T) {
	if v, ok := meanOfLines("90\n70\n"); !ok || v != 80 {
		t.Errorf("got %v %v", v, ok)
	}
	if _, ok := meanOfLines("[N/A]\n"); ok {
		t.Error("N/A parsed")
	}
}

func keys(m map[string][]byte) []string {
	var out []string
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
