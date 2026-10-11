# Validation

## Checked on 11 October 2026

**34 tests pass.** Lint, formatting, source distribution, and wheel builds pass. The test client
emits one upstream Starlette/httpx deprecation warning; it does not affect the passing checks.

- Functional tests cover API authentication, enrollment, crop serving, review decisions, hard
  negatives, competing identities, insufficient references, cursor pagination, idempotent tag
  writes, HTTP-200 bulk failures, queue recovery, retry backoff, source changes, model changes,
  retained reference re-embedding, atomic queue claims, partial-sync checkpoint preservation,
  background-worker publication, and isolation between real and demo data directories.
- Ruff lint and formatting checks.
- Real CPU inference using the pinned RF-DETR Nano and **Pet Recognition Large** ONNX files
  against Roboflow's public [dog sample](https://media.roboflow.com/dog.jpg), linked in the
  [upstream README](https://github.com/roboflow/rf-detr/blob/develop/README.md).
  One dog was detected at confidence 0.8613 with pixel box `[61,247,652,933]`.
  Recognition produced a finite, normalized 512-dimensional embedding (norm 1.0).
  Initial detection/load/download took 6.54 seconds; recognition/load/download took 18.10 seconds
  on the development Mac. These are smoke checks, not accuracy or NAS throughput benchmarks.
- Browser checks of the review UI: saved a positive reference, verified its gallery count, then
  confirmed a crop and verified it moved out of the pending queue. Demo remains clearly labeled
  with synthetic illustrations and simulated scores, with no Immich writeback.

## Still requires the NAS

- Both CPU and CUDA container builds (Docker is unavailable in the current local environment).
- NVIDIA driver/container access and actual CUDA model execution on the RTX 3080.
- Live Immich 3.3.1 API key permissions, connectivity, preview retrieval, and tag writeback.
- Identity accuracy and thresholds on a held-out set of the user's pet photos.

The automated suite mocks Immich's responses and model stages. The real-model smoke test checks
the published model contract and execution, not individual-pet recognition accuracy. The GitHub
workflow is configured to run tests and build both container variants when the repository is pushed.
