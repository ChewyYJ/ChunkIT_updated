from shared.schemas import ContextBlock, ContextUnit, SelectionPlan


def _is_block_omitted(units: list[ContextUnit], omit_set: set[str]) -> bool:
    return bool(units) and all(u.unit_id in omit_set for u in units)


def _original_content(block: ContextBlock):
    if block.original_message and "content" in block.original_message:
        return block.original_message["content"]
    return block.content


def _has_rewritable_content(block: ContextBlock) -> bool:
    """Only None-or-string content may ever be replaced by markers or
    compact text; multimodal (list-form) content must survive verbatim."""
    original = _original_content(block)
    return original is None or isinstance(original, str)


def _is_compactable(block: ContextBlock, units: list[ContextUnit]) -> bool:
    """Non-pinned blocks with units and plain-string content forward their
    compressed unit text. Pinned blocks are always verbatim; multimodal
    (non-string) content is never rewritten; blank blocks have no units."""
    if block.pinned or not units:
        return False
    return _has_rewritable_content(block)


def assemble_messages(
    blocks: list[ContextBlock],
    units_by_block: dict[str, list[ContextUnit]],
    plan: SelectionPlan,
    retrieval_available: bool = True,
) -> list[dict]:
    """Rebuild the outgoing message list in original order.

    Message STRUCTURE is always preserved (role, tool_calls, tool_call_id,
    name) so agent tool histories stay valid upstream. Content follows the
    selection tiers:
    - pinned blocks: original content, verbatim
    - compacted blocks: kept units' compressed text (source order), with a
      one-line note when some units were omitted
    - fully omitted blocks: a retrieval marker

    Notes and markers mention retrieve_context only when that tool will
    actually be advertised.
    """
    omit_set = set(plan.omit)
    messages = []
    for block in sorted(blocks, key=lambda b: b.message_index):
        units = units_by_block.get(block.block_id, [])
        # Preserve the verbatim incoming message when we have it.
        message = dict(block.original_message) if block.original_message else {
            "role": block.role,
            "content": block.content,
        }
        if _is_block_omitted(units, omit_set) and _has_rewritable_content(block):
            total_tokens = sum(u.tokens for u in units)
            content_type = units[0].content_type if units else "text"
            if retrieval_available:
                message["content"] = (
                    f"[CONTEXT OMITTED — {block.block_id}, {content_type}, "
                    f"~{total_tokens} tokens. Use retrieve_context with context_id={block.block_id} if needed.]"
                )
            else:
                message["content"] = (
                    f"[CONTEXT OMITTED — {block.block_id}, {content_type}, ~{total_tokens} tokens.]"
                )
        elif _is_compactable(block, units):
            kept_units = sorted(
                (u for u in units if u.unit_id not in omit_set),
                key=lambda u: (u.source_start, u.unit_id),
            )
            parts = [u.text for u in kept_units]
            omitted_tokens = sum(u.tokens for u in units if u.unit_id in omit_set)
            if omitted_tokens:
                if retrieval_available:
                    parts.append(
                        f"[… ~{omitted_tokens} tokens omitted — use retrieve_context "
                        f"with context_id={block.block_id} if needed.]"
                    )
                else:
                    parts.append(f"[… ~{omitted_tokens} tokens omitted.]")
            message["content"] = "\n".join(parts)
        messages.append(message)
    return messages


def count_omitted_blocks(
    blocks: list[ContextBlock],
    units_by_block: dict[str, list[ContextUnit]],
    plan: SelectionPlan,
) -> int:
    omit_set = set(plan.omit)
    return sum(
        1 for block in blocks
        if _is_block_omitted(units_by_block.get(block.block_id, []), omit_set)
        and _has_rewritable_content(block)
    )


def count_compacted_blocks(
    blocks: list[ContextBlock],
    units_by_block: dict[str, list[ContextUnit]],
    plan: SelectionPlan,
) -> int:
    """Blocks forwarded as compressed unit text rather than verbatim."""
    omit_set = set(plan.omit)
    total = 0
    for block in blocks:
        units = units_by_block.get(block.block_id, [])
        if _is_block_omitted(units, omit_set):
            continue
        if _is_compactable(block, units):
            total += 1
    return total
