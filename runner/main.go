// Command krater-runner runs a Krater Compute job on a rented Vast machine.
//
// Krater's bootstrap script downloads it, checks its sha256 and starts it. It then:
//
//  1. tells Krater it started (POST /runner/v1/start) and learns the command and whether this is a resume;
//  2. restores the job's checkpoints into $KRATER_CHECKPOINT_DIR;
//  3. runs the member's command with `sh -c`;
//  4. sends a heartbeat every minute with the GPU use it samples from nvidia-smi, and stops the command when Krater
//     says so;
//  5. uploads changed checkpoints on a timer, and checkpoints and outputs when the command exits;
//  6. reports the exit code. Krater then destroys the machine.
//
// The runner is not the container's main process: the bootstrap shell is, and it waits forever after the runner
// returns, because Vast restarts a container whose main process exits (and keeps billing). Only Krater ends a machine.
//
// It holds one secret, the instance's runner token ($KRATER_JOB_TOKEN), which works for this instance only. Storage
// is reached only through presigned URLs Krater hands out for this job.
package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/exec"
	"sync"
	"syscall"
	"time"
)

var version = "dev"

func logf(format string, args ...any) { log.Printf("krater-runner: "+format, args...) }

// Config is what the runner reads from its environment.
type Config struct {
	RunnerURL     string
	Token         string
	CheckpointDir string
	OutputDir     string
}

func configFromEnv() (Config, error) {
	cfg := Config{
		RunnerURL:     os.Getenv("KRATER_RUNNER_URL"),
		Token:         os.Getenv("KRATER_JOB_TOKEN"),
		CheckpointDir: envOr("KRATER_CHECKPOINT_DIR", "/krater/checkpoints"),
		OutputDir:     envOr("KRATER_OUTPUT_DIR", "/krater/outputs"),
	}
	if cfg.RunnerURL == "" || cfg.Token == "" {
		return cfg, errors.New("KRATER_RUNNER_URL and KRATER_JOB_TOKEN must be set")
	}
	return cfg, nil
}

func envOr(name, fallback string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return fallback
}

func main() {
	log.SetFlags(log.LstdFlags | log.LUTC)
	logf("version %s", version)
	cfg, err := configFromEnv()
	if err != nil {
		logf("%v", err)
		return
	}
	code, err := Run(context.Background(), cfg, &http.Client{Timeout: 60 * time.Second}, &http.Client{}, nvidiaSMI)
	if err != nil {
		logf("%v", err)
	}
	logf("done (exit code %d); Krater stops this machine", code)
}

// Run does the whole job and returns the command's exit code. `api` and `storage` are separate clients: storage
// requests can take far longer than API calls.
func Run(ctx context.Context, cfg Config, apiHTTP, storageHTTP *http.Client, gpu func() (float64, bool)) (int, error) {
	api := &API{BaseURL: cfg.RunnerURL, Token: cfg.Token, HTTP: apiHTTP}
	var info *StartInfo
	err := retry(ctx, 10*time.Minute, func() error {
		var err error
		info, err = api.Start(ctx)
		return err
	})
	if err != nil {
		return -1, fmt.Errorf("could not start: %w", err)
	}
	logf("job %s, attempt %d, resumed=%v", info.JobID, info.Attempt, info.Resumed)

	for _, dir := range []string{cfg.CheckpointDir, cfg.OutputDir} {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return -1, err
		}
	}
	checkpoints := NewSyncer(api, "checkpoints", cfg.CheckpointDir, storageHTTP)
	outputs := NewSyncer(api, "outputs", cfg.OutputDir, storageHTTP)
	if n, err := checkpoints.Restore(ctx); err != nil {
		logf("restoring checkpoints failed: %v", err)
	} else if n > 0 {
		logf("restored %d checkpoint files", n)
	}

	if info.Command == "" {
		// A box with nothing to run: keep reporting until Krater stops it.
		heartbeatLoop(ctx, api, seconds(info.HeartbeatSeconds, 60), gpu, nil)
		return 0, nil
	}

	cmd := exec.Command("sh", "-c", info.Command)
	cmd.Stdout, cmd.Stderr = os.Stdout, os.Stderr
	cmd.Env = append(os.Environ(),
		"KRATER_RESUMED="+boolEnv(info.Resumed),
		"KRATER_CHECKPOINT_DIR="+cfg.CheckpointDir,
		"KRATER_OUTPUT_DIR="+cfg.OutputDir,
	)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := cmd.Start(); err != nil {
		_ = api.Exit(ctx, 127)
		return 127, fmt.Errorf("could not start the command: %w", err)
	}

	done := make(chan struct{})
	var stopOnce sync.Once
	stop := func() {
		stopOnce.Do(func() {
			logf("Krater asked to stop the job")
			_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
			go func() {
				select {
				case <-done:
				case <-time.After(30 * time.Second):
					_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
				}
			}()
		})
	}
	loops, cancelLoops := context.WithCancel(ctx)
	var wg sync.WaitGroup
	wg.Add(2)
	go func() { defer wg.Done(); heartbeatLoop(loops, api, seconds(info.HeartbeatSeconds, 60), gpu, stop) }()
	go func() {
		defer wg.Done()
		syncLoop(loops, checkpoints, seconds(info.CheckpointSyncSeconds, 600))
	}()

	code := exitCode(cmd.Wait())
	close(done)
	cancelLoops()
	wg.Wait()
	logf("the command exited with code %d", code)

	for _, s := range []*Syncer{checkpoints, outputs} {
		if n, err := s.Push(ctx); err != nil {
			logf("final upload of %s failed: %v", s.Area, err)
		} else if n > 0 {
			logf("uploaded %d %s files", n, s.Area)
		}
	}
	if err := retry(ctx, 10*time.Minute, func() error { return api.Exit(ctx, code) }); err != nil {
		return code, fmt.Errorf("could not report the exit code: %w", err)
	}
	return code, nil
}

func heartbeatLoop(ctx context.Context, api *API, every time.Duration, gpu func() (float64, bool), stop func()) {
	sampler := newGPUSampler(gpu)
	sampler.take() // so the first heartbeat already carries a sample
	go sampler.run(ctx, 15*time.Second)
	ticker := time.NewTicker(every)
	defer ticker.Stop()
	for {
		beat := map[string]any{"phase": "running"}
		if util, ok := sampler.average(); ok {
			beat["gpu_util"] = util
		}
		shouldStop, err := api.Heartbeat(ctx, beat)
		var denied *ErrDenied
		switch {
		case errors.As(err, &denied):
			shouldStop = true
		case err != nil && ctx.Err() == nil:
			logf("heartbeat failed: %v", err)
		}
		if shouldStop {
			if stop != nil {
				stop()
			} else {
				return
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

func syncLoop(ctx context.Context, s *Syncer, every time.Duration) {
	ticker := time.NewTicker(every)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if n, err := s.Push(ctx); err != nil && ctx.Err() == nil {
				logf("checkpoint upload failed: %v", err)
			} else if n > 0 {
				logf("uploaded %d checkpoint files", n)
			}
		}
	}
}

func exitCode(err error) int {
	if err == nil {
		return 0
	}
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		if status, ok := exitErr.Sys().(syscall.WaitStatus); ok && status.Signaled() {
			return 128 + int(status.Signal())
		}
		return exitErr.ExitCode()
	}
	return -1
}

func boolEnv(b bool) string {
	if b {
		return "1"
	}
	return "0"
}

func seconds(n, fallback int) time.Duration {
	if n <= 0 {
		n = fallback
	}
	return time.Duration(n) * time.Second
}
