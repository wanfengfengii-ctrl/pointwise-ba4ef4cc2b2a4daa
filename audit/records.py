import sqlite3
from dataclasses import dataclass

from delivery.handoff import ConfirmationReceipt
from delivery.provenance import DeliveryLine
from delivery.ticket import TicketOrder
from intake.manifest import ManifestEntry


@dataclass(frozen=True, slots=True)
class AuditLine:
    original_id: str
    origin: str
    reading: int
    digest: str

    def as_manifest_entry(self) -> ManifestEntry:
        return ManifestEntry(self.original_id, self.origin, self.reading, self.digest)


@dataclass(frozen=True, slots=True)
class JointAuditLine:
    original_id: str
    origin: str
    reading: int
    digest: str
    confirmation: ConfirmationReceipt

    def as_manifest_entry(self) -> ManifestEntry:
        return ManifestEntry(self.original_id, self.origin, self.reading, self.digest)


@dataclass(frozen=True, slots=True)
class AuditRecord:
    number: int
    ticket_number: int
    volume_id: str
    lines: tuple[AuditLine, ...]
    total: int


@dataclass(frozen=True, slots=True)
class JointAuditRecord:
    number: int
    ticket_number: int
    volume_id: str
    lines: tuple[JointAuditLine, ...]
    total: int


@dataclass(frozen=True, slots=True)
class AuditResult:
    record: AuditRecord
    originals: tuple[DeliveryLine, ...]


@dataclass(frozen=True, slots=True)
class JointAuditResult:
    record: JointAuditRecord
    originals: tuple[DeliveryLine, ...]


def initialize_audit_records(db: sqlite3.Connection) -> None:
    db.execute('''
        CREATE TABLE IF NOT EXISTS audit_records (
            number INTEGER PRIMARY KEY,
            ticket_number INTEGER NOT NULL REFERENCES tickets(number),
            total_hex TEXT NOT NULL
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS audit_lines (
            audit_number INTEGER NOT NULL REFERENCES audit_records(number),
            position INTEGER NOT NULL CHECK (position >= 0),
            original_id TEXT NOT NULL,
            origin TEXT NOT NULL,
            reading_hex TEXT NOT NULL,
            digest TEXT NOT NULL,
            PRIMARY KEY (audit_number, position)
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS joint_audit_records (
            audit_number INTEGER PRIMARY KEY NOT NULL REFERENCES audit_records(number),
            ticket_number INTEGER NOT NULL UNIQUE REFERENCES tickets(number)
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS joint_audit_confirmations (
            audit_number INTEGER NOT NULL REFERENCES audit_records(number),
            position INTEGER NOT NULL CHECK (position >= 0),
            operation_number INTEGER NOT NULL REFERENCES ticket_operations(number),
            PRIMARY KEY (audit_number, position)
        )
    ''')


def store_audit_record(db: sqlite3.Connection, fixed_ticket: TicketOrder,
                       entries: tuple[ManifestEntry, ...], total: int) -> AuditRecord:
    result = db.execute('INSERT INTO audit_records (ticket_number, total_hex) VALUES (?, ?)',
                        (fixed_ticket.number, format(total, 'x')))
    number = result.lastrowid
    lines = tuple(AuditLine(entry.original_id, entry.origin, entry.reading, entry.digest) for entry in entries)
    db.executemany('''
        INSERT INTO audit_lines (audit_number, position, original_id, origin, reading_hex, digest)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (
        (number, position, line.original_id, line.origin, format(line.reading, 'x'), line.digest)
        for position, line in enumerate(lines)
    ))
    return AuditRecord(number, fixed_ticket.number, fixed_ticket.volume_id, lines, total)


def load_audit_record(db: sqlite3.Connection, audit_number: int) -> AuditRecord | None:
    if type(audit_number) is not int or not 1 <= audit_number < (1 << 63):
        return None
    header = db.execute('''
        SELECT records.ticket_number, tickets.volume_id, records.total_hex
        FROM audit_records AS records JOIN tickets ON tickets.number = records.ticket_number
        WHERE records.number = ?
    ''', (audit_number,)).fetchone()
    if header is None:
        return None
    rows = db.execute('''
        SELECT original_id, origin, reading_hex, digest FROM audit_lines
        WHERE audit_number = ? ORDER BY position
    ''', (audit_number,))
    lines = tuple(AuditLine(row['original_id'], row['origin'], int(row['reading_hex'], 16), row['digest'])
                  for row in rows)
    return AuditRecord(audit_number, header['ticket_number'], header['volume_id'], lines, int(header['total_hex'], 16))


def store_joint_audit(db: sqlite3.Connection, fixed_ticket: TicketOrder,
                      joint_lines: tuple[JointAuditLine, ...], total: int,
                      operation_numbers: tuple[int, ...]) -> JointAuditRecord:
    if len(operation_numbers) != len(joint_lines):
        raise ValueError('each joint audit line needs its confirmation operation')
    result = db.execute('INSERT INTO audit_records (ticket_number, total_hex) VALUES (?, ?)',
                        (fixed_ticket.number, format(total, 'x')))
    number = result.lastrowid
    db.execute('INSERT INTO joint_audit_records (audit_number, ticket_number) VALUES (?, ?)',
               (number, fixed_ticket.number))
    db.executemany('''
        INSERT INTO audit_lines (audit_number, position, original_id, origin, reading_hex, digest)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (
        (number, position, line.original_id, line.origin, format(line.reading, 'x'), line.digest)
        for position, line in enumerate(joint_lines)
    ))
    db.executemany('''
        INSERT INTO joint_audit_confirmations (audit_number, position, operation_number)
        VALUES (?, ?, ?)
    ''', (
        (number, position, operation_number)
        for position, operation_number in enumerate(operation_numbers)
    ))
    return JointAuditRecord(number, fixed_ticket.number, fixed_ticket.volume_id, joint_lines, total)


def is_joint_audit(db: sqlite3.Connection, audit_number: int) -> bool:
    if type(audit_number) is not int or not 1 <= audit_number < (1 << 63):
        return False
    return db.execute('''
        SELECT 1 FROM joint_audit_records WHERE audit_number = ?
    ''', (audit_number,)).fetchone() is not None


def load_joint_audit_record(db: sqlite3.Connection, audit_number: int) -> JointAuditRecord | None:
    if type(audit_number) is not int or not 1 <= audit_number < (1 << 63):
        return None
    header = db.execute('''
        SELECT records.ticket_number, tickets.volume_id, records.total_hex
        FROM audit_records AS records
        JOIN joint_audit_records AS joint ON joint.audit_number = records.number
        JOIN tickets ON tickets.number = records.ticket_number
        WHERE records.number = ?
    ''', (audit_number,)).fetchone()
    if header is None:
        return None
    rows = db.execute('''
        SELECT lines.original_id AS original_id, lines.origin AS origin,
               lines.reading_hex AS reading_hex, lines.digest AS digest,
               ops.ticket_number AS ticket_number, ops.shift AS shift,
               ops.request_id AS request_id, ops.start_pos AS start_pos, ops.stop_pos AS stop_pos
        FROM audit_lines AS lines
        JOIN joint_audit_confirmations AS joint
             ON joint.audit_number = lines.audit_number AND joint.position = lines.position
        JOIN ticket_operations AS ops ON ops.number = joint.operation_number
        WHERE lines.audit_number = ?
        ORDER BY lines.position
    ''', (audit_number,))
    joint_lines = tuple(
        JointAuditLine(
            row['original_id'], row['origin'], int(row['reading_hex'], 16), row['digest'],
            ConfirmationReceipt(row['ticket_number'], row['shift'], row['request_id'],
                                 row['start_pos'], row['stop_pos']),
        )
        for row in rows
    )
    return JointAuditRecord(audit_number, header['ticket_number'], header['volume_id'],
                            joint_lines, int(header['total_hex'], 16))
