"""One agent, explicit memory calls, no conversation carried between decisions."""
import hashlib
import json
import os
import time
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from scenarios import DEMO_VERSION, TEACHING, EVALUATION, assess, evaluate, feedback, org_score, snapshot

POLICY = (
    "You resolve component shortages for Cedar Manufacturing. Choose exactly one eligible "
    "option, balancing production continuity and recovery spend using established local "
    "judgment when available. No organization-specific tradeoff is supplied initially. "
    "Without an applicable local precedent, use the conservative default: minimize combined "
    "receiving and donor downtime, then recovery spend. Apply a supplied operator-confirmed "
    "precedent only to its stated order class and respect its exceptions and current facts. "
    "Do not invent local thresholds. Return a concise rationale, not private chain-of-thought. "
    "Cite only supplied lesson IDs actually used."
)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    option_id: str
    rationale: str = Field(min_length=1, max_length=1800)
    lesson_ids: list[str]


class Lesson(BaseModel):
    model_config = ConfigDict(extra="forbid")
    applies_to: Literal["replenishment", "firm"]
    applicability: str = Field(min_length=1, max_length=200)
    guidance: str = Field(min_length=1, max_length=500)
    exceptions: str = Field(min_length=1, max_length=350)


def missing_config():
    return [key for key in ("GEMINI_API_KEY", "MUBIT_ENDPOINT", "MUBIT_API_KEY")
            if not os.getenv(key, "").strip()]


class Gemini:
    def __init__(self):
        from google import genai
        self.model = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"],
                                  http_options={"timeout": 90000})

    def generate(self, system, payload, schema):
        started = time.monotonic()
        from google.genai.errors import APIError
        try:
            response = self.client.models.generate_content(
                model=self.model, contents=json.dumps(payload, sort_keys=True),
                config={"system_instruction": system, "temperature": 0,
                        "response_mime_type": "application/json",
                        "response_json_schema": schema.model_json_schema(),
                        "automatic_function_calling": {"disable": True}},
            )
        except APIError as exc:
            message = f"Gemini HTTP {exc.code}: {exc.message}"
            for key in ("GEMINI_API_KEY", "MUBIT_API_KEY"):
                if os.getenv(key):
                    message = message.replace(os.environ[key], "[redacted]")
            raise ValueError(message) from None
        usage = response.usage_metadata
        stats = dict(model=self.model, calls=1, elapsed_seconds=round(time.monotonic()-started, 3),
                     prompt_tokens=getattr(usage, "prompt_token_count", None),
                     output_tokens=getattr(usage, "candidates_token_count", None),
                     thinking_tokens=getattr(usage, "thoughts_token_count", None),
                     total_tokens=getattr(usage, "total_token_count", None))
        return schema.model_validate_json(response.text or ""), stats

    def close(self):
        self.client.close()


class Memory:
    def __init__(self, experiment):
        from mubit import Client
        self.experiment = experiment
        # ponytail: one persistent Mubit run per experiment; executions are metadata.
        self.client = Client(endpoint=os.environ["MUBIT_ENDPOINT"],
                             api_key=os.environ["MUBIT_API_KEY"], transport="http",
                             run_id=f"{experiment}-{DEMO_VERSION}", timeout_ms=60000)

    def recall(self, query):
        result = self.client.recall(query=query, limit=10, entry_types=["lesson"],
                                    evidence_only=True, include_working_memory=False,
                                    include_linked_runs=False, prefer_current_run=True)
        lessons = []
        for entry in result.get("evidence") or []:
            content = entry.get("content") or entry.get("text") or ""
            marker = f"[experiment:{self.experiment}] [demo:{DEMO_VERSION}]\n"
            if not content.startswith(marker) or not entry.get("id"):
                continue
            compact, separator, evidence = content[len(marker):].partition("\nSupporting observed incident: ")
            try:
                record = json.loads(compact)
                lesson = Lesson.model_validate(record["lesson"]).model_dump()
                if record["source_case"] not in ("T1", "T2"):
                    continue
                audit = json.loads(evidence) if separator else {}
            except (ValueError, KeyError, TypeError):
                continue
            lessons.append(dict(id=str(entry["id"]), source_case=record["source_case"],
                                **lesson, evidence=audit, confidence=entry.get("confidence")))
        return lessons

    def remember(self, c, execution, lesson, decision, outcome, operator):
        # Only verbatim operator-confirmed content is stored; no invented threshold can enter.
        if lesson != operator["confirmed_lesson"]:
            raise ValueError("Lesson differs from the operator-confirmed evidence")
        evidence = dict(snapshot=snapshot(c), decision=decision, outcome=outcome,
                        operator_feedback=operator, execution_id=execution)
        content = (f"[experiment:{self.experiment}] [demo:{DEMO_VERSION}]\n"
                   + json.dumps(dict(source_case=c["id"], lesson=lesson), sort_keys=True)
                   + "\nSupporting observed incident: " + json.dumps(evidence, sort_keys=True))
        stored = self.client.remember(
            content=content, intent="lesson", lesson_type="success" if operator["verdict"] == "Confirmed" else "failure",
            lesson_scope="run", lesson_importance="high", agent_id="shortage-agent",
            upsert_key=f"{DEMO_VERSION}:{self.experiment}:{c['id']}",
            item_id=f"{execution}-{c['id']}", wait=True, timeout_ms=60000,
            metadata=dict(experiment=self.experiment, execution_id=execution,
                          source_case=c["id"], demo_version=DEMO_VERSION),
        )
        if stored.get("error") or stored.get("status") in ("failed", "error"):
            raise ValueError("Mubit did not finish ingesting the teaching lesson")
        return self.recall("Cedar stock replenishment firm customer order recovery tradeoffs")

    def record(self, ids, operator, outcome):
        for lesson_id in ids:
            success = operator["verdict"] == "Confirmed"
            self.client.record_outcome(reference_id=lesson_id,
                outcome="success" if success else "failure", signal=1.0 if success else 0.0,
                rationale=f"Simulated teaching result: {json.dumps(outcome)}. {operator['text']}",
                verified_in_production=False)

def prompt_lessons(lessons):
    # Full historical snapshots remain in the trace/UI, never in decision context.
    fields = ("id", "source_case", "applies_to", "applicability", "guidance", "exceptions")
    return [{key: deepcopy(lesson[key]) for key in fields} for lesson in lessons]


def decide(model, c, lessons):
    payload = dict(snapshot=snapshot(c), lessons=prompt_lessons(lessons))
    decision, usage = model.generate(POLICY, payload, Decision)
    d = decision.model_dump()
    eligible = {o["id"] for o in c["options"] if o["approved"]}
    if d["option_id"] not in eligible:
        raise ValueError("Model chose an unknown or ineligible option")
    if not set(d["lesson_ids"]) <= {lesson["id"] for lesson in lessons}:
        raise ValueError("Model cited a lesson that was not recalled")
    return d, usage, payload


def teach(model, memory, execution, emit):
    for c in TEACHING:
        emit("case_started", phase="teach", case=c["id"], title=c["title"], snapshot=snapshot(c))
        lessons = memory.recall(f"{c['topic']} shortage operational judgment")
        emit("recall", phase="teach", case=c["id"], arm="memory", lessons=lessons)
        decision, usage, payload = decide(model, c, lessons)
        emit("decision", phase="teach", case=c["id"], arm="memory",
             decision=decision, usage=usage, prompt=payload)
        outcome = evaluate(c, decision["option_id"])
        operator = feedback(c, decision, outcome)
        emit("outcome", phase="teach", case=c["id"], arm="memory", outcome=outcome, feedback=operator, judgment=operator["judgment"])
        lesson = Lesson.model_validate(operator["confirmed_lesson"])
        emit("lesson_drafted", phase="teach", case=c["id"], lesson=lesson.model_dump(),
             grounding="Verbatim operator-confirmed lesson; no generated generalization")
        recalled = memory.remember(c, execution, lesson.model_dump(), decision, outcome, operator)
        memory.record(decision["lesson_ids"], operator, outcome)
        emit("lesson_stored", phase="teach", case=c["id"], lesson=lesson.model_dump(),
             lessons=recalled, recorded_lesson_ids=decision["lesson_ids"],
             searchable=any(l["source_case"] == c["id"] for l in recalled))


def compare(model, memory, execution, emit):
    # Freeze actual Mubit recall once. No write or outcome reinforcement in evaluation.
    recalled = deepcopy(memory.recall("Cedar stock replenishment firm customer order recovery tradeoffs"))
    if {lesson["source_case"] for lesson in recalled} != {"T1", "T2"}:
        raise ValueError("Both v2 teaching lessons must be recalled. Run teaching incidents for this "
                         "experiment first; legacy v1 memory is intentionally isolated.")
    digest = hashlib.sha256(json.dumps(recalled, sort_keys=True).encode()).hexdigest()
    emit("memory_frozen", phase="compare", lessons_by_case={c["id"]: recalled for c in EVALUATION}, sha256=digest)
    counts = dict(wins=0, ties=0, regressions=0)
    arm_names = ("baseline", "memory", "ablated")
    totals = {arm: dict(total_downtime_hours=0, recovery_cost=0, aligned_cases=0,
                       prompt_tokens=0, total_tokens=0, usage_complete=True) for arm in arm_names}
    influence = 0
    for index, c in enumerate(EVALUATION):
        emit("case_started", phase="compare", case=c["id"], title=c["title"], snapshot=snapshot(c))
        results = {}
        removed = [l["id"] for l in recalled if l["applies_to"] == c["order_class"]]
        # Same prompt in all arms; ablation retains only the unrelated lesson.
        contexts = dict(baseline=[], memory=recalled,
                        ablated=[l for l in recalled if l["id"] not in removed])
        arms = arm_names[index:] + arm_names[:index]
        for arm in arms:
            lessons = contexts[arm]
            decision, usage, payload = decide(model, c, lessons)
            emit("decision", phase="compare", case=c["id"], arm=arm,
                 decision=decision, usage=usage, prompt=payload, lessons=lessons,
                 lesson_characters=len(json.dumps(payload["lessons"], sort_keys=True)))
            outcome = evaluate(c, decision["option_id"])
            judgment = assess(c, outcome)
            results[arm] = dict(decision=decision, outcome=outcome, judgment=judgment)
            emit("outcome", phase="compare", case=c["id"], arm=arm, outcome=outcome, judgment=judgment)
            for key in ("total_downtime_hours", "recovery_cost"):
                totals[arm][key] += outcome[key]
            totals[arm]["aligned_cases"] += int(judgment["aligned"])
            for key in ("prompt_tokens", "total_tokens"):
                if usage.get(key) is None:
                    totals[arm]["usage_complete"] = False
                else:
                    totals[arm][key] += usage[key]
        base, warm, ablated = (org_score(c, results[arm]["outcome"]) for arm in arm_names)
        verdict = "wins" if warm < base else "regressions" if warm > base else "ties"
        counts[verdict] += 1
        changed = results["memory"]["decision"]["option_id"] != results["ablated"]["decision"]["option_id"]
        supported = changed and warm < ablated
        influence += int(supported)
        emit("comparison", phase="compare", case=c["id"], verdict=verdict, results=results,
             ablation=dict(removed_lesson_ids=removed, decision_changed=changed,
                           improved_with_lesson=supported),
             rubric=assess(c, results["memory"]["outcome"])["basis"])
    emit("summary", phase="compare", counts=counts, totals=totals, memory_sha256=digest,
         ablation_supported_cases=influence,
         note="Scored against synthetic Cedar preferences, not minimum downtime alone. "
              "Ablation tests sensitivity to removing the applicable lesson; three cases and "
              "single model samples are evidence of behavior, not statistical proof or weight training.")
