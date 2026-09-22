"""File/session storage for uploaded imaging sets and resulting ZIPs.

Everything is kept on the local filesystem under a per-job directory — no
database, no third-party service. Jobs expire after a configurable TTL.
"""

from __future__ import annotations

import os
import shutil
import time
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

from .anonymizer import looks_like_dicom

WORK_ROOT = Path(os.environ.get("WORK_ROOT", "/tmp/dicom-anon-jobs")).resolve()
JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", str(3600)))
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(4 * 1024**3)))

INCOMING_DIRNAME = "incoming"
OUTPUT_DIRNAME = "anonymized"
OUTPUT_ZIPNAME = "anonymized.zip"
SCAN_FILE = "scan.pkl"
SALT_FILE = "server.salt"


def ensure_root() -> None:
    WORK_ROOT.mkdir(parents=True, exist_ok=True)


def server_salt() -> str:
    """Return the persistent server salt (created on first use).

    A stable salt makes the patient-level pseudonym reproducible across
    separate upload batches: the same source patient always maps to the same
    pseudonymous identity, while the on-disk salt keeps the mapping
    irreversible. Delete ``server.salt`` to invalidate every existing
    pseudonym.
    """
    ensure_root()
    path = WORK_ROOT / SALT_FILE
    if path.exists():
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    import secrets

    salt = secrets.token_hex(16)
    path.write_text(salt, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return salt


def job_dir(job_id: str) -> Path:
    ensure_root()
    d = WORK_ROOT / job_id
    d.mkdir(parents=True, exist_ok=True)
    (d / INCOMING_DIRNAME).mkdir(exist_ok=True)
    (d / OUTPUT_DIRNAME).mkdir(exist_ok=True)
    return d


def job_exists(job_id: str) -> bool:
    return (WORK_ROOT / job_id).is_dir()


def incoming_dir(job_id: str) -> Path:
    return job_dir(job_id) / INCOMING_DIRNAME


def output_dir(job_id: str) -> Path:
    return job_dir(job_id) / OUTPUT_DIRNAME


def output_zip(job_id: str) -> Path:
    return job_dir(job_id) / OUTPUT_ZIPNAME


def _is_within(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _safe_join(base: Path, member: str) -> Optional[Path]:
    """Join an archive member name, rejecting zip-slip and absolute paths."""
    member = member.replace("\\", "/")
    if member.startswith("/") or ".." in [p for p in member.split("/") if p]:
        return None
    dest = base / member
    return dest if _is_within(base, dest) else None


def save_uploads(job_id: str, files: List[Tuple[str, bytes]]) -> List[str]:
    """Persist raw uploads and extract supported archives.

    Returns the list of top-level entries (files / folders) accepted.
    """
    base = incoming_dir(job_id)
    saved: List[str] = []
    total = 0
    for filename, content in files:
        total += len(content)
        if total > MAX_UPLOAD_BYTES:
            raise ValueError(f"上传数据超过上限 {MAX_UPLOAD_BYTES // (1024**2)} MB")
        safe = os.path.basename(filename.replace("\\", "/")) or f"upload_{len(saved)}"
        target = base / safe
        with open(target, "wb") as fh:
            fh.write(content)
        saved.append(safe)

        lower = safe.lower()
        if lower.endswith(".zip"):
            _extract_zip(target, base)
        elif lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz")):
            _extract_tar(target, base)
    return saved


def _extract_zip(archive: Path, dest: Path) -> None:
    try:
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                target = _safe_join(dest, info.filename)
                if target is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
    except zipfile.BadZipFile:
        pass


def _extract_tar(archive: Path, dest: Path) -> None:
    import tarfile

    try:
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                target = _safe_join(dest, member.name)
                if target is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(member)
                if src is None:
                    continue
                with src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
    except tarfile.TarError:
        pass


def iter_dicom_files(job_id: str) -> List[Tuple[str, str]]:
    """Return (absolute_path, relative_name) for every parseable DICOM file."""
    base = incoming_dir(job_id)
    found: List[Tuple[str, str]] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() in {".zip", ".tar", ".gz", ".tgz", ".bz2", ".tbz2", ".xz"}:
            continue
        abs_path = str(path)
        try:
            rel = str(path.relative_to(base))
        except ValueError:
            continue
        if looks_like_dicom(abs_path):
            found.append((abs_path, rel))
    return found


def iter_all_files(job_id: str) -> List[Tuple[str, str]]:
    base = incoming_dir(job_id)
    out: List[Tuple[str, str]] = []
    for path in sorted(base.rglob("*")):
        if path.is_file() and path.suffix.lower() not in {".zip", ".tar", ".gz", ".tgz", ".bz2", ".tbz2", ".xz"}:
            out.append((str(path), str(path.relative_to(base))))
    return out


def package_zip(job_id: str) -> Path:
    """Zip the anonymized output directory (deterministic order)."""
    src = output_dir(job_id)
    zip_path = output_zip(job_id)
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(src.rglob("*")):
            if path.is_file():
                zf.write(path, arcname=str(path.relative_to(src)))
    return zip_path


def cleanup_job(job_id: str) -> None:
    d = WORK_ROOT / job_id
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


def cleanup_expired() -> int:
    """Best-effort sweep; returns number of jobs removed.

    Only per-job directories are reaped; files such as the persistent
    ``server.salt`` are always preserved so cross-batch pseudonym stability
    is not broken.
    """
    if not WORK_ROOT.exists():
        return 0
    removed = 0
    now = time.time()
    for child in WORK_ROOT.iterdir():
        if not child.is_dir():
            continue
        try:
            if now - child.stat().st_mtime > JOB_TTL_SECONDS:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


def write_report(job_id: str, lines: List[str]) -> Path:
    """Write a human-readable field-change report next to the ZIP."""
    path = output_dir(job_id) / "ANONYMIZATION_REPORT.txt"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path
