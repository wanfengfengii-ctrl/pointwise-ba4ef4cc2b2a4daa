from audit.coverage import CoverageMismatch, verify_coverage
from audit.records import (
    AuditResult,
    JointAuditLine,
    JointAuditResult,
    load_joint_audit_record,
    store_audit_record,
    store_joint_audit,
)
from audit.totals import sum_microstrain
from delivery.handoff import (
    ConfirmationReceipt,
    HandoffError,
    load_position_confirmations,
    load_takeover_position,
)
from delivery.page import collect_originals
from delivery.provenance import ProvenanceMismatch, verify_provenance
from delivery.ticket import load_ticket
from intake.catalog import load_accepted_manifest, open_catalog


class AuditCloseError(ValueError):
    pass


def close(root, ticket_number: int, lines) -> AuditResult:
    with open_catalog(root, write=True) as db:
        fixed_ticket = load_ticket(db, ticket_number)
        by_id = load_accepted_manifest(db, fixed_ticket.volume_id).by_id()
        try:
            verified = verify_coverage(fixed_ticket, lines)
            entries = tuple(by_id[line.original_id] for line in verified)
            for entry, line in zip(entries, verified):
                verify_provenance(entry, line)
        except (CoverageMismatch, ProvenanceMismatch) as exc:
            raise AuditCloseError(str(exc)) from exc
        record = store_audit_record(db, fixed_ticket, entries, sum_microstrain(verified))
        return AuditResult(record, verified)


def close_joint(root, ticket_number: int, *, before_commit=None) -> JointAuditResult:
    with open_catalog(root, write=True) as db:
        fixed_ticket = load_ticket(db, ticket_number)
        length = len(fixed_ticket.original_ids)
        existing = load_joint_audit_record(db, ticket_number)
        if existing is not None:
            # Re-closing the same ticket always returns the same audit number and detail.
            entries = tuple(line.as_manifest_entry() for line in existing.lines)
            originals = collect_originals(root, fixed_ticket.volume_id, entries)
            return JointAuditResult(existing, originals)
        if load_takeover_position(db, ticket_number) is None:
            raise HandoffError('a joint audit requires an enabled ticket with a completed takeover')
        rows = load_position_confirmations(db, ticket_number)
        if len(rows) != length or tuple(row['position'] for row in rows) != tuple(range(length)):
            raise HandoffError('every ticket position needs exactly one valid confirmation')

        manifest = load_accepted_manifest(db, fixed_ticket.volume_id)
        by_id = manifest.by_id()
        joint_lines = []
        operation_numbers = []
        for position, row in enumerate(rows):
            wanted = fixed_ticket.original_ids[position]
            if row['original_id'] != wanted:
                raise HandoffError(f'ticket position {position} requires original {wanted!r}')
            entry = by_id[wanted]
            if (row['origin'] != entry.origin or int(row['reading_hex'], 16) != entry.reading
                    or row['digest'] != entry.digest):
                raise ProvenanceMismatch(f'original {wanted!r} differs from the verified record')
            receipt = ConfirmationReceipt(row['ticket_number'], row['shift'], row['request_id'],
                                          row['start_pos'], row['stop_pos'])
            joint_lines.append(JointAuditLine(wanted, row['origin'], entry.reading, entry.digest, receipt))
            operation_numbers.append(row['operation_number'])
        joint_lines = tuple(joint_lines)
        total = sum(line.reading for line in joint_lines)
        entries = tuple(line.as_manifest_entry() for line in joint_lines)
        originals = collect_originals(root, fixed_ticket.volume_id, entries)
        record = store_joint_audit(db, fixed_ticket, joint_lines, total, tuple(operation_numbers))
        if before_commit is not None:
            before_commit(db)
        return JointAuditResult(record, originals)
