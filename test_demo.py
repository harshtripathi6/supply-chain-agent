"""Offline contract checks. Fakes are test-only and never reachable from the app."""
import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch, Mock

from fastapi.testclient import TestClient

import app
from agent import Decision, Gemini, Lesson, Memory, POLICY, compare, decide, teach
from scenarios import CASES, DEMO_VERSION, ORG_RULES, TEACHING, EVALUATION, assess, evaluate, feedback, org_score, score, snapshot

LESSONS = [dict(id=f"real-recall-{i+1}", source_case=f"T{i+1}", **deepcopy(rule),
                evidence={"audit_only": "Historical snapshot should never reach a decision prompt"})
           for i,rule in enumerate(ORG_RULES.values())]


class FakeModel:
    def __init__(self):
        self.calls = []

    def generate(self, system, payload, schema):
        self.calls.append((system, deepcopy(payload), schema))
        usage = dict(calls=1, prompt_tokens=8, total_tokens=12, elapsed_seconds=0, model="test-only")
        c = next(c for c in CASES if c["id"] == payload["snapshot"]["id"])
        relevant = [l for l in payload["lessons"] if l["applies_to"] == c["order_class"]]
        metric = (lambda o:org_score(c,o)) if relevant else score
        choice = min(c["options"], key=lambda o:metric(evaluate(c,o["id"])))
        return Decision(option_id=choice["id"], rationale="TEST FIXTURE: local preference" if relevant else "TEST FIXTURE: conservative default",
                        lesson_ids=[l["id"] for l in relevant]), usage

    def close(self):
        pass


class FakeMemory:
    def __init__(self, experiment="sc-test"):
        self.calls = []

    def recall(self, query):
        self.calls.append(("recall", query))
        return deepcopy(LESSONS)

    def remember(self, *args):
        self.calls.append(("remember", args))
        return deepcopy(LESSONS)

    def record(self, *args):
        self.calls.append(("record", args))


class SimulationTests(unittest.TestCase):
    def test_every_option(self):
        expected = [(1.5,0,11,0),(1,0,11,2),(1,0,10,0),(5,0,1,0),(1,0,11,2)]
        for c, hours in zip(CASES,expected):
            for o, want in zip(c["options"],hours):
                with self.subTest(case=c["id"],option=o["id"]):
                    result = evaluate(c,o["id"])
                    self.assertEqual(result["total_downtime_hours"],want)
                    self.assertEqual(result["recovery_cost"],o["cost"])

    def test_organization_changes_the_winners(self):
        winners=lambda metric:[min(c["options"], key=lambda o:metric(c,evaluate(c,o["id"])))['id'] for c in EVALUATION]
        self.assertEqual(winners(org_score),["wait","transfer","expedite"])
        self.assertEqual(winners(lambda c,o:score(o)),["substitute","substitute","expedite"])

    def test_eligibility_cutoff_and_policy_boundary(self):
        c=deepcopy(TEACHING[0]);c["options"][3]["approved"]=False
        with self.assertRaises(ValueError): evaluate(c,"substitute")
        with self.assertRaises(ValueError): evaluate(c,"missing")
        c["options"][0]["arrival"]="2026-10-05T17:00:00"
        self.assertEqual(evaluate(c,"wait")["usable_at"],"2026-10-05T17:00:00")
        c["options"][0]["arrival"]="2026-10-05T17:01:00"
        self.assertEqual(evaluate(c,"wait")["usable_at"],"2026-10-06T08:00:00")
        for hour,allowed in [(16,True),(17,False)]:
            c["options"][0]["arrival"]=f"2026-10-05T{hour}:00:00"
            self.assertEqual(assess(c,evaluate(c,"wait"))["within_tolerance"],allowed)
        self.assertFalse(assess(c,evaluate(c,"transfer"))["within_tolerance"])

    def test_feedback_reveals_local_correction(self):
        c=TEACHING[0]
        self.assertEqual(feedback(c,{"option_id":"wait"},evaluate(c,"wait"))["verdict"],"Confirmed")
        correction=feedback(c,{"option_id":"substitute"},evaluate(c,"substitute"))
        self.assertEqual(correction["verdict"],"Needs improvement")
        self.assertEqual(correction["confirmed_lesson"],ORG_RULES["replenishment"])

    def test_same_physical_case_different_order_class(self):
        c=deepcopy(EVALUATION[0])
        self.assertTrue(assess(c,evaluate(c,"wait"))["aligned"])
        c["order_class"]="firm"
        self.assertFalse(assess(c,evaluate(c,"wait"))["aligned"])


class AgentTests(unittest.TestCase):
    def test_gemini_uses_json_schema_and_validates_output(self):
        model=Gemini.__new__(Gemini);model.model="test";model.client=Mock()
        model.client.models.generate_content.return_value=Mock(
            text='{"option_id":"wait","rationale":"test","lesson_ids":[]}', usage_metadata=None)
        decision,_=model.generate(POLICY,{},Decision)
        self.assertEqual(decision.option_id,"wait")
        config=model.client.models.generate_content.call_args.kwargs["config"]
        self.assertNotIn("response_schema",config)
        self.assertEqual(config["response_json_schema"],Decision.model_json_schema())
        model.client.models.generate_content.return_value.text='{"unexpected":true}'
        with self.assertRaises(ValueError):model.generate(POLICY,{},Decision)

    def test_gemini_error_explains_status_and_redacts_keys(self):
        from google.genai.errors import ClientError
        model=Gemini.__new__(Gemini);model.model="test";model.client=Mock()
        model.client.models.generate_content.side_effect=ClientError(400,{
            "error":{"message":"Invalid schema secret-test-key", "status":"INVALID_ARGUMENT"}})
        with patch.dict(os.environ,{"GEMINI_API_KEY":"secret-test-key"}):
            with self.assertRaisesRegex(ValueError,r"Gemini HTTP 400: Invalid schema \[redacted\]"):
                model.generate(POLICY,{},Decision)

    def test_snapshot_excludes_future_information(self):
        c=deepcopy(TEACHING[0]);c.update(outcome="SECRET",feedback="SECRET",expected_action="SECRET")
        value=snapshot(c)
        self.assertNotIn("SECRET",json.dumps(value))
        self.assertNotIn("title",value);self.assertNotIn("topic",value)

    def test_comparison_is_read_only_and_paired(self):
        model,memory=FakeModel(),FakeMemory();events=[]
        compare(model,memory,"execution",lambda kind,**data:events.append(dict(type=kind,**data)))
        self.assertEqual([c[0] for c in memory.calls],["recall"])
        self.assertEqual(len(model.calls),9)
        for c in EVALUATION:
            calls=[call for call in model.calls if call[1]["snapshot"]["id"]==c["id"]]
            self.assertEqual(len(calls),3)
            self.assertTrue(all(call[0]==POLICY and call[1]["snapshot"]==snapshot(c) for call in calls))
            self.assertEqual(sorted(len(call[1]["lessons"]) for call in calls),[0,1,2])
            for _,payload,_ in calls:
                self.assertNotIn("audit_only",json.dumps(payload))
                self.assertTrue(set(payload)=={"snapshot","lessons"})
        self.assertEqual(events[-1]["counts"],dict(wins=2,ties=1,regressions=0))
        self.assertEqual(events[-1]["ablation_supported_cases"],2)
        self.assertEqual(events[-1]["totals"]["memory"]["aligned_cases"],3)
        self.assertEqual(events[-1]["totals"]["baseline"]["aligned_cases"],1)

    def test_rejects_invented_citations_and_invalid_choices(self):
        for choice,ids in [("transfer",["invented"]),("unknown",[])]:
            model=Mock();model.generate.return_value=(Decision(option_id=choice,rationale="test",lesson_ids=ids),{})
            with self.assertRaises(ValueError): decide(model,TEACHING[0],[])

    def test_citations_without_choice_change_do_not_count_as_influence(self):
        class IgnoresMemory(FakeModel):
            def generate(self,system,payload,schema):
                decision,usage=super().generate(system,{**payload,"lessons":[]},schema)
                decision.lesson_ids=[l["id"] for l in payload["lessons"]]
                return decision,usage
        events=[]
        compare(IgnoresMemory(),FakeMemory(),"execution",lambda kind,**data:events.append(dict(type=kind,**data)))
        self.assertEqual(events[-1]["counts"],dict(wins=0,ties=3,regressions=0))
        self.assertEqual(events[-1]["ablation_supported_cases"],0)

    def test_teaching_records_only_used_lessons(self):
        model,memory=FakeModel(),FakeMemory();events=[]
        teach(model,memory,"execution",lambda kind,**data:events.append(dict(type=kind,**data)))
        self.assertEqual(len(model.calls),2)
        records=[args for name,args in memory.calls if name=="record"]
        self.assertEqual(len(records),2)
        self.assertEqual(records[0][0],[LESSONS[0]["id"]])
        stored=[args for name,args in memory.calls if name=="remember"]
        self.assertEqual([args[0]["id"] for args in stored],["T1","T2"])

    def test_empty_memory_cannot_claim_comparison(self):
        memory=FakeMemory();memory.recall=lambda q:[]
        with self.assertRaisesRegex(ValueError,"Both v2 teaching lessons"):
            compare(FakeModel(),memory,"execution",lambda *a,**k:None)

    def test_memory_round_trip_and_grounding(self):
        memory=Memory.__new__(Memory);memory.experiment="sc-test";memory.client=Mock()
        memory.client.remember.return_value={"done":True}
        memory.client.recall.return_value={"evidence":[]}
        c=TEACHING[0];decision=dict(option_id="substitute",lesson_ids=[])
        outcome=evaluate(c,"substitute");operator=feedback(c,decision,outcome)
        memory.remember(c,"execution",operator["confirmed_lesson"],decision,outcome,operator)
        content=memory.client.remember.call_args.kwargs["content"]
        memory.client.recall.return_value={"evidence":[
            {"id":"good","content":content},
            {"id":"other","content":content.replace("sc-test","sc-other")},
            {"id":"legacy","content":"[experiment:sc-test] [incident:T1] old lesson"}]}
        recalled=memory.recall("shortage")
        self.assertEqual([l["id"] for l in recalled],["good"])
        self.assertEqual(recalled[0]["guidance"],ORG_RULES["replenishment"]["guidance"])
        self.assertEqual(recalled[0]["evidence"]["outcome"],outcome)
        invented={**operator["confirmed_lesson"],"guidance":"Accept 20 hours of downtime"}
        with self.assertRaisesRegex(ValueError,"differs"):
            memory.remember(c,"execution",invented,decision,outcome,operator)

    def test_initial_prompt_does_not_reveal_hidden_tradeoff(self):
        text=POLICY+json.dumps(snapshot(TEACHING[0]))
        self.assertNotIn("2 hours",text)
        self.assertNotIn("confirmed_lesson",text)
        self.assertNotIn("ORG_RULES",text)
        self.assertNotIn("org_score",text)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.data_patch=patch.object(app,"DATA",Path(self.tmp.name));self.data_patch.start()
        self.env_patch=patch.dict(os.environ,{"GEMINI_API_KEY":"", "MUBIT_API_KEY":"", "MUBIT_ENDPOINT":"", "DEMO_EXPERIMENT":""});self.env_patch.start()
        app.active=None;self.client=TestClient(app.app)

    def tearDown(self):
        self.client.close();self.data_patch.stop();self.env_patch.stop();self.tmp.cleanup()

    def test_configuration_and_identifier_persistence(self):
        self.assertEqual(self.client.get("/").status_code,200)
        first=self.client.get("/api/status").json()
        self.assertEqual(len(first["missing"]),3)
        self.assertEqual(self.client.get("/api/status").json()["experiment"],first["experiment"])
        self.assertEqual(self.client.post("/api/runs",json={"phase":"teach"}).status_code,503)
        new=self.client.post("/api/experiment",json={}).json()["experiment"]
        self.assertNotEqual(first["experiment"],new)
        self.client.post("/api/experiment",json={"experiment":first["experiment"]})
        self.assertEqual(self.client.get("/api/status").json()["experiment"],first["experiment"])
        self.assertEqual(self.client.post("/api/experiment",json={"experiment":"../bad"}).status_code,422)

    def test_active_run_and_origin_guards(self):
        app.active="busy"
        self.assertEqual(self.client.post("/api/runs",json={"phase":"teach"}).status_code,409)
        self.assertEqual(self.client.post("/api/experiment",json={}).status_code,409)
        self.assertEqual(self.client.post("/api/experiment",json={},headers={"Origin":"https://other.example"}).status_code,403)
        app.active=None

    def test_starting_again_replaces_last_run(self):
        with patch.object(app,"missing_config",return_value=[]), patch.object(app.threading,"Thread") as worker:
            first=self.client.post("/api/runs",json={"phase":"teach"})
            self.assertEqual(first.status_code,202)
            app.active=None  # Previous execution finished, or server restarted.
            second=self.client.post("/api/runs",json={"phase":"teach"})
            self.assertEqual(second.status_code,202)
            self.assertNotEqual(first.json()["id"],second.json()["id"])
            self.assertEqual(app.state()["last_run"],second.json()["id"])
            self.assertTrue(app.trace_path(first.json()["id"]).exists())
            self.assertEqual(worker.return_value.start.call_count,2)
            app.active=None

    def test_execution_export_and_sse_replay(self):
        trace=dict(id="a"*32,experiment="sc-test",phase="teach",status="running",events=[])
        app.active=trace["id"]
        with patch.object(app,"Gemini",FakeModel),patch.object(app,"Memory",FakeMemory):
            app.execute(trace)
        result=self.client.get(f"/api/runs/{trace['id']}")
        self.assertEqual(result.json()["status"],"completed")
        self.assertIn("attachment",result.headers["content-disposition"])
        stream=self.client.get(f"/api/runs/{trace['id']}/events",headers={"Last-Event-ID":"1"})
        self.assertNotIn("id: 1\n",stream.text);self.assertIn("event: end",stream.text)
        self.assertIsNone(app.active)

    def test_failed_execution_and_restart(self):
        trace=dict(id="b"*32,experiment="sc-test",phase="teach",status="running",events=[])
        with patch.object(app,"Gemini",side_effect=RuntimeError("private provider details")):
            app.execute(trace)
        self.assertEqual(trace["status"],"failed")
        self.assertNotIn("private provider",json.dumps(trace))
        trace["status"]="running";app.save(app.trace_path(trace["id"]),trace)
        self.assertEqual(app.read_trace(trace["id"])["status"],"interrupted")


if __name__ == "__main__":
    unittest.main()
