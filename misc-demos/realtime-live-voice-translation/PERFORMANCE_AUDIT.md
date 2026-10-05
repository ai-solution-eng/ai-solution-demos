# Performance Audit — Realtime Live Voice Translation
*Fleet audit (5 specialist subagents: backend pipeline, VAD/audio, ASR+LLM inference, frontend, architecture/ops) + lead verification of all critical claims. All paths relative to `code/`.*

## 1. Where the time goes (validated end-to-end latency model)

Pipeline: mic → AudioWorklet (32ms PCM16 @16kHz) → WS → Silero VAD → **segment finalize gate** → Whisper ASR (full segment) → LLM translation → broadcast → DOM render.

**Speaker stops → final translation on screen: ~2.1–2.9s best case, 3.3–5.5s typical, up to ~7s** (incomplete-sentence path). First *partial* appears ~1.3–2.6s after speech onset.

| # | Budget item | Cost | Root cause |
|---|---|---|---|
| 1 | **Tail-silence finalize gate** | **1300–2200ms dead time** | `config.py:107-111`; nothing runs during it |
| 2 | Final ASR re-transcribes ENTIRE accumulated segment incl. trailing silence | 0.6–1.1s (5s audio) | `websocket.py:691,752-758`; buffer never trimmed |
| 3 | Final LLM translation (sequential after ASR) | 0.3–0.8s | `translation.py:143-159` |
| 4 | Final queues behind stale in-flight partial | +0.3–2.5s intermittent | `cancel_analysis()` not called at finalize (`websocket.py:721-758`) |
| 5 | Broadcast fan-out sequential; **per-attendee LLM calls inline in the loop** | +0.3–0.8s per attendee | `websocket.py:153-186`, cache wiped per revision (`rooms.py:156-157`) |
| 6 | Short utterances (<700ms) always pay the 2200ms incomplete gate | +900ms | no partial can exist → `looks_sentence_complete("")`=False (`websocket.py:733-740`) |
| 7 | First partial: 700ms audio + 750ms cadence + full ASR+LLM | 1.3–2.6s after onset | `config.py:116-119` |
| 8 | Client: full innerHTML re-render + forced scroll-to-top per message | jank at 100+ turns | `shared-transcript.js:89-113` |

Call amplification: one 5s utterance ≈ **5–6 full ASR calls + 5–6 presenter LLM calls + N_attendee×revision LLM calls (~10–11 LLM calls total)**. ~30–60% of live LLM calls are provably wasted (identical ASR text re-translated, dedupe only happens after the LLM returns — `websocket.py:261-266` vs `335-383`).

## 2. The minimal-change improvement plan (top priority: time-to-decent-translation)

> **STATUS: Tier 0 + Tier 1 + robustness fixes IMPLEMENTED** (all items below applied to the working tree; see §2b for the change log). Tier 2/3 remain future work.

### Tier 0 — env-only, zero code (values.yaml `env:` / `.env`) ✅ IMPLEMENTED
```
LIVE_SILENCE_THRESHOLD_MS: "1000"      # 1300→1000 (safe; two-tier design still guards clause-ends; aggressive: 800)
LIVE_INCOMPLETE_FINALIZE_MS: "1700"    # 2200→1700
LIVE_PARTIAL_UPDATE_MS: "500"          # 750→500
LIVE_MIN_PARTIAL_AUDIO_MS: "500"       # 700→500 (watch hallucination-filter hits; 600 conservative)
HALLUCINATION_PATTERNS: extended list  # only 3 exact-match strings today (config.py:123-134)
```
Expected: **−350 to −850ms** to final; partials ~250ms fresher. Note: `TORCH_NUM_THREADS` env is read (`config.py:14-21`) but the vendored Silero hard-sets 1 thread (`silero-vad/src/silero_vad/model.py:4`).

### Tier 1 — tiny patches (<30 lines each), ranked by impact/effort
1. **Speculative partial at VAD speech-end** (~8 lines, `websocket.py:685-688`): on VAD `"end"`, immediately queue a partial snapshot bypassing the cadence gates → ASR+LLM runs *during* the silence window. **−1000 to −2000ms perceived; the single best code change.**
2. **Pre-translate dedupe** (~6 lines, `analyze_snapshot`): skip `translate_live_text` when ASR text matches the last emitted revision. **−30–60% LLM calls, zero accuracy change.**
3. **`await cancel_analysis()` before final `queue_snapshot(is_final=True)`** (~7 lines): final no longer waits behind a stale partial. **−0.3–1.5s.**
4. **Stop wiping per-language translations on non-final revision bumps** (~5 lines, `rooms.py:156-157`) + **skip per-attendee LLM on partials** (~8 lines, `websocket.py:160-173`): kills serial per-attendee partial LLM calls.
5. **Short-utterance gate fix** (~4 lines): `sentence_ms < LIVE_MIN_PARTIAL_AUDIO_MS` → use the complete-silence threshold. **−900ms for "yes/OK/sure".**
6. **OpenAI client hygiene** (~10 lines, `services/clients.py`): `timeout=20s, max_retries=1` + module-level client cache keyed by (base_url, key). Bounds p99 (SDK default is 600s timeout, 2 silent retries re-uploading full WAV); keeps TLS pools warm.
7. **1-line accuracy fix**: `recording.js:355` → `new AudioContext({sampleRate: 16000})` — browser's high-quality sinc resampler replaces the aliasing linear-interp worklet resampler (sibilant energy currently folds into Whisper's band).
8. **Frontend, ~26 lines total** (`shared-transcript.js`, `attendee/connection.js`, `attendee.js`): scroll-guard (only autoscroll when near top), provisional "Translating…" shimmer styling, port the presenter's revision guard to the attendee, language-switch pending mask.

**Combined Tier 0+1 (~60 lines + env): speech-end → decent translation drops from ~2.6–4.5s to ~1.3–2.5s.**

### Tier 2 — targeted (next sprint)
- Parallel broadcast fan-out (`asyncio.gather` + per-send 2s timeout) — tail becomes max() not sum(). *(partially addressed: attendee LLM calls on partials eliminated; finals still serialize per-attendee)*
- ~~Fire-and-forget persistence + drop redundant per-final `sync_room_persisted_session` UPDATE~~ ✅
- Early energy-confirmed finalize (punctuation + VAD-end agree → finalize at ~max(500, 0.5×gate)).
- ~~Export zip/write/read off the event loop (`asyncio.to_thread`)~~ ✅
- Join-snapshot fast-path: cap lazy per-segment backfill to last ~20 segments.
- Keyed incremental DOM rendering (O(1)/message) + rAF coalescing. *(mitigated meanwhile by CSS containment + content-visibility)*
- App-level WS reconnect (client, exp backoff 1.5→5s) — today a dropped socket = frozen forever.
- Metrics: `asr_latency_seconds`, `llm_latency_seconds`, `e2e_segment_latency` (now−ts_ms) — **prerequisite for proving any of this**; currently print()-only.

### 2b. Implemented change log (this working tree)

**Backend (`app/`)**
- `realtime/websocket.py`:
  - *Speculative partial at VAD speech-end* — on the `"end"` VAD event, queues a partial immediately (bypassing cadence gates, guarded by `LIVE_MIN_PARTIAL_AUDIO_MS`), so ASR+LLM runs during the tail-silence window. Biggest single perceived-latency win (−1 to −2s).
  - *Pre-translate dedupe* — skips `translate_live_text` when a non-final snapshot's ASR text equals the last emitted revision (finals always re-translate). −30–60% live LLM calls.
  - *Cancel-on-finalize* — `await cancel_analysis()` before both final `queue_snapshot` sites (silence threshold + max-sentence flush); the final no longer waits behind a stale partial (−0.3–1.5s).
  - *Attendee partial skip* — `broadcast_segment_to_room` no longer runs the serial per-attendee `ensure_room_translation` LLM call for non-final revisions (finals still do).
  - *Short-utterance gate* — when no partial text can exist (`sentence_ms < LIVE_MIN_PARTIAL_AUDIO_MS` or empty partial text), the complete-silence threshold applies instead of the incomplete one (−900ms for "yes/OK/sure").
  - *Broadcast guard* — `broadcast_segment_to_room` wrapped in try/except so a broadcast/LLM throw can't unwind `emit_segment_update` before persistence.
  - *Fire-and-forget persistence* — `persist_finalized_segment` via `asyncio.create_task`; DB latency no longer gates the next ASR/LLM cycle.
- `services/rooms.py` — per-language translation cache survives non-final revision bumps (wiped only on final), keeping last-good attendee text between revisions.
- `services/persistence.py` — `persist_finalized_segment` uses the cache-hit `ensure_room_persisted_session` (drops one redundant session UPDATE per sentence).
- `services/clients.py` — shared `AsyncOpenAI` cache keyed by (base_url, api_key); `timeout=20s (connect 2s)`, `max_retries=1` (bounds the 600s/2-retry SDK defaults).
- `services/exports.py` + `state/export_jobs.py` — export ZIP deflate, artifact write, and artifact read moved to `asyncio.to_thread` (an export click can no longer freeze all live rooms).
- `routes/rooms.py` + `utils/auth.py` — presenter-only HMAC auth (previous change; unchanged).
- `charts/values.yaml` / `example.env` — Tier 0 latency knobs exposed: `LIVE_SILENCE_THRESHOLD_MS=1000`, `LIVE_INCOMPLETE_FINALIZE_MS=1700`, `LIVE_PARTIAL_UPDATE_MS=500`, `LIVE_MIN_PARTIAL_AUDIO_MS=500`.

**Frontend (`frontend/`)**
- `presenter/recording.js` — `AudioContext({sampleRate: 16000})`: browser sinc resampler replaces the aliasing linear-interp (ASR accuracy, esp. fricatives).
- `shared-transcript.js` — "Translating…" + `.is-pending` shimmer for empty translations on live segments; scroll-to-top only when the reader is near the top (history reading no longer yanked every ~750ms).
- `attendee/connection.js` — revision guard ported from presenter (stale partials can't overwrite newer text); `langSwitchPending` cleared on snapshot arrival.
- `attendee.js` — sets `langSwitchPending` on target-language change (mask shows "Translating…" instead of stale-language text during server re-translation).
- `shared.js` — `isLanguageSwitchPending()` shared getter.
- `styles/{attendee,presenter,attendee-mobile}.css` — shimmer keyframes; `contain: content` on `.stack`; `content-visibility: auto` on `.history-list` (cuts O(n) layout/paint per partial update at 100+ turns).

**Verification:** `py_compile` clean on all 9 touched Python files; `node --check` clean on all 9 touched JS files; logic tests passed for revision retention, finalize-gate semantics, speculative-partial guard, dedupe condition, and attendee partial-skip (isolated mirrors — the sandbox lacks sqlalchemy/pydantic for full-app import).**Recommended before deploy:** one manual live test (presenter + 1 attendee, different languages) watching for (a) attendee partial text falling back to source/last-good until final, (b) the speculative partial appearing during the silence window, (c) shimmer behavior.

### 2c. Post-deploy findings from live pod logs (namespace `realtime-translation`, 2026-09-29) — second fix round

Live logs confirmed the latency fixes landed (`required=1000 ms` finalize gates, ASR 0.2–0.5s, LLM 0.4–0.8s, speculative partials firing) and exposed three residual bugs, all fixed:

1. **Empty translations on FINAL segments** ("Hello?" → FINAL chip + "Awaiting translated output."): the deployed LLM is `glm-5.3-flash` — a hybrid-reasoning model — and `max_tokens=200` (sized for Qwen-instruct) can be consumed by its reasoning phase, returning empty `message.content`. Fix in `services/translation.py`: new shared `_chat_completion()` entry point sends `chat_template_kwargs={"enable_thinking": False}` (ignored by non-hybrid models), raises the budget to `LLM_MAX_TOKENS=512`, and retries once at `LLM_RETRY_MAX_TOKENS=1024` without stop strings when content comes back empty, logging `finish_reason` + reasoning-channel size for diagnosis. The removed `stop=["\n\n","```"]` also eliminated a second silent-truncation path (content starting with the stop string → empty).
2. **"Fatal WS error: listen address already in use"** (presenter connection dropped at 19:01:48): the join/snapshot path translated EVERY uncached history segment sequentially inline; one LLM failure (here: connect-timeout after 2 retries ≈4.4s, endpoint busy) unwound the whole WS handler. Fix in `realtime/session.py`: per-segment try/except (ship untranslated, never kill the connection) + `SNAPSHOT_INLINE_TRANSLATION_LIMIT=20` — only the 20 most recent segments translate inline on join/language-switch; older ones render original text.
3. **Silent final drop** (ASR RMS/length/hallucination gates return "" with no trace → segment stuck "Awaiting translated output." forever): `transcribe_snapshot` now reports a `drop_reason` (too_short/silent/text_too_short/hallucination/sound_effect/asr_error); on an empty FINAL, `analyze_snapshot` closes the segment with the last partial's original+translation instead of leaving it open, and logs the reason. Frontend (`shared-transcript.js`): a FINAL card with no translation now reads "Translation unavailable for this segment." — the placeholder is never shown as if it were awaiting data that will never arrive.

**Config added:** `LLM_MAX_TOKENS` (512), `LLM_RETRY_MAX_TOKENS` (1024), `SNAPSHOT_INLINE_TRANSLATION_LIMIT` (20) — all env-tunable, defaults applied in code.

### 2d. "Fatal WS error: Cannot call receive once a disconnect message has been received" (third fix round)

**Root cause:** three join-rejection paths (`presenter token failure`, `attendee invalid room code`, `attendee room not found`) did `await websocket.close(1008)` **then `continue`** — re-entering `websocket.receive()` on a closed socket, which raises this Starlette error (uncaught by `except WebSocketDisconnect` since no *new* disconnect message arrives) and logged "Fatal WS error" on every rejection. The attendee paths carried this latent bug for a long time; the presenter-token guard inherited it and made it visible on every auth rejection/redeploy.

**Fix:** all three close sites now `return` (the `finally` block still runs: epoch bump, analysis cancel, connection unregister, recording pause, room-state broadcast). Verified by source assertion that every `close(code=1008)` is followed by `return`.

**Also fixed the lockout that made the rejection common:** without `PRESENTER_AUTH_SECRET` set, each backend restart generates a fresh per-process secret, so previously-issued presenter cookies stopped validating AND the pristine-only re-issue rule locked the presenter out of their recovered room. `routes/rooms.py` now re-issues tokens when `has_persistent_secret()` is false (token lifetime = process lifetime, so the takeover window is unchanged by this); with a persistent secret set, active-room tokens stay non-re-issuable. **Recommendation: set `PRESENTER_AUTH_SECRET` in values.yaml for restart-stable strictness.**

**Also fixed (live-log follow-up):** first-join stall — pod logs showed the fatal at 19:17:06 landing 200ms after "Loading Silero VAD...", i.e. the lazy torch.hub VAD load running inside the first presenter WS handshake. `main.py` lifespan now preloads VAD via `asyncio.to_thread(_load_vad)` at startup (best-effort; lazy path remains as fallback).

### 2e. "TypeError: AsyncCompletions.create() got an unexpected keyword argument 'chat_template_kwargs'" (fourth fix round)

**Root cause (lead bug, introduced in §2c):** the think-mode guard passed `chat_template_kwargs=...` as a **top-level kwarg** to the OpenAI SDK's `create()`, which only accepts named parameters. Every translation call therefore raised TypeError → "Translating…" forever (the screenshot's REFINING card with the full original transcribed but zero translations). The WS disconnect-handling fix and the transcript/ASR pipeline were already working — the screenshot's instant transcript render and live REFINING status confirm it.

**Fix:** the field now travels in `extra_body` (`extra_body={"chat_template_kwargs": {"enable_thinking": False}}`) — the SDK-sanctioned passthrough for provider-specific fields, which vLLM merges into the request payload. Verified against a strict SDK simulation that rejects unknown top-level kwargs.

**Model guidance:** no model swap needed. `glm-5.3-flash` works once the request is well-formed; `extra_body.chat_template_kwargs.enable_thinking=False` is exactly the vLLM-documented way to disable its reasoning phase. If translations are still slow after this, the alternatives are (a) the recommended `Qwen3-30B-A3B-Instruct-2507-FP8` (non-reasoning, no template flags needed) or (b) any plain instruct model — but try the corrected request first.

### 2f. Model answers with task narration instead of the translation ("The user wants me to translate…") (fifth fix round)

With the TypeError fixed, the LLM call succeeds — but `glm-5.3-flash` responds as a helpful assistant (narrating the task back) instead of emitting raw translated text. Three defenses added:

1. **Prompts rebuilt as a strict transformer contract** (`prompts.py`): "You are a raw text transformer, not an assistant. This feeds a LIVE caption screen: your reply is displayed verbatim" — with an explicit banned-opener list ("Okay / Here is / The translation / The user wants me to"), a closing directive ("Reply now with ONLY the Spanish translation of the current segment:"), and "DO NOT translate or repeat" labeling on the context block. Both the live contextual prompt and the plain translator system prompt.
2. **Meta-reply detection + enforced retry** (`utils/text.py` `looks_like_meta_reply`, wired into `_chat_completion`): a precision-first regex flags unambiguous narration openers ("The user wants me to", "Here is the translation", "I'll translate", "As an AI…", plus common Spanish equivalents) while never matching plausible spoken sentences ("I'm going to the store", "The user wants a faster checkout" are safe). A flagged reply triggers one retry with an appended "Output the translation only…" reminder message. Tuned against an 18-positive/12-negative test set — zero false positives on realistic captions.
3. **Diagnosis**: if the retry still fails, the log names the template/model mismatch and recommends swapping `LLM_MODEL` to a plain instruct model.

**Model-swap decision:** still optional — this fix makes any instruction-capable model usable. If narration recurs despite the retry (log line `LLM retry STILL unusable`), switch `LLM_MODEL` to `Qwen/Qwen3-30B-A3B-Instruct-2507-FP8` (README's recommendation; plain instruct, no reasoning phase, no template flags).

### 2g. "Translating… the whole utterance, then a flash of translation" (sixth fix round — user-reported, live)

**Reported UX:** while speaking, the card shows "Translating…"; when speech ends the translation appears for a moment, then the card flips. Log evidence showed the mechanism: with a slow LLM (2–6.5s/call due to GLM narration+retry), the speculative partial (queued at VAD end) completed **after** the final was requested → the `segment_final_requested` guard **suppressed it** → viewers saw nothing until the final's own full ASR+LLM round finished. The speculative-partial head start was being discarded exactly when it mattered most.

**Fix — promote instead of suppress (`websocket.py`):**
1. **Queued case** (`queue_snapshot`): when the final is requested while the speculative partial is still in the pending slot, flip that snapshot's `is_final` to True in place (one chain of work, emitted as final; no duplicate ASR+LLM round).
2. **In-flight case** (`emit_segment_update`): when a partial completes after a final was requested but **no final result has been emitted yet**, promote it to final (`is_final_emitted` sentinel in segment state) instead of dropping it. A later real final still supersedes it normally (dedupe prevents flicker when the text matches; revision bump corrects it when it differs).
3. Verified with a state-machine mirror across 4 scenarios: slow-LLM suppression (the reported bug), healthy partials, promoted-final + identical real final (no flicker), promoted-final + differing real final (corrected).

**Residual expectation:** this removes the "Translating…" dead zone and the end-flip for the presenter target. The underlying latency is still the LLM's 2–6.5s narration+retry — the **model swap to Qwen3-30B-A3B-Instruct remains the real fix**; promotion just makes the pipeline degrade gracefully instead of hiding work it already paid for.

### 2h. ASR 403 → UnboundLocalError crash + 409 chunk-spam (seventh fix round)

Live log after the Qwen install surfaced three issues:

1. **CRITICAL (lead bug): `UnboundLocalError: cannot access local variable 'original'`** — in `analyze_snapshot`, when the ASR call *raises*, the exception handler appended a drop-reason but `original` was never assigned; the empty-final handling then read it, killing the analysis task (`Task exception was never retrieved`) on EVERY ASR failure — so ASR errors also broke the finalize path. Fixed: `original = ""` initialized before the try block. (Introduced in §2c's empty-final handling; the 403 below triggered it on every segment.)
2. **Environment (user action needed): ASR endpoint 403 `failed to verify token`** — the ASR_API_KEY/token for the ASR endpoint (Cohere transcribe on MLIS) is invalid or expired. This is config, not code: set a valid `ASR_API_KEY` in the deployment env. Note the LLM (Qwen) was NOT failing — only ASR. Until fixed: no transcription, no translation (and my promotion logic can't help without text).
3. **Client resilience: hundreds of 409 Conflicts** from the recording-chunk uploader — when the server-side recording session goes stale (cleared/stopped/ownership change), the client kept POSTing every chunk. Fixed (`recording.js`): on the first 409, set `recordingUploadAborted`, log ONE clear line ("Recording stopped on the server … further chunk uploads paused"), skip subsequent uploads; flag resets when a new recording starts. Also: the "Partial/Final ASR empty (reason='silent')" lines in the log are the new drop-reason instrumentation working as designed — that segment was genuinely below the silence threshold (e.g. mic picked up only breath/room tone), not an error.

**Also note:** `✅ End of sentence confirmed … [2464 ms, silence=1024 ms, required=1000 ms]` shows the finalize gate and short-utterance fix working; the incomplete path correctly waited 2200→(tuned) because no partial text existed.

### 2i. Hold-last-translation UX (user request): keep the most recent translation on screen instead of "Translating…"

**Rationale (user):** during long utterances the live card churned between stale text and "Translating…", and each arriving translation stayed visible for only a moment before the placeholder returned. Broadcast-captioning practice is the opposite: never blank out a caption you can still show.

**Implemented (`shared-transcript.js` + all stylesheets):** when the LIVE card has no translation yet, the renderer carries the most recent **finalized** segment's translation into the live card — styled muted+italic with a small "(previous)" tag (`.is-held` + `.held-note`) so it's readable but clearly not the current sentence's translation. Priority order: own translation → held previous → "Translating…" shimmer (only when genuinely nothing exists, e.g. conversation start or language switch) → "Translation unavailable" (finals). History cards are unaffected; the hold applies to the latest card only. Language-switch mask still takes precedence via `switchPending`.

**Why finals-only for the hold source:** a partial revision of the *previous* segment can still be revised; holding only finalized text guarantees the held line was a complete thought the viewer already saw as settled.

### 2j. Attendee language-switch regression + touched-languages-in-report (user-reported, eighth fix round)

**Reported:** (a) guests could no longer change the target language away from the invite's language; (b) requirement clarified: EVERY language touched by any participant (attendee switches + presenter) must appear in the recorded report/minutes — "10 languages touched = 10 languages in the package."

**Root causes found:**
1. **My §2c snapshot inline-budget (last 20 segments) regressed switching:** the switch reply only inline-translated the last 20 segments; in rooms longer than that, older history never received the new language (before my change, the whole history was translated inline — slow but complete). With GLM's 2–6.5s/call even the inline 20 took minutes, making the switch appear dead.
2. **Touched ≠ translated:** `used_translation_languages` (the report's language source, `room_export_language_codes`) only registered languages when a translation was actually *stored* (`remember_room_translation_language` called from `ensure_room_translation`). A selected-but-pending language never reached the report.
3. **Hold-last UX could hold old-language text** right after a switch (minor).

**Fixes:**
- `realtime/session.py`: new `backfill_room_translations_for_target()` — translates the FULL finalized history for one target language as a **background task** (per-segment fault isolation, idempotent, skips source-language/non-final/already-translated); `maybe_start_backfill()` dedupes per (room, language) so N attendees switching to the same language trigger one pass.
- `realtime/websocket.py`: `set_target_language` handler now (a) registers the language in `used_translation_languages` **at switch time** (spec: touched counts, regardless of backfill outcome), (b) replies with the fast inline snapshot (recent 20), (c) kicks off the background full-history backfill. Same treatment on attendee **join** with a fresh language.
- `shared-transcript.js`: during `langSwitchPending`, the hold-last translation is suppressed (mask shows "Translating…" instead of old-language text).
- Verified with a 30-segment-room test suite: 30/30 backfilled, report languages accumulate across 10 touched languages (`['fr','de','it','ja','zh','km','vi','ru','tr','pt','en','es']`), dedupe + idempotence hold.

**Report-language semantics now:** a language appears in the export package if (a) it was ever **selected** by an attendee or presenter (registered at selection), OR (b) it has at least one stored segment translation. This matches the user spec ("every language they chose will be used in the report") and guarantees a selected language isn't lost even if backfill partially fails.

### 2k. "(previous)" hold-last REVERTED + live per-attendee partial translation restored (user-reported regression, ninth fix round)

**Reported:** the hold-last-translation UX (§2i) showed "(previous)" constantly — "absolutely awful". Root cause: two of my earlier changes interacted badly:
1. §2b's **attendee-partial skip** (no per-attendee LLM on partial revisions, to avoid serial fan-out blocking) meant attendee-language partials arrived with **no translation during live speech**;
2. §2i's **hold-last** then filled that gap with the previous segment's translation + "(previous)" tag — on *every* revision, permanently, for any attendee whose language ≠ presenter's (the screenshot: live card stuck on "Eu vou ter que fazer a tradução. (PREVIOUS)").

**Fix (both halves corrected):**
- **REVERTED the hold-last display entirely** (`shared-transcript.js`): no "(previous)", no held text — translation slot shows live translated text, brief "Translating…" shimmer when empty, "Translation unavailable" on finals. The `findLastTranslation`/`heldTranslation` plumbing removed; `.is-held` CSS remains but unused (harmless).
- **RESTORED live per-attendee partial translation** (`websocket.py` `broadcast_segment_to_room`): every missing attendee language is translated on **every revision** (partials included) — affordable with Qwen (~0.5s/call) and it's what the product is supposed to do.
- **Made the fan-out PARALLEL**: missing languages are collected first, translated via `asyncio.gather` (per-language try/except, failure ships empty for that language only), then sent. 5 languages × 0.5s: serial 2.5s → parallel 0.5s (tail = max, not sum) — verified. This removes the head-of-line-blocking reason the skip existed in the first place.

**Net behavior now:** an attendee in any language sees live-refining translations in *their* language during speech, ~0.5s behind the presenter — no "(previous)", no dead slot. Load note: each partial revision now costs one LLM call per distinct attendee language (same as the original design, but parallelized and deduped by the per-(segment,revision,target) cache).

### 2l. FINAL language semantics (user decision): live = new segments only; history translated at export

*(superseded in part by §2m: attendees no longer switch languages at all; the export-time translation of touched languages remains exactly as decided here.)*

**User decision (supersedes §2j's backfill approach):** when a presenter or attendee changes target language, live translation starts in the chosen language for **new segments only**; the conversation's past history stays in the language it was produced in. No instant/retro translation of history — "that process is a big toll on the model, which becomes slow and does not trace the live conversation anymore." All needed retro-translation happens **at export time**, when extra latency is acceptable.

**Implemented:**
- `realtime/session.py` — `build_room_snapshot` makes **ZERO LLM calls**: history segments ship their existing translation only if already in the requested language, else the original text. The §2j background backfill machinery and `SNAPSHOT_INLINE_TRANSLATION_LIMIT` config are removed.
- **Live path (§2k):** new segments translate live in the room's target language via the parallel fan-out.
- **Export path:** `run_export_job` → `room_export_language_codes` (touched ∪ stored) → `translate_segments_to_language` batch-translates every segment missing each touched language.
- **Verified (30-segment room):** switch = zero LLM calls; existing translations preserved; touched languages reach the export list.
- **Trade-off:** history is a mix of languages per attendee (accepted cost of keeping the model tracing the live conversation).

### 2m. Attendee language switching REMOVED + presenter export-language chooser (user decisions, tenth round)

**Decisions:** (1) attendees no longer choose a language — everyone sees the presenter's target (removes per-attendee LLM pressure); (2) at download time the presenter picks, via a multiselect popup, which languages the package (reports/minutes/translations) includes.

**Attendee language lock:**
- `realtime/websocket.py`: attendee join target = `room.presenter_tgt` (join payload language ignored); `set_target_language` is now a **no-op** (accepted-and-ignored so older cached frontends don't error).
- `attendee.html` + `attendee-mobile.html`: picker replaced by a **static display chip** (`language-trigger-static`); `attendee/core.js` binds no picker events (only the label updater); `attendee/connection.js` `room_state` handler now tracks `presenter_tgt` so a presenter language change updates every attendee view immediately (subsequent segments arrive in the new language; history stays as produced).
- Effect on load: one target language live at a time → the parallel fan-out (§2k) has at most ONE attendee language to translate per revision; LLM pressure drops to ~1 call/revision.

**Export language chooser:**
- `schemas/api.py`: `RoomExportRequest.export_languages: list[str] = []` (empty = previous behavior, all touched languages).
- `routes/exports.py`: presenter selection is validated (`code_to_language`), intersected with available languages, and used as the export language list; empty selection → all touched; final last-resort fallback `[source, target]`. Response includes the resolved `languages` list.
- `routes/rooms.py`: `GET /api/rooms/{id}` now returns `used_translation_languages` (via `room_export_language_codes`) so the dialog can pre-select.
- `index.html` + `presenter/export.js` + `presenter.css`: `#exportLanguagesModal` — checkbox list of touched languages (all pre-selected), Cancel/Confirm; Confirm proceeds with the selection, Cancel aborts the download. Previous-room downloads (`downloadPackageOnly`) pass an empty selection → all touched languages (the room may be gone; its persisted list governs).

**Note:** with switching removed, the "touched languages" set = the presenter's language history for the room (each `config` change) — attendees contribute no additional languages.

### 2n. Attendee showed Spanish while the room was Japanese (stale-presenter-UI race, eleventh round)

**Reported:** share link `?room=…&lang=ja`, presenter on Japanese, attendee chip showed Spanish.

**Root cause (presenter-side race, not attendee):** the presenter page initializes its language form fields from backend `/defaults` (env `SOURCE/TARGET_LANGUAGE` = Spanish) on every load. If the presenter had set the room to Japanese earlier and then reloaded the page, the UI showed Spanish while the ROOM still held Japanese. On reconnect, the `joined` handler called `sendConfig()` **before adopting the room's language** — pushing Spanish as a `config` → server set `presenter_tgt=Spanish` → the attendee's follow-presenter wiring (§2m) correctly displayed Spanish. The attendee was the messenger, not the bug.

**Fixes (two layers):**
1. **Presenter client adopts the room's language pair BEFORE sending config** (`presenter/connection.js`): on `joined`, `room_state`, and `snapshot` messages the UI applies `msg.src`/`msg.tgt`/`msg.presenter_tgt` first — so the post-join `sendConfig()` pushes the room's actual language back (idempotent), never the stale form value.
2. **Server config presence-guard** (`websocket.py`): language fields are only applied when the payload explicitly carries them (`"src"/"tgt" in payload` and valid) — a config that updates only endpoints/models can no longer touch the room's language pair.

**Residual note:** a *deliberate* presenter change (pick French in the picker → `sendConfig`) still works exactly as before — the guard only blocks silent reappearance of stale defaults, not intentional changes.

**User decision (supersedes §2j's backfill approach):** when a presenter or attendee changes target language, live translation starts in the chosen language for **new segments only**; the conversation's past history stays in the language it was produced in. No instant/retro translation of history — "that process is a big toll on the model, which becomes slow and does not trace the live conversation anymore." All needed retro-translation happens **at export time**, when extra latency is acceptable.

**Implemented:**
- `realtime/session.py` — `build_room_snapshot` makes **ZERO LLM calls**: each history segment ships its existing translation only if it's already in the requested language, else the original text (attendees read past turns as-produced). The §2j background backfill machinery (`backfill_room_translations_for_target`, `maybe_start_backfill`, claim-set) and `SNAPSHOT_INLINE_TRANSLATION_LIMIT` config are **removed**.
- `realtime/websocket.py` — switch/join handlers keep the touched-language registration (`remember_room_translation_language` at selection time) but no longer start backfills.
- **Live path unchanged (§2k):** new segments translate live in every connected participant's chosen language via the parallel per-language fan-out — the live conversation is never competing with history translation.
- **Export path (already correct):** `run_export_job` → `room_export_language_codes` (touched ∪ stored) → `translate_segments_to_language` batch-translates every segment missing each touched language, with progress reporting ("Translating transcripts…").

**Verified (30-segment room):** switch to `fr` = zero LLM calls, 30/30 history segments ship original text; existing `es` history preserved; touched languages reach the export list; source-language segments show verbatim original; simulated export fills `fr` fully.

**Trade-off made explicit:** history is a mix of languages per attendee (source text or whatever language that turn was produced in). This is the accepted cost of keeping the live path fast and the model tracing the conversation.

### 2o. Export chooser: ALL supported languages + Select all/Clear (user request)

**Decision:** since all package translation happens at export time, the presenter may pick **any app-supported language** (the same 20-language menu as the presenter room: `LANGUAGE_OPTIONS` in shared.js = `code_to_language` in config.py) — not just previously-touched ones. Bulk **Select all** / **Clear** buttons added.

**Frontend** (`index.html`, `presenter/export.js`, `presenter.css`):
- The chooser lists all 20 supported languages; **touched languages sort first, show a green "used" badge, and start checked**; untouched ones are offered unchecked (with native names, e.g. "Japanese 日本語 (ja)").
- `Select all` / `Clear` mass-toggle every checkbox; Confirm resolves the checked set (empty allowed — the server then falls back to source+target), Cancel aborts.

**Backend** (`routes/exports.py`): selection semantics fixed — the previous version intersected the selection with touched/available languages, which would silently **drop never-used languages**. Now: requested valid languages are honored in full (export-time translation works from originals regardless of live history); languages already available keep first positions; dedup via `dict.fromkeys`. Empty selection = all touched; last-resort `[source, target]` unchanged.
- Verified: `['fr','ja']` on an es-only room → `['es','fr','ja']`-style resolution (both honored), invalid codes dropped, empty = touched-all.

**Load implication:** exporting with many never-translated languages costs one batch pass per language over the full transcript at export time — acceptable by design (export is asynchronous with progress reporting); live path is untouched.

### 2s. Language SWAP restored/verified (user: essential feature, "lost")

**Reported:** the swap-languages button (en→ja, then rotate to ja→en mid-session) appeared lost.

**Findings:** the swap machinery is fully intact in the working tree — `index.html` `#swapBtn`, `presenter.js` swap handler (`applyLanguagePair(tgt, src, {emit})` → `sendConfig`), `languages.js` pickers, server config handler (presence-guard passes: swap sends both src AND tgt explicitly), and the §2m attendee `room_state.presenter_tgt` follow. The full round-trip was verified by simulation: en→ja session, presenter clicks Swap → room `src=ja, presenter_tgt=en` → attendees' view flips to English → subsequent segments translate ja→en live. **If the button appeared "lost" in a deployed build, the likely cause is the §2p attendee/presenter JS crash class or a stale cached frontend — the current working tree is correct.**

**One gap fixed:** the config (swap) handler never registered the NEW target language as touched for the export package — swapping en→ja→en left `en` out of the export languages (only live-translated targets were tracked). Now `remember_room_translation_language(room, cfg["tgt"])` runs on every config/swap, so a bidirectional session exports both directions. Backend-only change (~2 lines).

### 2t. Swap bug confirmed & fixed: post-swap segments stayed in the OLD language ("jp→jp") (user-confirmed, fourteenth round)

**User-confirmed symptom:** swapping en→jp made BOTH presenter and attendees see jp→jp — the translation column showed Japanese for Japanese speech (i.e., no translation), instead of flipping to jp→en.

**Root cause (my §2k parallel fan-out kept a stale source of truth):** `broadcast_segment_to_room` resolved each connection's translation target from the **per-connection `target_language` recorded at join time** (`connection.get("target_language") or presenter_tgt`). That field froze at join: after a swap, the room's `presenter_tgt` changed but every existing connection kept translating/broadcasting in the pre-swap language. The §2m UI follow (room_state.presenter_tgt → chip update) made it *look* like the swap worked while the data path didn't.

**Fix — one language authority:** the room's live `presenter_tgt` is the translation target for **every** connection (presenter and attendees) in all three data paths:
1. `broadcast_segment_to_room` — `_target_for()` returns `presenter_tgt` unconditionally;
2. `send_snapshot_to_current_client` — reads room `presenter_tgt` (was per-connection `attendee_target_language`);
3. `broadcast_snapshots_to_room` — same.

No per-connection stale reads remain (grep-verified). The join-time `target_language` field is bookkeeping only.

**Verified by simulation:** en→ja phase both views get `en→ja`; swap to `ja→en`; presenter speaks Japanese → **both views get `ja→en`** (the reported bug showed `ja→ja`); touched set contains both `ja` and `en` for the export. Backend-only change (`websocket.py`).

### 2u. FINAL language model (user decision, supersedes §2m/§2t): attendee-owned targets; presenter swaps propagate SOURCE only

**User's final word:** "I want the attendee to keep the language which at that point they may have chosen, so it's good that only the source propagates." (Reverses §2m's follow-the-presenter and §2t's room-authority-for-everyone — both were correct for the decision at the time; the decision changed after live use.)

**The model:**
- **Presenter** controls `src` and `presenter_tgt` (swap button / pickers) — as always.
- **Attendee** owns `target_language` on their connection: pick any language (restored picker UI from baseline) or arrive via `?lang=`; the choice **survives presenter source swaps** — the presenter changing what they speak must not change what the attendee reads.
- **Follow-mode sync**: when the presenter changes the target, connections whose `target_language` equals the room's *previous* `presenter_tgt` (i.e., they never made their own choice — they were following) are synced to the new target. Explicit choosers are untouched.
- **Touched-language tracking** covers all paths: attendee switch, presenter config/swap, attendee join.

**Implementation:** `websocket.py` — `set_target_language` re-enabled (updates the connection record + touched set + returns a fresh snapshot); config handler syncs only *followers* (`target_language == previous presenter_tgt`); `broadcast_segment_to_room`/`send_snapshot_to_current_client`/`broadcast_snapshots_to_room` resolve per-connection targets: presenter → `presenter_tgt`, attendee → own `target_language` (fallback room target). `attendee.html`/`attendee-mobile.html`/`attendee/core.js`/`attendee.js` restored to baseline (interactive picker back) — the §2m/§2p static-chip changes are fully reverted; §2q's autojoin/retry and §2p's lesson live on in connection.js/attendee.js where compatible.

**Verified:** three-actor simulation — presenter swaps `en→ja`→`ja→en`; attendee A explicitly chose `pt` (keeps `pt`, sees `ja→pt`); attendee B never chose (follows to `en`, sees `ja→en`); presenter sees `ja→en`; touched = {ja, en, pt}.

**Note:** §2t's "jp→jp" bug is fixed *differently* under this model — the root cause (per-connection target frozen at join) is resolved by the follow-sync on presenter changes plus live attendee switching, not by forcing room authority onto choosers.

### 2w. Presenter room recovery: copyable token + recovery code (user request)

**Request:** "the presenter should be given a room token which they can copy once, which allows them to re-access the same room if needed — as of now they cannot re-enter if they closed the terminal or created a new room."

**The gap:** the presenter token lived only in a browser cookie + was shown implicitly. Lose the cookie (closed browser, cleared storage, new device) and an active room was unrecoverable — pristine-only re-issue refused, and without `PRESENTER_AUTH_SECRET` even the token itself was dead after a restart.

**Implemented:**
- `utils/auth.py`: `recovery_code_for_room()` — a second deterministic HMAC derivation (domain-separated from the token), rendered as `XXXX-XXXX-XXXX` (12 hex chars, ≈2.8e14 space, impractical to guess alongside a 64-char room id); `verify_recovery_code()` is case/dash/space tolerant.
- `routes/rooms.py`: room creation returns `recovery_code` alongside `presenter_token` (shown ONCE at creation); new `POST /api/rooms/{id}/presenter-token` reclaims the token when the body carries the correct `recovery_code` — works regardless of pristine state or secret persistence (that's its purpose: cookie-lost recovery). Wrong/missing code → 403.
- Presenter UI (`index.html`, `presenter/core.js`, `presenter.js`, `presenter.css`): a **Room credentials** note appears under the header showing the room token + recovery code with copy-each buttons, persisted in a cookie as a convenience; an **"Locked out?" re-entry panel** takes a recovery code and reclaims the token; the WS auth-rejection handler now surfaces that panel automatically.

**Security posture:** room code (known to attendees) + recovery code (presenter-only, shown once) = proof of ownership; deterministic derivation means codes stay valid across restarts *when `PRESENTER_AUTH_SECRET` is set* — recommended pairing for this feature. Recovery-code authentication bypasses the pristine-only rule by design but requires a secret that was never broadcast to attendees.

**Verified:** derivation determinism, format, tolerant verification (case/dash/space), wrong-code/cross-room/empty rejection, cookie-loss → reclaim-same-token flow. Both backend + frontend files compile clean.

**§2w hotfix — recovery-code over-issuance closed:** the first cut returned `recovery_code` from EVERY `POST /api/rooms` call, including persistent-secret mode for an ACTIVE room — meaning anyone holding the room code (attendees) could fetch the recovery code and then reclaim the presenter token via the recovery endpoint. Fixed: the recovery code is now issued ONLY alongside the token itself (creation / pristine room / non-persistent-secret mode). Issuance matrix verified: active room + persistent secret + room code alone → no token, no recovery code (403 on the recovery endpoint without the correct code).

**Clarification for the presenter UI (user question):** the ROOM CODE (attendee-facing identity, in share links) and the ROOM TOKEN (presenter credential) are intentionally DIFFERENT values — equality would collapse the auth boundary. A token that CHANGES between page loads without a server restart means `PRESENTER_AUTH_SECRET` is unset (per-process random key); set it in values.yaml to make tokens + recovery codes restart-stable.

**§2w "I still see them" — stale-build diagnosis (user screenshot):** the screenshot showed the FIRST §2w iteration (values printed, no ⧉ chip button, old header text) — every subsequent fix was already in the working tree. Root cause of persistence: browser-cached `index.html`/JS from an earlier image. Defense added: nginx now serves `Cache-Control: no-store, must-revalidate` for `.html`/`.js`/`.css` explicitly (previously only the catch-all location set no-store, and the header did not cover all content types deterministically). Verification recipe for any UI change: view-source the live page and grep for the marker string (e.g. "never shown on screen") — if absent, the browser served a cached copy (hard-refresh: Cmd/Ctrl+Shift+R).

**§2w privacy hardening, final form (user request):** the masked <code> displays were removed entirely — the panel now shows ONLY two labeled copy buttons ("Room token → [Copy room token]", "Recovery code → [Copy recovery code]"). The credential VALUES are never rendered anywhere in the visible DOM: the former display elements survive as hidden anchors to preserve the app's DOM contract (the display function no longer writes to them at all). Values exist solely in cookies/state and reach the clipboard exclusively through the copy buttons. The room CODE stays visible — attendee knowledge by design.

**§2w UX addition (user request):** the room code chip in the presenter header now carries an inline **⧉ copy button** (right after the code, space-efficient) — copies the room code with a ✓ flash confirmation, same pattern as the credential copy buttons. No more hunting for the code in the attendee-link modal.

**§2w follow-up — the token now has a manual entry point (user question: "where do I use the room token?"):** the token's designed use is AUTOMATIC (cookie → WebSocket join payload); the recovery code is the typed credential. But a presenter who saved only the token had no way to present it. The recovery endpoint now accepts EITHER credential — `presenter_token` or `recovery_code` (knowing the token IS proof of ownership; the room code alone still fails with 403). The re-entry input auto-detects a 40-hex token vs a recovery-code format client-side and sends the right field.

**User's 4-step scenario:** (1) presenter en→ja, shares link → attendee sees en→ja; (2) src becomes fr, attendee explicitly picks ja → fr→ja; (3) presenter chooses fr→nd → attendee must KEEP fr→ja; (4) attendee switches to it → fr→it always.

**Flaw in §2u's first cut:** the follow-sync inferred "follower" by VALUE (`target_language == previous presenter_tgt`). Step 2→3 broke it: the attendee's explicit `ja` coincided with the room's previous target `ja`, so the presenter's fr→nd swap treated them as a follower and clobbered their choice to `nd`.

**Fix — explicit flag, not inferred value:** connections carry `has_custom_target` (set by an explicit picker choice, a `?lang=` join request, or `set_target_language`; never set for plain joins without a request). The follow-sync updates only `not has_custom_target` connections. `register_room_connection` takes the flag; `websocket.py` join resolution sets it from the join payload; `set_target_language` sets it on switch.

**Verified:** all 4 user steps pass exactly (en→ja / fr→ja kept across fr→nd / fr→it sticky); touched set accumulates {ja, nd, it}.

### 2x. FINAL-FINAL semantics (user correction): the link is the ONLY presenter→attendee target channel

**User's correction (supersedes the follow-sync in §2u/§2v):** "the presenter only controls the target for the attendees when he creates the room link which he shares with them; from that moment, each attendee controls the target, no matter what the presenter chooses. The source will track the presenter source of course."

**What changed:** the presenter-config follow-sync (which dragged non-flagged attendee connections to the presenter's new target) is REMOVED — it was the bug. The complete model:

- **Link-time**: attendee joins via the presenter's link; a `?lang=` param sets their starting target; a plain link starts them at the room's current target. That single moment is the presenter's target influence.
- **Join-onward**: attendee targets are attendee-owned. Presenter target changes (swap/picker) update ONLY the room's presenter_tgt — existing attendee connections are never rewritten. Each attendee sees `presenter_src → their_target`.
- **Source**: always tracks the presenter (room["src"]), for everyone — the per-attendee translation direction is (presenter's current src) → (attendee's target).
- **Attendee switching**: free at any time via their picker; registers the language for export.

`has_custom_target` remains as join-time bookkeeping but gates no sync — there is no sync. Verified: presenter swaps (fr→nd, nd→es) move ONLY the source column; both attendees' targets stay put; A1 then picks `it` freely.

**§2x follow-up — "selector still shows the presenter's language" (user screenshot):** traced every vector that can put a target into the attendee picker. The tree is clean (room_state adoption removed, snapshot `tgt` is the connection's own echo, server never rewrites attendee connections after join — the ONLY write site is `set_target_language`). The Italian chip in the screenshot is the **link-time value**: `presenter/core.js attendeeLink()` bakes the presenter's CURRENT target into the shared URL as `?lang=<tgt>` — a link copied while the presenter's target was Italian gives every attendee an Italian *starting* target, which then sticks by §2x design (their own, presenter changes don't move it). The card's `Portuguese -> French` shows segments translated under an earlier connection target. No tracking remains; if the presenter wants attendees to start in a different language, they set their target BEFORE copying the link — or attendees change the picker after joining.

**Correction 2 (user, definitive):** the picker showed French while the server kept translating to English — chip/connection divergence. Root cause found: the attendee's picker change is only SENT to the server when the WebSocket is OPEN at that instant (`attendee.js` guard); if the socket is down/reconnecting, the choice was applied LOCALLY and silently lost — picker=fr, server connection=en. And because snapshots only fire on join/switch/boundary (NOT on speech), nothing repaired the chip. **Fix, two halves:** (1) choices made while the socket is down are buffered (`pendingTargetLanguage`) and flushed to the server on the next `joined`; (2) every SEGMENT message now re-adopts `msg.tgt` (the server's truth about this connection) into the picker with notify:false — the picker can never drift from what is actually being delivered; the ONLY way it changes is the attendee's own picker action (or a buffered flush after reconnect). Invariant: chip == server connection target, always.

### 2y. Connect resets the presenter pair + attendee picker default-leak (user report, sixteenth round)

**Bug 1 — "Connect changes the presenter source/target to some default":** the join message carried NO src, so a fresh room booted with server defaults (en/es) and the joined-adoption then reset the presenter's UI to them — the pair the presenter had picked (e.g. jp→en) never reached the server before the join clobbered the UI. **Fix:** the join now carries `src` + `target_language` from the presenter's UI, and the server adopts the pair at join (creating the room with it, or re-asserting the presenter's intent on re-entry). The joined/room_state/snapshot UI-adoption stays for stale-tab safety but now echoes back what the presenter just asserted.

**Bug 2 — "attendee multichoice selector still tracks the presenter target":** two leaks compounding: (a) the attendee JOIN sent `target_language: app.state.targetLanguage` — the hardcoded boot default "es" from core.js — which the server treated as an EXPLICIT choice, pinning fresh attendees to es regardless of the room's actual target; (b) after the previous snapshot-echo change, a picker flip to the presenter's value could persist when the choice never reached the server. **Fix:** the join now sends `target_language: app.state.joinLangParam` — populated ONLY from a real `?lang=` URL param; empty otherwise (server then resolves the room's current target, and the snapshot/segment echo displays it). Combined with §2x's segment re-adoption: the picker ALWAYS equals the server's connection target, changes ONLY via a real picker click (sent, or buffered when the socket is down) — never via presenter activity, never via boot defaults.

**Files:** `presenter/connection.js` (join carries src), `websocket.py` (presenter join adopts UI pair + registers touched), `attendee/connection.js` (join sends joinLangParam), `attendee.js` (joinLangParam from URL only). Backend rebuild required; both frontends ride the same deploy. (user re-report: "attendees target is tracking the presenter's target"):** the server-side sync was deleted in §2x, but the baseline-restore of `attendee/connection.js` resurrected the §2m-era CLIENT-side adoption: the `room_state` handler called `app.setTargetLanguage(msg.presenter_tgt)` on EVERY config broadcast — so every presenter target change flipped the attendee's picker back to the presenter's target, server-independent. Removed (targets are attendee-owned from join; no client adoption of presenter_tgt). The `snapshot` handler's `setTargetLanguage(msg.tgt)` is KEPT and made non-notifying: the snapshot's `tgt` is THIS connection's own resolved target (server echo), so adopting it only ever confirms the attendee's own choice — it can never drag them to the presenter's value. Verified in a full client boot simulation: `room_state{presenter_tgt:fr}` leaves the attendee's select at `ja`; the snapshot echo keeps it consistent; attendee-initiated switches still send `set_target_language` to the server. Lesson recorded: after any baseline file restore, re-audit it against EVERY decision made since — restores resurrect old semantics wholesale.
### 2p. Attendee join crashed before connecting (§2m markup regression, twelfth round)

**Reported:** attendee page stuck at "Room not joined / Disconnected"; chip frozen on Spanish even with a fresh link and a Japanese room.

**Root cause (my §2m regression):** replacing the picker with a static chip **dropped `id="languageTrigger"`**, which `attendee/core.js` still resolves through `refs.languageTriggerEl`. `updateLanguageTrigger()` then threw on `null.querySelector`, aborting the whole attendee IIFE — `connectToRoom()` never ran (no join at all) and the chip never updated. The Spanish chip was just the hardcoded initial value surviving a dead script.

**Fixes:**
- Both attendee pages keep `id="languageTrigger"` on the static chip (DOM contract restored).
- `updateLanguageTrigger()` is null-safe — a ref/markup mismatch can never again abort the attendee app.
- `attendee.js` init: with `?room=` present, the page now fetches `GET /api/rooms/{id}` first and sets the chip from `presenter_tgt` before connecting (the `?lang=` param remains as a fallback only; the room's presenter_tgt governs).

**Lesson recorded:** when replacing interactive markup that other scripts address by id, preserve the id contract **or** remove the consumers in the same change — and keep label-update functions null-safe so a UI regression can never take down the connection flow.

**§2p follow-up — the crash survived one more layer:** `updateLanguageTrigger` was null-guarded, but `setTargetLanguage` also called `renderLanguagePicker`, which dereferenced the REMOVED `languageSearchEl` (`Cannot read properties of null (reading 'value')` at core.js:131) — same abort-the-IIFE effect on the attendee boot. **Definitive fix:** the dead picker machinery is deleted outright (`renderLanguagePicker`/`open`/`close`/`toggle`/`focusLanguageButton` and their four element refs) — with no picker, the functions had no purpose; `setTargetLanguage` now updates only state + the display chip, all chip/field derefs are guarded. Boot-tested headlessly with picker elements absent: full script chain (core → transcript → connection → attendee.js) runs clean, auto-fills the room, auto-connects, and adopts `presenter_tgt` (ja).

**§2q end-to-end verification (final):** a scripted DOM simulation drives the *actual* shipped files through the complete attendee journey — URL `?room=…&lang=ja` → input auto-filled → WebSocket created → join message sent (correct room id) → `joined`/`room_state`/`snapshot` handled → `connected=true` → chip language = presenter_tgt (ja). All five assertions pass. This is the regression gate for the attendee boot: if any future markup/JS change breaks the chain, this test fails before deploy.

**Autojoin confirmation (§2p follow-up):** the `?room=` autojoin code itself was never broken — the §2m crash (null `languageTrigger` in `populateLanguages`, line 23) aborted `attendee.js` *before* the URL-param block (line 30) could fill the input and connect. That's why the input stayed empty, manual paste worked (the Join button was bound at line 5, before the crash), and the chip stayed Spanish (every update threw inside try/catch). The §2p null-safety + id restore fixes the root cause; §2q below hardens the flow further.

### 2q. Autojoin hardening (user confirmation request)

- `attendee.js`: **connects immediately** on `?room=` (no pre-fetch delay); the chip's language is corrected to the room's `presenter_tgt` by the WS join/room_state/snapshot sequence, with a parallel best-effort `GET /api/rooms/{id}` refresh for the pre-join display.
- `attendee/connection.js`: `connectToRoom` gains a **bounded retry ladder** (2 retries, 1.5s apart) when the room isn't reachable yet — covers the attendee opening the share link while the backend is mid-restart, or the presenter still creating the room. Status shows "Retrying… (2/3)"; after the ladder it fails visibly with the reason.

### 2r. "Could not load defaults: Unexpected token '<'" — unrouted `/defaults` through the gateway (thirteenth round)

**Reported:** `core.js:302 Could not load defaults: SyntaxError: Unexpected token '<', "<!doctype "...`.

**Root cause (deployment routing, latent since launch):** the presenter page fetches `GET {HTTP_BASE}/defaults`, but the Istio VirtualService only routes **`/api/*`** and **`/ws/translate`** to the backend. `/defaults` fell through to the frontend nginx, whose `try_files … /index.html` served **index.html with 200** → JSON parse failed. Consequences: the presenter form never received backend defaults (models/URLs/language pair) in gateway deployments — a silent degradation feeding the stale-language race (§2n). Locally (uvicorn direct) it worked, which is why it never surfaced before.

**Fix:** `app/routes/health.py` serves the payload under **both** `/defaults` (local dev) and **`/api/defaults`** (gateway-safe); the presenter fetches `/api/defaults`.

**Routing audit:** swept every frontend `fetch` path — all others already carry `/api/` (rooms, recordings, exports, TTS) or are the WS endpoint; `/defaults` was the sole offender.

**Transition-state hardening (follow-up):** the first frontend using `/api/defaults` against the still-running old backend produced the same warning (404 instead of JSON). `loadDefaultsFromBackend` is now a **candidate ladder**: try `/api/defaults`, then `/defaults`; validate `Content-Type: application/json` and a `{`-leading body before parsing; degrade to one clear console warning if neither yields JSON. Works across all three deployment states: new backend (route 1), direct uvicorn (route 2), gateway+old backend (clean no-op).

### Tier 3 — strategic (the ceiling)
1. **Optimistic raw-transcript echo**: show the ASR partial instantly, shimmer the translation column → perceived latency drops to ASR-only (~−40–60%).
2. **Streaming ASR** (incremental hypotheses): kills full-segment re-transcription; final lands ~0.5–1.0s after speech.
3. **LLM token streaming** (`stream=True` + `segment_delta` + client append): first decent words at TTFT (~+0.1–0.3s post-ASR).
4. Two-tier translation (small model for partials, Qwen3-30B for finals); per-room llm cfg already plumbed.

## 3. Correctness landmines (fix before multi-room / export-during-live)
- **Shared Silero LSTM state across sessions** (verified): `VADIterator.reset_states()` → `model.reset_states()` on the process-global model (`vad.py:24`, `websocket.py:218`, `utils_vad.py:499-501`) — room A finalize corrupts room B mid-speech; export jobs stream whole recordings through the same model. Fix: per-session `torch.jit.load` (~80 lines).
- **Finalize-time LLM error silently skips Postgres persistence**: `ensure_room_translation` inside `broadcast_segment_to_room` is unguarded; a throw unwinds `emit_segment_update` before `persist_finalized_segment` (`websocket.py:291→311`).
- **No auth on the control plane** → **[IMPLEMENTED 2026] presenter-only auth** (attendees intentionally stay open — room code is all they need): deterministic HMAC token per room (`app/utils/auth.py`), issued by `POST /api/rooms` only when the room is created or pristine, required on WS presenter joins to an existing room (`websocket.py` join guard, `role="presenter"` only), stored/sent by the presenter client (`presenter/core.js` cookie `realtime-voice-presenter-token`, `presenter/connection.js` join field). Set `PRESENTER_AUTH_SECRET` for token stability across restarts/replicas; `PRESENTER_AUTH_ENABLED=false` is the kill switch. Rollout notes: without a set secret, tokens are per-process — after a pod restart presenters must reload the page (a pristine-room re-issue lets them back in; an active room stays closed to token-less joins).
- **Deployment drift**: `replicas: 1` hardcoded in `backend-deployment.yaml:8` (values key does nothing); README claims 3 + no sticky sessions (multi-replica would break rooms). In-memory ROOMS + no Redis = single-pod architecture; docs/diagrams say otherwise. Frontend nginx serves static only (WS goes browser→Istio→pod).
- Docs: "incomplete detected via LLM" is stale — it's a regex (`text.py:38-42`).

## 4. Not-latency weaknesses (recorded for later)
Security (F11 above, CORS `*`, plaintext DB creds), no backpressure/seq on audio frames, O(n²) `np.concatenate` per frame, unbounded `room["segments"]`/`emitted_segments` growth over 12h TTL, single global `ROOMS_LOCK`, TTS autoplay-block silently discards audio, `frontend-configmap.yaml` dead artifact, chart version skew (0.6.4 vs appVersion 0.6.0).

## 5. Recommended execution order
1. ~~Tier 0 env tuning + Tier 1 items 1–5~~ ✅ **DONE** (one PR, ~35 lines + env) → measure.
2. ~~Tier 1 items 6–8 + export off-loop + metrics~~ ✅ mostly DONE (metrics still missing) → measure.
3. Tier 2 by observed pain; Tier 3 items 1–2 are the structural ceiling.
4. **Before any multi-room or export-during-live use**: fix the shared-Silero-LSTM-state landmine (§3, per-session `torch.jit.load`).

---

## RELEASE 0.7.0 — verified by the user ("I think we finally fixed it")

## RELEASE — verified by the user ("I think we finally fixed it")

**Chart image tags (user-provided, authoritative):** backend `0.6.10`, frontend `0.6.11`, `pullPolicy: Always` (values.yaml updated to match — my 0.7.0 bump was wrong; the registry tags follow the project's existing 0.6.x line).
Full-session changelog: §2a–§2y above. Headlines:

**Language model (final, §2x/§2y):**
- Presenter owns the room pair at Connect (join carries UI src+tgt; server adopts)
- The share link is the ONLY presenter→attendee target channel (?lang=)
- Attendee targets are attendee-owned forever after join; presenter changes never move them
- Picker ≡ server connection target always (segment re-adoption + buffered flush on reconnect)
- Source always tracks the presenter; per-attendee direction = presenter_src → attendee_target
- Export tracks every touched language from every path

**Reliability/UX:**
- Attendee autojoin with retry ladder; full-chain boot regression gate
- Presenter room recovery: copyable token + recovery code (copy-only UI, never displayed)
- ⧉ inline room-code copy
- /api/defaults routing fix (form populates through the gateway)
- nginx no-store for html/js/css (no more stale-UI ghosts)

**Known user actions outstanding:**
- Set PRESENTER_AUTH_SECRET (tokens + recovery codes survive restarts)
- ASR_API_KEY refresh (user-side 403s seen earlier)

**Backlog (Tier 2/3, unstarted):** per-segment latency metrics, attendee WS auto-reconnect UI, Silero VAD per-session isolation (multi-presenter), streaming ASR, LLM token streaming.
