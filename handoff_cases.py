import json
from pathlib import Path
from tempfile import TemporaryDirectory

from audit.close import AuditCloseError, close, close_joint
from audit.history import readback
from delivery.page import page
from delivery.ticket import confirm, enable_handoff, take_over, ticket
from demo import load_sample
from intake.catalog import initialize_catalog
from intake.register import accept
from media.store import initialize_media
from reorder.request import reorder

DAY = "day"
EVENING = "evening"
HANDOFF_REQUEST = "handoff"
EXPECTED_ORDER = ("A", "B", "C")
EXPECTED_TOTAL = 31

# Handoff position and (shift, confirmation request) in original ticket order.
EXPECTED = {
    "confirm-first": (2, ((DAY, "day-A"), (DAY, "day-B"), (EVENING, "evening-C"))),
    "handoff-first": (1, ((DAY, "day-A"), (EVENING, "evening-BC"), (EVENING, "evening-BC"))),
}


def open_example(root):
    initialize_catalog(root)
    initialize_media(root)
    manifest, originals, target = load_sample()
    accept(root, manifest, originals)
    return ticket(root, manifest.volume_id), target


def open_handoff_ticket(root):
    issued, target = open_example(root)
    enable_handoff(root, issued.number, DAY, EVENING)
    return issued, target


def check_v1(root):
    issued, target = open_example(root)
    ticket_number = issued.number
    first = page(root, ticket_number, 0, 1)
    before = page(root, ticket_number, 1, 2)
    try:
        close(root, ticket_number, first.lines)
    except AuditCloseError:
        pass
    else:
        raise RuntimeError("V1 must reject a partial audit")

    reorder(root, issued.volume_id, "change-route", target)
    if page(root, ticket_number, 1, 2) != before:
        raise RuntimeError("V1 must keep the old ticket order")
    lines = first.lines + before.lines
    if tuple(line.original_id for line in lines) != EXPECTED_ORDER:
        raise RuntimeError("unexpected V1 ticket order")

    sealed = close(root, ticket_number, lines)
    duplicate = close(root, ticket_number, lines)
    if sealed.record.total != EXPECTED_TOTAL or sealed.record.number == duplicate.record.number:
        raise RuntimeError("unexpected V1 whole-ticket audit")
    if readback(root, sealed.record.number) != sealed:
        raise RuntimeError("unexpected V1 audit readback")
    return {"v1_order": EXPECTED_ORDER, "total": sealed.record.total, "v1_repeat_uses_new_number": True}


def _assert_joint(result, case_id, ticket_number):
    expected_position, expected_confirmations = EXPECTED[case_id]
    record = result.record
    if record.ticket_number != ticket_number or record.total != EXPECTED_TOTAL:
        raise RuntimeError(f"{case_id}: unexpected joint audit header {record!r}")
    actual = tuple((line.confirmation.shift, line.confirmation.request_id) for line in record.lines)
    if actual != expected_confirmations:
        raise RuntimeError(f"{case_id}: confirmation ownership {actual!r} != {expected_confirmations!r}")
    if tuple(line.original_id for line in record.lines) != EXPECTED_ORDER:
        raise RuntimeError(f"{case_id}: joint audit must keep the frozen ticket order")
    if tuple(line.reading for line in record.lines) != (7, 11, 13):
        raise RuntimeError(f"{case_id}: joint audit readings differ from the originals")
    if tuple(line.original_id for line in result.originals) != EXPECTED_ORDER:
        raise RuntimeError(f"{case_id}: originals must be paged in frozen ticket order")
    return expected_position


def run_confirm_first(root):
    """Directory order: day-B commits before the takeover; its success reply is lost."""
    issued, target = open_handoff_ticket(root)
    ticket_number = issued.number

    day_a = confirm(root, ticket_number, DAY, "day-A", 0, page(root, ticket_number, 0, 1).lines)
    if day_a != day_a.__class__(ticket_number, DAY, "day-A", 0, 1):
        raise RuntimeError("confirm-first: unexpected day-A receipt")

    lost_replies = []

    def lose_day_b_reply(db):
        # The row is written inside this catalog transaction; dropping the return models a lost reply.
        lost_replies.append(db.execute(
            "SELECT number FROM ticket_operations WHERE ticket_number = ? AND request_id = 'day-B'",
            (ticket_number,)).fetchone()["number"])

    confirm(root, ticket_number, DAY, "day-B", 1, page(root, ticket_number, 1, 1).lines,
            before_commit=lose_day_b_reply)
    if not lost_replies:
        raise RuntimeError("confirm-first: lost-reply hook did not run")

    takeover = take_over(root, ticket_number, HANDOFF_REQUEST)
    if takeover.next_position != 2:
        raise RuntimeError(f"confirm-first: handoff position {takeover.next_position} != 2")

    # The day shift is no longer eligible after the takeover, but the identical request replays its receipt.
    day_b = confirm(root, ticket_number, DAY, "day-B", 1, page(root, ticket_number, 1, 1).lines)
    if day_b != day_b.__class__(ticket_number, DAY, "day-B", 1, 2):
        raise RuntimeError("confirm-first: day-B replay must return the original receipt")

    # Coverage is still missing the C position, so the joint audit cannot be issued yet.
    try:
        close_joint(root, ticket_number)
    except ValueError:
        pass
    else:
        raise RuntimeError("confirm-first: a premature joint audit must be rejected")

    evening_c = confirm(root, ticket_number, EVENING, "evening-C", 2, page(root, ticket_number, 2, 1).lines)
    if evening_c != evening_c.__class__(ticket_number, EVENING, "evening-C", 2, 3):
        raise RuntimeError("confirm-first: unexpected evening-C receipt")

    # The disk may be rearranged during the shift break; the frozen ticket keeps A,B,C.
    reorder(root, issued.volume_id, "change-route", target)
    sealed = close_joint(root, ticket_number)
    handoff_position = _assert_joint(sealed, "confirm-first", ticket_number)
    repeated = close_joint(root, ticket_number)
    if repeated.record.number != sealed.record.number or repeated != sealed:
        raise RuntimeError("confirm-first: re-closing must return the same audit number and detail")

    # Reopen with a fresh catalog connection and claim the sealed joint audit by number.
    initialize_catalog(root)
    history = readback(root, sealed.record.number)
    if history != sealed:
        raise RuntimeError("confirm-first: joint audit readback differs from the sealed result")
    return {
        "case_id": "confirm-first",
        "directory_order": ["day-B 确认先提交成功（回包丢失）", "handoff 接手"],
        "handoff_position": handoff_position,
        "takeover_receipt": {"ticket_number": takeover.ticket_number, "request_id": takeover.request_id,
                             "next_position": takeover.next_position},
        "confirmation_requests": [list(item) for item in EXPECTED["confirm-first"][1]],
        "total": history.record.total,
        "audit_number": history.record.number,
        "shifts": [line.confirmation.shift for line in history.record.lines],
    }


def run_handoff_first(root):
    """Directory order: the takeover commits before day-B; the late day confirmation is rejected."""
    issued, _target = open_handoff_ticket(root)
    ticket_number = issued.number

    confirm(root, ticket_number, DAY, "day-A", 0, page(root, ticket_number, 0, 1).lines)
    takeover = take_over(root, ticket_number, HANDOFF_REQUEST)
    if takeover.next_position != 1:
        raise RuntimeError(f"handoff-first: handoff position {takeover.next_position} != 1")

    rejected = False
    try:
        confirm(root, ticket_number, DAY, "day-B", 1, page(root, ticket_number, 1, 1).lines)
    except ValueError:
        rejected = True
    if not rejected:
        raise RuntimeError("handoff-first: the day shift's first confirmation after handoff must be rejected")

    try:
        close_joint(root, ticket_number)
    except ValueError:
        pass
    else:
        raise RuntimeError("handoff-first: a premature joint audit must be rejected")

    evening_bc = confirm(root, ticket_number, EVENING, "evening-BC", 1, page(root, ticket_number, 1, 2).lines)
    if evening_bc != evening_bc.__class__(ticket_number, EVENING, "evening-BC", 1, 3):
        raise RuntimeError("handoff-first: unexpected evening-BC receipt")

    sealed = close_joint(root, ticket_number)
    handoff_position = _assert_joint(sealed, "handoff-first", ticket_number)
    initialize_catalog(root)
    history = readback(root, sealed.record.number)
    if history != sealed:
        raise RuntimeError("handoff-first: joint audit readback differs from the sealed result")
    return {
        "case_id": "handoff-first",
        "directory_order": ["handoff 接手先提交成功", "day-B 首次确认被拒"],
        "handoff_position": handoff_position,
        "takeover_receipt": {"ticket_number": takeover.ticket_number, "request_id": takeover.request_id,
                             "next_position": takeover.next_position},
        "confirmation_requests": [list(item) for item in EXPECTED["handoff-first"][1]],
        "total": history.record.total,
        "audit_number": history.record.number,
        "shifts": [line.confirmation.shift for line in history.record.lines],
    }


def main():
    results = []
    with TemporaryDirectory(prefix="haizhan-handoff-v1-") as directory:
        results.append({"v1": check_v1(Path(directory))})
    for runner in (run_confirm_first, run_handoff_first):
        with TemporaryDirectory(prefix=f"haizhan-handoff-{runner.__name__}-") as directory:
            results.append(runner(Path(directory)))
    print(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
