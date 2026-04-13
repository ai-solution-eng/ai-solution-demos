# Real-Time Live Voice Translation System

## System Overview

A production-grade real-time voice translation platform built with FastAPI, enabling live multilingual transcription and translation for meetings and conversations. The system processes audio streams in real-time, providing immediate transcriptions and translations via WebSocket connections.

## Architecture

### Core Components

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   Frontend      │────▶│  FastAPI Backend │────▶│  AI Services    │
│   (HTML/JS)     │◀────│  (WebSocket)     │◀────│  (Whisper/LLM)  │
└─────────────────┘     └──────────────────┘     └─────────────────┘
                               │
                               ▼
                        ┌──────────────────┐
                        │  PostgreSQL DB   │
                        │  (Persistence)   │
                        └──────────────────┘
```

### Real-Time Translation Sequence

```
┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐
│  Client  │     │WebSocket │     │   VAD    │     │ Whisper  │     │   LLM    │
│          │     │ Handler  │     │ Service  │     │   ASR    │     │ Service  │
└────┬─────┘     └────┬─────┘     └────┬─────┘     └────┬─────┘     └────┬─────┘
     │                │                │                │                │
     │─── PCM chunk ─▶│                │                │                │
     │                │── samples ────▶│                │                │
     │                │                │                │                │
     │                │  [speech?]     │                │                │
     │                │◀───────────────│                │                │
     │                │                │                │                │
     │                │── [finalize] ─▶│                │                │
     │                │                │── WAV ────────▶│                │
     │                │                │                │                │
     │                │                │◀── transcript ─│                │
     │                │                │                │                │
     │                │─────────────────────────────────│── text ───────▶│
     │                │                │                │                │
     │◀── segment ────│──────────────────────────────────────────────────│
     │   (translated) │                │                │                │
```

### Multi-Room Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                           FastAPI Application                            │
│  ┌──────────────────────────────────────────────────────────────────┐    │
│  │                      In-Memory ROOMS Dict                        │    │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐               │    │
│  │  │  Room A     │  │  Room B     │  │  Room C     │               │    │
│  │  │  src: en    │  │  src: es    │  │  src: en    │               │    │
│  │  │  tgt: es    │  │  tgt: en    │  │  tgt: fr    │               │    │
│  │  │  ┌─────────┐│  │  ┌─────────┐│  │  ┌─────────┐│               │    │
│  │  │  │Presenter││  │  │Presenter││  │  │Presenter││               │    │
│  │  │  └───┬─────┘│  │  └───┬─────┘│  │  └───┬─────┘│               │    │
│  │  │      │ 1:N  │  │      │ 1:N  │  │      │ 1:N  │               │    │
│  │  │  ┌───┴─────┐│  │  ┌───┴─────┐│  │  ┌───┴─────┐│               │    │
│  │  │  │Attendees││  │  │Attendees││  │  │Attendees││               │    │
│  │  │  │[es][es] ││  │  │[en][fr] ││  │  │[de][it] ││               │    │
│  │  │  └─────────┘│  │  └─────────┘│  │  └─────────┘│               │    │
│  │  └─────────────┘  └─────────────┘  └─────────────┘               │    │
│  └──────────────────────────────────────────────────────────────────┘    │
│                                    │                                     │
│                                    │ async writes                        │
│                                    ▼                                     │
│  ┌──────────────────────────────────────────────────────────────────┐    │
│  │                    SQLAlchemy (asyncpg)                          │    │
│  └──────────────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────────┘
                                     │
                                     │
                            ┌────────┴────────┐
                            │   PostgreSQL    │
                            │   (persistence) │
                            └─────────────────┘
```

### Export Pipeline (Parallel Processing)

```
┌─────────────────┐
│  Meeting WAV    │  (full recording)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  VAD Analysis   │  (detect speech segments)
└────────┬────────┘
         │
         ▼
┌────────────────────────────────────────────────────────────────┐
│                    Overlapping Audio Chunks                    │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐            │
│  │Chunk 1  │  │Chunk 2  │  │Chunk 3  │  │Chunk N  │            │
│  │0:00-0:30│  │0:25-0:55│  │0:50-1:20│  │...      │            │
│  └────┬────┘  └────┬────┘  └────┬────┘  └────┬────┘            │
└───────┼───────────┼───────────┼───────────┼────────────────────┘
        │           │           │           │
        │ concurrent (6 workers)│           │
        ▼           ▼           ▼           ▼
┌─────────────────────────────────────────────────────────────┐
│                    Whisper ASR (parallel)                   │
│  ┌─────────┐  ┌────────┐  ┌────────┐  ┌────────┐            │
│  │text 1   │  │text 2  │  │text 3  │  │text N  │            │
│  └────┬────┘  └───┬────┘  └───┬────┘  └───┬────┘            │
└───────┼───────────┼───────────┼───────────┼─────────────────┘
        │           │           │           │
        ▼           ▼           ▼           ▼
┌─────────────────────────────────────────────────────────────────┐
│              LLM Batch Processing (20 segments/batch)           │
│  ┌─────────────────────────┐  ┌─────────────────────────┐       │
│  │  Batch 1: segments 1-20 │  │  Batch 2: segments 21-40│       │
│  │  → English cleanup      │  │  → English cleanup      │       │
│  │  → Translation (es,fr)  │  │  → Translation (es,fr)  │       │
│  └─────────────────────────┘  └─────────────────────────┘       │
└─────────────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Document Generation                          │
│  Summary → Minutes → Notes (per language) → ZIP Package         │
└─────────────────────────────────────────────────────────────────┘
```

### Database Schema (ERD)

```
┌──────────────────┐       ┌──────────────────┐
│  call_sessions   │       │  export_jobs     │
├──────────────────┤       ├──────────────────┤
│ id (PK)          │◀──────│ session_id (FK)  │
│ room_id          │  1:N  │ status           │
│ status           │       │ progress         │
│ recording_state  │       │ artifact_path    │
│ src_language     │       │ expires_at       │
│ presenter_tgt    │       └──────────────────┘
│ started_at       │
│ ended_at         │       ┌─────────────────────┐
│ expires_at       │       │ session_checkpoints │
└────────┬─────────┘       ├─────────────────────┤
         │                 │ session_id (FK)     │
         │ 1:N             │ summary_en          │
         ▼                 │ minutes_en          │
┌──────────────────┐       └─────────────────────┘
│  call_segments   │
├──────────────────┤       ┌──────────────────┐
│ id (PK)          │       │ recording_chunks │
│ session_id (FK)  │◀──────│ session_id (FK)  │
│ segment_id       │  1:N  │ sequence_no      │
│ revision         │       │ relative_path    │
│ is_final         │       │ mime_type        │
│ source_text      │       │ size_bytes       │
│ source_language  │       └──────────────────┘
│ translations_json│
│ ts_ms            │
│ created_at       │
└──────────────────┘
```

### Kubernetes Deployment Topology

```
┌─────────────────────────────────────────────────────────────────┐
│                        Kubernetes Cluster                       │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              Ingress/Nginx                               │   │
│  │         (TLS termination, routing)                       │   │
│  └─────────────────────┬────────────────────────────────────┘   │
│                        │                                        │
│         ┌──────────────┴──────────────┐                         │
│         │                             │                         │
│         ▼                             ▼                         │
│  ┌─────────────┐              ┌─────────────┐                   │
│  │  Frontend   │              │   Backend   │                   │
│  │  (nginx)    │              │  (FastAPI)  │                   │
│  │  :80/:443   │              │   :8000     │                   │
│  │  3 replicas │              │   3 replicas│                   │
│  └─────────────┘              └──────┬──────┘                   │
│                                      │                          │
│                    ┌─────────────────┼─────────────────┐        │
│                    │                 │                 │        │
│                    ▼                 ▼                 ▼        │
│             ┌────────────┐  ┌────────────┐  ┌─────────────┐     │
│             │  External  │  │ PostgreSQL │  │  Persistent │     │
│             │  Whisper   │  │  (RDS/     │  │  Storage    │     │
│             │  API       │  │  CloudSQL) │  │  (PVC)      │     │
│             │            │  │            │  │             │     │
│             │  External  │  │            │  │  /sessions  │     │
│             │  LLM API   │  │            │  │  /recordings│     │
│             │            │  │            │  │  /exports   │     │
│             └────────────┘  └────────────┘  └─────────────┘     │
└─────────────────────────────────────────────────────────────────┘
```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   Frontend      │────▶│  FastAPI Backend │────▶│  AI Services    │
│   (HTML/JS)     │◀────│  (WebSocket)     │◀────│  (Whisper/LLM)  │
└─────────────────┘     └──────────────────┘     └─────────────────┘
                               │
                               ▼
                        ┌──────────────────┐
                        │  PostgreSQL DB   │
                        │  (Persistence)   │
                        └──────────────────┘
```

### Technology Stack

- **Backend**: FastAPI (Python 3.11+) with async WebSocket support
- **ASR**: OpenAI Whisper (large-v3-turbo) via compatible API
- **LLM**: Qwen3-30B-A3B-Instruct (FP8 quantized) for translation
- **VAD**: Silero Voice Activity Detection (PyTorch)
- **Database**: PostgreSQL with async SQLAlchemy
- **Audio Processing**: FFmpeg, torchaudio, pydub
- **Deployment**: Kubernetes (Helm chart), Docker

## Real-Time Audio Pipeline

### 1. Voice Activity Detection (VAD)

```
Audio Stream (16kHz PCM) → Silero VAD → Speech Segments
```

- **Model**: Silero VAD (loaded via torch.hub)
- **Threshold**: 0.5 (configurable)
- **Sample Rate**: 16kHz
- **Speech Start**: Triggers segment accumulation
- **Speech End**: Initiates tail silence detection

### 2. Segment Detection Logic

```python
# Key thresholds (configurable)
LIVE_COMPLETE_SILENCE_MS = 1300    # Silence after complete sentence
LIVE_INCOMPLETE_FINALIZE_MS = 2200 # Silence after incomplete sentence
MAX_SENTENCE_MS = 18000            # Maximum segment duration
LIVE_PARTIAL_UPDATE_MS = 750       # Partial update frequency
LIVE_MIN_PARTIAL_AUDIO_MS = 700    # Minimum audio for partial
```

**Segment Finalization Triggers**:
1. **Complete silence**: 1.3s of silence after detected speech end (complete sentences)
2. **Incomplete silence**: 2.2s of silence (incomplete sentences detected via LLM)
3. **Maximum length**: 18 seconds forces flush
4. **Manual boundary**: Recording pause/stop events

### 3. Parallel Processing Pipeline

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│  Audio Buffer   │───▶│  Whisper ASR    │───▶│  LLM Translate  │
│  (PCM16)        │    │  (Transcribe)   │    │  (Context-aware)│
└─────────────────┘    └─────────────────┘    └─────────────────┘
                              │                       │
                              ▼                       ▼
                       ┌─────────────────┐    ┌─────────────────┐
                       │  Silence Check  │    │  Emit Segment   │
                       │  (RMS < -45dB)  │    │  (WebSocket)    │
                       └─────────────────┘    └─────────────────┘
```

**Async Processing**:
- Partial segments processed every 750ms
- Queue-based snapshot processing prevents blocking
- Epoch-based invalidation handles config changes
- Concurrent transcription + translation pipeline

### 4. Contextual Translation

The system maintains conversation context for improved translation quality:

```python
# Context window (configurable)
LIVE_CONTEXT_TURNS = 3  # Recent finalized segments

# Translation prompt includes:
# - Recent finalized context (up to 3 turns)
# - Current active segment
# - Source/target language metadata
```

**Translation Workflow**:
1. Receive finalized audio segment
2. Transcribe with Whisper (source language)
3. Build context-aware prompt with recent history
4. Translate with LLM (target language)
5. Emit to all connected clients
6. Cache translation in room state

## Room Management

### Room State Machine

```
┌──────┐   start    ┌───────────┐   pause     ┌───────┐
│ Idle │───────────▶│ Recording │────────────▶│ Paused│
└──────┘            └───────────┘             └───────┘
     ▲                   │                      │
     │                   │ stop                 │
     └───────────────────┴──────────────────────┘
```

### In-Memory State (app/state/rooms.py)

```python
ROOMS = {
    "room-id": {
        "room_id": "room-id",
        "src": "en",
        "presenter_tgt": "es",
        "segments": [...],
        "connections": [...],
        "recording_state": "recording",
        "recording_segment_ids": {...},
        "recording_transcript_items": [...],
        "persisted_session_id": "uuid",
        ...
    }
}
```

**State Persistence**:
- Segments persisted to PostgreSQL in real-time
- Session checkpoints for recovery
- Recording chunks stored on filesystem
- Automatic cleanup after TTL (default 12 hours)

### Connection Management

**Presenter**:
- Creates/owns room
- Configures source/target languages
- Sets Whisper/LLM endpoints
- Controls recording state
- Broadcasts to all attendees

**Attendee**:
- Joins existing room via room code
- Selects target language independently
- Receives translated segments
- No recording control

## Audio Processing Pipeline

### FFmpeg Integration

```python
# PCM16LE → Float32 Tensor (for VAD/Whisper)
def pcm16le_bytes_to_float_tensor(pcm_bytes: bytes) -> torch.Tensor:
    # Convert to numpy int16
    # Normalize to float32 [-1, 1]
    # Return torch tensor
```

**Audio Validation**:
- Minimum duration: 300ms
- RMS threshold: -45 dBFS (silence detection)
- Sample rate: 16kHz mono

### Recording Pipeline

```
Client PCM Chunks → Room Chunk Dir → Finalize → WAV Conversion
                                              ↓
                                    Meeting Audio Package
```

**Recording Storage**:
```
SESSION_STORAGE_ROOT/
├── recordings/
│   └── {room_id}/
│       └── {session_id}/
│           ├── chunk-000000.webm
│           ├── chunk-000001.webm
│           └── meeting_audio.wav
└── exports/
    └── {export_job_id}/
        └── meeting_package.zip
```

## Export System

### Meeting Package Generation

The export pipeline (`app/services/exports.py`) generates comprehensive meeting packages:

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│  Recording WAV  │───▶│  VAD Chunking   │───▶│  Parallel ASR   │
└─────────────────┘    └─────────────────┘    └─────────────────┘
                                                      │
                                                      ▼
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│  ZIP Package    │◀───│  Document Gen   │◀───│  LLM Processing │
└─────────────────┘    └─────────────────┘    └─────────────────┘
```

**Export Configuration**:
```python
EXPORT_CHUNK_SECONDS = 30          # Audio chunk size for ASR
EXPORT_OVERLAP_SECONDS = 5         # Overlap between chunks
EXPORT_MAX_ASR_CONCURRENCY = 6     # Parallel ASR workers
EXPORT_TEXT_BATCH_SEGMENTS = 20    # Segments per LLM batch
EXPORT_TEXT_BATCH_CHARS = 5000     # Characters per batch
EXPORT_VAD_PAD_MS = 250            # VAD segment padding
EXPORT_VAD_MERGE_GAP_MS = 400      # Gap to merge segments
EXPORT_VAD_MAX_UNIT_SECONDS = 15   # Max segment length
```

**Package Contents**:
1. `meeting_audio.wav` - Full meeting recording
2. `transcript.json` - Multilingual transcript with timestamps
3. `summary_en.txt` - AI-generated meeting summary
4. `minutes_en.txt` - Formal meeting minutes
5. `notes_{lang}.txt` - Translated meeting notes (per language)
6. `source_documents/` - Original language documents

### Export Job Lifecycle

```python
# Database-tracked export jobs
ExportJob:
    id: str (UUID)
    session_id: str (FK)
    status: "queued" | "processing" | "completed" | "failed"
    progress: int (0-100)
    stage: str (current processing stage)
    artifact_path: str (ZIP file location)
    expires_at: datetime
```

**Processing Stages**:
1. **Queued**: Job created, waiting in queue
2. **Extracting audio**: Reconstructing WAV from chunks
3. **Building ASR chunks**: VAD-based segmentation
4. **Running ASR**: Parallel Whisper transcription
5. **Cleaning English**: LLM-based ASR cleanup
6. **Translating**: Batch translation to target languages
7. **Generating summary**: AI summary generation
8. **Generating minutes**: AI minutes generation
9. **Translating documents**: Document translation
10. **Packaging**: ZIP archive creation

## Database Schema

### Core Tables

**call_sessions**:
```sql
id                  TEXT PRIMARY KEY
room_id             TEXT INDEX
status              TEXT (active|ended)
recording_state     TEXT (idle|recording|paused|stopped)
src_language        TEXT
presenter_tgt_language TEXT
started_at          TIMESTAMP
ended_at            TIMESTAMP
expires_at          TIMESTAMP INDEX
```

**call_segments**:
```sql
id                  SERIAL PRIMARY KEY
session_id          TEXT FK (CASCADE DELETE)
segment_id          TEXT INDEX
revision            INT
is_final            BOOLEAN
included_in_recording BOOLEAN
source_text         TEXT
source_language     TEXT
translations_json   JSONB
ts_ms               BIGINT
created_at          TIMESTAMP
```

**recording_chunks**:
```sql
id                  SERIAL PRIMARY KEY
session_id          TEXT FK (CASCADE DELETE)
sequence_no         INT
relative_path       TEXT
mime_type           TEXT
size_bytes          BIGINT
created_at          TIMESTAMP
```

**export_jobs**:
```sql
id                  TEXT PRIMARY KEY
session_id          TEXT FK (SET NULL)
status              TEXT
progress            INT
stage               TEXT
detail              TEXT
archive_name        TEXT
artifact_path       TEXT
error               TEXT
expires_at          TIMESTAMP INDEX
```

**session_checkpoints**:
```sql
id                  SERIAL PRIMARY KEY
session_id          TEXT FK (CASCADE DELETE)
window_start_ms     BIGINT
window_end_ms       BIGINT
summary_en          TEXT
minutes_en          TEXT
created_at          TIMESTAMP
```

## WebSocket Protocol

### Connection Flow

```
Client                          Server
  │                               │
  │──── join (role, room_id) ────▶│
  │                               │
  │◀─── joined (role, room_id) ───│
  │                               │
  │◀─── room_state (segments) ────│
  │                               │
  │◀─── snapshot (full state) ────│
  │                               │
  │──── PCM audio chunks ────────▶│
  │                               │
  │◀─── segment (partial/final) ──│
  │                               │
  │──── config (lang, models) ───▶│  (Presenter only)
  │                               │
  │◀─── ack (config applied) ─────│
```

### Message Types

**Client → Server**:
```json
{
  "type": "join",
  "role": "presenter|attendee",
  "room_id": "abc123",
  "target_language": "es",
  "client_session_id": "uuid"
}

{
  "type": "config",
  "src": "en",
  "tgt": "es",
  "whisper": {"base_url": "...", "model": "..."},
  "llm": {"base_url": "...", "model": "..."}
}

{
  "type": "recording",
  "action": "start|pause"
}

{
  "type": "bytes",  // Binary PCM16LE audio
  "data": <audio_chunk>
}
```

**Server → Client**:
```json
{
  "type": "segment",
  "segment_id": "seg-abc-0001",
  "revision": 3,
  "status": "listening|refining|final",
  "is_final": false,
  "original": "Hello world",
  "translation": "Hola mundo",
  "src": "en",
  "tgt": "es",
  "ts_ms": 1234567890
}

{
  "type": "snapshot",
  "role": "presenter",
  "segments": [...],
  "src": "en",
  "tgt": "es",
  "recording_state": "recording"
}
```

## Prompt Engineering

### Live Translation Prompt

```python
def build_live_translation_prompt(src, tgt, current_text, recent_items):
    return f"""
You are a real-time translation engine.
Translate the current active segment from {src_name} to {tgt_name}.
The segment may be incomplete and can be revised as more words arrive.
Use the recent finalized context only to resolve ambiguity.
Translate only the current active segment.
Do not summarize, explain, or answer the speaker.
Return only the translated text with no tags or commentary.

Recent finalized context:
{context_text}

Current active segment ({src_name}):
{current_text.strip()}
"""
```

### Batch Translation Prompt

```python
def build_batch_translation_prompt(batch, target_language):
    return f"""
You are a professional translator.
Translate every item's text into {target_language_name}.
The source text may contain multiple languages.
Return valid JSON only with this exact structure:
{{"items":[{{"id":1,"text":"translated text"}}]}}
Keep the same ids and order.
Do not omit any item.
Do not add commentary.

Input JSON:
{json.dumps(payload)}
"""
```

## Configuration

### Environment Variables

```bash
# AI Services
WHISPER_BASE_URL=https://...
WHISPER_API_KEY=...
WHISPER_MODEL=openai/whisper-large-v3-turbo

LLM_BASE_URL=https://...
LLM_API_KEY=...
LLM_MODEL=Qwen/Qwen3-30B-A3B-Instruct-2507-FP8

# Languages
SOURCE_LANGUAGE=en
TARGET_LANGUAGE=es

# Database
DATABASE_URL=postgresql+asyncpg://...
DB_POOL_SIZE=5
DB_MAX_OVERFLOW=10
SESSION_TTL_HOURS=12

# Export Settings
EXPORT_CHUNK_SECONDS=30
EXPORT_OVERLAP_SECONDS=5
EXPORT_MAX_ASR_CONCURRENCY=6
EXPORT_JOB_TTL_SECONDS=3600

# Audio Processing
SAMPLE_RATE=16000
LIVE_SILENCE_THRESHOLD_MS=1300
LIVE_INCOMPLETE_FINALIZE_MS=2200
LIVE_MAX_SENTENCE_MS=18000
LIVE_PARTIAL_UPDATE_MS=750
LIVE_CONTEXT_TURNS=3
```

### Torch Threading

```bash
TORCH_NUM_THREADS=1
TORCH_NUM_INTEROP_THREADS=1
```

## Recovery & Fault Tolerance

### Session Recovery

On server restart:
1. Load persisted sessions from database
2. Reconstruct room state from segments
3. Resume recording state
4. Invalidate stale connections

```python
async def recover_persisted_rooms():
    # Load all active sessions
    # Rebuild ROOMS dict from DB
    # Restore segment_index, translations, etc.
```

### Chunk Persistence

Recording chunks are persisted in real-time:
- Each uploaded chunk → DB record
- Enables recovery if server crashes
- Finalization reconstructs from chunks

### Export Job Recovery

- Jobs tracked in database with TTL
- Progress checkpoints saved
- Failed jobs can be retried
- Artifacts cleaned up after expiry

## Performance Considerations

### Concurrency Model

- **WebSocket handlers**: Async, non-blocking
- **ASR calls**: Parallel (up to 6 concurrent)
- **Translation**: Batch processing (20 segments/batch)
- **Database**: Async SQLAlchemy with connection pool

### Memory Management

- In-memory room state (fast access)
- Periodic persistence (durability)
- Automatic cleanup (TTL-based)
- Audio buffers cleared after processing

### Latency Optimization

- Partial updates every 750ms
- Context-aware translation (reduces ambiguity)
- Parallel ASR + translation pipeline
- Epoch-based invalidation (no stale processing)

## Deployment

### Kubernetes (Helm)

```yaml
# charts/values.yaml
replicaCount: 3
resources:
  limits:
    cpu: 2
    memory: 4Gi
  requests:
    cpu: 1
    memory: 2Gi

env:
  - name: DATABASE_URL
    valueFrom:
      secretKeyRef:
        name: db-secret
        key: url
```

### Docker

```dockerfile
FROM python:3.11-slim
RUN apt-get install -y ffmpeg
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app /app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

## Monitoring & Debugging

### Logging Points

- VAD events (speech start/end)
- ASR latency (per transcription)
- Translation latency (per segment)
- WebSocket connections/disconnections
- Export job progress

### Key Metrics

- Active rooms count
- WebSocket connections
- Average segment latency
- Export queue depth
- Database connection pool usage

## Security Considerations

- API keys via environment variables (not hardcoded)
- Room isolation (no cross-room data leakage)
- Session TTL (automatic cleanup)
- Database FK constraints (data integrity)
- CORS configured for frontend origin

## Limitations & Trade-offs

1. **In-memory room state**: Fast but not horizontally scalable without external state store
2. **Silero VAD**: Requires PyTorch (memory footprint ~500MB)
3. **WebSocket connections**: Limited by server capacity (recommend horizontal scaling)
4. **Recording storage**: Local filesystem (consider S3/GCS for production)
5. **Export concurrency**: Limited by ASR API rate limits

## Future Enhancements

- Redis-backed room state for horizontal scaling
- Speaker diarization for multi-speaker meetings
- Real-time caption export (WebVTT, SRT)
- Custom vocabulary/domain adaptation
- Streaming translation output (token-by-token)
- Multi-room broadcast/presentation mode
