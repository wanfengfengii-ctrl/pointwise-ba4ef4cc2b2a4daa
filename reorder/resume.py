from collections import defaultdict
from dataclasses import dataclass

from intake.manifest import VolumeManifest, matches_entry
from media.layout import VolumeLayout, make_layout
from media.page import PageImage
from media.read import PageReadError, scan_pages
from media.write import PageWriteError
from reorder.catalog import OrderRequest
from reorder.move import MoveSafetyError, apply_move, choose_next_move, clear_redundant_spare


class ReorderInterrupted(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Progress:
    layout: VolumeLayout
    pages: tuple[PageImage | None, ...]
    copies: dict[str, tuple[int, ...]]


def reconstruct_progress(root, manifest: VolumeManifest) -> Progress:
    layout = make_layout(manifest.volume_id, len(manifest.entries))
    pages = scan_pages(root, manifest.volume_id)
    if len(pages) != len(layout.all_slots):
        raise MoveSafetyError('physical layout differs from the accepted manifest')
    expected = manifest.by_id()
    found = defaultdict(list)
    for slot, image in zip(layout.all_slots, pages):
        if image is None:
            continue
        entry = expected.get(image.original_id)
        if entry is None or not matches_entry(entry, image.original_id, image.origin, image.reading, image.payload):
            raise MoveSafetyError(f'slot {slot} cannot be verified against the accepted manifest')
        found[image.original_id].append(slot)
    missing = tuple(identity for identity in expected if identity not in found)
    if missing:
        raise MoveSafetyError(f'originals missing from the media: {missing!r}')
    return Progress(layout, pages, {identity: tuple(slots) for identity, slots in found.items()})


def resume_request(root, manifest: VolumeManifest, request: OrderRequest, *, write_hook=None) -> None:
    # The caller holds the catalog writer transaction while planning and applying steps.
    try:
        while True:
            progress = reconstruct_progress(root, manifest)
            layout, pages, copies = progress.layout, progress.pages, progress.copies
            step = choose_next_move(layout, request.target, pages, copies)
            if step is not None:
                apply_move(root, layout, step, pages, copies, write_hook=write_hook)
                continue
            if pages[layout.spare_slot - 1] is None:
                return
            clear_redundant_spare(root, layout, pages, copies, write_hook=write_hook)
    except (MoveSafetyError, PageReadError, PageWriteError) as exc:
        raise ReorderInterrupted(f'{request.volume_id!r} request {request.request_id!r}: {exc}') from exc
