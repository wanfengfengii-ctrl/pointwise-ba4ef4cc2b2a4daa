import sqlite3
from dataclasses import replace
from hashlib import sha256
from threading import Barrier, Thread

from audit.close import close, close_joint
from audit.history import UnknownAuditNumber, readback
from audit.records import JointAuditResult
from delivery.handoff import HandoffError
from delivery.page import page
from delivery.ticket import (
    ConfirmationReceipt,
    TakeoverReceipt,
    UnknownTicket,
    confirm,
    enable_handoff,
    take_over,
    ticket,
)
from intake.manifest import VolumeManifest
from intake.register import accept
from reorder.request import reorder
from tests.assertions import assert_originals
from tests.fixtures import VolumeCase, deny_commit, initialize

DAY = 'day'
EVENING = 'evening'
HANDOFF_REQUEST = 'handoff'


class HandoffCase(VolumeCase):
    def setUp(self):
        super().setUp()
        self.admit()
        self.issued = ticket(self.root, self.volume_id)
        self.ticket_number = self.issued.number
        enable_handoff(self.root, self.ticket_number, DAY, EVENING)

    def window(self, start, size):
        return page(self.root, self.ticket_number, start, size).lines

    def joint_close_flow(self, plan):
        # plan: ordered (shift, request_id, start, size) confirmation steps with an optional takeover point.
        for step in plan:
            if step == 'takeover':
                take_over(self.root, self.ticket_number, HANDOFF_REQUEST)
                continue
            shift, request_id, start, size = step
            confirm(self.root, self.ticket_number, shift, request_id, start, self.window(start, size))
        return close_joint(self.root, self.ticket_number)


class EnableHandoffTests(HandoffCase):
    def test_unknown_ticket(self):
        for bad in (self.ticket_number + 1, 0, True, 1 << 70):
            with self.subTest(ticket=bad), self.assertRaises(UnknownTicket):
                enable_handoff(self.root, bad, DAY, EVENING)

    def test_shift_text_rules(self):
        for day, evening in (('', EVENING), ('  ', EVENING), (DAY, ''), (DAY, DAY), (DAY, 'day')):
            with self.subTest(day=day, evening=evening), self.assertRaises(ValueError):
                enable_handoff(self.root, self.ticket_number + 0, day, evening)

    def test_idempotent_same_shifts(self):
        self.assertIsNone(enable_handoff(self.root, self.ticket_number, DAY, EVENING))
        self.assertEqual(self.count('ticket_handoffs'), 1)
        receipt = confirm(self.root, self.ticket_number, DAY, 'day-A', 0, self.window(0, 1))
        self.assertEqual(receipt, ConfirmationReceipt(self.ticket_number, DAY, 'day-A', 0, 1))

    def test_different_shifts_rejected_but_progress_kept(self):
        confirm(self.root, self.ticket_number, DAY, 'day-A', 0, self.window(0, 1))
        with self.assertRaises(HandoffError):
            enable_handoff(self.root, self.ticket_number, 'other-day', EVENING)
        # The original binding still owns the ticket and keeps its progress.
        take_over(self.root, self.ticket_number, HANDOFF_REQUEST)
        receipt = confirm(self.root, self.ticket_number, EVENING, 'evening-BC', 1, self.window(1, 2))
        self.assertEqual((receipt.start, receipt.stop), (1, 3))

    def test_confirm_before_enable(self):
        fresh = ticket(self.root, self.volume_id)
        with self.assertRaises(HandoffError):
            confirm(self.root, fresh.number, DAY, 'day-A', 0, page(self.root, fresh.number, 0, 1).lines)
        with self.assertRaises(HandoffError):
            take_over(self.root, fresh.number, HANDOFF_REQUEST)

    def test_state_survives_reinitialization(self):
        confirm(self.root, self.ticket_number, DAY, 'day-A', 0, self.window(0, 1))
        initialize(self.root)
        take_over(self.root, self.ticket_number, HANDOFF_REQUEST)
        self.assertEqual(
            confirm(self.root, self.ticket_number, DAY, 'day-A', 0, self.window(0, 1)),
            ConfirmationReceipt(self.ticket_number, DAY, 'day-A', 0, 1),
        )


class ConfirmationTests(HandoffCase):
    def test_first_confirmation_receipt_fields(self):
        receipt = confirm(self.root, self.ticket_number, DAY, 'day-A', 0, self.window(0, 1))
        self.assertEqual(receipt, ConfirmationReceipt(self.ticket_number, DAY, 'day-A', 0, 1))
        self.assertEqual(self.count('ticket_operations'), 1)
        self.assertEqual(self.count('ticket_confirmation_lines'), 1)

    def test_continuous_segment_confirmation(self):
        receipt = confirm(self.root, self.ticket_number, DAY, 'day-AB', 0, self.window(0, 2))
        self.assertEqual((receipt.start, receipt.stop), (0, 2))
        self.assertEqual(self.count('ticket_confirmation_lines'), 2)

    def test_replay_same_parameters_returns_original_receipt(self):
        first = confirm(self.root, self.ticket_number, DAY, 'day-A', 0, self.window(0, 1))
        again = confirm(self.root, self.ticket_number, DAY, 'day-A', 0, self.window(0, 1))
        self.assertEqual(first, again)
        self.assertEqual(self.count('ticket_operations'), 1)
        self.assertEqual(self.count('ticket_confirmation_lines'), 1)

    def test_replay_after_takeover_keeps_day_receipt(self):
        first = confirm(self.root, self.ticket_number, DAY, 'day-A', 0, self.window(0, 1))
        take_over(self.root, self.ticket_number, HANDOFF_REQUEST)
        with self.assertRaises(HandoffError):
            confirm(self.root, self.ticket_number, DAY, 'day-B', 1, self.window(1, 1))
        self.assertEqual(
            confirm(self.root, self.ticket_number, DAY, 'day-A', 0, self.window(0, 1)), first
        )

    def test_same_request_different_parameters_rejected(self):
        confirm(self.root, self.ticket_number, DAY, 'day-A', 0, self.window(0, 1))
        with self.assertRaises(HandoffError):
            confirm(self.root, self.ticket_number, EVENING, 'day-A', 0, self.window(0, 1))
        with self.assertRaises(HandoffError):
            confirm(self.root, self.ticket_number, DAY, 'day-A', 1, self.window(1, 1))
        altered = (replace(self.window(0, 1)[0], origin='别处'),)
        with self.assertRaises(HandoffError):
            confirm(self.root, self.ticket_number, DAY, 'day-A', 0, altered)
        with self.assertRaises(HandoffError):
            confirm(self.root, self.ticket_number, DAY, 'day-A', 0, self.window(0, 1) + self.window(1, 1))
        self.assertEqual(self.count('ticket_operations'), 1)
        # The original request still returns its original receipt.
        self.assertEqual(
            confirm(self.root, self.ticket_number, DAY, 'day-A', 0, self.window(0, 1)),
            ConfirmationReceipt(self.ticket_number, DAY, 'day-A', 0, 1),
        )

    def test_request_id_shared_with_takeover(self):
        take_over(self.root, self.ticket_number, 'shared')
        with self.assertRaises(HandoffError):
            confirm(self.root, self.ticket_number, EVENING, 'shared', 0, self.window(0, 1))
        fresh = ticket(self.root, self.volume_id)
        enable_handoff(self.root, fresh.number, DAY, EVENING)
        confirm(self.root, fresh.number, DAY, 'shared-2', 0, page(self.root, fresh.number, 0, 1).lines)
        with self.assertRaises(HandoffError):
            take_over(self.root, fresh.number, 'shared-2')

    def test_gap_overrun_and_bad_windows(self):
        confirm(self.root, self.ticket_number, DAY, 'day-A', 0, self.window(0, 1))
        with self.assertRaises(HandoffError):
            confirm(self.root, self.ticket_number, DAY, 'gap', 2, self.window(2, 1))
        with self.assertRaises(HandoffError):
            confirm(self.root, self.ticket_number, DAY, 'overrun-window', 1, self.window(2, 1))
        one_line = self.window(2, 1)
        with self.assertRaises(HandoffError):
            confirm(self.root, self.ticket_number, DAY, 'overrun-start', 4, one_line)
        with self.assertRaises(HandoffError):
            confirm(self.root, self.ticket_number, DAY, 'rewind', 0, self.window(0, 1))
        for bad_position in (-1, True, 1.0):
            with self.subTest(position=bad_position), self.assertRaises(ValueError):
                confirm(self.root, self.ticket_number, DAY, f'pos-{bad_position}', bad_position,
                        self.window(0, 1))
        for bad_lines in ((), 'not-lines', (object(),)):
            with self.subTest(lines=bad_lines), self.assertRaises(ValueError):
                confirm(self.root, self.ticket_number, DAY, f'lines-{type(bad_lines)}', 1, bad_lines)
        self.assertEqual(self.count('ticket_confirmation_lines'), 1)

    def test_identity_origin_bytes_and_reading_checks(self):
        a, b, c = self.window(0, 3)
        for bad in ((b,), (replace(a, origin=b.origin),), (replace(a, payload=a.payload + b' '),),
                    (replace(a, reading=8),)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                confirm(self.root, self.ticket_number, DAY, f'bad-{id(bad)}', 0, bad)
        self.assertEqual(self.count('ticket_confirmation_lines'), 0)

    def test_eligibility_switches_on_takeover(self):
        with self.assertRaises(HandoffError):
            confirm(self.root, self.ticket_number, EVENING, 'evening-early', 0, self.window(0, 1))
        confirm(self.root, self.ticket_number, DAY, 'day-A', 0, self.window(0, 1))
        receipt = take_over(self.root, self.ticket_number, HANDOFF_REQUEST)
        self.assertEqual(receipt, TakeoverReceipt(self.ticket_number, HANDOFF_REQUEST, 1))
        with self.assertRaises(HandoffError):
            confirm(self.root, self.ticket_number, DAY, 'day-late', 1, self.window(1, 1))
        evening = confirm(self.root, self.ticket_number, EVENING, 'evening-BC', 1, self.window(1, 2))
        self.assertEqual(evening, ConfirmationReceipt(self.ticket_number, EVENING, 'evening-BC', 1, 3))

    def test_takeover_before_any_confirmation(self):
        receipt = take_over(self.root, self.ticket_number, HANDOFF_REQUEST)
        self.assertEqual(receipt.next_position, 0)
        evening = confirm(self.root, self.ticket_number, EVENING, 'evening-all', 0, self.window(0, 3))
        self.assertEqual((evening.start, evening.stop), (0, 3))
        with self.assertRaises(HandoffError):
            confirm(self.root, self.ticket_number, DAY, 'day-A', 0, self.window(0, 1))

    def test_full_ticket_takeover_at_end(self):
        confirm(self.root, self.ticket_number, DAY, 'day-ABC', 0, self.window(0, 3))
        receipt = take_over(self.root, self.ticket_number, HANDOFF_REQUEST)
        self.assertEqual(receipt.next_position, 3)
        # The boundary stays at the list length even after later replay.
        self.assertEqual(take_over(self.root, self.ticket_number, HANDOFF_REQUEST), receipt)

    def test_failed_commit_keeps_state_and_retry_eligibility(self):
        with self.assertRaises(sqlite3.Error):
            confirm(self.root, self.ticket_number, DAY, 'day-A', 0, self.window(0, 1),
                    before_commit=deny_commit)
        self.assertEqual(self.count('ticket_operations'), 0)
        self.assertEqual(self.count('ticket_confirmation_lines'), 0)
        receipt = confirm(self.root, self.ticket_number, DAY, 'day-A', 0, self.window(0, 1))
        self.assertEqual(receipt, ConfirmationReceipt(self.ticket_number, DAY, 'day-A', 0, 1))

    def test_unknown_ticket(self):
        with self.assertRaises(UnknownTicket):
            confirm(self.root, self.ticket_number + 99, DAY, 'x', 0, self.window(0, 1))

    def test_blank_identifiers_rejected(self):
        for request_id in ('', '   '):
            with self.subTest(request_id=request_id), self.assertRaises(ValueError):
                confirm(self.root, self.ticket_number, DAY, request_id, 0, self.window(0, 1))
        with self.assertRaises(ValueError):
            take_over(self.root, self.ticket_number, '')


class TakeoverTests(HandoffCase):
    def test_takeover_replay_keeps_original_boundary(self):
        confirm(self.root, self.ticket_number, DAY, 'day-A', 0, self.window(0, 1))
        first = take_over(self.root, self.ticket_number, HANDOFF_REQUEST)
        confirm(self.root, self.ticket_number, EVENING, 'evening-BC', 1, self.window(1, 2))
        again = take_over(self.root, self.ticket_number, HANDOFF_REQUEST)
        self.assertEqual(first, again)
        self.assertEqual(again.next_position, 1)
        self.assertEqual(self.count('ticket_takeovers'), 1)

    def test_second_takeover_request_rejected(self):
        take_over(self.root, self.ticket_number, HANDOFF_REQUEST)
        with self.assertRaises(HandoffError):
            take_over(self.root, self.ticket_number, 'second-handoff')
        self.assertEqual(self.count('ticket_operations'), 1)

    def test_takeover_requires_enabled_ticket(self):
        fresh = ticket(self.root, self.volume_id)
        with self.assertRaises(HandoffError):
            take_over(self.root, fresh.number, HANDOFF_REQUEST)

    def test_failed_commit_rolls_back_takeover(self):
        with self.assertRaises(sqlite3.Error):
            take_over(self.root, self.ticket_number, HANDOFF_REQUEST, before_commit=deny_commit)
        self.assertEqual(self.count('ticket_takeovers'), 0)
        # Day shift remains eligible after the failed takeover.
        receipt = confirm(self.root, self.ticket_number, DAY, 'day-A', 0, self.window(0, 1))
        self.assertEqual(receipt.shift, DAY)
        take_over(self.root, self.ticket_number, HANDOFF_REQUEST)
        self.assertEqual(self.count('ticket_takeovers'), 1)

    def test_committed_takeover_reply_can_be_lost_and_replayed(self):
        confirm(self.root, self.ticket_number, DAY, 'day-A', 0, self.window(0, 1))
        captured = {}

        def capture_after_write(db):
            # The takeover row is already written in this transaction; the caller later loses the return.
            captured['next_position'] = db.execute(
                'SELECT next_position FROM ticket_takeovers WHERE ticket_number = ?',
                (self.ticket_number,)).fetchone()['next_position']

        first = take_over(self.root, self.ticket_number, HANDOFF_REQUEST, before_commit=capture_after_write)
        self.assertEqual(captured['next_position'], 1)
        # Replay with the same request id after losing the first reply returns the same boundary.
        second = take_over(self.root, self.ticket_number, HANDOFF_REQUEST)
        self.assertEqual(first, second)
        self.assertEqual(self.count('ticket_takeovers'), 1)
        self.assertEqual(self.count('ticket_operations'), 2)


class CommitOrderingTests(HandoffCase):
    def _park(self, barrier):
        def hook(db):
            db.execute('PRAGMA busy_timeout=10000')
            barrier.wait(timeout=20)
        return hook

    def test_confirmation_commits_before_takeover(self):
        confirm(self.root, self.ticket_number, DAY, 'day-A', 0, self.window(0, 1))
        b_lines = self.window(1, 1)
        barrier = Barrier(2)
        errors = []

        def day_confirm():
            try:
                confirm(self.root, self.ticket_number, DAY, 'day-B', 1, b_lines,
                        before_commit=self._park(barrier))
            except BaseException as exc:  # pragma: no cover - surfaced via assertion below
                errors.append(exc)

        thread = Thread(target=day_confirm)
        thread.start()
        barrier.wait(timeout=20)
        takeover = take_over(self.root, self.ticket_number, HANDOFF_REQUEST)
        thread.join(timeout=20)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(takeover.next_position, 2)
        # The lost reply is recovered by replaying the identical request.
        self.assertEqual(
            confirm(self.root, self.ticket_number, DAY, 'day-B', 1, b_lines),
            ConfirmationReceipt(self.ticket_number, DAY, 'day-B', 1, 2),
        )
        evening_c = confirm(self.root, self.ticket_number, EVENING, 'evening-C', 2, self.window(2, 1))
        self.assertEqual((evening_c.start, evening_c.stop), (2, 3))
        sealed = close_joint(self.root, self.ticket_number)
        self.assertEqual(tuple(line.confirmation.request_id for line in sealed.record.lines),
                         ('day-A', 'day-B', 'evening-C'))
        self.assertEqual(tuple(line.confirmation.shift for line in sealed.record.lines),
                         (DAY, DAY, EVENING))

    def test_takeover_commits_before_confirmation(self):
        confirm(self.root, self.ticket_number, DAY, 'day-A', 0, self.window(0, 1))
        b_lines = self.window(1, 1)
        barrier = Barrier(2)
        errors = []

        def handoff():
            try:
                take_over(self.root, self.ticket_number, HANDOFF_REQUEST,
                          before_commit=self._park(barrier))
            except BaseException as exc:  # pragma: no cover - surfaced via assertion below
                errors.append(exc)

        thread = Thread(target=handoff)
        thread.start()
        barrier.wait(timeout=20)
        with self.assertRaises(HandoffError):
            confirm(self.root, self.ticket_number, DAY, 'day-B', 1, b_lines)
        thread.join(timeout=20)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        evening_bc = confirm(self.root, self.ticket_number, EVENING, 'evening-BC', 1, self.window(1, 2))
        self.assertEqual((evening_bc.start, evening_bc.stop), (1, 3))
        sealed = close_joint(self.root, self.ticket_number)
        self.assertEqual(tuple(line.confirmation.request_id for line in sealed.record.lines),
                         ('day-A', 'evening-BC', 'evening-BC'))
        self.assertEqual(tuple(line.confirmation.shift for line in sealed.record.lines),
                         (DAY, EVENING, EVENING))


class JointAuditTests(HandoffCase):
    def test_joint_record_pins_positions_receipts_and_originals(self):
        confirm(self.root, self.ticket_number, DAY, 'day-A', 0, self.window(0, 1))
        confirm(self.root, self.ticket_number, DAY, 'day-B', 1, self.window(1, 1))
        take_over(self.root, self.ticket_number, HANDOFF_REQUEST)
        confirm(self.root, self.ticket_number, EVENING, 'evening-C', 2, self.window(2, 1))
        sealed = close_joint(self.root, self.ticket_number)
        self.assertIsInstance(sealed, JointAuditResult)
        record = sealed.record
        self.assertEqual((record.ticket_number, record.volume_id, record.total),
                         (self.ticket_number, self.volume_id, 31))
        expected_receipts = (
            ConfirmationReceipt(self.ticket_number, DAY, 'day-A', 0, 1),
            ConfirmationReceipt(self.ticket_number, DAY, 'day-B', 1, 2),
            ConfirmationReceipt(self.ticket_number, EVENING, 'evening-C', 2, 3),
        )
        self.assertEqual(tuple(line.confirmation for line in record.lines), expected_receipts)
        self.assertEqual(tuple(line.original_id for line in record.lines), ('A', 'B', 'C'))
        self.assertEqual(tuple(line.reading for line in record.lines), (7, 11, 13))
        assert_originals(self, sealed.originals, 'ABC')
        self.assertEqual(tuple(line.digest for line in record.lines),
                         tuple(sha256(line.payload).hexdigest() for line in sealed.originals))

    def test_one_confirmation_covers_two_positions(self):
        confirm(self.root, self.ticket_number, DAY, 'day-A', 0, self.window(0, 1))
        take_over(self.root, self.ticket_number, HANDOFF_REQUEST)
        receipt = confirm(self.root, self.ticket_number, EVENING, 'evening-BC', 1, self.window(1, 2))
        sealed = close_joint(self.root, self.ticket_number)
        self.assertEqual(tuple(line.confirmation for line in sealed.record.lines[1:]), (receipt, receipt))
        self.assertEqual(sealed.record.total, 31)

    def test_close_rejected_until_complete(self):
        with self.assertRaises(HandoffError):
            close_joint(self.root, self.ticket_number)
        confirm(self.root, self.ticket_number, DAY, 'day-A', 0, self.window(0, 1))
        with self.assertRaises(HandoffError):
            close_joint(self.root, self.ticket_number)
        take_over(self.root, self.ticket_number, HANDOFF_REQUEST)
        with self.assertRaises(HandoffError):
            close_joint(self.root, self.ticket_number)
        confirm(self.root, self.ticket_number, EVENING, 'evening-B', 1, self.window(1, 1))
        with self.assertRaises(HandoffError):
            close_joint(self.root, self.ticket_number)
        self.assertEqual(self.count('audit_records'), 0)

    def test_empty_ticket_zero_joint_audit(self):
        accept(self.root, VolumeManifest('empty', ()), ())
        empty = ticket(self.root, 'empty')
        enable_handoff(self.root, empty.number, DAY, EVENING)
        self.assertEqual(take_over(self.root, empty.number, HANDOFF_REQUEST).next_position, 0)
        sealed = close_joint(self.root, empty.number)
        self.assertEqual((sealed.record.total, sealed.record.lines, sealed.originals), (0, (), ()))
        self.assertEqual(readback(self.root, sealed.record.number), sealed)

    def test_full_ticket_takeover_joint_audit(self):
        confirm(self.root, self.ticket_number, DAY, 'day-ABC', 0, self.window(0, 3))
        take_over(self.root, self.ticket_number, HANDOFF_REQUEST)
        sealed = close_joint(self.root, self.ticket_number)
        self.assertTrue(all(line.confirmation.shift == DAY for line in sealed.record.lines))
        self.assertEqual(sealed.record.total, 31)

    def test_repeat_close_returns_same_number_and_detail(self):
        confirm(self.root, self.ticket_number, DAY, 'day-ABC', 0, self.window(0, 3))
        take_over(self.root, self.ticket_number, HANDOFF_REQUEST)
        first = close_joint(self.root, self.ticket_number)
        second = close_joint(self.root, self.ticket_number)
        self.assertEqual(first, second)
        self.assertEqual(self.count('audit_records'), 1)
        initialize(self.root)
        history = readback(self.root, first.record.number)
        self.assertEqual(history, first)
        self.assertIsInstance(history, JointAuditResult)

    def test_joint_and_ordinary_audits_share_number_space(self):
        other = ticket(self.root, self.volume_id)
        ordinary = close(self.root, other.number, page(self.root, other.number, 0, 3).lines)
        confirm(self.root, self.ticket_number, DAY, 'day-ABC', 0, self.window(0, 3))
        take_over(self.root, self.ticket_number, HANDOFF_REQUEST)
        joint = close_joint(self.root, self.ticket_number)
        self.assertNotEqual(ordinary.record.number, joint.record.number)
        self.assertEqual(readback(self.root, ordinary.record.number), ordinary)
        self.assertEqual(readback(self.root, joint.record.number), joint)
        for unknown in (joint.record.number + 100, 0, True, 1 << 70):
            with self.subTest(number=unknown), self.assertRaises(UnknownAuditNumber):
                readback(self.root, unknown)

    def test_failed_joint_commit_can_retry(self):
        confirm(self.root, self.ticket_number, DAY, 'day-ABC', 0, self.window(0, 3))
        take_over(self.root, self.ticket_number, HANDOFF_REQUEST)
        with self.assertRaises(sqlite3.Error):
            close_joint(self.root, self.ticket_number, before_commit=deny_commit)
        self.assertEqual(self.count('audit_records'), 0)
        sealed = close_joint(self.root, self.ticket_number)
        self.assertEqual(sealed.record.total, 31)

    def test_frozen_ticket_order_used_after_reorder(self):
        confirm(self.root, self.ticket_number, DAY, 'day-A', 0, self.window(0, 1))
        take_over(self.root, self.ticket_number, HANDOFF_REQUEST)
        reorder(self.root, self.volume_id, 'change-route', ('B', 'C', 'A'))
        # The old ticket still pages its frozen A,B,C order from the rearranged panels.
        receipt = confirm(self.root, self.ticket_number, EVENING, 'evening-BC', 1, self.window(1, 2))
        self.assertEqual((receipt.start, receipt.stop), (1, 3))
        sealed = close_joint(self.root, self.ticket_number)
        self.assertEqual(tuple(line.original_id for line in sealed.record.lines), ('A', 'B', 'C'))
        assert_originals(self, sealed.originals, 'ABC')
        self.assertEqual(sealed.record.total, 31)

    def test_equal_lines_verify_independently_on_two_tickets(self):
        second = ticket(self.root, self.volume_id)
        enable_handoff(self.root, second.number, DAY, EVENING)
        shared_lines = page(self.root, self.ticket_number, 0, 1).lines
        first_receipt = confirm(self.root, self.ticket_number, DAY, 'day-A', 0, shared_lines)
        second_receipt = confirm(self.root, second.number, DAY, 'day-A', 0, shared_lines)
        self.assertEqual(first_receipt.ticket_number, self.ticket_number)
        self.assertEqual(second_receipt.ticket_number, second.number)
        for number in (self.ticket_number, second.number):
            take_over(self.root, number, HANDOFF_REQUEST)
            confirm(self.root, number, EVENING, 'evening-BC', 1,
                    page(self.root, number, 1, 2).lines)
            sealed = close_joint(self.root, number)
            self.assertEqual(sealed.record.total, 31)

    def test_ordinary_audit_still_works_alongside_handoff(self):
        # Enabling dual shifts must not remove the original whole-ticket audit path.
        ordinary = close(self.root, self.ticket_number, page(self.root, self.ticket_number, 0, 3).lines)
        self.assertEqual(ordinary.record.total, 31)
        self.assertEqual(readback(self.root, ordinary.record.number), ordinary)
        with self.assertRaises(UnknownTicket):
            close_joint(self.root, self.ticket_number + 99)

    def test_exact_integer_total_through_joint_audit(self):
        big = 9223372036854775808
        entries = tuple(replace(entry, reading=value)
                        for entry, value in zip(self.manifest.entries, (big, 1, 0)))
        originals = tuple(replace(image, reading=value)
                          for image, value in zip(self.originals, (big, 1, 0)))
        accept(self.root, VolumeManifest('exact-joint', entries), originals)
        issued = ticket(self.root, 'exact-joint')
        enable_handoff(self.root, issued.number, DAY, EVENING)
        confirm(self.root, issued.number, DAY, 'day-all', 0, page(self.root, issued.number, 0, 3).lines)
        take_over(self.root, issued.number, HANDOFF_REQUEST)
        sealed = close_joint(self.root, issued.number)
        self.assertEqual(sealed.record.total, big + 1)
        self.assertIs(type(sealed.record.total), int)
        self.assertEqual(tuple(line.reading for line in sealed.record.lines), (big, 1, 0))
        initialize(self.root)
        self.assertEqual(readback(self.root, sealed.record.number), sealed)
