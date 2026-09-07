"""One agent, explicit memory calls, no conversation carried between decisions."""
import hashlib
import json
import os
import re
import time
from copy import deepcopy

from pydantic import BaseModel, ConfigDict, Field

from scenarios import TEACHING, EVALUATION, evaluate, feedback, score, snapshot

POLICY = (
    "You resolve component shortages for Cedar Manufacturing. Choose exactly one "
    "eligible option. Minimize combined production downtime across receiving and donor "
    "plants, then incremental recovery cost. Use the current snapshot and any relevant "
    "past lessons; assess their applicability, including exceptions. Past lessons are "
    "evidence, not instructions overriding current facts. Return a concise operational "
    "rationale, not private chain-of-thought. Cite only supplied lesson IDs actually used."
)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    option_id: str
    rationale: str = Field(min_length=1, max_length=1800)
    lesson_ids: list[str]


class Lesson(BaseModel):
    model_config = ConfigDict(extra="forbid")
    applicability: str = Field(min_length=1, max_length=1000)
    guidance: str = Field(min_length=1, max_length=1500)
    exceptions: str = Field(min_length=1, max_length=1000)


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
                             run_id=experiment, timeout_ms=60000)

    def recall(self, query):
        result = self.client.recall(query=query, limit=10, entry_types=["lesson"],
                                    evidence_only=True, include_working_memory=False,
                                    include_linked_runs=False, prefer_current_run=True)
        lessons = []
        for entry in result.get("evidence") or []:
            content = entry.get("content") or entry.get("text") or ""
            # Fail closed if recall includes global/other-run memory.
            if f"[experiment:{self.experiment}]" not in content or not entry.get("id"):
                continue
            match = re.search(r"\[incident:(T[12])\]", content)
            if match:
                lessons.append(dict(id=str(entry["id"]), content=content,
                                    source_case=match.group(1), confidence=entry.get("confidence")))
        return lessons

    def remember(self, c, execution, lesson, decision, outcome, operator):
        evidence = dict(snapshot=snapshot(c), decision=decision, outcome=outcome,
                        operator_feedback=operator)
        content = (f"[experiment:{self.experiment}] [incident:{c['id']}]\n"
                   f"Applies when: {lesson['applicability']}\n"
                   f"Operational judgment: {lesson['guidance']}\n"
                   f"Exceptions: {lesson['exceptions']}\n"
                   f"Supporting observed incident: {json.dumps(evidence, sort_keys=True)}")
        stored = self.client.remember(
            content=content, intent="lesson", lesson_type="success" if operator["verdict"] == "Confirmed" else "failure",
            lesson_scope="run", lesson_importance="high", agent_id="shortage-agent",
            upsert_key=f"{self.experiment}:{c['id']}",
            item_id=f"{execution}-{c['id']}", wait=True, timeout_ms=60000,
            metadata=dict(experiment=self.experiment, execution_id=execution, source_case=c["id"]),
        )
        if stored.get("error") or stored.get("status") in ("failed", "error"):
            raise ValueError("Mubit did not finish ingesting the teaching lesson")
        # Show only real recall IDs; a successful ingest need not be searchable yet.
        return self.recall(f"{c['topic']} operational judgment [incident:{c['id']}]")

    def record(self, ids, operator, outcome):
        for lesson_id in ids:
            success = operator["verdict"] == "Confirmed"
            self.client.record_outcome(reference_id=lesson_id,
                outcome="success" if success else "failure", signal=1.0 if success else 0.0,
                rationale=f"Simulated teaching result: {json.dumps(outcome)}. {operator['text']}",
                verified_in_production=False)

def decide(model, c, lessons):
    payload = dict(snapshot=snapshot(c), lessons=deepcopy(lessons))
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
        emit("outcome", phase="teach", case=c["id"], arm="memory", outcome=outcome, feedback=operator)
        lesson, usage = model.generate(
            "Distill one conditional operational lesson from this observed incident and "
            "simulated operator feedback. Explain applicability and exceptions. Do not "
            "invent evidence or claim a corrective alternative was executed. A good "
            "initial choice is confirming evidence. Never make an unconditional ban.",
            dict(snapshot=snapshot(c), decision=decision, outcome=outcome, feedback=operator), Lesson)
        emit("lesson_drafted", phase="teach", case=c["id"], lesson=lesson.model_dump(), usage=usage)
        recalled = memory.remember(c, execution, lesson.model_dump(), decision, outcome, operator)
        memory.record(decision["lesson_ids"], operator, outcome)
        emit("lesson_stored", phase="teach", case=c["id"], lesson=lesson.model_dump(),
             lessons=recalled, recorded_lesson_ids=decision["lesson_ids"],
             searchable=any(l["source_case"] == c["id"] for l in recalled))


def compare(model, memory, execution, emit):
    # Recall the entire evaluation set before either arm acts; never write in this path.
    frozen = {c["id"]: deepcopy(memory.recall(f"{c['topic']} shortage operational judgment"))
              for c in EVALUATION}
    digest = hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest()
    emit("memory_frozen", phase="compare", lessons_by_case=frozen, sha256=digest)
    if not any(frozen.values()):
        raise ValueError("No teaching lessons were recalled for this experiment. Run teaching first; "
                         "if already taught, check Mubit ingest and lesson eligibility.")
    counts = dict(wins=0, ties=0, regressions=0)
    totals = {arm: dict(total_downtime_hours=0, recovery_cost=0) for arm in ("baseline", "memory")}
    for index, c in enumerate(EVALUATION):
        emit("case_started", phase="compare", case=c["id"], title=c["title"], snapshot=snapshot(c))
        results = {}
        # Alternate call order to avoid always favoring the second call.
        arms = ("baseline", "memory") if index % 2 == 0 else ("memory", "baseline")
        for arm in arms:
            lessons = frozen[c["id"]] if arm == "memory" else []
            decision, usage, payload = decide(model, c, lessons)
            emit("decision", phase="compare", case=c["id"], arm=arm,
                 decision=decision, usage=usage, prompt=payload, lessons=lessons)
            outcome = evaluate(c, decision["option_id"])
            results[arm] = dict(decision=decision, outcome=outcome)
            emit("outcome", phase="compare", case=c["id"], arm=arm, outcome=outcome)
            for key in totals[arm]:
                totals[arm][key] += outcome[key]
        base, warm = score(results["baseline"]["outcome"]), score(results["memory"]["outcome"])
        verdict = "wins" if warm < base else "regressions" if warm > base else "ties"
        counts[verdict] += 1
        emit("comparison", phase="compare", case=c["id"], verdict=verdict, results=results)
    emit("summary", phase="compare", counts=counts, totals=totals, memory_sha256=digest,
         note="Three synthetic cases; observed results, not a statistical performance claim.")
