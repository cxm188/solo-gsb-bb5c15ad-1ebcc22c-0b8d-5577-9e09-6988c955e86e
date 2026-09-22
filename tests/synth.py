"""Synthesize small in-memory DICOM studies for tests / local demos."""

from __future__ import annotations

import os
from typing import List, Tuple

import pydicom
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid


def _file_meta(sop_class_uid: str, sop_uid: str) -> FileMetaDataset:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = sop_class_uid
    meta.MediaStorageSOPInstanceUID = sop_uid
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.ImplementationClassUID = generate_uid()
    meta.SourceApplicationEntityTitle = "SECRET_AE"
    return meta


def _base_ds(
    patient_id: str,
    patient_name: str,
    study_uid: str,
    series_uid: str,
    sop_uid: str,
    modality: str = "CT",
    instance_number: int = 1,
    frame_uid: str | None = None,
    with_private: bool = False,
) -> FileDataset:
    sop_class = "1.2.840.10008.5.1.4.1.1.2"  # CT Image Storage
    ds = FileDataset(
        "",
        Dataset(),
        file_meta=_file_meta(sop_class, sop_uid),
        preamble=b"\0" * 128,
    )
    ds.is_little_endian = True
    ds.is_implicit_VR = False

    ds.SOPClassUID = sop_class
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    if frame_uid:
        ds.FrameOfReferenceUID = frame_uid
    ds.Modality = modality
    ds.InstanceNumber = str(instance_number)
    ds.PatientName = patient_name
    ds.PatientID = patient_id
    ds.IssuerOfPatientID = "SECRET_HOSPITAL_MRN"
    ds.PatientBirthDate = "19820315"
    ds.PatientSex = "M"
    ds.PatientAge = "042Y"
    ds.PatientTelephoneNumbers = "555-1234"
    ds.PatientAddress = "1 Secret Street"
    ds.StudyDate = "20240501"
    ds.StudyTime = "093000"
    ds.AccessionNumber = "ACC123456"
    ds.ReferringPhysicianName = "DR^HOUSE"
    ds.OperatorsName = "OP^NURSE"
    ds.InstitutionName = "Secret General Hospital"
    ds.StationName = "SECRET-CT-1"
    ds.DeviceSerialNumber = "SN-998877"
    ds.ImageComments = "free text may contain PHI: Zhang San 13800000000"
    ds.StudyDescription = "CHEST^ROUTINE"
    ds.SeriesDescription = "Axial 5mm"
    ds.PatientIdentityRemoved = "NO"

    # Minimal pixel data so files are valid images.
    ds.Rows = 4
    ds.Columns = 4
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelRepresentation = 0
    ds.PixelData = bytes(range(16))

    if with_private:
        block = ds.private_block(0x0019, "SECRETCORP", create=True)
        block.add_new(0x10, "LO", "hidden phi 13900000000")

    return ds


def build_cross_referenced_study(tmpdir: str, salt_tag: str = "P1") -> Tuple[List[str], dict]:
    """Create two series where the KOS-style instance references series 1.

    Returns (written_paths, uids dict).
    """
    os.makedirs(tmpdir, exist_ok=True)
    patient_id = f"MRN-{salt_tag}"
    patient_name = f"ZHANG^SAN^{salt_tag}"
    study_uid = generate_uid()
    series1 = generate_uid()
    series2 = generate_uid()
    frame = generate_uid()

    sops = [generate_uid() for _ in range(3)]
    paths: List[str] = []

    for i, sop in enumerate(sops, start=1):
        ds = _base_ds(patient_id, patient_name, study_uid, series1, sop,
                      modality="CT", instance_number=i, frame_uid=frame,
                      with_private=(i == 1))
        p = os.path.join(tmpdir, f"ct_{i}.dcm")
        ds.save_as(p, enforce_file_format=True)
        paths.append(p)

    # 4th instance (a fake "SR-like") referencing the first CT instance via
    # ReferencedSeriesSequence -> ReferencedSOPSequence.
    ref_sop = generate_uid()
    ds = _base_ds(patient_id, patient_name, study_uid, series2, ref_sop,
                  modality="SR", instance_number=1, frame_uid=frame)
    seq_series = Dataset()
    seq_series.SeriesInstanceUID = series1
    ref_item = Dataset()
    ref_item.ReferencedSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    ref_item.ReferencedSOPInstanceUID = sops[0]
    seq_series.ReferencedSOPSequence = pydicom.sequence.Sequence([ref_item])
    ds.ReferencedSeriesSequence = pydicom.sequence.Sequence([seq_series])
    p = os.path.join(tmpdir, "sr_ref.dcm")
    ds.save_as(p, enforce_file_format=True)
    paths.append(p)

    return paths, {
        "patient_id": patient_id,
        "patient_name": patient_name,
        "study": study_uid,
        "series1": series1,
        "series2": series2,
        "frame": frame,
        "ct_sops": sops,
        "ref_sop": ref_sop,
    }


def build_second_patient_file(tmpdir: str) -> str:
    os.makedirs(tmpdir, exist_ok=True)
    ds = _base_ds("MRN-OTHER", "LI^SI", generate_uid(), generate_uid(),
                  generate_uid(), modality="MR", instance_number=1)
    p = os.path.join(tmpdir, "other.dcm")
    ds.save_as(p, enforce_file_format=True)
    return p
