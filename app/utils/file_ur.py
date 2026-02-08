def is_url(s: str | None) -> bool:
    if not s:
        return False
    s = s.strip().lower()
    return s.startswith("http://") or s.startswith("https://")
