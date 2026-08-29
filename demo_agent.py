"""A real coding agent talking through ChunkIT — using the UNMODIFIED OpenAI SDK.

This is the drop-in-compatibility proof for the live demo: the only thing
that changed versus talking to the provider directly is base_url. The agent
carries a long session history; ChunkIT compresses it in flight while the
gateway terminal narrates what was kept, compacted, and omitted.

Run it in a second terminal next to the gateway:

    python demo_agent.py                      # stream an answer through the gateway
    python demo_agent.py --with-tools         # prove client tool calls pass through
    python demo_agent.py --session team-demo  # shared-memory session across agents
    python demo_agent.py --target-tokens 250  # harsher budget -> more omission
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from openai import NOT_GIVEN, OpenAI

from app.gateway.termcolor import BOLD, BRIGHT_GREEN, DIM, GREEN, RESET, WHITE, YELLOW

TRACE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evals", "sample_trace.json")

READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file from the repository",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenAI-SDK agent pointed at the ChunkIT gateway")
    parser.add_argument("--gateway", default="http://localhost:8000")
    parser.add_argument("--question", default=None,
                        help="override the final user message of the sample session")
    parser.add_argument("--target-tokens", type=int, default=400)
    parser.add_argument("--session", default=None,
                        help="shared session id (agents sharing it share context memory)")
    parser.add_argument("--with-tools", action="store_true",
                        help="register an agent tool and show the tool call passing through")
    args = parser.parse_args()

    with open(TRACE_PATH, encoding="utf-8") as f:
        messages = json.load(f)
    if args.question:
        messages.append({"role": "user", "content": args.question})
    if args.with_tools:
        messages.append({"role": "user", "content": "Use your read_file tool to open payments/retry.py before answering."})

    # The entire integration: base_url points at ChunkIT instead of the provider.
    client = OpenAI(base_url=f"{args.gateway}/v1", api_key="chunkit-demo")

    extra_body: dict = {"target_tokens": args.target_tokens}
    if args.session:
        extra_body["session_id"] = args.session

    print(f"{GREEN}{BOLD}[agent]{RESET} {WHITE}{len(messages)} messages of session history{RESET} "
          f"{DIM}->{RESET} {GREEN}ChunkIT{RESET} {DIM}(target={args.target_tokens}"
          + (f", session={args.session}" if args.session else "") + f"){RESET} {DIM}->{RESET} model")
    print(f"{DIM}watch the gateway terminal for the compression narration...{RESET}\n")

    started = time.perf_counter()
    stream = client.chat.completions.create(
        model="chunkit",  # pinned by the gateway's env; accepted for compatibility
        messages=messages,
        stream=True,
        tools=[READ_FILE_TOOL] if args.with_tools else NOT_GIVEN,
        extra_body=extra_body,
    )

    first_token_ms = None
    tool_calls: dict[int, dict] = {}
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta is None:
            continue
        if delta.content:
            if first_token_ms is None:
                first_token_ms = (time.perf_counter() - started) * 1000
            print(f"{WHITE}{delta.content}{RESET}", end="", flush=True)
        for tc in delta.tool_calls or []:
            if first_token_ms is None:
                first_token_ms = (time.perf_counter() - started) * 1000
            slot = tool_calls.setdefault(tc.index or 0, {"name": "", "arguments": ""})
            if tc.function is not None:
                if tc.function.name:
                    slot["name"] = tc.function.name
                if tc.function.arguments:
                    slot["arguments"] += tc.function.arguments

    total_ms = (time.perf_counter() - started) * 1000
    print()
    for slot in tool_calls.values():
        print(f"\n{YELLOW}{BOLD}[agent]{RESET} {YELLOW}received tool call through the gateway:{RESET} "
              f"{BRIGHT_GREEN}{slot['name']}({slot['arguments']}){RESET}")
        print(f"{DIM}        a real agent would execute this and continue the loop -- "
              f"the gateway passed it through untouched{RESET}")
    print(f"\n{GREEN}{BOLD}[agent]{RESET} {DIM}first token {first_token_ms:.0f} ms - total {total_ms:.0f} ms{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
