"""
make_call.py — Dispatch an outbound call to this agent
=======================================================
Usage:
    python make_call.py --to +15550003333

Requires .env with LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET
"""

import argparse
import asyncio
import json
import os
import random

from dotenv import load_dotenv
from livekit import api

load_dotenv(".env")

AGENT_NAME = "webhook-crm-agent"

async def main():
    parser = argparse.ArgumentParser(description=f"Place an outbound call via {AGENT_NAME}")
    parser.add_argument("--to", required=True, help="E.164 phone number, e.g. +15550003333")
    args = parser.parse_args()

    phone = args.to.strip()
    if not phone.startswith("+"):
        print("ERROR: Phone number must start with + (E.164 format)")
        return

    url        = os.getenv("LIVEKIT_URL")
    api_key    = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")

    if not (url and api_key and api_secret):
        print("ERROR: Missing LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET in .env")
        return

    lk   = api.LiveKitAPI(url=url, api_key=api_key, api_secret=api_secret)
    room = f"{AGENT_NAME}-{phone.replace('+', '')}-{random.randint(1000, 9999)}"

    print(f"\nAgent  : {AGENT_NAME}")
    print(f"Calling: {phone}")
    print(f"Room   : {room}")
    print("-" * 50)

    try:
        dispatch = await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=AGENT_NAME,
                room=room,
                metadata=json.dumps({"phone_number": phone}),
            )
        )
        print(f"Dispatched — ID: {dispatch.id}")
        print("Agent is dialing. Watch the agent terminal for logs.")
    except Exception as e:
        print(f"ERROR: {e}")
    finally:
        await lk.aclose()


if __name__ == "__main__":
    asyncio.run(main())
