"""Anonymization profiles: which tags are touched and how.

Three built-in profiles are provided:

* ``basic``    – only the direct identity fields (names, IDs, birth date, …).
* ``standard`` – DICOM Basic Application Confidentiality Profile style
                 cleaning (default): physician/operator names, device
                 identifiers, accession numbers, free-text comments and
                 private tags are removed in addition to patient identity.
* ``strict``   – everything in *standard* plus institution/address fields,
                 acquisition dates/times and demographic measurements
                 (age, sex, size, weight).

Every profile always rewrites the DICOM UIDs (Patient/Study/Series/
FrameOfReference/SOP) with patient-stable pseudonymous UIDs and keeps all
in-series references consistent — that is the core feature of this
workbench.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

Tag = Tuple[int, int]

# --- Direct patient identity ------------------------------------------------
TAGS_PATIENT: List[Tag] = [
    (0x0010, 0x0010),  # PatientName
    (0x0010, 0x0020),  # PatientID
    (0x0010, 0x0021),  # IssuerOfPatientID
    (0x0010, 0x0030),  # PatientBirthDate
    (0x0010, 0x0032),  # PatientBirthTime
    (0x0010, 0x0040),  # PatientSex
    (0x0010, 0x1000),  # OtherPatientIDs
    (0x0010, 0x1001),  # OtherPatientNames
    (0x0010, 0x1005),  # PatientBirthName
    (0x0010, 0x1010),  # PatientAge
    (0x0010, 0x1020),  # PatientSize
    (0x0010, 0x1030),  # PatientWeight
    (0x0010, 0x1040),  # PatientAddress
    (0x0010, 0x1060),  # PatientMotherBirthName
    (0x0010, 0x1080),  # MilitaryRank
    (0x0010, 0x1081),  # BranchOfService
    (0x0010, 0x1090),  # MedicalRecordLocator
    (0x0010, 0x2154),  # PatientTelephoneNumbers
    (0x0010, 0x2155),  # PatientTelecomInformation
    (0x0010, 0x2160),  # EthnicGroup
    (0x0010, 0x2180),  # Occupation
    (0x0010, 0x21A0),  # SmokingStatus
    (0x0010, 0x21B0),  # AdditionalPatientHistory
    (0x0010, 0x21C0),  # PregnancyStatus
    (0x0010, 0x21D0),  # LastMenstrualDate
    (0x0010, 0x21F0),  # PatientReligiousPreference
    (0x0010, 0x4000),  # PatientComments
]

# --- Staff / institution identity -------------------------------------------
TAGS_STAFF: List[Tag] = [
    (0x0008, 0x0080),  # InstitutionName
    (0x0008, 0x0081),  # InstitutionAddress
    (0x0008, 0x0090),  # ReferringPhysicianName
    (0x0008, 0x0092),  # ReferringPhysicianAddress
    (0x0008, 0x0094),  # ReferringPhysicianTelephoneNumbers
    (0x0008, 0x1040),  # InstitutionalDepartmentName
    (0x0008, 0x1048),  # PhysiciansOfRecord
    (0x0008, 0x1050),  # PerformingPhysicianName
    (0x0008, 0x1060),  # NameOfPhysiciansReadingStudy
    (0x0008, 0x1070),  # OperatorsName
    (0x0040, 0x2008),  # OrderEnteredBy
]

# --- Device / station identifiers -------------------------------------------
TAGS_DEVICE: List[Tag] = [
    (0x0008, 0x1010),  # StationName
    (0x0018, 0x1000),  # DeviceSerialNumber
    (0x0018, 0x1030),  # ProtocolName
    (0x0018, 0x700A),  # DetectorID
    (0x0040, 0x0242),  # PerformedStationName
]

# --- Workflow / accession identifiers ---------------------------------------
TAGS_WORKFLOW: List[Tag] = [
    (0x0008, 0x0050),  # AccessionNumber
    (0x0038, 0x0010),  # AdmissionID
    (0x0040, 0x1001),  # RequestedProcedureID
    (0x0040, 0x1002),  # ReasonForTheRequestedProcedure
    (0x0040, 0x2001),  # ReasonForTheImagingServiceRequest
    (0x0040, 0x2004),  # IssueDateOfImagingServiceRequest
    (0x0040, 0x2010),  # OrderCallbackPhoneNumber
    (0x0040, 0x2011),  # OrderCallbackTelecomInformation
    (0x0040, 0x2016),  # PlacerOrderNumberImagingServiceRequest
    (0x0040, 0x2017),  # FillerOrderNumberImagingServiceRequest
]

# --- Free-text comments that may leak PHI -----------------------------------
TAGS_FREETEXT: List[Tag] = [
    (0x0008, 0x4000),  # IdentifyingComments
    (0x0018, 0x4000),  # AcquisitionComments
    (0x0020, 0x4000),  # ImageComments
    (0x0028, 0x4000),  # ImagePresentationComments
    (0x0032, 0x4000),  # StudyComments
]

# --- Dates / times (cleared in strict profile) ------------------------------
TAGS_DATETIME: List[Tag] = [
    (0x0008, 0x0020),  # StudyDate
    (0x0008, 0x0021),  # SeriesDate
    (0x0008, 0x0022),  # AcquisitionDate
    (0x0008, 0x0023),  # ContentDate
    (0x0008, 0x0024),  # OverlayDate
    (0x0008, 0x0025),  # CurveDate
    (0x0008, 0x0030),  # StudyTime
    (0x0008, 0x0031),  # SeriesTime
    (0x0008, 0x0032),  # AcquisitionTime
    (0x0008, 0x0033),  # ContentTime
    (0x0008, 0x0034),  # OverlayTime
    (0x0008, 0x0035),  # CurveTime
]

# Source AE title lives in the File Meta Information header.
SOURCE_AE_TITLE: Tag = (0x0002, 0x0016)


def _dedup(seq: List[Tag]) -> Tuple[Tag, ...]:
    return tuple(dict.fromkeys(seq))


# basic: direct patient identity + accession (a common crosswalk back to a
# person in the RIS).
BASIC_TAGS = _dedup(
    [
        (0x0010, 0x0010),
        (0x0010, 0x0020),
        (0x0010, 0x0021),
        (0x0010, 0x0030),
        (0x0010, 0x0032),
        (0x0010, 0x1000),
        (0x0010, 0x1001),
        (0x0010, 0x1040),
        (0x0010, 0x2154),
        (0x0008, 0x0090),
        (0x0008, 0x1050),
        (0x0008, 0x1070),
        (0x0008, 0x0050),
        (0x0038, 0x0010),
    ]
)

# A few coarse demographics stay in the standard profile (they are kept for
# clinical usefulness and are not direct identifiers).
_STANDARD_KEEP = {
    (0x0010, 0x0040),  # PatientSex
    (0x0010, 0x1010),  # PatientAge
    (0x0010, 0x1020),  # PatientSize
    (0x0010, 0x1030),  # PatientWeight
}

# standard: Basic Application Confidentiality Profile, minus dates and
# minus a few coarse demographics kept for clinical usefulness (age, sex).
STANDARD_TAGS = tuple(
    t for t in _dedup(TAGS_PATIENT + TAGS_STAFF + TAGS_DEVICE + TAGS_WORKFLOW + TAGS_FREETEXT)
    if t not in _STANDARD_KEEP
)

# strict: also wipe the remaining demographics and every acquisition date.
STRICT_TAGS = _dedup(list(STANDARD_TAGS) + list(TAGS_PATIENT))


@dataclass(frozen=True)
class Profile:
    """A named anonymization configuration."""

    key: str
    label: str
    description: str
    blank_tags: Tuple[Tag, ...] = ()
    date_tags: Tuple[Tag, ...] = ()
    blank_source_ae: bool = True
    remove_private_tags: bool = True


PROFILES: Dict[str, Profile] = {
    "basic": Profile(
        key="basic",
        label="基础 (Basic)",
        description="仅清除直接身份字段：姓名、患者 ID、出生日期、联系方式以及"
        "检查/申请编号。保留机构、医师、设备、日期与私有标签。",
        blank_tags=BASIC_TAGS,
        blank_source_ae=False,
        remove_private_tags=False,
    ),
    "standard": Profile(
        key="standard",
        label="标准 (Standard)",
        description="对标 DICOM 基础保密配置：清除患者身份、医师姓名、"
        "机构/设备标识、申请编号、自由文本与全部私有标签；"
        "保留年龄、性别、身高体重与检查日期。",
        blank_tags=STANDARD_TAGS,
    ),
    "strict": Profile(
        key="strict",
        label="严格 (Strict)",
        description="在标准配置之上清空全部检查/采集日期与时间，"
        "并移除年龄、性别、身高、体重及来源 AE 标题。",
        blank_tags=STRICT_TAGS,
        date_tags=tuple(TAGS_DATETIME),
    ),
}

DEFAULT_PROFILE = "standard"


def list_profiles() -> List[Profile]:
    return [PROFILES[k] for k in ("basic", "standard", "strict")]


def get_profile(key: str) -> Profile:
    if key not in PROFILES:
        raise KeyError(f"unknown profile: {key!r}")
    return PROFILES[key]
