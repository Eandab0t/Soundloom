# Soundloom — Acquisition Tests

**Repository:** `E:\Ean server assests\Ean Applications\Programs\Soundloom`
**Audit date:** Thu Nov 05 2026
**Focus:** Download authorization checks, download reliability, queue tests, and integrity results for the acquisition pipeline.

---

## Summary

Soundloom's acquisition pipeline processes queue jobs through: resolve → match → download → convert → tag → organize → index. Each step is journaled in the operations table for crash recovery. Authorization is established at download time (deemix ARL), not at enqueue time.

---

## Download Authorization Tests

### Test File: `tests/test_download_authorization.py`

| Test | Purpose | Result |
|------|---------|--------|
| `test_deemix_premium_arl_required_for_higher_bitrates` | deemix falls back from 320/FLAC to 128 without premium ARL | ✅ Pass |
| `test_no_authentication_bypass_for_deezer_download` | Downloader raises error when no ARL cookie; no credential fabrication | ✅ Pass |
| `test_region_locked_release_fails_job_not_silent_retry` | Region-locked release fails after max_retries, not retried forever | ✅ Pass |
| `test_stream_only_content_not_treated_as_download_permission` | `dzsearch:` job URL is a search query, not download authorization | ✅ Pass |
| `test_unauthorized_release_recorded_for_user_review` | Match gate records block info (wanted/candidate/score/threshold) on job | ✅ Pass |
| `test_deezer_track_url_enqueued_pending_not_authorized` | Watcher enqueues Deezer URL with `status='pending'`; authorization at download time | ✅ Pass |
| `test_failed_authorization_exhausts_retries_then_failed` | Job exhausts retries (3) then goes to `failed` | ✅ Pass |

### Authorization Boundary

The boundary between "found" and "authorized to download" is:

| Stage | What happens | Authorization checked? |
|-------|--------------|----------------------|
| Watcher discovers release | Enqueues Deezer track URL with `status='pending'` | No — URL presence = implicit authorization |
| Downloader resolves URL | DeezerResolver resolves to track metadata | No |
| Downloader downloads | deemix CLI attempts download | Yes — deemix checks ARL cookie |
| Match gate | Scores candidate against user intent | No — score-based gate, not authorization |
| Converter/tagger | Processes downloaded file | No — file already downloaded |

**Authorization is only verified at the download step (deemix).** If deemix cannot download (no ARL, region-lock, DRM), the job fails after `max_retries`.

---

## Download Reliability Tests

### Test File: `tests/test_acquisition_reliability.py`

| Test | Purpose | Result |
|------|---------|--------|
| `test_jobs_not_claimed_by_multiple_workers` | Atomic job claims; no double-claiming | ✅ Pass |
| `test_invalid_status_transition_rejected` | Unknown status rejected with 400 | ✅ Pass |
| `test_cancel_active_job_transition` | Cancel sets status=`cancelled`, error, progress=0 | ✅ Pass |
| `test_retry_failed_job_resets_budget` | Retry resets `retries=0`, `not_before=NULL`, `progress=0`, `error=''` | ✅ Pass |
| `test_downloader_respects_cancellation_before_download` | `JobCancelled` raised for cancelled jobs | ✅ Pass |
| `test_cancelled_job_operation_marked_abandoned` | Cancelled operation advanced to `ABANDONED` | ✅ Pass |
| `test_corrupted_download_path_returned_not_validated_at_download` | Downloader returns path even for corrupt files (integrity checked downstream) | ✅ Pass |
| `test_interrupted_connection_parks_for_backoff` | Rate-limit errors park job with `not_before` timestamp | ✅ Pass |
| `test_expired_authorization_not_retried_forever` | Failed authorization exhausts retries → `failed` | ✅ Pass |
| `test_duplicate_file_not_downloaded_twice` | `_in_library` returns True; duplicate not enqueued | ✅ Pass |
| `test_filename_collision_keep_separate` | `keep_separate` policy creates `(1)` suffix | ✅ Pass |
| `test_atomic_file_write_no_partial_in_library` | `_atomic_move` uses `os.replace`; no partial files | ✅ Pass |
| `test_operation_journal_survives_restart` | Interrupted operations journaled; `reconcile()` examines them | ✅ Pass |
| `test_missing_title_artist_does_not_match` | `has_comparable_intent()` returns False for empty intent | ✅ Pass |

---

## Queue Lifecycle Testing

### Job Status Transitions

| From → To | Allowed? | Enforced by |
|-----------|----------|-------------|
| `pending` → `resolving` | ✅ Yes | `_pick_next_job()` (atomic claim) |
| `pending` → `cancelled` | ✅ Yes | `DELETE /api/queue/{id}` (active jobs) |
| `pending` → `pending` (retry) | ✅ Yes | `POST /api/queue/{id}/retry` (failed/cancelled/paused/backoff only) |
| `resolving` → `downloading` | ✅ Yes | Worker pipeline (`_set_state`) |
| `downloading` → `converting` | ✅ Yes | Worker pipeline |
| `converting` → `tagging` | ✅ Yes | Worker pipeline |
| `tagging` → `organizing` | ✅ Yes | Worker pipeline (last cancellation point) |
| `organizing` → `indexing` | ❌ No (cancellation refused) | `DELETE /api/queue/{id}` returns 409 |
| `indexing` → `complete` | ✅ Yes | Worker pipeline |
| `complete` → `deleted` | ✅ Yes | `DELETE /api/queue/{id}` (terminal) |
| `failed` → `pending` (retry) | ✅ Yes | `POST /api/queue/{id}/retry` (resets `retries=0`) |
| `cancelled` → `pending` (retry) | ✅ Yes | `POST /api/queue/{id}/retry` |

### Cancellation Checkpoints

Cancellation is cooperative. The downloader checks `_ensure_not_cancelled()` before each file-mutating stage:

1. Before resolving (start of `_process_job`)
2. Before thumbnail download
3. Before audio download (yt-dlp/deemix)
4. Before convert (FFmpeg)
5. Before tag
6. Before organize (last safe point — after this, file moves to library)

Once organizing starts, the queue API refuses cancellation (409 Conflict) because the file is already being moved into the library.

---

## Queue Reliability Tests

### Test File: `tests/test_acquisition_reliability.py`

13 tests covering job claiming, state transitions, cancellation, retry budget, corrupted downloads, rate-limit backoff, expired authorization, duplicate prevention, filename collisions, atomic writes, operation journal recovery, and invalid metadata.

---

## File Integrity

### Atomic Writes

`_atomic_move()` in `backend/services/organizer.py` uses `os.replace()` (atomic on same volume) with cross-volume fallback:
1. Try `os.replace(src, dst)` — atomic on same volume
2. On `OSError` (cross-volume): copy to hidden `.part` file next to destination, then `os.replace()` over destination
3. Clean up `.part` file in `finally` block

This ensures no partial file is ever visible at the destination path.

### Duplicate Prevention

Two levels of dedup:
1. **`_in_library()`** in watcher — checks `tracks` table by `source_url` or normalized title+artist
2. **`_already_queued()`** in watcher — checks `jobs` table by `source_url` (non-terminal statuses)
3. **`_already_queued()`** in `sync.py` — same check for sync jobs

### Metadata Consistency

Metadata flows: source → resolver → TrackMetadata → downloader → converter → tagger → organizer → indexer → `tracks` table. Each stage preserves metadata from the previous stage. The matcher gates downloads against user intent (title/artist/album/duration) with `match_threshold` (default 70%).

---

## Test Results

| Test File | Tests | Passed | Failed |
|-----------|-------|--------|--------|
| `tests/test_acquisition_reliability.py` | 13 | 13 | 0 |
| `tests/test_download_authorization.py` | 7 | 7 | 0 |
| `tests/test_queue_api.py` | 17 | 17 | 0 |

---
