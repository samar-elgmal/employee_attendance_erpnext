# Session-Based Flex Attendance Attribution — Design

## Problem

`process_employee_for_date` (`daily_job.py`) attributes a Flexible Hours
employee's working hours to a date by fetching all Employee Checkins in the
window `date 00:00 → (date+1) 12:00` and pairing them **positionally**
(1st+2nd, 3rd+4th, ...), then truncating a trailing odd entry. This is not
session-aware: it doesn't know where one shift ends and the next begins, only
where the window happens to end.

Concretely, this silently misattributes hours whenever a checkin count
happens to work out even. Example:

```
1/7 09:00 IN
1/7 17:00 OUT      (real 1/7 session, 8h)

2/7 05:00 IN
2/7 07:00 OUT       (real 2/7 session, 2h — unrelated to 1/7's shift,
                      just an early second job that day)
```

Both fall inside 1/7's window (window end is noon 2/7). Since the total count
is even (4), no trailing entry is dropped, so today's naive pairing computes
`(09:00,17:00)=8h` **and** `(05:00,07:00)=2h`, attributing all 10h to 1/7.
The 2/7 checkins get linked to 1/7's Attendance
(`update_attendance_in_checkins`), so when 2/7 is processed the next day, its
own genuine session is already claimed — 2/7 shows Absent even though the
employee worked.

The fix: pair checkins into sessions first, *then* decide which date each
session belongs to (its IN date) — never truncate by window position before
pairing is complete.

## Goals

- A session's hours are always attributed to its **IN date**, regardless of
  checkin count parity in any day's fetch window.
- Support multiple sessions per day and overnight (and multiple-overnight)
  sessions without special-casing.
- Never double-claim or lose a checkin across two days' runs.
- Tolerate device noise (duplicate punches) without desyncing the rest of a
  window's pairing.
- Detect (not silently miscompute) a pairing desync that noise-dedup didn't
  catch, and contain its blast radius to the point it occurs.
- Keep the existing per-`(employee, date)` entrypoint, scheduler contract,
  and `reprocess_all` / `reset_and_reprocess` signatures unchanged — this is
  a targeted algorithm fix, not a pipeline rewrite.

## Non-goals

- No global, cross-day session-building pipeline (`fetch → pair → group by
  date → create all attendance` in one pass). Rejected in favor of keeping
  the existing daily-scheduler-per-date architecture (see
  `ARCHITECTURE.md` Design Decision #1) — smaller diff, no change to how
  the scheduler, `reprocess_all`, or `reset_and_reprocess` are invoked.
- No validation against `Employee Checkin.log_type`. It is not trustworthy
  on this system's devices (this is *why* the existing code already uses
  HRMS's "Alternating entries" pairing mode instead of "Strictly based on
  Log Type") — no logic here may assume it's accurate.
- No new doctype for anomaly tracking. Reuses `frappe.log_error`, consistent
  with existing per-employee error handling in `process_flex_attendance`.
- No break-time/overtime rules. Out of scope; the `Session` object is
  introduced partly because it makes such rules easy to add *later*, not
  because they're needed now.

## Design

### Session

Replace the flat "list of checkin logs" that flows through the pipeline with
an explicit unit:

```python
class Session(NamedTuple):
    in_log: dict       # Employee Checkin row (name, time)
    out_log: dict
    duration: float     # hours
    attendance_date: date   # in_log.time's date — the attribution key
    crosses_midnight: bool  # out_log.time.date() != in_log.time.date()
```

`crosses_midnight` isn't consumed by any logic yet — it's carried for future
reporting (e.g. flagging overnight sessions in the weekly summary report)
now that it's nearly free to compute during session-building.

Everything downstream of pairing (totals, `in_time`/`out_time`, which
checkins get claimed) operates on `Session` objects, not raw logs.

### Window (`_get_checkins`)

```python
day_start = date 00:00
grace_hours = Flexible Hours Settings.checkin_window_grace_hours  # default 12
day_end = (date + 1 day) + grace_hours
```

New Single field: `Flexible Hours Settings.checkin_window_grace_hours` (Int,
default `12`). At the default, `day_end` is `(date+1) 12:00` — identical to
today's hardcoded noon cutoff, so behavior doesn't get wider by default; it's
just no longer a magic constant. Ops can raise it if a real shift legitimately
needs a longer window, without a code change.

Correctness no longer depends on this bound being tight — pairing happens
before date-filtering (see below), so a same-day-but-later next-day session
inside the window can never be merged into today's total by coincidence. The
setting exists purely to bound how much data a single run reads.

Same filters as today: `employee`, `time between [day_start, day_end]`,
`skip_auto_attendance=1`, `attendance: not set`, ordered by `time asc`.

### Dedup (`_dedupe_checkins`)

```python
DEDUP_THRESHOLD_SECONDS = 60

def _dedupe_checkins(logs, threshold_seconds=DEDUP_THRESHOLD_SECONDS):
    kept = []
    for log in logs:
        if kept and (log.time - kept[-1].time) <= timedelta(seconds=threshold_seconds):
            continue
        kept.append(log)
    return kept
```

Collapses **any** two consecutive checkins within 60 seconds into one (keep
the first), regardless of `log_type` — since `log_type` can't be trusted to
tell a true duplicate scan from a real fast IN/OUT, a type-agnostic gap
check is the only reliable signal available. Narrowed from an earlier 2-minute
draft to 60 seconds: true device double-scans happen within seconds, and a
wider threshold risks eating a real, if unusually quick, punch. Runs once,
immediately after fetch, before pairing.

### Build Sessions (`_build_sessions`)

```python
MIN_SESSION_HOURS = 5 / 60   # 5 minutes — anomaly signal only, not a hard rule

def _is_plausible(duration, max_session_hours):
    return MIN_SESSION_HOURS <= duration <= max_session_hours

def _build_sessions(logs, employee):
    max_session_hours = frappe.get_single("Flexible Hours Settings").max_session_hours
    sessions = []
    anomalies = []
    i = 0
    while i + 1 < len(logs):
        in_log, out_log = logs[i], logs[i + 1]
        duration = time_diff_in_hours(in_log.time, out_log.time)

        if _is_plausible(duration, max_session_hours):
            sessions.append(_make_session(in_log, out_log, duration))
            i += 2
            continue

        anomalies.append((in_log, out_log, duration))

        # Bounded resync: assume in_log was a stray extra punch dedup
        # didn't catch, and try pairing out_log against the *next* log
        # instead. If that's plausible, the rest of the window recovers
        # normally. If it's also implausible, this isn't one stray punch —
        # stop rather than keep guessing.
        if i + 2 < len(logs):
            retry_out = logs[i + 2]
            retry_duration = time_diff_in_hours(out_log.time, retry_out.time)
            if _is_plausible(retry_duration, max_session_hours):
                sessions.append(_make_session(out_log, retry_out, retry_duration))
                i += 3
                continue

        break

    if anomalies:
        frappe.log_error(
            "\n".join(
                f"{a[0].time} -> {a[1].time} ({a[2]:.2f}h) outside plausible range"
                for a in anomalies
            ),
            f"Flex attendance pairing anomaly: {employee}",
        )

    return sessions, anomalies


def _make_session(in_log, out_log, duration):
    return Session(
        in_log, out_log, duration, getdate(in_log.time),
        crosses_midnight=getdate(out_log.time) != getdate(in_log.time),
    )
```

Walks the deduped, chronological list positionally (same alternating
assumption as today — `log_type` still isn't used). The new signal is
duration plausibility: a pair whose gap is too short (a near-punch dedup
didn't catch, e.g. two punches 4 minutes apart) or too long (beyond
`max_session_hours`) is implausible as a single real session.

On an implausible pair, the algorithm doesn't just discard it and resume two
positions later — that would silently shift every later pair by one position
and can produce a plausible-looking but wrong total (see design discussion:
skipping `(08:00,08:04)` in `08:00,08:04,17:00,20:00,23:00` and resuming at
`17:00` mis-pairs `(17:00,20:00)` as a fabricated 3h session and stops
`23:00` from ever pairing, instead of recovering the true `08:04→17:00` and
`20:00→23:00` sessions). Instead it drops only `in_log` (the presumed stray)
and retries `out_log` against the next log — recovering the rest of the
window correctly in the common single-stray-punch case. Only if that retry
is *also* implausible does it give up and stop building further sessions in
this window; sessions already built are kept, everything from that point is
left unclaimed for manual review and reprocessing via `reset_and_reprocess`.

Every implausible pair encountered (whether ultimately recovered or not) is
recorded in `anomalies` and logged via `frappe.log_error` — so a recovered
single stray punch still leaves an audit trail, even though it didn't halt
processing.

A trailing unmatched single log (odd count) is simply never reached by the
`i + 1 < len(logs)` loop bound — same effective behavior as today's
drop-the-trailing-entry, just falling out naturally instead of via a
separate truncation step.

### Filter by IN date, calculate, claim (`process_employee_for_date`)

```python
raw_logs = _get_checkins(employee, date)          # fetch + dedupe
if not raw_logs:
    _mark_absent_or_skip_holiday(...)
    return

sessions, _anomalies = _build_sessions(raw_logs, employee)  # already logged inside
day_sessions = [s for s in sessions if s.attendance_date == date]

if not day_sessions:
    _mark_absent_or_skip_holiday(...)
    return

total_hours = sum(s.duration for s in day_sessions)
in_time = day_sessions[0].in_log.time
out_time = day_sessions[-1].out_log.time
effective_hours = min(total_hours, settings.daily_max_hours)
status = "Present" if effective_hours >= settings.daily_min_hours else "Absent"

attendance = _create_attendance(employee, date, status, effective_hours, in_time, out_time)
claimed = [log.name for s in day_sessions for log in (s.in_log, s.out_log)]
update_attendance_in_checkins(claimed, attendance.name)
```

Only checkins belonging to `day_sessions` (IN date == `date`) get linked to
this Attendance. Sessions whose IN date differs (a next-day session that
happened to fall inside this window) are left untouched — unclaimed,
`attendance` still unset — so that date's own future run fetches and pairs
them fresh, correctly.

This drops the call to HRMS's `calculate_working_hours` — sessions already
carry precomputed durations, so summing them directly is simpler than
re-flattening into a log list and calling back into a helper built for raw
lists. `time_diff_in_hours` (used inside `_build_sessions`) is imported
directly from `hrms.hr.doctype.employee_checkin.employee_checkin`, same
module the app already depends on.

### `_reset_attendance` window consistency

`_reset_attendance` independently rebuilds `day_start`/`day_end` to re-arm
`skip_auto_attendance` before a reprocess. Extract the window-bound
computation (`_get_checkins`'s `day_start`/`day_end` logic, including the new
`checkin_window_grace_hours` read) into one shared helper so both call sites
stay in sync — today they're duplicated literals and would silently drift if
only one were updated.

## Failure scenarios (updates to `ARCHITECTURE.md`)

| Scenario | New behavior |
|---|---|
| Next-day early session falls inside today's window | Never merged into today's total, regardless of checkin count parity — filtered out by IN date after pairing, left unclaimed for its own day |
| Overnight session (single or back-to-back multiple) | Attributed entirely to its IN date; supported natively by pair-then-filter, no window-edge special casing |
| Device double-scan within 60 sec | Collapsed by dedup before pairing, regardless of type |
| Single stray punch beyond dedup's threshold (e.g. two punches 6 min apart, not caught by 60s dedup) | Detected via implausible session duration; bounded resync drops the presumed stray and retries against the next log — recovers the rest of the window's sessions correctly in the common case, logs the anomaly either way |
| Pairing desync that resync can't recover (retry also implausible) | Session-building stops at that point; sessions before it are kept, everything from the anomaly onward is left unclaimed for manual review / `reset_and_reprocess` |
| Missing OUT (trailing unmatched IN) | Same as today: not included in any session, stays unclaimed until a future run finds its OUT |

Also update:
- **Assumption #4** ("shift never runs past noon the day after it starts")
  → replaced by the configurable `checkin_window_grace_hours` (default 12,
  same effective default as today).
- **Assumption #3** ("checkins strictly alternate IN/OUT... a double punch
  desyncs pairing for the rest of the day") → narrowed: dedup handles
  near-simultaneous duplicates; a single stray punch beyond that is
  self-corrected by the bounded resync in `_build_sessions`; only a genuine
  multi-punch desync (retry also implausible) still stops session-building
  at that point rather than the rest of the day.

## Rollout

After deploy, run `reprocess_all` (existing bench-execute utility, already
documented in `CLAUDE.md`) over any historical date range suspected of the
old cross-day contamination bug — dates where an Absent Flexible Hours
employee might actually have worked a genuine early/late session that got
silently absorbed into a neighboring day. No new tooling needed; this is
already idempotent and two-phase (delete-then-reprocess) by design.

## Testing

Extend `test_daily_job.py`:

- Contamination case (the bug this design fixes): a 1/7 session plus a
  same-day-parity 2/7 early session that would previously coincide to an
  even total count → 1/7 gets only its own hours; 2/7, processed on its own
  run, gets its own hours (not zero/Absent).
- Multiple-overnight-in-a-row: `1/7 22:00→2/7 01:00`, `2/7 10:00→2/7 12:00`,
  `2/7 15:00→2/7 18:00` → 1/7 = 3h; 2/7 = 2h + 3h = 5h.
- Same-day, multiple sessions plus a trailing overnight one:
  `08:00→12:00`, `13:00→18:00`, `19:00→01:00(next day)` → all three
  attribute to the first day (their IN date), totaling 4h + 5h + 6h = 15h.
- Dedup: a duplicate within 60 sec (either same or different `log_type`)
  collapses to one checkin and doesn't affect the resulting session.
- Anomaly resync recovers the rest of the day: `08:00, 08:04, 17:00, 20:00,
  23:00` (a stray punch at 08:04 — 4 min gap, below `MIN_SESSION_HOURS` but
  beyond the 60s dedup threshold) → `08:04→17:00` and `20:00→23:00` are both
  recovered as real sessions (not the fabricated `17:00→20:00`), `08:00` is
  left unclaimed, and `frappe.log_error` is called once for the recovered
  anomaly.
- Anomaly resync fails (genuinely broken data): a sequence where both the
  original pair and the resync retry are implausible → session-building
  stops at that point, sessions built before it are kept, no Attendance is
  fabricated from the unresolvable portion, `frappe.log_error` is called.
- `test_holiday_not_marked_absent_by_next_day_checkin_bleed` (existing) must
  still pass unchanged — a single dangling next-day IN with no OUT yields no
  sessions for the holiday date either way.

## Files touched

- `employee_custom_attendance/attendance/daily_job.py` — `_get_checkins`,
  new `_dedupe_checkins`, new `_build_sessions`, `process_employee_for_date`,
  `_reset_attendance` (shared window-bound helper).
- `employee_custom_attendance/employee_custom_attendance/doctype/flexible_hours_settings/` —
  add `checkin_window_grace_hours` and `max_session_hours` fields (doctype
  JSON + migration).
- `employee_custom_attendance/attendance/test_daily_job.py` — new test cases
  above.
- `ARCHITECTURE.md` — update Assumptions #3/#4 and the Failure Scenarios
  table as described above.
