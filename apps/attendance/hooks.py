"""Things that happen after a register save commits."""


def on_register_saved(record_ids: list[int]) -> None:
    """Called on commit with the ids of records that became ``absent`` in a save.

    A no-op for now; the absence emails hook in here.
    """
