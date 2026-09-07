# Supply — operational memory lab

A minimal supply-chain agent that learns from simulated incidents and operator feedback. One Gemini agent chooses how to recover a component shortage; Mubit carries conditional operational lessons into fresh executions.

The comparison measures **combined production downtime first, recovery spend second**. It reports wins, ties, and regressions honestly. The model may make the right choice before learning anything; this is a demonstration, not a benchmark claiming guaranteed improvement.

## Run locally

Python 3.11+ and credentials for Gemini and a running Mubit instance are required for live runs.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# Fill GEMINI_API_KEY, MUBIT_ENDPOINT, and MUBIT_API_KEY in .env.
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 7870
```

Open [localhost:7870](http://localhost:7870). The page loads without credentials and identifies missing configuration. The default model is `gemini-3.6-flash`; set `GEMINI_MODEL` to another available Gemini model and restart to change it. Credentials remain server-side.

1. **Run teaching incidents.** The agent makes two live decisions. A deterministic simulator reports consequences, then explicitly labeled scripted operator feedback explains the tradeoff. The same agent distills an applicability / guidance / exceptions lesson and stores it in Mubit. Correct initial choices receive confirming feedback.
2. **Compare fresh runs.** Three new cases run with and without recalled memory. Inspect choices, downtime at both plants, recovery spend, citations, and lesson provenance. Neither evaluation arm writes lessons or outcomes to Mubit.
3. **Download JSON trace.** The trace includes actual prompts, decisions, outcomes, lesson IDs, provider usage, the frozen memory snapshot and its SHA-256 hash. Missing usage fields remain null; they are not estimated.

All plants, inventory, feedback, and consequences are fictional. There are no purchasing actions, ERP connections, or external business-system writes.

## What the agent learns

The operating principle is explicit in both arms: protect production at both sites, then minimize recovery spend. Experience teaches conditional ways to apply it:

| Incident | Operational judgment |
| --- | --- |
| Teaching 1 | Compare **usable arrival time**, including receiving windows and inspection, before paying for freight. |
| Teaching 2 | Check **donor coverage through replenishment** before a stock transfer; consider an approved substitute. |
| Evaluation 1 | Changed quantities and dates favor a transfer with adequate donor coverage. |
| Evaluation 2 | A cheap transfer shifts downtime to another plant; a substitute protects both. |
| Evaluation 3 | Expediting really does protect production; prior lessons must not become an unconditional ban. |

All relevant current constraints are available to both arms. `snapshot()` explicitly excludes scenario themes, narrative titles, evaluator results, and future feedback. The evaluator computes receiving admission, inspection completion, donor stock depletion, and downtime from the current facts; no expected-action lookup is used for scoring. The simulated feedback can explain corrective reasoning, but the lesson prompt forbids claiming that an unexecuted alternative was observed.

## Persistence and experiment isolation

An experiment identifier is saved in `.demo/state.json`. Reuse it in the page after restarting, or set `DEMO_EXPERIMENT=sc-your-id` before the **first** startup to choose an initial identifier. Once state exists, the page's selected identifier takes precedence. **New experiment** creates a new identifier without deleting earlier memory or traces.

The experiment is one persistent Mubit `run_id`, scoped to that experiment. Each application execution has its own UUID in metadata and trace files. A fresh execution is a fresh process/conversation—not a new Mubit namespace. This deliberately small mapping makes restart persistence explicit without global or linked-run retrieval. Lessons also carry an experiment marker, and returned entries from other experiments are rejected.

Mubit integration uses `Client.recall`, `remember(wait=True)`, and `record_outcome`. Teaching outcomes reinforce only recalled lessons the decision actually cited. Evaluation preloads and freezes recalled lessons for all three cases, then performs **no memory writes or reinforcement**. Both arms use the same system policy, current snapshot, Gemini model, temperature, and response schema; the only prompt difference is the lesson list. Call order alternates by case. Each call uses `generate_content` directly, with no chat history.

The page only displays memory IDs actually returned by Mubit. An ingested lesson may not be searchable immediately because of server-side ingestion or eligibility rules; this is shown explicitly. An empty evaluation recall stops the comparison instead of silently falling back to an in-process lesson cache. No mock provider is available in the application.

Traces persist under `.demo/runs/`; they are local audit artifacts and are never used as agent memory. Restarting during a run marks its trace as interrupted. Use one server process / one worker: there is one active execution and one selected experiment. Parallel workers and production deployment are out of scope.

## Checks

```sh
# Offline simulator, agent-boundary, memory-isolation, API, and SSE checks
.venv/bin/python -m unittest -v test_demo

# Page API error handling (Node.js; no dependencies)
node --test test_api.cjs

# Opt-in: real API calls, a new isolated experiment, and persistent Mubit writes
.venv/bin/python check_live.py
```

The live check teaches in one subprocess, exits it, then recalls both teaching incidents and compares in a second subprocess. It writes separate teaching/comparison traces under `.demo/`. It fails rather than fabricating evidence if credentials are absent or persisted lessons cannot be retrieved. It does not delete the created experiment.

Offline tests use clearly separated test-only model and memory doubles. They prove orchestration and simulator behavior, not live Gemini quality or Mubit availability. Live validation has not been performed in this checkout because credentials are not configured.

Optional browser check (Chrome installed, server running without provider credentials):

```sh
.venv/bin/pip install playwright
.venv/bin/python check_browser.py
```

It checks the real configuration/identifier controls, then intercepts browser requests with explicitly test-only teaching and comparison traces. It saves desktop/mobile screenshots in `.demo/`; no fake memory or run trace is written to the app. Playwright is a development-only check, not a runtime dependency.

## Small surface area

- `scenarios.py`: synthetic snapshots, outcome arithmetic, and simulated feedback.
- `agent.py`: Gemini decisions, explicit Mubit calls, teaching and frozen comparison loops.
- `app.py` / `index.html`: local API, durable JSON traces, SSE, and a static web page.
- `test_demo.py` / `check_live.py`: offline checks and opt-in restart integration check.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/status` | Selected experiment, model, active/last execution, missing config names. |
| `POST /api/experiment` | `{}` creates a namespace; `{"experiment":"sc-existing"}` selects one. |
| `POST /api/runs` | `{"phase":"teach"}` or `{"phase":"compare"}` starts a run; returns its ID. |
| `GET /api/runs/{id}/events` | Replayable SSE; supports `Last-Event-ID`, heartbeat comments, and a terminal `end` event. |
| `GET /api/runs/{id}` | Download the complete or partial JSON trace. |

The UI serves only on loopback by default. Run requests return 503 for missing configuration and 409 when another execution is active. Model choices and lesson citations are validated before simulation. Provider failures produce an explicit failed trace and restore the controls.

Inspired by the [Mubit SRE demo](https://github.com/mubit-ai/mubit-sre-demo). Integration references: [Mubit SDK methods](https://docs.mubit.ai/sdk/sdk-methods) and [Google Gen AI SDK](https://googleapis.github.io/python-genai/).
