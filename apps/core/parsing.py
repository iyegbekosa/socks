def parse_id(raw: str) -> int | None:
    """Parse a database id from user input: ASCII digits only, 1 to 18 characters."""
    if not isinstance(raw, str) or not (1 <= len(raw) <= 18):
        return None
    if not all(c in "0123456789" for c in raw):
        return None
    return int(raw)
