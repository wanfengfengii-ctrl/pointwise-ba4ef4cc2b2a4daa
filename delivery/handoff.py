import sqlite3
from dataclasses import dataclass

from delivery.provenance import DeliveryLine, verify_provenance
from intake.catalog import load_accepted_manifest, open_catalog
from intake.manifest import original_digest, require_text


class HandoffError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ConfirmationReceipt:
    ticket_number: int
    shift: str
    request_id: str
    start: int
    stop: int


@dataclass(frozen=True, slots=True)
class TakeoverReceipt:
    ticket_number: int
    request_id: str
    next_position: int


def initialize_handoff_tables(db: sqlite3.Connection) -> None:
    db.execute('''
        CREATE TABLE IF NOT EXISTS ticket_handoffs (
            ticket_number INTEGER PRIMARY KEY NOT NULL REFERENCES tickets(number),
            day_shift TEXT NOT NULL,
            evening_shift TEXT NOT NULL
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS ticket_operations (
            number INTEGER PRIMARY KEY,
            ticket_number INTEGER NOT NULL REFERENCES tickets(number),
            request_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('confirm', 'takeover')),
            shift TEXT,
            start_pos INTEGER,
            stop_pos INTEGER,
            UNIQUE (ticket_number, request_id)
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS ticket_confirmation_lines (
            ticket_number INTEGER NOT NULL REFERENCES tickets(number),
            operation_number INTEGER NOT NULL REFERENCES ticket_operations(number),
            position INTEGER NOT NULL CHECK (position >= 0),
            original_id TEXT NOT NULL,
            origin TEXT NOT NULL,
            reading_hex TEXT NOT NULL,
            digest TEXT NOT NULL,
            PRIMARY KEY (operation_number, position)
        )
    ''')
    # Each frozen ticket position belongs to exactly one successful confirmation.
    db.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS one_confirmation_per_ticket_position
        ON ticket_confirmation_lines(ticket_number, position)
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS ticket_takeovers (
            ticket_number INTEGER PRIMARY KEY NOT NULL REFERENCES tickets(number),
            operation_number INTEGER NOT NULL REFERENCES ticket_operations(number),
            next_position INTEGER NOT NULL CHECK (next_position >= 0)
        )
    ''')


def _load_handoff(db: sqlite3.Connection, ticket_number: int) -> sqlite3.Row | None:
    return db.execute('''
        SELECT day_shift, evening_shift FROM ticket_handoffs WHERE ticket_number = ?
    ''', (ticket_number,)).fetchone()


def _load_operation(db: sqlite3.Connection, ticket_number: int, request_id: str) -> sqlite3.Row | None:
    return db.execute('''
        SELECT * FROM ticket_operations WHERE ticket_number = ? AND request_id = ?
    ''', (ticket_number, request_id)).fetchone()


def _load_takeover(db: sqlite3.Connection, ticket_number: int) -> sqlite3.Row | None:
    return db.execute('''
        SELECT operation_number, next_position FROM ticket_takeovers WHERE ticket_number = ?
    ''', (ticket_number,)).fetchone()


def _confirmed_count(db: sqlite3.Connection, ticket_number: int) -> int:
    return db.execute('''
        SELECT COUNT(*) FROM ticket_confirmation_lines WHERE ticket_number = ?
    ''', (ticket_number,)).fetchone()[0]


def _load_confirmation_lines(db: sqlite3.Connection, operation_number: int) -> tuple[sqlite3.Row, ...]:
    return tuple(db.execute('''
        SELECT original_id, origin, reading_hex, digest
        FROM ticket_confirmation_lines WHERE operation_number = ? ORDER BY position
    ''', (operation_number,)))


def _normalize_lines(lines) -> tuple[DeliveryLine, ...]:
    if isinstance(lines, (str, bytes)):
        raise HandoffError('lines must be a nonempty ordered sequence of DeliveryLine values')
    try:
        submitted = tuple(lines)
    except TypeError as exc:
        raise HandoffError('lines must be an ordered sequence of DeliveryLine values') from exc
    if not submitted or any(not isinstance(line, DeliveryLine) for line in submitted):
        raise HandoffError('confirmation requires a nonempty sequence of DeliveryLine values')
    return submitted


def _same_lines(lines, stored: tuple[sqlite3.Row, ...]) -> bool:
    try:
        submitted = tuple(lines)
    except TypeError:
        return False
    if len(submitted) != len(stored):
        return False
    for line, row in zip(submitted, stored):
        if not isinstance(line, DeliveryLine) or type(line.reading) is not int or type(line.payload) is not bytes:
            return False
        if (line.original_id != row['original_id'] or line.origin != row['origin']
                or line.reading != int(row['reading_hex'], 16)
                or original_digest(line.payload) != row['digest']):
            return False
    return True


def enable_handoff(root, ticket_number: int, day_shift: str, evening_shift: str) -> None:
    day = require_text(day_shift, 'day shift identifier')
    evening = require_text(evening_shift, 'evening shift identifier')
    if day == evening:
        raise HandoffError('the two shift identifiers must differ')
    with open_catalog(root, write=True) as db:
        # Imported lazily so delivery.ticket can re-export this module without an import cycle.
        from delivery.ticket import load_ticket

        load_ticket(db, ticket_number)
        existing = _load_handoff(db, ticket_number)
        if existing is not None:
            if existing['day_shift'] != day or existing['evening_shift'] != evening:
                raise HandoffError('ticket handoff is already bound to different shifts')
            return None
        db.execute('''
            INSERT INTO ticket_handoffs (ticket_number, day_shift, evening_shift) VALUES (?, ?, ?)
        ''', (ticket_number, day, evening))
    return None


def confirm(root, ticket_number: int, shift: str, request_id: str, position: int, lines,
            *, before_commit=None) -> ConfirmationReceipt:
    shift_name = require_text(shift, 'shift identifier')
    request = require_text(request_id, 'request identifier')
    if type(position) is not int or position < 0:
        raise HandoffError('position must be a nonnegative integer')
    with open_catalog(root, write=True) as db:
        from delivery.ticket import load_ticket

        fixed_ticket = load_ticket(db, ticket_number)
        handoff = _load_handoff(db, ticket_number)
        if handoff is None:
            raise HandoffError('dual shifts are not enabled for this ticket')
        if shift_name not in (handoff['day_shift'], handoff['evening_shift']):
            raise HandoffError('unknown shift for this ticket')
        operation = _load_operation(db, ticket_number, request)
        if operation is not None:
            if operation['kind'] != 'confirm':
                raise HandoffError('request identifier is already used by a different operation')
            stored = _load_confirmation_lines(db, operation['number'])
            if (operation['shift'] == shift_name and operation['start_pos'] == position
                    and _same_lines(lines, stored)):
                return ConfirmationReceipt(ticket_number, shift_name, request,
                                           operation['start_pos'], operation['stop_pos'])
            raise HandoffError('request identifier is already bound to different confirmation parameters')

        takeover = _load_takeover(db, ticket_number)
        eligible = handoff['evening_shift'] if takeover is not None else handoff['day_shift']
        if shift_name != eligible:
            raise HandoffError('this shift is not eligible to confirm the next unconfirmed position')
        submitted = _normalize_lines(lines)
        confirmed = _confirmed_count(db, ticket_number)
        length = len(fixed_ticket.original_ids)
        stop = position + len(submitted)
        if stop > length:
            raise HandoffError('confirmation window runs past the frozen ticket list')
        if position != confirmed:
            raise HandoffError('confirmation must start at the first unconfirmed ticket position')
        manifest = load_accepted_manifest(db, fixed_ticket.volume_id)
        by_id = manifest.by_id()
        for offset, line in enumerate(submitted):
            wanted = fixed_ticket.original_ids[position + offset]
            if line.original_id != wanted:
                raise HandoffError(f'ticket position {position + offset} requires original {wanted!r}')
            verify_provenance(by_id[wanted], line)

        result = db.execute('''
            INSERT INTO ticket_operations
                (ticket_number, request_id, kind, shift, start_pos, stop_pos)
            VALUES (?, ?, 'confirm', ?, ?, ?)
        ''', (ticket_number, request, shift_name, position, stop))
        operation_number = result.lastrowid
        db.executemany('''
            INSERT INTO ticket_confirmation_lines
                (ticket_number, operation_number, position, original_id, origin, reading_hex, digest)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            (ticket_number, operation_number, position + offset, line.original_id, line.origin,
             format(line.reading, 'x'), original_digest(line.payload))
            for offset, line in enumerate(submitted)
        ))
        if before_commit is not None:
            before_commit(db)
        return ConfirmationReceipt(ticket_number, shift_name, request, position, stop)


def take_over(root, ticket_number: int, request_id, *, before_commit=None) -> TakeoverReceipt:
    request = require_text(request_id, 'request identifier')
    with open_catalog(root, write=True) as db:
        from delivery.ticket import load_ticket

        load_ticket(db, ticket_number)
        handoff = _load_handoff(db, ticket_number)
        if handoff is None:
            raise HandoffError('dual shifts are not enabled for this ticket')
        operation = _load_operation(db, ticket_number, request)
        if operation is not None:
            if operation['kind'] != 'takeover':
                raise HandoffError('request identifier is already used by a different operation')
            takeover = _load_takeover(db, ticket_number)
            return TakeoverReceipt(ticket_number, request, takeover['next_position'])
        if _load_takeover(db, ticket_number) is not None:
            raise HandoffError('the ticket has already been taken over')

        next_position = _confirmed_count(db, ticket_number)
        result = db.execute('''
            INSERT INTO ticket_operations
                (ticket_number, request_id, kind, shift, start_pos, stop_pos)
            VALUES (?, ?, 'takeover', NULL, NULL, NULL)
        ''', (ticket_number, request))
        db.execute('''
            INSERT INTO ticket_takeovers (ticket_number, operation_number, next_position)
            VALUES (?, ?, ?)
        ''', (ticket_number, result.lastrowid, next_position))
        if before_commit is not None:
            before_commit(db)
        return TakeoverReceipt(ticket_number, request, next_position)


def load_position_confirmations(db: sqlite3.Connection, ticket_number: int) -> tuple[sqlite3.Row, ...]:
    return tuple(db.execute('''
        SELECT lines.position AS position, lines.operation_number AS operation_number,
               lines.original_id AS original_id, lines.origin AS origin,
               lines.reading_hex AS reading_hex, lines.digest AS digest,
               ops.ticket_number AS ticket_number, ops.request_id AS request_id, ops.shift AS shift,
               ops.start_pos AS start_pos, ops.stop_pos AS stop_pos
        FROM ticket_confirmation_lines AS lines
        JOIN ticket_operations AS ops ON ops.number = lines.operation_number
        WHERE lines.ticket_number = ?
        ORDER BY lines.position
    ''', (ticket_number,)))


def load_takeover_position(db: sqlite3.Connection, ticket_number: int) -> int | None:
    row = _load_takeover(db, ticket_number)
    return None if row is None else row['next_position']
