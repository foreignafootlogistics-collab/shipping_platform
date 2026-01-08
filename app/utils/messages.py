def make_thread_key(a_id: int, b_id: int) -> str:
    lo, hi = sorted([int(a_id), int(b_id)])
    return f"u{lo}_u{hi}"
