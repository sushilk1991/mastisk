# Safe Restart Implementation Plan

**Goal:** Restart only after active scheduled runs and HTTP-triggered work finish and persist results. A timeout or interrupted CLI cancels the restart without killing work.

**Architecture:** A per-app admission gate counts entire scheduled invocations and HTTP requests, including response background tasks. A private Unix socket lets the local CLI hold a drain session: pause scheduling, reject new requests with 503, wait for zero active work, then restart launchd while keeping admission closed. Socket disconnect resumes work automatically. The passive feed SSE stream is excluded from counting. Claude/Codex bridges remain unchanged.

**Tech stack:** Python, FastAPI/ASGI, APScheduler, asyncio Unix sockets, launchctl, pytest.

## Approved behavior

- Wait for whole runs, not just subprocess exit or an empty jobs table.
- Keep current runs alive through draining; never cancel them to meet a restart timeout.
- Default drain timeout: 30 minutes. On timeout, resume admission and scheduling.
- Permit one drain owner; other restart attempts fail closed.
- Control stays outside HTTP, in a user-only Unix socket directory.
- Refuse old daemons without drain support; no force-restart fallback.
- Check launchd identity and executable before stopping anything; verify a new daemon identity and HTTP health after restart.
- Route updater restarts through the same protocol. Do not silently treat a failed restart as success.

## 1. Admission gate

Create `src/mastisk/runtime.py` and `tests/test_runtime.py`.

1. Write tests for active scheduled work, blocked late starts, HTTP 503 admission, background result persistence, synchronous jobs, and passive SSE exclusion.
2. Run `.venv/bin/python -m pytest tests/test_runtime.py -q`; observe missing-feature failures.
3. Implement a thread-safe private gate, synchronous/asynchronous job wrappers, and ASGI admission middleware.
4. Rerun until green. Test error paths release counts without swallowing errors.

## 2. Drain transport and restart driver

Create `src/mastisk/restart_control.py` and `tests/test_restart_control.py`.

1. Test real Unix-socket drain sessions with an event-controlled run. Assert readiness follows persistence, disconnect restores scheduling, concurrent drain fails, and directory/socket permissions are private.
2. Test restart driver boundaries with fake launchctl: no restart while busy, timeout never invokes kickstart, identity mismatch and unsupported daemon fail closed, launchctl failure propagates, and replacement PID/health must be verified.
3. Observe red, implement the protocol and OS driver, then rerun until green.

## 3. Daemon and CLI integration

Modify `src/mastisk/app.py`, `src/mastisk/scheduler.py`, `src/mastisk/cli.py`, and `install.sh`. Add integration tests before implementation.

1. Give each app one gate. Pass it to a tracked scheduler; start the socket in lifespan and clean it up at shutdown.
2. Register `mastisk restart --timeout SECONDS`; reject unsupported/unloaded services rather than forcing a restart.
3. Replace CLI update's kickstart and install.sh update's kickstart with safe restart. Retain explicit `--skip-restart` behavior.
4. Test scheduler/HTTP integration, CLI errors, and updater wiring.

## 4. Verification and documentation

1. Run focused runtime, transport, scheduler, CLI, tunnel-scope, and CORS tests; run Ruff on changed Python files and `bash -n install.sh`.
2. Document drain semantics, temporary 503 responses, timeout recovery, and first-deployment limits in README. Point agent restart instructions at the safe command.
3. Preserve the existing verified Tailscale setup and unrelated working-tree changes.
4. Do not reinstall or restart the current pre-feature daemon as a test: it cannot participate in the new protocol. Initial deployment requires a separately approved maintenance window.

## Session state

Tailscale setup is complete: TLS-terminated TCP 8443 to localhost:5555, hostname allowlisted, page/API verified 200, proxy-marked requests still 404, Mobius routes unchanged. README and CLAUDE changes for that task are uncommitted. Safe-restart design approved by the user: let runs finish before restarting.

Implementation and focused verification are complete in this checkout. The gate, private socket protocol, scheduler/HTTP integration, restart CLI, and both updater paths are implemented. Real-child-process coverage verifies normal child exit and parent result persistence before restart. Final focused run: 84 tests passed in 4.41 seconds; new Python files and app lint clean; CLI/scheduler have no new violations against 35 baseline violations; shell syntax and diff checks pass. The production local API and tailnet endpoint still return 200.

Full-suite execution remains blocked: boxrun reports the shared dev box suspended, and instabox wake reports an operation conflict. No cloud suite ran.

## Approved deployment completed

The user approved the initial maintenance rollout. Immediately before stopping the old daemon, read-only checks showed no running jobs or daemon child processes. The first reinstall selected Homebrew Python 3.15; `/api/sidebar` returned 500 because that interpreter could not read the iCloud vault (`PermissionError`). Reinstalling with the previous Python 3.14 interpreter and lockfile-constrained dependencies restored API access.

Added a red/green regression for interpreter preservation: private control status now reports the daemon's resolved Python path, and the updater pins that path even when the checkout CLI uses a different interpreter. Final build was installed under a safe idle drain and the replacement daemon passed health checks. Final focused run: 84 tests passed in 4.32 seconds.

Production verification: installed app/scheduler/CLI/runtime/control source matches the checkout; Python 3.14, launchd identity, and private socket permissions verified; local API and TLS-verified tailnet page/API return 200; proxy-marked API requests still return 404; config, launchd plist, and all Serve routes match pre-rollout fingerprints. Mobile URL: https://sushils-macbook-pro.tailec4b87.ts.net:8443/ . Phone-side browser verification remains the user's next step. No commits or pushes made.
