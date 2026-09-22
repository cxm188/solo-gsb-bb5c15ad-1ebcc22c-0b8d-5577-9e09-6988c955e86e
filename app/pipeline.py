"""Orchestration: scan uploads, build the change manifest and produce ZIPs."""

from __future__ import annotations

import pickle
from pathlib import Path
from typing import Dict, List, Optional

from . import anonymizer, storage
from .anonymizer import (
    ScanResult,
    anonymize_dataset,
    build_changes,
    load_dataset,
    save_dataset,
)
from .config import Profile, get_profile


def _scan_path(job_id: str) -> Path:
    return storage.job_dir(job_id) / storage.SCAN_FILE


def run_scan(job_id: str) -> ScanResult:
    paths = storage.iter_dicom_files(job_id)
    scan = anonymizer.scan_files(paths, salt=storage.server_salt())
    with open(_scan_path(job_id), "wb") as fh:
        pickle.dump(scan, fh)
    return scan


def load_scan(job_id: str) -> Optional[ScanResult]:
    p = _scan_path(job_id)
    if not p.exists():
        return None
    with open(p, "rb") as fh:
        return pickle.load(fh)


def preview(job_id: str, profile_key: str) -> Dict:
    profile = get_profile(profile_key)
    scan = load_scan(job_id)
    if scan is None:
        scan = run_scan(job_id)
    changes = build_changes(scan, profile)
    scan.profile = profile
    with open(_scan_path(job_id), "wb") as fh:
        pickle.dump(scan, fh)
    return {
        "job_id": job_id,
        "profile": profile_key,
        "summary": {
            "total_files": len(scan.files),
            "dicom_files": sum(1 for f in scan.files if f.error is None),
            "failed_files": [
                {"name": f.source_name, "error": f.error} for f in scan.files if f.error
            ],
            "patients": len({f.patient_uid for f in scan.files if f.patient_uid}),
            "studies": len({f.study_uid for f in scan.files if f.study_uid}),
            "series": len({f.series_uid for f in scan.files if f.series_uid}),
            "changed_fields": len(changes),
            "files_with_private_tags": len(scan.private_files),
        },
        "hierarchy": scan.hierarchy(),
        "uid_remaps": scan.uid_map.reverse_preview(),
        "changes": [_change_to_dict(c) for c in changes],
        "files": [
            {
                "name": f.source_name,
                "patient_name": f.patient_name,
                "patient_id": f.patient_id,
                "modality": f.modality,
                "study_date": f.study_date,
                "old_sop_uid": f.sop_uid,
                "new_sop_uid": scan.uid_map.remap("sop", f.sop_uid) if f.sop_uid else "",
                "size": f.size,
                "error": f.error,
            }
            for f in scan.files
        ],
    }


def _change_to_dict(c) -> Dict:
    g, e = c.tag
    return {
        "tag": f"({g:04X},{e:04X})",
        "keyword": c.keyword,
        "name": c.name,
        "vr": c.vr,
        "action": c.action,
        "before": c.before,
        "after": c.after,
        "file_count": len(c.files),
        "files": c.files[:20] + (["…"] if len(c.files) > 20 else []),
    }


def apply(job_id: str, profile_key: str) -> Dict:
    """Rewrite every instance into the output folder and package the ZIP."""
    profile = get_profile(profile_key)
    scan = load_scan(job_id)
    if scan is None:
        raise FileNotFoundError("scan result missing; re-upload the data")

    out_root = storage.output_dir(job_id)
    # wipe any previous run
    import shutil

    if out_root.exists():
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    written = 0
    failures: List[Dict[str, str]] = []

    # Output layout mirrors the study/series hierarchy with anonymized names:
    #   <pseudo-patient>/<pseudo-study>/<pseudo-series>/<instance>.dcm
    for info in scan.files:
        if info.error:
            failures.append({"name": info.source_name, "error": info.error})
            continue
        try:
            ds = load_dataset(info.rel_path)
            anonymize_dataset(ds, info, scan, profile)

            p_pseudo = scan.uid_map.remap("patient", info.patient_uid)[-16:]
            s_pseudo = scan.uid_map.remap("study", info.study_uid)[-16:] if info.study_uid else "nostudy"
            se_pseudo = scan.uid_map.remap("series", info.series_uid)[-16:] if info.series_uid else "noseries"
            # use the SOP pseudonym suffix for the file name so instances can
            # never collide even when InstanceNumber is missing/duplicated.
            sop_tail = scan.uid_map.remap("sop", info.sop_uid)[-10:] if info.sop_uid else f"{info.index + 1:05d}"
            rel = f"{p_pseudo}/{s_pseudo}/{se_pseudo}/IMG_{sop_tail}.dcm"
            dest = out_root / rel
            save_dataset(ds, str(dest))
            info.output_relpath = rel
            written += 1
        except Exception as exc:  # keep one bad file from ruining the batch
            failures.append({"name": info.source_name, "error": f"{type(exc).__name__}: {exc}"})

    report_path = storage.write_report(job_id, _text_report(scan, profile))
    zip_path = storage.package_zip(job_id)

    return {
        "job_id": job_id,
        "profile": profile_key,
        "written": written,
        "failures": failures,
        "zip_size": zip_path.stat().st_size,
        "report_name": report_path.name,
    }


def _text_report(scan: ScanResult, profile: Profile) -> List[str]:
    changes = build_changes(scan, profile)
    lines: List[str] = []
    lines.append("DICOM 匿名化报告")
    lines.append("=" * 60)
    lines.append(f"配置文件: {profile.key} – {profile.label}")
    lines.append(f"文件总数: {len(scan.files)}")
    lines.append("")
    lines.append("UID 映射（患者级稳定伪名）")
    lines.append("-" * 60)
    for row in scan.uid_map.reverse_preview():
        lines.append(f"{row['domain']:20s} {row['old']}  ->  {row['new']}")
    lines.append("")
    lines.append("逐字段变更")
    lines.append("-" * 60)
    action_labels = {
        "blanked": "清空/移除",
        "date_cleared": "日期清空",
        "uid_remapped": "UID 重写",
        "private_removed": "私有标签移除",
        "added": "新增审计字段",
        "unchanged": "未变更",
    }
    for c in changes:
        lines.append(
            f"[{action_labels.get(c.action, c.action)}] {c.keyword} ({c.tag[0]:04X},{c.tag[1]:04X}) "
            f"{c.before} -> {c.after} (影响 {len(c.files)} 个文件)"
        )
    return lines
