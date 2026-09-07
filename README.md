# Supply — operational memory lab

A minimal supply-chain agent that learns from simulated incidents and operator feedback. One Gemini agent chooses how to recover a component shortage; Mubit carries conditional operational lessons into fresh executions.

Version 2 tests whether **operator feedback changes future decisions**. The initial conservative policy minimizes downtime; Cedar’s previously unknown replenishment preference accepts a small delay to avoid unnecessary spend. Evaluation scores alignment with that synthetic preference and includes a lesson-removal control. Wins, ties, and regressions remain visible.

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

1. **Run teaching incidents.** The agent makes two live decisions. A deterministic simulator reports consequences, then explicitly labeled scripted operator feedback explains the tradeoff. The agent stores the operator-confirmed applicability / guidance / exceptions verbatim in Mubit. There is no generative lesson-distillation call to invent additional thresholds. Correct initial choices receive confirming feedback.
2. **Compare fresh runs.** Three new cases each run in three conditions: no memory, full memory, and the applicable lesson removed while retaining the unrelated lesson. Inspect choices, downtime at both plants, recovery spend, citations, and lesson provenance. None of the evaluation conditions writes lessons or outcomes to Mubit.
3. **Download JSON trace.** The trace includes actual prompts, decisions, outcomes, lesson IDs, provider usage, the frozen memory snapshot and its SHA-256 hash. Missing usage fields remain null; they are not estimated.

All plants, inventory, feedback, and consequences are fictional. There are no purchasing actions, ERP connections, or external business-system writes.

## What the agent learns

Both conditions start with the same public default: absent an applicable local precedent, minimize combined downtime and then spend. The scenario snapshot provides logistics facts and the order class, but no local tolerance. Operator feedback reveals these **fictional**, deliberately chosen preferences:

- **Stock replenishment:** accept up to two hours of receiving downtime and zero donor downtime; select the cheapest acceptable option, then the least downtime.
- **Firm customer orders:** permit zero receiving and donor downtime; select the cheapest acceptable option.
- **No acceptable option:** minimize combined downtime, then spend.

These rules live in the private evaluator and teaching feedback, not the initial agent prompt. This is acquisition and application of explicit organizational feedback through persistent context; it is not autonomous discovery of a threshold from a single outcome or model-weight training.

| Incident | Test |
| --- | --- |
| T1 | The default buys a substitute to avoid 1.5 hours of replenishment downtime. The operator explains why waiting was preferred. The decision is live, not forced. |
| T2 | A firm order teaches the exception: freight is justified to prevent a small delay. Correct initial choices are confirmed. |
| E1 | A new replenishment case should transfer the tolerance to waiting. |
| E2 | Waiting exceeds the tolerance, but a stock transfer causes only one hour of receiving downtime with no donor disruption. This tests applying the rule to a different action. |
| E3 | A firm order still warrants expediting. This guards against learning “always wait” or “never expedite.” |

Physical outcomes remain deterministic: receiving windows, inspections, donor depletion, downtime, and recovery cost come from current facts. `assess()` scores the private organizational preference, and the UI also reports raw downtime/cost, so an organizational improvement is not mislabeled a physical improvement. The rubric and cases are fixed before runs; they do not adapt to the model’s choices.

**Grounding and context:** lessons are copied exactly from scripted operator confirmation and checked for equality before storage. Full snapshots, decisions, and feedback persist as supporting evidence, but `prompt_lessons()` allowlists only the short lesson and provenance IDs. Audit evidence is displayed separately in the UI and trace; it never enters subsequent decision prompts.

**Influence check:** the third condition removes lessons whose stated order class matches the case, retaining the unrelated lesson. The application compares the actual action and organizational score against the full-memory condition. Citations alone do not count as influence. Removing a lesson changes prompt length, and a single temperature-zero sample is not guaranteed deterministic; this is a useful controlled sensitivity check, not statistical causal proof.

### Observed v2 live run

On 2026-09-07, `sc-check-f929baecc753` taught in one process, then recalled both lessons and evaluated in a new process with real Gemini and Mubit:

| Condition | Aligned cases | Downtime | Recovery spend | Input tokens | Total tokens |
| --- | ---: | ---: | ---: | ---: | ---: |
| No memory | 1/3 | 0 h | $3,000 | 2,058 | 6,883 |
| Full memory | 3/3 | 2 h | $1,750 | 2,886 | 8,221 |
| Applicable lesson removed | 1/3 | 0 h | $3,000 | 2,463 | 7,800 |

Full memory changed the two replenishment choices; removal reversed both. The firm-order case remained unchanged. Input overhead was 40.2%, and total-token overhead was 19.4% compared with no memory. These costs are per condition; running all three conditions costs more than running either alone. No repeated runs were selected to manufacture this result. Model behavior may vary on subsequent runs.

The local artifacts are `.demo/sc-check-f929baecc753-teach.json` and `.demo/sc-check-f929baecc753-compare.json` (ignored by Git). The supplied attachment was an older failed trace; completed local v1 traces confirmed the original 0-win/3-tie result.

## Persistence and experiment isolation

An experiment identifier is saved in `.demo/state.json`. Reuse it in the page after restarting, or set `DEMO_EXPERIMENT=sc-your-id` before the **first** startup to choose an initial identifier. Once state exists, the page's selected identifier takes precedence. **New experiment** creates a new identifier without deleting earlier memory or traces.

The Mubit `run_id` is `<experiment>-judgment-v2`. The version suffix isolates these lessons from v1, whose objective contradicted the new tolerance. Existing memory and JSON traces are retained; the old last-run pointer is cleared once when moving to v2. Each application execution has its own UUID in metadata and trace files. A fresh execution is a fresh process/conversation—not a new Mubit namespace. This deliberately small mapping makes restart persistence explicit without global or linked-run retrieval. Lessons also carry an experiment marker, and returned entries from other experiments are rejected.

Mubit integration uses `Client.recall`, `remember(wait=True)`, and `record_outcome`. Teaching outcomes reinforce only recalled lessons the decision actually cited. Evaluation preloads and freezes recalled lessons for all three cases, then performs **no memory writes or reinforcement**. All three conditions use the same system policy, current snapshot, Gemini model, temperature, and response schema; the only prompt difference is the lesson list. Call order rotates by case. Each call uses `generate_content` directly, with no chat history.

The page only displays memory IDs actually returned by Mubit. An ingested lesson may not be searchable immediately because of server-side ingestion or eligibility rules; this is shown explicitly. Missing either teaching lesson stops the comparison instead of silently falling back to an in-process lesson cache. No mock provider is available in the application.

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

Offline tests use clearly separated test-only model and memory doubles. They prove orchestration and simulator behavior, not live Gemini quality or Mubit availability. The real-provider restart check has passed; results and limitations are reported above.

Optional browser check (Chrome installed and local server running):

```sh
.venv/bin/pip install playwright
.venv/bin/python check_browser.py
```

It loads the real page and intercepts API requests with test-only responses so it cannot accidentally invoke paid providers or alter the selected experiment. API behavior is covered separately by the Python tests. It saves desktop/mobile screenshots in `.demo/`; no fake memory or run trace is written to the app. Playwright is a development-only check, not a runtime dependency.

## Small surface area

- `scenarios.py`: synthetic snapshots, outcome arithmetic, and simulated feedback.
- `agent.py`: Gemini decisions, explicit Mubit calls, teaching and frozen comparison loops.
- `app.py` / `index.html`: local API, durable JSON traces, SSE, and a static web page.
- `test_demo.py` / `check_live.py`: offline checks and opt-in restart integration check.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/status` | Selected experiment, model, demo version, active/last execution, missing config names. |
| `POST /api/experiment` | `{}` creates a namespace; `{"experiment":"sc-existing"}` selects one. |
| `POST /api/runs` | `{"phase":"teach"}` or `{"phase":"compare"}` starts a run; returns its ID. |
| `GET /api/runs/{id}/events` | Replayable SSE; supports `Last-Event-ID`, heartbeat comments, and a terminal `end` event. |
| `GET /api/runs/{id}` | Download the complete or partial JSON trace. |

The UI serves only on loopback by default. Run requests return 503 for missing configuration and 409 when another execution is active. Model choices and lesson citations are validated before simulation. Provider failures produce an explicit failed trace and restore the controls.

Inspired by the [Mubit SRE demo](https://github.com/mubit-ai/mubit-sre-demo). Integration references: [Mubit SDK methods](https://docs.mubit.ai/sdk/sdk-methods) and [Google Gen AI SDK](https://googleapis.github.io/python-genai/).
