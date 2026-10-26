# dogfood_v0

51 real traces from the docs assistant (`examples/src/rootsignal_examples/docs_assistant/`), one per question in its fixed question set. Recorded on 2026-10-26.
- **Agent:** LangGraph 1.2.14, agent version v1, with LangGraph's default tool-error handling.
- **Model:** `llama3.1:8b` on Ollama 0.35.0, temperature 0, seed 7.
- **Instrumentation:** `openinference-instrumentation-langchain` 0.1.79 plus the Rootsignal SDK, exporting through the SDK's provider.

Each `cases/<question id>.json` is the trace's OTLP/JSON export request, as sent to the API. The IDs are hex, per the OTLP spec. One edit was made after recording: the local repository path in OpenInference's tracebacks (status messages and exception events, in a01 and n04) was replaced with `<repo>`, because it contained the home directory and username. That is the privacy issue noted in the field report.

These are the first traces in `evaluation/datasets/` that weren't constructed: the failures happened on their own.

## Labels (`labels.json`)

The `expected` and `category` fields use the same rubric as [detectors_v0](../detectors_v0/README.md):
- `expected` lists the detectors a reviewer would want to fire on the trace.
- `category` is one of:
  - `positive`: a detector should fire;
  - `negative`: healthy;
  - `undetectable`: the run failed, but in a way no current detector can see.

Two more fields record a review of the final answer against the docs:
- `answer_review` is one of `correct`, `partial`, `wrong`, `missed` (said "not in the docs" for something that is), `false_or_skipped_action`, or `crashed`.
- `note` explains the review.

**Who labelled:** the AI assistant that wrote the agent and the detectors, by reading each trace's spans and answer against the docs. Nobody else has reviewed the labels. See `disclaimer.txt`.

## Reproduce

```bash
# needs a running API + worker, Ollama with llama3.1:8b, and a key with ingest+read
ROOTSIGNAL_API_KEY=... uv run python -m rootsignal_examples.docs_assistant.run_questions \
    --record evaluation/datasets/dogfood_v0/cases --out evaluation/reports/dogfood_v0_run_v1.json
uv run python evaluation/run_detector_eval.py --dataset dogfood_v0
```

A new run produces new traces and possibly different answers: the model is deterministic in practice at temperature 0 with a fixed seed, but Ollama doesn't guarantee it. Labels belong to the recorded traces, not to the questions.

## Limits

- One agent, one model, and one run, with 51 traces.
- Only 3 (trace, detector) positives, so per-detector precision and recall from this set mean almost nothing.
- The useful signal is in the `undetectable` traces (18 of 51) and in the instrumentation findings; see [docs/FIELD_REPORT_DOGFOOD.md](../../../docs/FIELD_REPORT_DOGFOOD.md).
