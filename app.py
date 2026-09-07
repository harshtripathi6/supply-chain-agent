"""Local single-user demo server. Run with one uvicorn worker."""
import asyncio
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field

from agent import Gemini, Memory, compare, missing_config, teach
from scenarios import DEMO_VERSION

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
DATA = ROOT / ".demo"
app = FastAPI(title="Supply / Operational memory")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])
lock = threading.Lock()
active = None


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2))
    temporary.replace(path)


def state():
    path = DATA / "state.json"
    if not path.exists():
        experiment = os.getenv("DEMO_EXPERIMENT") or "sc-" + uuid.uuid4().hex[:12]
        ExperimentRequest(experiment=experiment)
        save(path, dict(experiment=experiment))
    current = json.loads(path.read_text())
    if current.get("demo_version") != DEMO_VERSION:
        current["demo_version"] = DEMO_VERSION
        current.pop("last_run", None)  # Old traces remain downloadable, but are not v2 results.
        save(path, current)
    return current


class ExperimentRequest(BaseModel):
    experiment: str | None = Field(default=None, pattern=r"^sc-[a-zA-Z0-9_-]{1,60}$")


class RunRequest(BaseModel):
    phase: Literal["teach", "compare"]


def trace_path(execution):
    if len(execution) != 32 or any(c not in "0123456789abcdef" for c in execution):
        raise HTTPException(404, "Run not found")
    return DATA / "runs" / (execution + ".json")


def read_trace(execution):
    path = trace_path(execution)
    if not path.exists():
        raise HTTPException(404, "Run not found")
    trace = json.loads(path.read_text())
    if trace["status"] == "running" and active != execution:
        trace["status"] = "interrupted"
    return trace


@app.middleware("http")
async def same_origin(request: Request, call_next):
    if request.method == "POST":
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "Cross-origin writes are not allowed"}, status_code=403)
    return await call_next(request)


@app.get("/")
def index():
    return FileResponse(ROOT / "index.html")


@app.get("/api/status")
def status():
    with lock:
        current = state()
        return dict(**current, active=active, missing=missing_config(),
                    model=os.getenv("GEMINI_MODEL", "gemini-3.6-flash"))


@app.post("/api/experiment")
def experiment(body: ExperimentRequest):
    with lock:
        if active:
            raise HTTPException(409, "Wait for the active run to finish")
        current = dict(experiment=body.experiment or "sc-" + uuid.uuid4().hex[:12], demo_version=DEMO_VERSION)
        save(DATA / "state.json", current)
        return current


def execute(trace):
    global active
    model = memory = None
    def emit(kind, **data):
        event = dict(id=len(trace["events"])+1, type=kind, execution_id=trace["id"],
                     experiment=trace["experiment"], timestamp=datetime.now(timezone.utc).isoformat(), **data)
        trace["events"].append(event)
        save(trace_path(trace["id"]), trace)
    try:
        model, memory = Gemini(), Memory(trace["experiment"])
        (teach if trace["phase"] == "teach" else compare)(model, memory, trace["id"], emit)
        trace["status"] = "completed"
        emit("complete", phase=trace["phase"])
    except Exception as exc:
        trace["status"] = "failed"
        # Provider exception strings may include request URLs/API keys. Keep them server-side too.
        message = str(exc) if isinstance(exc, ValueError) else (
            f"{type(exc).__name__}: provider operation failed. Check credentials, model availability, "
            "endpoint connectivity, and Mubit ingest status; no simulated fallback was used.")
        for key in ("GEMINI_API_KEY", "MUBIT_API_KEY"):
            if os.getenv(key):
                message = message.replace(os.environ[key], "[redacted]")
        emit("error", phase=trace["phase"], message=message)
    finally:
        for client in (model, memory):
            if client and hasattr(client, "close"):
                try:
                    client.close()
                except Exception:
                    pass
        with lock:
            active = None


@app.post("/api/runs", status_code=202)
def start(body: RunRequest):
    global active
    with lock:
        if active:
            raise HTTPException(409, "A run is already active")
        missing = missing_config()
        if missing:
            raise HTTPException(503, "Missing configuration: " + ", ".join(missing))
        execution = uuid.uuid4().hex
        current = state()
        trace = dict(id=execution, experiment=current["experiment"], phase=body.phase, demo_version=DEMO_VERSION,
                     model=os.getenv("GEMINI_MODEL", "gemini-3.6-flash"), temperature=0,
                     status="running", events=[], created_at=datetime.now(timezone.utc).isoformat())
        save(trace_path(execution), trace)
        save(DATA / "state.json", {**current, "last_run": execution})
        active = execution
        threading.Thread(target=execute, args=(trace,), daemon=True).start()
        return dict(id=execution)


@app.get("/api/runs/{execution}")
def get_trace(execution: str):
    return JSONResponse(read_trace(execution), headers={
        "Content-Disposition": f'attachment; filename="supply-{execution}.json"'})


@app.get("/api/runs/{execution}/events")
async def events(execution: str, request: Request):
    read_trace(execution)
    try:
        cursor = max(0, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        raise HTTPException(400, "Invalid event cursor")
    async def stream():
        nonlocal cursor
        while not await request.is_disconnected():
            trace = read_trace(execution)
            for event in trace["events"][cursor:]:
                cursor = event["id"]
                yield f"id: {cursor}\ndata: {json.dumps(event)}\n\n"
            if trace["status"] != "running":
                yield f"event: end\ndata: {json.dumps({'status': trace['status']})}\n\n"
                return
            yield ": heartbeat\n\n"
            await asyncio.sleep(0.3)
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
