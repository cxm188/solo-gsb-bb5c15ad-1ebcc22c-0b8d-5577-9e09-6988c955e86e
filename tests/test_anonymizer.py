"""Engine-level tests: UID stability, reference consistency, profiles."""

import os

import pydicom
import pytest

from app.anonymizer import (
    anonymize_dataset,
    build_changes,
    load_dataset,
    scan_files,
)
from app.config import get_profile

from .synth import build_cross_referenced_study, build_second_patient_file


@pytest.fixture()
def study(tmp_path):
    d = tmp_path / "study1"
    paths, uids = build_cross_referenced_study(str(d))
    other = build_second_patient_file(str(tmp_path / "study2"))
    all_paths = [(p, os.path.basename(p)) for p in paths + [other]]
    return all_paths, uids


def test_scan_hierarchy(study):
    paths, uids = study
    scan = scan_files(paths, salt="fixed-salt")
    assert len(scan.files) == 5
    assert all(f.error is None for f in scan.files)
    # two distinct patients
    patients = {f.patient_uid for f in scan.files}
    assert len(patients) == 2
    # 2 studies, 3 series (2 in study 1 + 1 in study 2)
    assert len({f.study_uid for f in scan.files}) == 2
    assert len({f.series_uid for f in scan.files}) == 3


def test_uid_remap_is_salt_deterministic(study):
    paths, uids = study
    s1 = scan_files(paths, salt="abc")
    s2 = scan_files(paths, salt="abc")
    s3 = scan_files(paths, salt="different")

    old = uids["study"]
    assert s1.uid_map.remap("study", old) == s2.uid_map.remap("study", old)
    assert s1.uid_map.remap("study", old) != s3.uid_map.remap("study", old)
    # patient pivot for patient 1 maps through ID:MRN-P1
    new_p1 = s1.uid_map.remap("patient", "ID:MRN-P1")
    assert new_p1.startswith("2.25.")
    assert new_p1 != "ID:MRN-P1"
    assert s1.uid_map.remap("patient", "ID:MRN-P1") == new_p1  # stable
    # uid format valid: digits + dots, <=64 chars
    for table in s1.uid_map.tables.values():
        for new in table.values():
            assert len(new) <= 64
            assert all(c.isdigit() or c == "." for c in new)


def test_standard_profile_rewrites_identity_and_private(tmp_path, study):
    paths, uids = study
    scan = scan_files(paths, salt="salt-x")
    profile = get_profile("standard")

    # anonymize the first CT instance in place copy
    info = scan.files[0]
    assert info.sop_uid == uids["ct_sops"][0]
    ds = load_dataset(info.rel_path)
    anonymize_dataset(ds, info, scan, profile)

    # identity gone / pseudo
    assert str(ds.PatientName).startswith("ANON^PATIENT^")
    assert str(ds.PatientID).startswith("ANON-")
    assert "ZHANG" not in str(ds.PatientName)
    assert (0x0010, 0x0030) not in ds          # birth date
    assert (0x0008, 0x0090) not in ds            # referring physician
    assert (0x0008, 0x0080) not in ds            # institution
    assert (0x0018, 0x1000) not in ds            # device serial
    assert (0x0020, 0x4000) not in ds            # image comments
    # standard keeps age/sex/date
    assert str(ds.PatientAge) == "042Y"
    assert str(ds.PatientSex) == "M"
    assert str(ds.StudyDate) == "20240501"

    # private tags removed
    assert not any(el.tag.group % 2 == 1 for el in ds)

    # UIDs rewritten
    assert ds.StudyInstanceUID != uids["study"]
    assert ds.SeriesInstanceUID != uids["series1"]
    assert ds.SOPInstanceUID != uids["ct_sops"][0]
    assert ds.FrameOfReferenceUID != uids["frame"]
    assert ds.file_meta.MediaStorageSOPInstanceUID == ds.SOPInstanceUID
    # SOP class UIDs untouched
    assert str(ds.SOPClassUID) == "1.2.840.10008.5.1.4.1.1.2"
    assert str(ds.file_meta.MediaStorageSOPClassUID) == str(ds.SOPClassUID)

    # audit tags
    assert str(ds.PatientIdentityRemoved) == "YES"
    assert "ANWS" in str(ds.DeidentificationMethod)


def test_cross_reference_remains_consistent(tmp_path, study):
    paths, uids = study
    scan = scan_files(paths, salt="salt-ref")
    profile = get_profile("standard")

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    for info in scan.files:
        ds = load_dataset(info.rel_path)
        anonymize_dataset(ds, info, scan, profile)
        ds.save_as(str(out_dir / f"{info.index}.dcm"), enforce_file_format=True)

    # find the SR-like instance (4th input file ordering may sort by name;
    # locate via modality)
    sr = None
    cts = []
    for p in out_dir.iterdir():
        d = pydicom.dcmread(str(p))
        if d.Modality == "SR":
            sr = d
        else:
            cts.append(d)
    assert sr is not None

    new_study = cts[0].StudyInstanceUID
    new_series1 = next(c.SeriesInstanceUID for c in cts if c.Modality == "CT")
    new_ct1 = next(
        c.SOPInstanceUID
        for c in cts
        if c.Modality == "CT" and str(c.InstanceNumber) == "1"
    )

    assert sr.StudyInstanceUID == new_study
    ref_series_item = sr.ReferencedSeriesSequence[0]
    assert ref_series_item.SeriesInstanceUID == new_series1
    ref_sop = ref_series_item.ReferencedSOPSequence[0]
    assert ref_sop.ReferencedSOPInstanceUID == new_ct1
    # referenced SOP *class* uid preserved
    assert ref_sop.ReferencedSOPClassUID == "1.2.840.10008.5.1.4.1.1.2"
    # frame UIDs of both modalities in same study match each other
    assert sr.FrameOfReferenceUID == cts[0].FrameOfReferenceUID


def test_strict_clears_dates_and_demographics(study):
    paths, uids = study
    scan = scan_files(paths, salt="s")
    profile = get_profile("strict")
    info = next(f for f in scan.files if f.modality == "CT" and f.instance_number == "1")
    ds = load_dataset(info.rel_path)
    anonymize_dataset(ds, info, scan, profile)

    assert (0x0008, 0x0020) not in ds      # study date
    assert (0x0008, 0x0030) not in ds      # study time
    assert (0x0010, 0x1010) not in ds      # age
    assert (0x0010, 0x0040) not in ds      # sex
    assert (0x0010, 0x1030) not in ds      # weight


def test_basic_keeps_institution_and_dates_and_private(study):
    paths, uids = study
    scan = scan_files(paths, salt="s")
    profile = get_profile("basic")
    info = next(f for f in scan.files if f.modality == "CT" and f.instance_number == "1")
    ds = load_dataset(info.rel_path)
    anonymize_dataset(ds, info, scan, profile)

    assert str(ds.InstitutionName) == "Secret General Hospital"
    assert str(ds.StudyDate) == "20240501"
    # private tag retained
    assert any(el.tag.group % 2 == 1 for el in ds)
    # but patient identity is gone
    assert "ZHANG" not in str(ds.PatientName)
    assert (0x0010, 0x0030) not in ds


def test_change_manifest_lists_fields(study):
    paths, uids = study
    scan = scan_files(paths, salt="s")
    changes = build_changes(scan, get_profile("standard"))
    tags = {(c.tag[0], c.tag[1]): c for c in changes}

    assert (0x0010, 0x0010) in tags and tags[(0x0010, 0x0010)].action == "blanked"
    assert (0x0020, 0x000D) in tags and tags[(0x0020, 0x000D)].action == "uid_remapped"
    assert tags[(0xFFFF, 0x0000)].action == "private_removed"
    # audit added
    assert (0x0012, 0x0062) in tags
    # each change references at least one file
    assert all(c.files for c in changes if c.action != "added" or True)


def test_patient_stable_across_series_and_instances(study):
    """All instances of one patient map to the same pseudo patient UID."""
    paths, uids = study
    scan = scan_files(paths, salt="stable")
    p1_files = [f for f in scan.files if f.patient_uid == "ID:MRN-P1"]
    assert len(p1_files) == 4
    pids = set()
    for info in p1_files:
        ds = load_dataset(info.rel_path)
        anonymize_dataset(ds, info, scan, get_profile("standard"))
        pids.add(str(ds.PatientID))
    assert len(pids) == 1
    # second patient differs
    other_info = next(f for f in scan.files if f.patient_uid == "ID:MRN-OTHER")
    ds = load_dataset(other_info.rel_path)
    anonymize_dataset(ds, other_info, scan, get_profile("standard"))
    assert str(ds.PatientID) not in pids
