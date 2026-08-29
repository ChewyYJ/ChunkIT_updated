import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.gateway.assembler import assemble_messages, count_omitted_blocks
from app.gateway.budgeter import build_selection_plan
from app.gateway.message_parser import parse_messages
from shared.schemas import mock_score_units, mock_units_from_block

messages = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hi, can you help me with a Python script?"},
    {"role": "assistant", "content": "Sure, what does the script need to do?"},
    {"role": "user", "content": "It processes CSV files."},
    {"role": "assistant", "content": "Got it, I'll avoid external libraries."},
    {
        "role": "user",
        "content": "Actually, you must not read the entire file into memory at once — stream it.",
    },
    {"role": "assistant", "content": "Understood, I'll use chunked reading."},
    {"role": "user", "content": "Great, now write the function."},
]

TARGET_TOKENS = 30

blocks = parse_messages(messages)
units_by_block = {b.block_id: mock_units_from_block(b) for b in blocks}
all_units = [u for units in units_by_block.values() for u in units]
scored = mock_score_units(all_units)

plan = build_selection_plan(blocks, units_by_block, scored, TARGET_TOKENS)

print("keep:", plan.keep)
print("omit:", plan.omit)
print(f"estimated_tokens={plan.estimated_tokens} target_tokens={plan.target_tokens}")

assembled = assemble_messages(blocks, units_by_block, plan)
for block, msg in zip(sorted(blocks, key=lambda b: b.message_index), assembled):
    status = "OMITTED" if msg["content"].startswith("[CONTEXT OMITTED") else "kept"
    print(f"{block.block_id:10} pinned={block.pinned!s:5} {status}")

print("omitted_blocks:", count_omitted_blocks(blocks, units_by_block, plan))

pinned_ids = {b.block_id for b in blocks if b.pinned}
assert pinned_ids == {"msg_0", "msg_5", "msg_6", "msg_7"}

pinned_unit_ids = {
    u.unit_id for b in blocks if b.pinned for u in units_by_block[b.block_id]
}
assert pinned_unit_ids.issubset(set(plan.keep)), "pinned blocks must survive the tight budget"

compressible_unit_ids = {
    u.unit_id
    for b in blocks
    if not b.pinned
    for u in units_by_block[b.block_id]
}
assert compressible_unit_ids & set(plan.omit), "expected at least one compressible block to be omitted"

print("All assertions passed.")
