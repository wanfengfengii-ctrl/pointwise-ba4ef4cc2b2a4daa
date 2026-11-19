from dataclasses import dataclass

from media.layout import VolumeLayout
from media.page import PageImage
from media.write import clear_page, write_page


class MoveSafetyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MoveStep:
    source: int
    destination: int


def choose_next_move(layout: VolumeLayout, target: tuple[str, ...], pages: tuple[PageImage | None, ...],
                     copies: dict[str, tuple[int, ...]]) -> MoveStep | None:
    first_blocked = None
    for slot, wanted in zip(layout.normal_slots, target):
        occupant = pages[slot - 1]
        if occupant is not None and occupant.original_id == wanted:
            continue
        if occupant is None or len(copies[occupant.original_id]) > 1:
            return MoveStep(copies[wanted][0], slot)
        if first_blocked is None:
            first_blocked = slot
    if first_blocked is not None and pages[layout.spare_slot - 1] is None:
        return MoveStep(first_blocked, layout.spare_slot)
    return None


def apply_move(root, layout: VolumeLayout, step: MoveStep, pages: tuple[PageImage | None, ...],
               copies: dict[str, tuple[int, ...]], *, write_hook=None) -> None:
    image = pages[step.source - 1]
    occupant = pages[step.destination - 1]
    if image is None or step.source == step.destination:
        raise MoveSafetyError('a move requires a complete page at a distinct source slot')
    if occupant is not None and len(copies[occupant.original_id]) < 2:
        raise MoveSafetyError(f'cannot overwrite the last copy of {occupant.original_id!r}')
    write_page(root, layout.volume_id, step.destination, image, write_hook=write_hook)


def clear_redundant_spare(root, layout: VolumeLayout, pages: tuple[PageImage | None, ...],
                          copies: dict[str, tuple[int, ...]], *, write_hook=None) -> None:
    spare = pages[layout.spare_slot - 1]
    if spare is None:
        return
    if not any(slot in layout.normal_slots for slot in copies[spare.original_id]):
        raise MoveSafetyError(f'cannot clear the last copy of {spare.original_id!r}')
    clear_page(root, layout.volume_id, layout.spare_slot, write_hook=write_hook)
