import json
from pathlib import Path
from tempfile import TemporaryDirectory

from audit.close import AuditCloseError, close
from audit.history import readback
from delivery.page import page
from delivery.ticket import ticket
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


def main():
    with TemporaryDirectory(prefix="haizhan-handoff-fixture-") as directory:
        print(json.dumps(check_v1(Path(directory)), sort_keys=True))


if __name__ == "__main__":
    main()
