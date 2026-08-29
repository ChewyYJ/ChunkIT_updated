import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.gateway.message_parser import parse_messages, summarize_split

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

blocks = parse_messages(messages)

for block in blocks:
    print(f"{block.block_id:10} role={block.role:10} pinned={block.pinned}")

summary = summarize_split(blocks)
print(summary)
