from __future__ import annotations


def chunk_text_content(content: str, target_chars: int = 600) -> list[tuple[int, int, str]]:
    if not content.strip():
        return []

    chunks: list[tuple[int, int, str]] = []
    seen_normalized: set[str] = set()

    start = 0
    while start < len(content):
        end = min(len(content), start + target_chars)
        if end < len(content):
            split_at = max(
                content.rfind("\n\n", start, end),
                content.rfind(". ", start, end),
                content.rfind("\n", start, end),
            )
            if split_at > start:
                end = split_at + (2 if content[split_at:split_at + 2] in ("\n\n", ". ") else 1)

        raw_chunk = content[start:end].strip()
        normalized = " ".join(raw_chunk.split())
        if raw_chunk and normalized not in seen_normalized:
            seen_normalized.add(normalized)
            chunks.append((start, end, raw_chunk))

        if end <= start:
            end = min(len(content), start + target_chars)
        start = end

    return chunks
