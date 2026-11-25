# AI findings per ignition zone

Step 2 of the prototype: a vision model reads each Home Ignition Zone against the
[aerial checklist](FIRESMART_CHECKLIST.md), and Python re-checks every answer.

## Flow (one property)

1. `pipeline/ai/zones.py` cuts one 15 cm orthophoto crop per zone with the zone ring (zone colour),
   the LiDAR house outline (cyan) and numbered LiDAR tree tops (magenta `T12`) drawn on it, and
   collects that zone's LiDAR facts (trees, heights, distances, canopy cover, crown gaps, slope).
2. `pipeline/ai/prompt.py` builds the same system prompt, text and **fixed JSON schema** for every
   backend. Per item: `status` (`ok` / `concern` / `cant_tell`), `confidence` 0-1, short `reason`,
   optional pixel `location`. Image text is declared untrusted data.
3. `pipeline/ai/backends.py` sends it to either backend:
   - **local**: Ollama on this Mac, `qwen3-vl:8b-instruct` (already installed, 6.1 GB, Apache 2.0),
     JSON-schema constrained output, temperature 0.
   - **claude**: Claude API via the official `anthropic` SDK, `claude-opus-5-5` at effort `medium`,
     structured outputs (`output_config.format`), server-side refusal fallback enabled
     (`fallbacks: "default"`). Key from `ANTHROPIC_API_KEY` in `.env`.
4. `pipeline/ai/validate.py` re-checks each answer:
   - **Schema**: valid JSON, every item exactly once (missing -> `cant_tell` and flagged,
     duplicates and unknown ids dropped), confidence clipped to 0-1, locations inside the crop.
   - **LiDAR**: rules per item give `agree` / `disagree` / `partial` / `n/a` with a note. Example:
     a reason saying "no conifers within 10 m" when LiDAR measured trees there is a `disagree`.
     Tree labels cited in a reason that were not drawn for that zone are flagged.
     Neither source wins automatically; disagreements are shown for a person to judge.
5. Results: `data/processed/<id>/ai/<backend>.json`; every API call is logged to
   `data/ai_calls.jsonl` (tokens, cost, latency, request id). All git-ignored.

## Spend control (Claude)

- Before each call the backend counts input tokens (free endpoint) and refuses the call if
  *spent so far + worst case* (input + all 8000 max output tokens) would pass the per-run cap.
- Cap: `FS_AI_RUN_CAP_USD` (default `0.50` per property run). Prices are in `PRICES` in
  `backends.py` (Opus 5.5: $4 in / $20 out per million tokens).
- A rejected key stops the run immediately without writing results; the app checks the key with
  the free Models API and shows the reason in the UI.

## UI

- Map (`/`): "AI findings" panel: pick a backend, Run, and pins appear per finding, coloured by
  status (red concern, grey can't tell; OK items are counted, not pinned). An orange ring means
  the LiDAR re-check disagrees. Pins without a model-given spot are placed around the ring and
  labelled approximate. The popup shows the zone crop with the spot marked.
- Compare (`/compare.html`): same property and zones, local and Claude side by side with agreement
  per item, latency, tokens, cost and the LiDAR verdicts. Each answer can be marked correct or
  wrong; marks go to `data/labels.sqlite` (git-ignored) and form a small labelled test set.

## CLI

```bash
.venv/bin/python -m pipeline.ai <property_id> local
.venv/bin/python -m pipeline.ai <property_id> claude
```

## Settings (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | (blank) | Claude backend; leave blank to use local only |
| `FS_AI_RUN_CAP_USD` | `0.50` | Max Claude spend per property run |
| `FS_CLAUDE_MODEL` | `claude-opus-5-5` | Claude model id (must be in `PRICES`) |
| `FS_CLAUDE_EFFORT` | `medium` | `low` / `medium` / `high` |
| `FS_OLLAMA_MODEL` | `qwen3-vl:8b-instruct` | Local vision model |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama server |
