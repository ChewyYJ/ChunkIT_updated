def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


# Cap for sanitised per-unit score lists in debug payloads (both API fronts).
MAX_DEBUG_SCORED_UNITS = 50
