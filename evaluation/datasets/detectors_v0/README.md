# detectors_v0

35 labelled agent traces for the deterministic detectors. Each case is one run recorded with the real Rootsignal SDK and stored as OTLP/JSON (`cases/<id>.json`, hex IDs per the OTLP spec). One case (`gen_ai_dialect_loop`) is hand-built to mimic a non-Rootsignal GenAI instrumentation. Labels live in `labels.json`.

Regenerate (trace and span IDs change, labels don't):

```bash
uv run python evaluation/datasets/detectors_v0/generate.py
make eval-detectors
```

## Labelling rubric

A case's `expected` set lists the detectors a reviewer **would want to fire** on that trace:

- **positive**: the failure mode is present and worth flagging.
- **negative**: healthy, or below a threshold on purpose. An expected detector can still appear when another failure is present (e.g. `single_retry_success` expects `tool_error` but not `excessive_retries`).
- **hard**: realistic traces where a rule's literal condition and a human's judgement differ, e.g. intentional polling. These are where false positives and negatives are expected, and the `note` says which.

Timing for latency cases is set by editing span end times after recording; everything else is exactly what the SDK emitted.

## Limits

Synthetic, small and written alongside the detectors: it measures rule correctness and catches regressions, not real-world precision or recall. Add real labelled traces (increment 10) before quoting any metric externally.
