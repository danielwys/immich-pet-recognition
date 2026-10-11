# Architecture

```mermaid
flowchart LR
  Immich[Immich REST API] --> Poll[Cursor-based synchronizer]
  Poll --> Queue[SQLite durable queue]
  Queue --> Detector[RF-DETR Nano]
  Detector --> Crops[Versioned crop cache]
  Crops --> ReID[Open Noodle identity embeddings]
  ReID --> Gallery[Reference matching + hard negatives]
  Gallery --> Review[Review UI]
  Review --> Tags[Confirmed Immich tags]
```

## Boundaries

The connector knows about Immich. Detector and recognizer protocols know about images and
embeddings. The service coordinates those stages and owns its metadata. FastAPI exposes the
authenticated review UI and commands. One background thread serializes model work while the
HTTP event loop stays available. SQLite owns the persistent queue; no Redis is needed for v1.

Detection cache keys include asset ID, source revision, detector revision, and confidence threshold.
The source revision derives from the original checksum, modification time, and thumbnail hash,
excluding metadata `updatedAt` so our tag writes cannot cause infinite inference loops. Crops use
preview coordinates; they are not projected into Immich People in this implementation.

Embedding cache keys include detection ID and recognizer revision. Reference membership survives
recognizer changes: the current model re-embeds the retained reference crops. Matching uses the
mean of the best three positive similarities per pet and requires separation from the best rival
pet or per-pet negative. Pet species must match. With no reference gallery, detections still reach
review without an identity guess.

## Reliability

Jobs transition `queued -> processing -> completed`; failures retry twice with backoff before
becoming `failed`. On startup interrupted jobs return to `queued`. Unique job keys deduplicate
overlapping syncs. Detection passes, including zero-pet results, are recorded separately so retries
and recognition changes do not repeat detection.

Incremental scans checkpoint their start time only after all pages succeed, with a one-minute
overlap. Failed partial scans retain enqueued work without advancing the watermark. Every
processing and publication action checks the current asset revision and eligibility. Stale crops
are hidden from the current queue. Tag writes are idempotent and checked for per-asset failure,
including HTTP-200 bulk responses that contain errors. Failed writes need an explicit retry.

Human review decisions are stored per detection/model. Confirmed/rejected decisions are never
overwritten by rematching under that model. Switching models creates fresh review records, while
leaving already-published Immich tags intact. A decision lock prevents a human rejection racing
with publication. The service adds tags only; corrective tag removal happens in Immich.

## Scope and operational limits

One Immich account and one running process. No distributed workers, arbitrary plugin loading,
People writeback, video sampling, schema-migration framework, or automated deletion/retention yet.
The detector/recognizer protocols are code extension points; this is not a plugin marketplace.
Cache growth is unbounded in v1. Runtime status reports errors, but health currently proves only
that the HTTP service is running, not that Immich or CUDA inference is ready.

The service token protects data/control endpoints; the UI shell and health endpoint are public.
Use HTTPS or a trusted SSH tunnel for remote access. No third-party scripts, web fonts, or analytics
are loaded. Demo illustrations and scores are synthetic and isolated from real inference.
