package main

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// The dead-man switch. Krater ends every machine it rents, but only while it is up: on staging (2026-10-14) Krater
// went down with a box running, and the box billed for four hours with nobody watching. So a runner that hears
// nothing from Krater for long enough stops its own machine through Vast's API, with the key Vast gives every
// container for its own instance (CONTAINER_API_KEY: it can start, stop or destroy that one instance and nothing
// else). It stops rather than destroys: GPU billing ends, and the disk keeps whatever the job or the member did while
// Krater was away, since nothing reaches storage without Krater. Back up, Krater starts a job's machine again (the job
// carries on from the checkpoints on its disk) and shows a box as stopped, for its member to resume.

// DefaultSelfStopAfter is the silence that stops the machine until Krater's /start says otherwise (its
// `runner_self_stop_minutes` policy). It also covers a runner whose /start never got through.
const DefaultSelfStopAfter = 3 * time.Hour

// DefaultVastURL is where Vast's API is, unless /start names another.
const DefaultVastURL = "https://console.vast.ai"

// Contact is when Krater last answered this runner, whatever it said: a refusal is an answer too. A gateway error
// (Krater down behind its proxy) or no connection at all is not.
type Contact struct {
	mu   sync.Mutex
	last time.Time
}

// NewContact starts the clock at `now`, so a runner that never reaches Krater still stops its machine in time.
func NewContact(now time.Time) *Contact { return &Contact{last: now} }

// Touch records an answer.
func (c *Contact) Touch(at time.Time) {
	if c == nil {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if at.After(c.last) {
		c.last = at
	}
}

// Last is when Krater last answered.
func (c *Contact) Last() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.last
}

// SelfStop stops this machine after `after` without an answer from Krater.
type SelfStop struct {
	Contact *Contact
	HTTP    *http.Client
	// Now is the clock; tests replace it.
	Now func() time.Time
	// Env reads a variable of the container's; tests replace it. Vast sets CONTAINER_API_KEY and CONTAINER_ID in the
	// container's environment, which a process started from `onstart` may not inherit, so the default also reads
	// /proc/1/environ.
	Env func(name string) string

	mu         sync.Mutex
	after      time.Duration
	instanceID int64
	vastURL    string
	before     func()
	fired      atomic.Bool
	stopped    atomic.Bool
	warnedKey  bool
}

// NewSelfStop is a switch with the defaults; Configure fills in what /start says.
func NewSelfStop(httpClient *http.Client) *SelfStop {
	return &SelfStop{
		Contact: NewContact(time.Now()),
		HTTP:    httpClient,
		Now:     time.Now,
		Env:     containerEnv,
		after:   DefaultSelfStopAfter,
		vastURL: DefaultVastURL,
	}
}

// Configure takes the silence, the instance and Vast's address from Krater's answer to /start.
func (s *SelfStop) Configure(info *StartInfo) {
	if s == nil || info == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if info.SelfStopAfterSeconds > 0 {
		s.after = time.Duration(info.SelfStopAfterSeconds) * time.Second
	}
	if info.VastInstanceID > 0 {
		s.instanceID = info.VastInstanceID
	}
	if info.VastAPIURL != "" {
		s.vastURL = info.VastAPIURL
	}
}

// Before sets what to do before the machine stops: a job stops its command, so it can save a last checkpoint.
func (s *SelfStop) Before(f func()) {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.before = f
}

// Fired reports whether the machine is stopping itself: the runner must then not report the command's end to Krater,
// which would take the stop for the job's own exit.
func (s *SelfStop) Fired() bool { return s != nil && s.fired.Load() }

// Run checks every `every` until ctx ends.
func (s *SelfStop) Run(ctx context.Context, every time.Duration) {
	ticker := time.NewTicker(every)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			s.Check(ctx)
		}
	}
}

// Check stops the machine if Krater has been silent too long. Once it has fired it keeps asking Vast until the stop
// goes through (the machine, and this process with it, then stops).
func (s *SelfStop) Check(ctx context.Context) {
	if s.stopped.Load() {
		return
	}
	s.mu.Lock()
	after, before := s.after, s.before
	s.mu.Unlock()
	silent := s.Now().Sub(s.Contact.Last())
	if !s.fired.Load() {
		if silent < after {
			return
		}
		s.fired.Store(true)
		logf("no answer from Krater for %s: stopping this machine so it doesn't bill with nobody watching; "+
			"its disk keeps the work, and Krater picks it up when it's back", silent.Round(time.Minute))
		if before != nil {
			before()
		}
	}
	if err := s.stopMachine(ctx); err != nil {
		logf("could not stop this machine: %v; trying again in a minute", err)
		return
	}
	s.stopped.Store(true)
	logf("asked Vast to stop this machine")
}

func (s *SelfStop) stopMachine(ctx context.Context) error {
	key := s.Env("CONTAINER_API_KEY")
	if key == "" {
		s.mu.Lock()
		warned := s.warnedKey
		s.warnedKey = true
		s.mu.Unlock()
		if !warned {
			return fmt.Errorf("Vast gave this container no CONTAINER_API_KEY")
		}
		return fmt.Errorf("still no CONTAINER_API_KEY")
	}
	s.mu.Lock()
	id, base := s.instanceID, s.vastURL
	s.mu.Unlock()
	if id <= 0 {
		parsed, err := strconv.ParseInt(s.Env("CONTAINER_ID"), 10, 64)
		if err != nil || parsed <= 0 {
			return fmt.Errorf("this machine's Vast instance id is unknown")
		}
		id = parsed
	}
	// Every Vast path ends with a slash: without it Vast answers 308 and does nothing.
	url := fmt.Sprintf("%s/api/v0/instances/%d/", strings.TrimRight(base, "/"), id)
	req, err := http.NewRequestWithContext(ctx, http.MethodPut, url, bytes.NewReader([]byte(`{"state": "stopped"}`)))
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+key)
	req.Header.Set("Content-Type", "application/json")
	resp, err := s.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	if resp.StatusCode/100 != 2 || bytes.Contains(data, []byte(`"success": false`)) ||
		bytes.Contains(data, []byte(`"success":false`)) {
		return fmt.Errorf("Vast answered %d: %s", resp.StatusCode, truncate(string(data), 200))
	}
	return nil
}

// containerEnv is the variable from this process's environment or, failing that, the container's main process's.
func containerEnv(name string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	data, err := os.ReadFile("/proc/1/environ")
	if err != nil {
		return ""
	}
	for _, entry := range bytes.Split(data, []byte{0}) {
		if k, v, ok := strings.Cut(string(entry), "="); ok && k == name {
			return v
		}
	}
	return ""
}
