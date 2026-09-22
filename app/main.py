"""FastAPI application: upload, configure/preview, anonymize and download."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import List

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import pipeline, storage
from .config import DEFAULT_PROFILE, list_profiles

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="DICOM 匿名化工作台",
    description="上传影像 → 选择匿名化配置 → 预览标签变更 → 打包下载",
    version="1.0.0",
)


@app.on_event("startup")
def _startup() -> None:
    storage.ensure_root()
    storage.cleanup_expired()


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/profiles")
def profiles() -> List[dict]:
    return [
        {"key": p.key, "label": p.label, "description": p.description}
        for p in list_profiles()
    ]


@app.post("/api/jobs")
async def create_job(files: List[UploadFile] = File(...)) -> dict:
    if not files:
        raise HTTPException(status_code=400, detail="请至少上传一个 DICOM 文件或 ZIP 包")
    job_id = secrets.token_urlsafe(12).replace("-", "a").replace("_", "b")
    payload: List = []
    total = 0
    for up in files:
        content = await up.read()
        total += len(content)
        if total > storage.MAX_UPLOAD_BYTES:
            storage.cleanup_job(job_id)
            raise HTTPException(
                status_code=413,
                detail=f"上传数据超过上限 {storage.MAX_UPLOAD_BYTES // (1024**2)} MB",
            )
        payload.append((up.filename or "dicomfile", content))
    try:
        storage.save_uploads(job_id, payload)
    except ValueError as exc:
        storage.cleanup_job(job_id)
        raise HTTPException(status_code=413, detail=str(exc))

    dicom_files = storage.iter_dicom_files(job_id)
    if not dicom_files:
        storage.cleanup_job(job_id)
        raise HTTPException(status_code=422, detail="未在上传内容中找到可解析的 DICOM 文件")

    scan = pipeline.run_scan(job_id)
    return {
        "job_id": job_id,
        "file_count": len(scan.files),
        "errors": [
            {"name": f.source_name, "error": f.error}
            for f in scan.files
            if f.error
        ],
        "profiles_url": "/api/profiles",
        "preview_url": f"/api/jobs/{job_id}/preview",
    }


def _require_job(job_id: str) -> None:
    if not job_id.isalnum() or not storage.job_exists(job_id):
        raise HTTPException(status_code=404, detail="任务不存在或已过期，请重新上传")


@app.get("/api/jobs/{job_id}/preview")
def get_preview(job_id: str, profile: str = DEFAULT_PROFILE) -> dict:
    _require_job(job_id)
    try:
        return pipeline.preview(job_id, profile)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/jobs/{job_id}/anonymize")
def post_anonymize(job_id: str, profile: str = Form(DEFAULT_PROFILE)) -> dict:
    _require_job(job_id)
    try:
        result = pipeline.apply(job_id, profile)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    result["download_url"] = f"/api/jobs/{job_id}/download"
    result["report_url"] = f"/api/jobs/{job_id}/report"
    return result


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str) -> FileResponse:
    _require_job(job_id)
    zip_path = storage.output_zip(job_id)
    if not zip_path.exists():
        raise HTTPException(status_code=409, detail="匿名化尚未执行")
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"anonymized_{job_id[:8]}.zip",
    )


@app.get("/api/jobs/{job_id}/report")
def report(job_id: str) -> FileResponse:
    _require_job(job_id)
    report_path = storage.output_dir(job_id) / "ANONYMIZATION_REPORT.txt"
    if not report_path.exists():
        raise HTTPException(status_code=409, detail="匿名化尚未执行")
    return FileResponse(report_path, media_type="text/plain; charset=utf-8", filename="ANONYMIZATION_REPORT.txt")


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str) -> dict:
    _require_job(job_id)
    storage.cleanup_job(job_id)
    return {"deleted": job_id}


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
    if isinstance(exc, HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return JSONResponse(status_code=500, content={"detail": f"服务器内部错误: {exc}"})
