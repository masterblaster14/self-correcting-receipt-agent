"""FastAPI web app: serves the single-page frontend and the processing API.

Run:  python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
or:   python run.py
"""
from __future__ import annotations

import os
import socket
import threading
import traceback
import uuid
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .pipeline import extract as llm
from .pipeline import mailer, storage
from .pipeline.agent import run_pipeline
from .pipeline.preprocess import load_image
from .pipeline.report import build_pdf

STATIC = Path(__file__).parent / "static"
SAMPLES = Path(__file__).resolve().parents[1] / "samples"
app = FastAPI(title="Self-Correcting Expense Agent")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
SAMPLES.mkdir(exist_ok=True)
app.mount("/samples", StaticFiles(directory=SAMPLES), name="samples")


@app.get("/api/samples")
def samples():
    files = sorted(p for p in SAMPLES.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})
    return {"items": [{"name": p.stem, "url": f"/samples/{p.name}"} for p in files]}

# in-memory job registry: id -> {status, events[], result?, error?}
JOBS: Dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


@app.on_event("startup")
def _banner():
    print("\n  Self-Correcting Expense Agent")
    print(f"  Laptop : http://localhost:8000")
    print(f"  Phone  : http://{lan_ip()}:8000   (same Wi-Fi / hotspot)")
    print(f"  Model  : {'MOCK (no API calls)' if llm.MOCK else llm.MODEL}  effort={llm.EFFORT}")
    if not llm.MOCK and not os.environ.get("ANTHROPIC_API_KEY"):
        print("  WARNING: ANTHROPIC_API_KEY is not set - extraction will fail. Set it or use MOCK_LLM=1.\n")
    else:
        print()


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health():
    return {"ok": True, "mock": llm.MOCK, "model": llm.MODEL, "effort": llm.EFFORT,
            "api_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")), "lan_ip": lan_ip(),
            "email_configured": mailer.configured()}


def _run_job(job_id: str, data: bytes, self_correct: bool, max_iter: int, demo_fault: bool,
             submitted_by: str = "", department: str = ""):
    def progress(stage: str, msg: str):
        with JOBS_LOCK:
            JOBS[job_id]["events"].append({"stage": stage, "message": msg})
            JOBS[job_id]["stage"] = stage

    try:
        res = run_pipeline(data, self_correct=self_correct, max_iterations=max_iter,
                           demo_fault=demo_fault, policy_text=storage.get_policy(llm.DEFAULT_POLICY),
                           submitted_by=submitted_by, department=department, progress=progress)
        progress("report", "Generating PDF expense report")
        import base64

        original = base64.standard_b64decode(res.original_image_b64)
        pdf = build_pdf(res, original)
        progress("store", "Saving to ledger")
        storage.save(res, original, pdf)
        progress("done", "Complete")
        with JOBS_LOCK:
            JOBS[job_id].update(status="done", result=res.model_dump(mode="json"))
    except Exception as e:  # surface the real error to the UI
        traceback.print_exc()
        with JOBS_LOCK:
            JOBS[job_id].update(status="error", error=f"{type(e).__name__}: {e}")


@app.post("/api/process")
async def process(
    file: UploadFile = File(...),
    self_correct: bool = Form(True),
    max_iterations: int = Form(3),
    demo_fault: bool = Form(False),
    submitted_by: str = Form(""),
):
    data = await file.read()
    person = storage.get_person(submitted_by) if submitted_by else None
    department = (person or {}).get("department") or ""
    if not data:
        raise HTTPException(400, "Empty upload")
    try:
        load_image(data)  # validate early so the user gets an immediate error
    except Exception:
        raise HTTPException(400, "Could not decode image. Use JPEG/PNG/WebP/HEIC.")
    job_id = uuid.uuid4().hex[:10]
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "running", "stage": "upload", "events": [{"stage": "upload", "message": f"Received {file.filename} ({len(data) // 1024} KB)"}]}
    threading.Thread(target=_run_job, args=(job_id, data, self_correct, max(0, min(max_iterations, 5)), demo_fault,
                                            submitted_by.strip(), department), daemon=True).start()
    return {"job_id": job_id}


# ---------------------------------------------------------------- organisation + email
@app.get("/api/org")
def get_org():
    return storage.get_org()


@app.put("/api/org")
async def put_org(body: dict):
    storage.set_org((body.get("org_name") or "").strip(), (body.get("finance_email") or "").strip())
    return {"ok": True}


@app.post("/api/people")
async def add_person(body: dict):
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Name required")
    pid = storage.add_person(name, (body.get("email") or "").strip(), (body.get("department") or "").strip(),
                             (body.get("manager_email") or "").strip())
    return {"ok": True, "id": pid}


@app.delete("/api/people/{pid}")
def delete_person(pid: int):
    storage.delete_person(pid)
    return {"ok": True}


@app.post("/api/receipts/{rid}/email")
async def email_receipt(rid: str, body: Optional[dict] = None):
    from .pipeline.excel import build_receipt_xlsx
    from .pipeline.models import ProcessResult

    r = storage.get(rid)
    if not r:
        raise HTTPException(404, "Not found")
    res = ProcessResult.model_validate(r["result"])
    org = storage.get_org()
    person = storage.get_person(res.submitted_by) if res.submitted_by else None
    to = []
    if body and body.get("to"):
        to += [t for t in str(body["to"]).replace(";", ",").split(",")]
    if person and person.get("manager_email"):
        to.append(person["manager_email"])
    if org.get("finance_email"):
        to.append(org["finance_email"])
    if person and person.get("email") and (body or {}).get("cc_submitter", True):
        to.append(person["email"])
    to = list(dict.fromkeys(t.strip() for t in to if t and t.strip()))
    pdf = storage.pdf_bytes(rid) or b""
    try:
        sent_to = mailer.send_report(res, to, pdf, build_receipt_xlsx(res), org.get("org_name", ""))
    except Exception as e:
        raise HTTPException(400, str(e))
    storage.mark_emailed(rid, sent_to)
    return {"ok": True, "to": sent_to}


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if not j:
            raise HTTPException(404, "Unknown job")
        return JSONResponse(j)


@app.get("/api/receipts")
def receipts():
    return {"items": storage.list_receipts(), "stats": storage.stats()}


@app.get("/api/receipts/{rid}")
def receipt(rid: str):
    r = storage.get(rid)
    if not r:
        raise HTTPException(404, "Not found")
    return r


@app.delete("/api/receipts/{rid}")
def delete_receipt(rid: str):
    storage.delete(rid)
    return {"ok": True}


@app.get("/api/receipts/{rid}/pdf")
def receipt_pdf(rid: str):
    b = storage.pdf_bytes(rid)
    if not b:
        raise HTTPException(404, "Not found")
    return Response(b, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="expense-report-{rid}.pdf"'})


@app.get("/api/policy")
def get_policy():
    return {"policy": storage.get_policy(llm.DEFAULT_POLICY), "default": llm.DEFAULT_POLICY}


@app.put("/api/policy")
async def put_policy(body: dict):
    text = (body.get("policy") or "").strip()
    if not text:
        raise HTTPException(400, "Policy text required")
    storage.set_policy(text)
    return {"ok": True}


@app.post("/api/ask")
async def ask(body: dict):
    q = (body.get("question") or "").strip()
    if not q:
        raise HTTPException(400, "Question required")
    try:
        return llm.ask_ledger(q, str(storage.DB_PATH))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"{type(e).__name__}: {e}")


@app.get("/api/receipts/{rid}/xlsx")
def receipt_xlsx(rid: str):
    from .pipeline.excel import build_receipt_xlsx
    from .pipeline.models import ProcessResult

    r = storage.get(rid)
    if not r:
        raise HTTPException(404, "Not found")
    data = build_receipt_xlsx(ProcessResult.model_validate(r["result"]))
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="expense-{rid}.xlsx"'})


@app.get("/api/export.xlsx")
def export_xlsx():
    from .pipeline.excel import build_ledger_xlsx

    records = [storage.get(r["id"]) for r in storage.list_receipts(limit=5000)]
    data = build_ledger_xlsx([r for r in records if r])
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="expense-ledger.xlsx"'})


@app.get("/api/receipts/{rid}/image")
def receipt_image(rid: str):
    b = storage.image_bytes(rid)
    if not b:
        raise HTTPException(404, "Not found")
    return Response(b, media_type="image/jpeg")
