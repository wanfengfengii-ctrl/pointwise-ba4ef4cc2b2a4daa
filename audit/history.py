from audit.records import AuditResult, JointAuditResult, is_joint_audit, load_audit_record, load_joint_audit_record
from delivery.page import collect_originals
from intake.catalog import open_catalog


class UnknownAuditNumber(LookupError):
    pass


def readback(root, audit_number: int):
    with open_catalog(root) as db:
        if is_joint_audit(db, audit_number):
            record = load_joint_audit_record(db, audit_number)
            if record is None:
                raise UnknownAuditNumber(audit_number)
            entries = tuple(line.as_manifest_entry() for line in record.lines)
            originals = collect_originals(root, record.volume_id, entries)
            return JointAuditResult(record, originals)
        record = load_audit_record(db, audit_number)
    if record is None:
        raise UnknownAuditNumber(audit_number)
    entries = tuple(line.as_manifest_entry() for line in record.lines)
    return AuditResult(record, collect_originals(root, record.volume_id, entries))
