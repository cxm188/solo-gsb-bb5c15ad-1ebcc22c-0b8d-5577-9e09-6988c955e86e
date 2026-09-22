"""Core anonymization engine.

The engine performs two passes over an uploaded imaging set:

1. ``scan()`` reads every DICOM instance, builds the patient / study /
   series hierarchy, derives the deterministic UID remap tables and produces
   a per-field change manifest used for the web preview.
2. ``apply()`` rewrites each instance according to the selected profile,
   walking every sequence so in-series references stay consistent, and
   writes anonymized files ready for ZIP packaging.

UID pseudonymization is *patient-stable*: given the same salt the same
source UID always maps to the same pseudonymous UID, so a patient uploaded
in multiple batches keeps one identity, while the mapping cannot be
reversed without the salt.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pydicom
from pydicom.dataelem import DataElement, RawDataElement
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.tag import BaseTag, Tag

from .config import Profile, SOURCE_AE_TITLE

# --- UID roots (arbitrary, unique, valid 2.x-style numeric components) ------
# 38 digits each, well below the DICOM 64-char UID length limit once a hash
# suffix is appended.
ROOT_ORG = "2.25."  # ISO branch allowing GUID/UUID-based UIDs
ROOT_PATIENT = ROOT_ORG + "890173024158603217459182736401"
ROOT_STUDY = ROOT_ORG + "736104928571620384756102938475"
ROOT_SERIES = ROOT_ORG + "651039482756109384756291038472"
ROOT_FRAME = ROOT_ORG + "482719305610293847561829304751"
ROOT_SOP = ROOT_ORG + "571920384657102938475610283940"
ROOT_CONCAT = ROOT_ORG + "394857102938475610293847560192"

# UID-valued attributes that must NOT be remapped (they are type/coding UIDs,
# not instance identifiers).
UID_TAGS_SKIP = {
    Tag(0x0002, 0x0002),  # MediaStorageSOPClassUID
    Tag(0x0008, 0x0016),  # SOPClassUID
    Tag(0x0008, 0x1150),  # ReferencedSOPClassUID
    Tag(0x0002, 0x0012),  # ImplementationClassUID
    Tag(0x0008, 0x0018),  # handled explicitly as SOP instance
    Tag(0x0002, 0x0003),  # handled explicitly as SOP instance
}

# Tag -> UID domain used when walking sequences / ContentSequence etc.
UID_DOMAIN_BY_TAG: Dict[BaseTag, str] = {
    Tag(0x0008, 0x0018): "sop",
    Tag(0x0002, 0x0003): "sop",
    Tag(0x0008, 0x1155): "sop",
    Tag(0x0020, 0x000D): "study",
    Tag(0x0020, 0x000E): "series",
    Tag(0x0020, 0x0052): "frame",
    Tag(0x0020, 0x0200): "frame",  # SynchronizationFrameOfReferenceUID
    Tag(0x0020, 0x9161): "concat",
}

# De-identification audit attributes (PS3.7 C.7.4.2).
DEID_AUDIT = {
    (0x0012, 0x0062): ("PatientIdentityRemoved", "YES"),
    (0x0012, 0x0063): ("DeidentificationMethod", "ANWS-PATIENT-STABLE-UID-REMAP"),
}

# How values are rendered in the change manifest.
_VALUE_MAX = 80
_UID_PREVIEW = 18


def generate_salt() -> str:
    """Return a fresh per-job 128-bit salt (hex)."""
    return secrets.token_hex(16)


@dataclass
class UidMap:
    """Deterministic, salted remapping for every UID domain."""

    salt: str
    tables: Dict[str, Dict[str, str]] = field(
        default_factory=lambda: {"patient": {}, "study": {}, "series": {}, "frame": {}, "sop": {}, "concat": {}}
    )
    roots: Dict[str, str] = field(
        default_factory=lambda: {
            "patient": ROOT_PATIENT,
            "study": ROOT_STUDY,
            "series": ROOT_SERIES,
            "frame": ROOT_FRAME,
            "sop": ROOT_SOP,
            "concat": ROOT_CONCAT,
        }
    )

    @staticmethod
    def _digest(salt: str, value: str) -> str:
        # 25 decimal digits of a SHA-256 digest – numeric, unique for all
        # practical purposes, keeps the resulting UID <= 64 chars.  The
        # result is forced into [10^24, 10^25) so the component never starts
        # with a zero (forbidden in a DICOM UI value).
        h = hashlib.sha256(f"{salt}|{value}".encode("utf-8")).hexdigest()
        n = (int(h, 16) % (10**25 - 10**24)) + 10**24
        return str(n)

    def remap(self, domain: str, value: str) -> str:
        value = (value or "").strip().strip("\x00")
        if not value:
            return value
        table = self.tables.setdefault(domain, {})
        if value not in table:
            candidate = f"{self.roots[domain]}.{self._digest(self.salt, value)}"
            if len(candidate) > 64:  # extremely defensive
                candidate = candidate[:64]
            table[value] = candidate
        return table[value]

    def reverse_preview(self) -> List[Dict[str, str]]:
        """All non-SOP remappings, for the manifest (SOPs listed per file)."""
        out: List[Dict[str, str]] = []
        labels = {"patient": "PatientUID", "study": "StudyInstanceUID", "series": "SeriesInstanceUID", "frame": "FrameOfReferenceUID", "concat": "ConcatenationUID"}
        for domain in ("patient", "study", "series", "frame", "concat"):
            for old, new in sorted(self.tables.get(domain, {}).items()):
                out.append({"domain": labels[domain], "old": old, "new": new})
        return out


# --- Hierarchy + manifest dataclasses ---------------------------------------

@dataclass
class FileInfo:
    index: int
    source_name: str          # display name inside the upload (anonymized later)
    rel_path: str             # path inside the extracted upload folder
    size: int
    patient_uid: str = ""
    study_uid: str = ""
    series_uid: str = ""
    sop_uid: str = ""
    sop_class_uid: str = ""
    patient_name: str = ""
    patient_id: str = ""
    study_date: str = ""
    modality: str = ""
    instance_number: str = ""
    output_relpath: str = ""
    new_sop_uid: str = ""
    error: Optional[str] = None


@dataclass
class TagChange:
    """Aggregated per-field change across one or more files."""

    tag: Tuple[int, int]
    keyword: str
    name: str
    vr: str
    action: str               # blanked | date_cleared | uid_remapped | private_removed | added | unchanged
    before: str
    after: str
    files: List[str] = field(default_factory=list)


def _short(value: object, limit: int = _VALUE_MAX) -> str:
    if value is None:
        return ""
    s = str(value)
    s = s.replace("\x00", "").strip()
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s


def _uid_preview(uid: str) -> str:
    if not uid:
        return ""
    if len(uid) <= _UID_PREVIEW + 4:
        return uid
    return uid[:_UID_PREVIEW] + "…"


def _is_private(tag: BaseTag) -> bool:
    return tag.group % 2 == 1


# --- Dataset loading ---------------------------------------------------------

def load_dataset(path: str) -> FileDataset:
    """Load a DICOM file tolerantly (also handles files without preamble)."""
    try:
        ds = pydicom.dcmread(path, force=False, stop_before_pixels=False)
        ds.file_meta  # touch to make sure it parsed
        return ds
    except Exception:
        # Retry forcing; some real-world exports miss the preamble.
        ds = pydicom.dcmread(path, force=True)
        if not hasattr(ds, "file_meta") or ds.file_meta is None:
            ds.file_meta = FileMetaDataset()
        return ds


def _el(ds: Dataset, tag: Tuple[int, int]) -> Optional[DataElement]:
    t = Tag(*tag)
    return ds.get(t) if t in ds else None


def _str_value(ds: Dataset, tag: Tuple[int, int]) -> str:
    el = _el(ds, tag)
    if el is None or el.VR == "SQ":
        return ""
    try:
        return _short(el.value)
    except Exception:
        return ""


def looks_like_dicom(path: str) -> bool:
    """Cheap magic-byte / parse probe used while walking uploads.

    A file counts as DICOM when it carries the DICM preamble or parses and
    exposes at least one of the mandatory SOP/study/series UID attributes
    (this rejects random text/binary that pydicom can force-parse to an
    empty dataset).
    """
    required = (
        Tag(0x0008, 0x0016),  # SOPClassUID
        Tag(0x0008, 0x0018),  # SOPInstanceUID
        Tag(0x0020, 0x000D),  # StudyInstanceUID
        Tag(0x0020, 0x000E),  # SeriesInstanceUID
    )
    try:
        with open(path, "rb") as fh:
            pre = fh.read(132)
        if len(pre) >= 132 and pre[128:132] == b"DICM":
            return True
        ds = pydicom.dcmread(path, force=True, stop_before_pixels=True, defer_size="1 KB")
        return any(t in ds for t in required)
    except Exception:
        return False


# --- Scan pass ---------------------------------------------------------------

@dataclass
class ScanResult:
    files: List[FileInfo]
    uid_map: UidMap
    # tag -> {value: [file display names]}
    tag_values: Dict[Tuple[int, int], Dict[str, List[str]]] = field(default_factory=dict)
    private_files: List[str] = field(default_factory=list)
    profile: Optional[Profile] = None

    @property
    def patients(self) -> List[Dict[str, str]]:
        seen: Dict[str, Dict[str, str]] = {}
        for f in self.files:
            if f.patient_uid and f.patient_uid not in seen:
                seen[f.patient_uid] = {"patient_uid": f.patient_uid, "patient_name": f.patient_name, "patient_id": f.patient_id}
        return list(seen.values())

    def hierarchy(self) -> List[dict]:
        """Patient -> Study -> Series -> instance tree for the UI."""
        pmap: Dict[str, dict] = {}
        for f in self.files:
            p = pmap.setdefault(
                f.patient_uid,
                {
                    "old_uid": f.patient_uid,
                    "new_uid": self.uid_map.remap("patient", f.patient_uid) if f.patient_uid else "",
                    "name": f.patient_name,
                    "patient_id": f.patient_id,
                    "studies": {},
                },
            )
            st = p["studies"].setdefault(
                f.study_uid,
                {
                    "old_uid": f.study_uid,
                    "new_uid": self.uid_map.remap("study", f.study_uid) if f.study_uid else "",
                    "date": f.study_date,
                    "series": {},
                },
            )
            sr = st["series"].setdefault(
                f.series_uid,
                {
                    "old_uid": f.series_uid,
                    "new_uid": self.uid_map.remap("series", f.series_uid) if f.series_uid else "",
                    "modality": f.modality,
                    "instances": 0,
                },
            )
            sr["instances"] += 1
        out = []
        for p in pmap.values():
            studies = []
            for st in p["studies"].values():
                studies.append({**st, "series": list(st["series"].values())})
            out.append({**p, "studies": studies})
        return out


def scan_files(paths: Sequence[Tuple[str, str]], salt: Optional[str] = None) -> ScanResult:
    """Read every instance and pre-build UID mappings + observed values.

    ``paths`` is a sequence of ``(absolute_path, relative_display_name)``.
    """
    uid_map = UidMap(salt or generate_salt())
    files: List[FileInfo] = []
    tag_values: Dict[Tuple[int, int], Dict[str, List[str]]] = {}
    private_files: List[str] = []

    for idx, (abs_path, rel_name) in enumerate(sorted(paths, key=lambda p: p[1])):
        info = FileInfo(index=idx, source_name=rel_name, rel_path=abs_path, size=_safe_size(abs_path))
        try:
            ds = load_dataset(abs_path)
            # DICOM has no top-level "PatientInstanceUID"; the stable patient
            # pivot is the PatientID (fallback: name / study) and drives the
            # pseudonymous patient identity.
            info.patient_uid = _pivot_identity(ds)
            info.study_uid = _uid_of(ds, (0x0020, 0x000D))
            info.series_uid = _uid_of(ds, (0x0020, 0x000E))
            info.sop_uid = _uid_of(ds, (0x0008, 0x0018))
            info.sop_class_uid = _uid_of(ds, (0x0008, 0x0016))
            info.patient_name = _str_value(ds, (0x0010, 0x0010))
            info.patient_id = _str_value(ds, (0x0010, 0x0020))
            info.study_date = _str_value(ds, (0x0008, 0x0020))
            info.modality = _str_value(ds, (0x0008, 0x0060))
            info.instance_number = _str_value(ds, (0x0020, 0x0013))

            if info.patient_uid:
                uid_map.remap("patient", info.patient_uid)
            if info.study_uid:
                uid_map.remap("study", info.study_uid)
            if info.series_uid:
                uid_map.remap("series", info.series_uid)
            if info.sop_uid:
                uid_map.remap("sop", info.sop_uid)
            frame = _uid_of(ds, (0x0020, 0x0052))
            if frame:
                uid_map.remap("frame", frame)
            concat = _uid_of(ds, (0x0020, 0x9161))
            if concat:
                uid_map.remap("concat", concat)

            _collect_values(ds, tag_values, rel_name)
            if _has_private(ds):
                private_files.append(rel_name)
        except Exception as exc:  # unreadable instance – surfaced to the UI
            info.error = f"{type(exc).__name__}: {exc}"
        files.append(info)

    return ScanResult(files=files, uid_map=uid_map, tag_values=tag_values, private_files=private_files)


def _safe_size(path: str) -> int:
    try:
        import os

        return os.path.getsize(path)
    except OSError:
        return 0


def _uid_of(ds: Dataset, tag: Tuple[int, int]) -> str:
    el = _el(ds, tag)
    if el is None or el.VR == "SQ":
        return ""
    return str(el.value or "").strip().strip("\x00")


def _pivot_identity(ds: Dataset) -> str:
    """Stable per-patient key used to derive the pseudonymous PatientUID."""
    pid = _str_value(ds, (0x0010, 0x0020))
    if pid:
        return f"ID:{pid}"
    name = _str_value(ds, (0x0010, 0x0010))
    if name:
        return f"NAME:{name}"
    study = _uid_of(ds, (0x0020, 0x000D))
    return f"STUDY:{study}" if study else "UNKNOWN"


def _collect_values(ds: Dataset, out: Dict[Tuple[int, int], Dict[str, List[str]]], name: str) -> None:
    """Record observed non-SQ values at top level (for the preview table)."""
    for el in ds:
        if el.VR == "SQ" or isinstance(el, RawDataElement) and el.VR == "SQ":
            continue
        tag = (el.tag.group, el.tag.element)
        if _is_private(el.tag):
            continue
        try:
            value = _short(el.value)
        except Exception:
            value = "<unprintable>"
        bucket = out.setdefault(tag, {})
        bucket.setdefault(value, []).append(name)


def _has_private(ds: Dataset) -> bool:
    try:
        return any(_is_private(el.tag) for el in ds)
    except Exception:
        return False


# --- Manifest construction ---------------------------------------------------

def build_changes(scan: ScanResult, profile: Profile) -> List[TagChange]:
    """Aggregated field-level change list for the selected profile."""
    changes: Dict[Tuple[int, int], TagChange] = {}

    def add(tag: Tuple[int, int], action: str, after: str, file_names: Iterable[str]) -> None:
        if tag not in scan.tag_values and action != "added":
            return
        observed = scan.tag_values.get(tag, {})
        before = ", ".join(sorted(v for v in observed if v)[:3]) or "（空）"
        vr, keyword, name = _tag_meta(tag)
        ch = changes.get(tag) or TagChange(tag=tag, keyword=keyword, name=name, vr=vr, action=action, before=before, after=after)
        ch.files.extend(file_names)
        changes[tag] = ch

    blank = set(profile.blank_tags)
    for tag in blank:
        vals = scan.tag_values.get(tag, {})
        files_having = [n for v, names in vals.items() if v for n in names]
        if files_having:
            add(tag, "blanked", "—（清空/移除）—", files_having)

    for tag in profile.date_tags:
        vals = scan.tag_values.get(tag, {})
        files_having = [n for v, names in vals.items() if v for n in names]
        if files_having:
            add(tag, "date_cleared", "—（日期清空）—", files_having)

    # UID remapping entries for the identity/study/series attributes.
    for tag, domain in (
        ((0x0010, 0x0020), "patient"),  # PatientID preview handled below; UID entries separate
        ((0x0020, 0x000D), "study"),
        ((0x0020, 0x000E), "series"),
        ((0x0020, 0x0052), "frame"),
        ((0x0008, 0x0018), "sop"),
    ):
        if domain == "patient":
            continue
        table = scan.uid_map.tables.get(domain, {})
        if not table:
            continue
        vals = scan.tag_values.get(tag, {})
        files_having = [n for names in vals.values() for n in names]
        old0, new0 = next(iter(sorted(table.items())))
        after = f"{_uid_preview(old0)}  →  {_uid_preview(new0)}"
        if len(table) > 1:
            after += f" 等 {len(table)} 个 UID"
        add(tag, "uid_remapped", after, files_having or [f.source_name for f in scan.files])

    if profile.remove_private_tags and scan.private_files:
        ch = TagChange(
            tag=(0xFFFF, 0x0000),
            keyword="PrivateCreators",
            name="全部私有标签 (odd group, e.g. 0009/0019/0029)",
            vr="—",
            action="private_removed",
            before=f"{len(scan.private_files)} 个文件含私有标签",
            after="全部移除",
            files=sorted(set(scan.private_files)),
        )
        changes[(0xFFFF, 0x0000)] = ch

    for tag, (keyword, value) in DEID_AUDIT.items():
        if any(f.error is None for f in scan.files):
            add(tag, "added", value, [f.source_name for f in scan.files if f.error is None])

    # de-dup file lists
    for ch in changes.values():
        ch.files = sorted(set(ch.files))

    order = {"blanked": 0, "date_cleared": 1, "uid_remapped": 2, "private_removed": 3, "added": 4, "unchanged": 5}
    return sorted(changes.values(), key=lambda c: (order.get(c.action, 9), c.tag))


def _tag_meta(tag) -> Tuple[str, str, str]:
    from pydicom.datadict import dictionary_VR, keyword_for_tag

    t = tag if isinstance(tag, BaseTag) else Tag(*tag)
    try:
        vr = dictionary_VR(t)
    except Exception:
        vr = ""
    keyword = keyword_for_tag(t) or f"({tag[0]:04X},{tag[1]:04X})"
    try:
        from pydicom.datadict import dictionary_description

        name = dictionary_description(t)
    except Exception:
        name = keyword
    return vr, keyword, name


# --- Apply pass --------------------------------------------------------------

def anonymize_dataset(ds: FileDataset, info: FileInfo, scan: ScanResult, profile: Profile) -> None:
    """Rewrite *ds* in place; recurse into all sequences for references."""
    blank = set(profile.blank_tags) | set(profile.date_tags)

    # 1) private creators / tags
    if profile.remove_private_tags:
        try:
            ds.remove_private_tags()
        except Exception:
            _remove_private_manual(ds)

    # 2) file meta header
    meta = getattr(ds, "file_meta", None)
    if meta is not None:
        if SOURCE_AE_TITLE in meta and profile.blank_source_ae:
            del meta[SOURCE_AE_TITLE]
        _rewrite_container(meta, blank, scan, in_file_meta=True, in_sequence=False)

    # 3) main dataset (recurses through every SQ automatically)
    _rewrite_container(ds, blank, scan, in_file_meta=False, in_sequence=False)

    # 4) patient-stable pseudonymous identity (same patient -> same pseudo)
    pseudo_id = scan.uid_map.remap("patient", info.patient_uid) if info.patient_uid else ""
    if pseudo_id:
        for t in (Tag(0x0010, 0x0020), Tag(0x0010, 0x0010)):
            if t in ds:
                del ds[t]
        ds.add_new(Tag(0x0010, 0x0020), "LO", _pseudo_patient_id(scan, info.patient_uid))
        ds.add_new(Tag(0x0010, 0x0010), "PN", f"ANON^PATIENT^{pseudo_id[-8:]}")

    # 5) de-identification audit attributes
    for tag, (keyword, value) in DEID_AUDIT.items():
        t = Tag(*tag)
        if t in ds:
            del ds[t]
        ds.add_new(t, "CS" if keyword.endswith("Removed") else "LO", value)

    # 5) ensure file meta SOP instance UID mirrors the new instance UID
    new_sop = scan.uid_map.remap("sop", info.sop_uid) if info.sop_uid else ""
    if meta is not None and new_sop and Tag(0x0002, 0x0003) in meta:
        meta.MediaStorageSOPInstanceUID = new_sop
    if new_sop:
        ds.SOPInstanceUID = new_sop
    info.new_sop_uid = new_sop


def _remove_private_manual(ds: Dataset) -> None:
    for container in _walk_containers(ds):
        for el in list(container):
            if _is_private(el.tag):
                del container[el.tag]


def _rewrite_container(container: Dataset, blank: set, scan: ScanResult, in_file_meta: bool, in_sequence: bool) -> None:
    for el in list(container):
        tag = el.tag
        if _is_private(tag):
            continue
        t = (tag.group, tag.element)

        if el.VR == "SQ":
            seq = el.value
            if seq is not None:
                for item in seq:
                    if isinstance(item, Dataset):
                        _rewrite_container(item, blank, scan, in_file_meta=False, in_sequence=True)
            continue

        # PatientID *inside a reference sequence* links to the patient; remap
        # it instead of deleting so the cross-reference stays resolvable.
        if Tag(t) == Tag(0x0010, 0x0020) and in_sequence:
            original = _short(el.value, limit=128)
            if original:
                el.value = _pseudo_patient_id(scan, original)
            continue

        if t in blank:
            try:
                del container[tag]
            except Exception:
                pass
            continue

        if el.VR == "UI" and Tag(t) not in UID_TAGS_SKIP:
            original = str(el.value or "").strip().strip("\x00")
            if original:
                el.value = _remap_any_uid(Tag(t), original, scan)
            continue

        if in_file_meta and t == SOURCE_AE_TITLE:
            # handled by caller depending on profile
            continue


def _remap_any_uid(tag: BaseTag, value: str, scan: ScanResult) -> str:
    """Remap any UI-valued attribute while never touching coding UIDs.

    Resolution order:

    1. Explicitly typed instance attributes (Study/Series/SOP/Frame/…).
    2. The value already appears in one of the known-domain tables — this is
       what keeps references inside arbitrary sequences consistent even when
       the referencing attribute name is unfamiliar.
    3. Keyword-based inference for uncommon reference attributes.
    4. Unrecognized, previously unseen UID (typically a coding / scheme UID):
       left untouched.
    """
    domain = UID_DOMAIN_BY_TAG.get(tag)
    if domain:
        return scan.uid_map.remap(domain, value)

    for candidate in ("sop", "study", "series", "frame", "concat"):
        if value in scan.uid_map.tables.get(candidate, {}):
            return scan.uid_map.remap(candidate, value)

    inferred = _infer_domain((tag.group, tag.element))
    if inferred:
        return scan.uid_map.remap(inferred, value)
    return value


def _infer_domain(t: Tuple[int, int]) -> Optional[str]:
    """Fallback domain classification for UI-valued attributes.

    The standard reference tags are covered by ``UID_DOMAIN_BY_TAG``; this
    function catches less common reference attributes (RT plans, SR content,
    presentation states) by keyword so cross-instance links stay intact.
    """
    tag = Tag(*t)
    from pydicom.datadict import keyword_for_tag

    kw = keyword_for_tag(tag) or ""
    if "FrameOfReferenceUID" in kw or "SynchronizationFrame" in kw:
        return "frame"
    if "ConcatenationUID" in kw:
        return "concat"
    if "StudyInstanceUID" in kw:
        return "study"
    if "SeriesInstanceUID" in kw:
        return "series"
    if "SOPInstanceUID" in kw:
        return "sop"
    if kw.endswith("UID") and ("Referenced" in kw or "Source" in kw):
        return "sop"
    # Unknown UI attributes (private tags were removed already): keep
    # untouched to avoid corrupting coding / transfer-syntax UIDs.
    return None


def _walk_containers(ds: Dataset) -> Iterable[Dataset]:
    yield ds
    for el in ds:
        if el.VR == "SQ" and el.value:
            for item in el.value:
                if isinstance(item, Dataset):
                    yield from _walk_containers(item)


_PSEUDO_CACHE: Dict[Tuple[str, str], str] = {}


def _pseudo_patient_id(scan: ScanResult, original: str) -> str:
    key = (scan.uid_map.salt, original)
    if key not in _PSEUDO_CACHE:
        h = hashlib.sha256(f"{scan.uid_map.salt}|PID|{original}".encode()).hexdigest()[:12].upper()
        _PSEUDO_CACHE[key] = f"ANON-{h}"
    return _PSEUDO_CACHE[key]


def save_dataset(ds: FileDataset, dest_path: str) -> None:
    """Write anonymized dataset preserving transfer syntax where possible."""
    import os

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    ts = getattr(getattr(ds, "file_meta", None), "TransferSyntaxUID", None)
    # Deflated transfer syntax cannot be re-encoded directly by pydicom's
    # plain writer; fall back to explicit VR little endian.
    if ts and str(ts) == "1.2.840.10008.1.2.1.99":
        ds.file_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian
    if not ts:
        ds.file_meta.TransferSyntaxUID = pydicom.uid.ExplicitVRLittleEndian
    ds.save_as(dest_path, enforce_file_format=True, write_like_original=False)
