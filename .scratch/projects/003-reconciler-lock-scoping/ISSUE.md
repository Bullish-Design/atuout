# The reconciler's single-instance lock is scoped to the wrong thing

**Status:** open, unfixed. Found 2026-08-24 while salvaging
`tests/test_reconciler_process.py` onto `main`. Not caused by that work.

**One line:** the lock lives in `XDG_RUNTIME_DIR`, the database lives in
`XDG_DATA_HOME`, and nothing keeps the two in agreement — so a shell that
overrides `XDG_RUNTIME_DIR` gets a second reconciler writing the first one's
database.

---

## 1. What was observed

Two `atuout reconcile --daemonize` processes were running at once on the
development machine:

| PID | Started | PPID | `XDG_RUNTIME_DIR` | cwd |
|---|---|---|---|---|
| 1297 | Aug 14 20:26 | 1034 (`systemd --user`) | `/run/user/1000` | `/home/andrew` |
| 1768083 | Aug 20 19:28 | 1 (orphaned) | `/tmp/kcl-xdg` | `~/Documents/ideas/kitty-context-layers` |

Their open file descriptors are the whole bug:

```
PID 1297      /home/andrew/.local/share/atuout/atuout.db      <- same database
              /run/user/1000/atuout/atuout-reconciler.lock    <- different lock

PID 1768083   /home/andrew/.local/share/atuout/atuout.db      <- same database
              /tmp/kcl-xdg/atuout/atuout-reconciler.lock      <- different lock
```

**Same database, different locks.** The single-instance guard did exactly what
it was written to do and still permitted two writers.

PID 1768083 was terminated with `SIGTERM` on 2026-08-24. It exited cleanly and
removed its pidfile, so nothing here is a shutdown problem. It had burned
**4 h 59 m of CPU in 3 d 19 h** duplicating the work of PID 1297.

## 2. The mechanism

Three resolvers in `src/atuout/settings.py` answer to three different
environment variables:

| Function | Follows | Used for |
|---|---|---|
| `runtime_dir()` | `XDG_RUNTIME_DIR`, else `/run/user/<uid>`, else `state_dir()` | the pidfile and the **lock** |
| `db_path()` | `ATUOUT_DB_PATH`, else `ATUOUT_DATA_DIR`, else `XDG_DATA_HOME` | the **database** |
| `state_dir()` | `ATUOUT_STATE_DIR`, else `XDG_STATE_HOME` | logs |

`reconciler.lockfile_path()` and `pidfile_path()` both build on `runtime_dir()`.
`_run_loop()` and `_agent_retry_loop()` both open `db_path()`.

So the guard is keyed to the **runtime** directory while the resource it exists
to protect is keyed to the **data** directory. Change one without the other and
the guard silently stops guarding:

1. A shell sets `XDG_RUNTIME_DIR=/tmp/kcl-xdg` for its own unrelated reasons.
2. `init-zsh` runs `atuout reconcile ensure`.
3. `ensure()` calls `is_running()`, which probes
   `/tmp/kcl-xdg/atuout/atuout-reconciler.lock`. Nobody holds it.
4. `ensure()` spawns a second reconciler. It opens
   `~/.local/share/atuout/atuout.db` — the same database the first one is
   tailing into.

Nothing in the sequence is a race. It is deterministic and reproducible.

## 3. What it costs

**Not data corruption.** `store.upsert_recording()` is `INSERT OR IGNORE` on the
`atuin_id` primary key, so a duplicated backfill is a no-op, not a double row.
That is why this went unnoticed for months.

What it does cost:

- **Duplicated work, indefinitely.** Two `TailHistory` gRPC streams, two agent
  retry loops, two 15-minute agent sweeps, two sets of transcript parsing. About
  5 CPU-hours in under 4 days, in the observed case.
- **Write contention on one SQLite file.** Two processes committing to the same
  WAL. Tolerable, but it is contention nobody designed for.
- **A first-boot crash window between processes.** `run()` now creates the DB on
  the main thread before its workers connect (`6046f4f`), which fixes the race
  *between threads of one process*. Two **processes** starting against a
  not-yet-existing database hit the same `PRAGMA journal_mode=WAL` →
  `database is locked` failure, and that fix does not reach across processes.
  Measured for the thread case: 37/200 fresh, 0/200 existing.
- **The duplicate is unmanageable from a normal shell.** `stop()` reads the
  pidfile from `runtime_dir()`, so `atuout reconcile stop` in a normal session
  only ever signals the reconciler in *its own* runtime dir. The other one is
  invisible to `status` and untouchable by `stop`. It survives the shell that
  spawned it, and is reparented to init — PID 1768083 outlived its sandbox by
  days.

## 4. Why it was written this way

`.scratch/projects/001-native-refactor/IMPLEMENTATION-GUIDE.md` (lines ~346-356)
states the intent:

> Pidfile: `${XDG_RUNTIME_DIR:-~/.local/share/atuout}/atuout-reconciler.pid`.
> Prefer `$XDG_RUNTIME_DIR` (tmpfs, auto-cleaned on logout) if set

The reasoning is sound on its own terms: a tmpfs pidfile cannot go stale across
a reboot. **The gap is that the guide never says which resource the lock
defends.** It reads as "one reconciler per login session", and the code
implements that faithfully. The actual invariant the program needs is **one
reconciler per database**.

Note also that the tmpfs argument is not load-bearing for correctness. The real
guard is `fcntl.flock`, which the kernel releases when the holding process dies,
however it dies. A leftover lock *file* is harmless — `test_crash_restart`
already covers exactly this (SIGKILL leaves a stale pidfile; `is_running()` is
lock-based and correctly reports False).

## 5. Options

**A. Key the lock to the resolved database path.** Put the lock and pidfile next
to the database, or hash `db_path()` into their names. Same DB → same lock.
Different DB → different lock, automatically and correctly.

- Gets the semantics exactly right, including the cases we *want* isolated: a
  test or sandbox that sets `ATUOUT_DB_PATH` genuinely should have its own
  reconciler, and this gives it one without special-casing.
- Compatible with the existing suite as written. `tests/test_reconciler_process
  .py`'s `runtime_env` fixture already sets `ATUOUT_DB_PATH` **and**
  `XDG_RUNTIME_DIR` per test, so isolation survives either keying.
- Costs the tmpfs auto-clean property. See above — flock does not need it.
- Caveat worth checking before committing: `flock` semantics are unreliable on
  network filesystems. `~/.local/share` is local here, but `ATUOUT_DATA_DIR`
  could point anywhere.

**B. Keep the runtime-dir lock, add a second lock beside the database.** The
runtime lock stays as the cheap same-session guard; the data lock is the real
invariant. Belt and braces, two things to reason about instead of one.

**C. Make `ensure()` compare more than the lock.** For example, refuse to spawn
if any live process already has this database open. Correct but expensive and
platform-specific; it inspects the system instead of asserting an invariant.

**Recommendation: A.** The lock should live where the resource lives. B is a
reasonable fallback if the network-filesystem caveat turns out to matter.

## 6. How to reproduce

The read-only half was verified on 2026-08-24, same machine, same moment, with
PID 1297 running:

```
$ atuout reconcile status
reconciler: running (pid=1297)

$ XDG_RUNTIME_DIR=/tmp/scratch-xdg atuout reconcile status
reconciler: stopped (pid=None)
```

Two answers about one machine. The second is what `ensure()` sees before it
decides to spawn.

**A side effect worth fixing at the same time:** that `status` call is
documented as read-only but is not. `is_running()` calls `_acquire_lock()`,
which does `runtime_dir().mkdir(parents=True, exist_ok=True)` and opens the
lockfile with mode `"w"`. Probing therefore **creates**
`/tmp/scratch-xdg/atuout/atuout-reconciler.lock`, and truncates an existing
lockfile. Truncation is harmless to `flock` (the lock is on the inode, not the
contents) but a status query should not create directories.

The full reproduction, which does spawn a second reconciler:

```sh
# terminal 1 — the normal session
atuout reconcile ensure
atuout reconcile status          # running, pid N

# terminal 2 — any shell that overrides the runtime dir
XDG_RUNTIME_DIR=/tmp/scratch-xdg atuout reconcile status
                                 # reports "stopped" — it is looking in the wrong place
XDG_RUNTIME_DIR=/tmp/scratch-xdg atuout reconcile ensure
                                 # spawns a SECOND reconciler on the SAME database

ps -ef | grep '[r]econcile --daemonize'     # two processes
ls -l /proc/<pid>/fd | grep atuout.db       # both have the same DB open
```

Confirm the divergence directly:

```sh
tr '\0' '\n' < /proc/<pid>/environ | grep XDG_RUNTIME_DIR
ls -l /proc/<pid>/fd | grep -E 'atuout\.db|\.lock'
```

## 7. What a fix should prove

- Two `ensure` calls with different `XDG_RUNTIME_DIR` and the **same** resolved
  `db_path()` produce **one** reconciler.
- Two `ensure` calls with different `ATUOUT_DB_PATH` still produce **two**, one
  per database — the isolation the test suite depends on.
- `atuout reconcile stop` from a shell with a different `XDG_RUNTIME_DIR` stops
  the reconciler that holds *this* database.
- `test_crash_restart` still passes: SIGKILL leaves a stale pidfile, and
  `is_running()` still reports False from the lock.
- Two processes started simultaneously against a **non-existent** database do
  not leave either one dead (§3's cross-process variant of the WAL race).
- `atuout reconcile status` creates no directory and no lockfile (§6).

The natural home for these is `tests/test_reconciler_process.py`, which already
spawns real detached reconcilers and controls both variables per test.

## 8. Related

- `6046f4f` — the intra-process version of the WAL race, fixed. Same failure
  signature, different scope.
- `1a81540` — `tests/test_reconciler_process.py`, which found it.
- `.scratch/projects/001-native-refactor/IMPLEMENTATION-GUIDE.md` §single-instance
  guard — the original design, and the sentence that should say "per database".
- `.scratch/projects/001-native-refactor/PLAN-reconciler-process-test.md` — the
  plan the salvaged test came from.
