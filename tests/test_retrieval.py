import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.gateway.budgeter import build_selection_plan
from app.gateway.message_parser import parse_messages
from app.kimi.tools import execute_retrieve, reset_active_session_id, set_active_session_id
from app.session.store import init_db, store_block
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

init_db()

blocks = parse_messages(messages)
units_by_block = {b.block_id: mock_units_from_block(b) for b in blocks}

session_id = "test-session"
for b in blocks:
    tokens = sum(u.tokens for u in units_by_block[b.block_id])
    store_block(session_id, b, tokens, units=units_by_block[b.block_id])

all_units = [u for units in units_by_block.values() for u in units]
scored = mock_score_units(all_units)
plan = build_selection_plan(blocks, units_by_block, scored, TARGET_TOKENS)
print("omit:", plan.omit)

# Ambient-session path (Person 2 convention).
token = set_active_session_id(session_id)
result = execute_retrieve(["msg_3"])
reset_active_session_id(token)
print("--- retrieve msg_3 (ambient session) ---")
print(result)
assert "It processes CSV files." in result
assert "not found" not in result.lower()

# Explicit-session path (Person 3 convention).
explicit = execute_retrieve(["msg_3"], session_id)
print("--- retrieve msg_3 (explicit session) ---")
assert "It processes CSV files." in explicit

missing = execute_retrieve(["msg_99"], session_id)
print("--- retrieve msg_99 ---")
print(missing)
assert "not found" in missing.lower()

wrong_session = execute_retrieve(["msg_3"], "some-other-session")
print("--- retrieve msg_3 from wrong session ---")
print(wrong_session)
assert "not found" in wrong_session.lower()

print("All assertions passed.")
