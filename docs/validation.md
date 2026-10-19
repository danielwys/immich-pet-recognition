# Validation

## Published assignment correction checked on 19 October 2026

**57 tests pass.** Added coverage checks published reassignment and untagging, correcting positive
and negative examples, stale-assignment preconditions, shared-photo tags for multiple animals,
retry after a partial remote write and restart, reversal of a partial correction, bulk untag
response validation, and verification that Immich actually removed the tag. The operation uses
the existing `asset.read` and `tag.asset` scopes and does not delete photos.

## Review redesign checked on 19 October 2026

**47 tests pass**, plus Ruff lint/formatting and a JavaScript syntax check. Added checks cover
bulk assignment with example enrollment, immediate fresh suggestions using cached vectors,
visible gate results, identity corrections, durable rematch recovery, partial batch failures,
published-tag protection, existing confirmations enrolled without retagging, and excluding a
suggested identity without discarding the crop. Token-protected defaults and explicit direct
access remain covered.
The HTML is not cached, and asset URLs include a content hash to prevent mixed UI versions after updates.

Browser checks with isolated synthetic demo data covered multi-photo assignment, example-count
progress, per-photo suggestions, disabling example enrollment, reusing existing confirmations,
and bulk acceptance of strong suggestions. A separate demo with a three-example gate exercised
the strong-match controls; the production default remains ten. The layout was inspected at
desktop and 390-pixel phone width. These checks verify workflow behavior, not identity accuracy.

## Checked on 11 October 2026

**34 tests pass.** Lint, formatting, source distribution, and wheel builds pass. The test client
emits one upstream Starlette/httpx deprecation warning; it does not affect the passing checks.

- Functional tests cover API authentication, enrollment, crop serving, review decisions, hard
  negatives, competing identities, insufficient references, cursor pagination, idempotent tag
  writes, HTTP-200 bulk failures, queue recovery, retry backoff, source changes, model changes,
  retained reference re-embedding, atomic queue claims, partial-sync checkpoint preservation,
  background-worker publication, and isolation between real and demo data directories.
- Ruff lint and formatting checks.
- GitHub Actions passed the test/lint suite and built both CPU and CUDA Docker images on Linux.
  See the [successful initial run](https://github.com/danielwys/immich-pet-recognition/actions/runs/38131299043).
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

- NVIDIA driver/container access and actual CUDA model execution on the RTX 3080.
- Live Immich 3.3.1 API key permissions, connectivity, preview retrieval, and tag writeback.
- Identity accuracy and thresholds on a held-out set of the user's pet photos.

The automated suite mocks Immich's responses and model stages. The real-model smoke test checks
the published model contract and execution, not individual-pet recognition accuracy. The GitHub
workflow runs tests and builds both container variants on pushes and pull requests. A successful
CUDA image build does not verify GPU execution; the hosted runner has no RTX 3080.
