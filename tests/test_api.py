"""End-to-end API tests using FastAPI TestClient against synthesized DICOM."""

import io
import os
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import storage

from .synth import build_cross_referenced_study


@pytest.fixture()
def client(tmp_path, monkeypatch):
    work = tmp_path / "jobs"
    work.mkdir()
    monkeypatch.setattr(storage, "WORK_ROOT", work)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def uploaded_job(client, tmp_path):
    paths, uids = build_cross_referenced_study(str(tmp_path / "study"))
    files = []
    for p in paths:
        with open(p, "rb") as fh:
            files.append(("files", (os.path.basename(p), fh.read(), "application/dicom")))
    resp = client.post("/api/jobs", files=files)
    assert resp.status_code == 200, resp.text
    return resp.json()["job_id"], uids


def test_health_and_profiles(client):
    assert client.get("/api/health").json() == {"status": "ok"}
    keys = [p["key"] for p in client.get("/api/profiles").json()]
    assert keys == ["basic", "standard", "strict"]


def test_index_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "DICOM" in resp.text


def test_upload_requires_dicom(client):
    resp = client.post(
        "/api/jobs",
        files=[("files", ("notes.txt", b"hello world", "text/plain"))],
    )
    assert resp.status_code == 422


def test_full_pipeline(client, uploaded_job):
    job_id, uids = uploaded_job

    # preview
    resp = client.get(f"/api/jobs/{job_id}/preview?profile=standard")
    assert resp.status_code == 200
    data = resp.json()
    assert data["summary"]["dicom_files"] == 4
    assert data["summary"]["patients"] == 1
    assert data["summary"]["studies"] == 1
    assert data["summary"]["series"] == 2

    change_tags = {c["tag"] for c in data["changes"]}
    assert "(0010,0010)" in change_tags
    assert "(0020,000D)" in change_tags
    assert "(0008,1155)" not in change_tags  # inside-sequence refs not top-level

    # hierarchy uids rewritten in preview
    study_row = next(r for r in data["uid_remaps"] if r["domain"] == "StudyInstanceUID")
    assert study_row["old"] == uids["study"]
    assert study_row["new"] != uids["study"]

    # anonymize
    resp = client.post(f"/api/jobs/{job_id}/anonymize", data={"profile": "standard"})
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["written"] == 4
    assert result["failures"] == []
    assert result["zip_size"] > 0

    # download zip and verify contents
    zresp = client.get(f"/api/jobs/{job_id}/download")
    assert zresp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(zresp.content))
    dcm_members = [n for n in zf.namelist() if n.endswith(".dcm")]
    assert len(dcm_members) == 4
    assert any(n.endswith("ANONYMIZATION_REPORT.txt") for n in zf.namelist())

    # write zip to temp and validate each member parses and has new identity
    import pydicom

    for member in dcm_members:
        ds = pydicom.dcmread(io.BytesIO(zf.read(member)))
        assert str(ds.PatientID).startswith("ANON-")
        assert "ZHANG" not in str(ds.PatientName)
        assert ds.StudyInstanceUID != uids["study"]
        assert ds.SeriesInstanceUID not in (uids["series1"], uids["series2"])
        assert ds.SOPInstanceUID not in uids["ct_sops"]
        assert str(ds.PatientIdentityRemoved) == "YES"
        assert str(ds.file_meta.MediaStorageSOPInstanceUID) == str(ds.SOPInstanceUID)

    # the SR reference now points to the anonymized CT
    sr_member = next(n for n in dcm_members if pydicom.dcmread(io.BytesIO(zf.read(n))).Modality == "SR")
    sr = pydicom.dcmread(io.BytesIO(zf.read(sr_member)))
    new_ct_uids = {
        pydicom.dcmread(io.BytesIO(zf.read(n))).SOPInstanceUID
        for n in dcm_members
        if pydicom.dcmread(io.BytesIO(zf.read(n))).Modality == "CT"
    }
    ref_uid = sr.ReferencedSeriesSequence[0].ReferencedSOPSequence[0].ReferencedSOPInstanceUID
    assert ref_uid in new_ct_uids

    # report endpoint
    r = client.get(f"/api/jobs/{job_id}/report")
    assert r.status_code == 200
    assert "匿名化报告" in r.text


def test_unknown_profile_rejected(client, uploaded_job):
    job_id, _ = uploaded_job
    resp = client.get(f"/api/jobs/{job_id}/preview?profile=nope")
    assert resp.status_code == 400


def test_missing_job_404(client):
    assert client.get("/api/jobs/doesnotexist123/preview").status_code == 404
