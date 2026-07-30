"""Minimal HA websocket client: run commands, capture events.

Usage:
  python ws.py '{"cmds":[{"type":"config_entries/get","domain":"fints_atruvia"}]}'
  python ws.py '{"cmds":[],"event":"fints_atruvia_new_transaction","secs":40}'

Reads the access token from $SB_TOKEN_FILE (default $SB_ROOT/token) and the
port from $SB_PORT (default 8199).
"""
import asyncio
import json
import os
import sys

import aiohttp

PORT = os.environ.get("SB_PORT", "8199")
TOKEN_FILE = os.environ.get(
    "SB_TOKEN_FILE", os.path.join(os.environ.get("SB_ROOT", "."), "token")
)
URL = f"http://127.0.0.1:{PORT}/api/websocket"


async def main(cmds, listen_event=None, listen_secs=0):
    token = open(TOKEN_FILE).read().strip()
    async with aiohttp.ClientSession() as s, s.ws_connect(URL) as ws:
        await ws.receive_json()  # auth_required
        await ws.send_json({"type": "auth", "access_token": token})
        msg = await ws.receive_json()
        if msg["type"] != "auth_ok":
            print("AUTH FAILED", msg)
            return
        i = 0
        for cmd in cmds:
            i += 1
            await ws.send_json({"id": i, **cmd})
            while True:
                r = await ws.receive_json()
                if r.get("id") == i and r["type"] == "result":
                    print(json.dumps(r.get("result", r), indent=1, default=str))
                    break
        if listen_event:
            i += 1
            await ws.send_json({"id": i, "type": "subscribe_events",
                                "event_type": listen_event})
            await ws.receive_json()
            print(f"--- listening {listen_secs}s for {listen_event} ---", flush=True)
            end = asyncio.get_event_loop().time() + listen_secs
            while asyncio.get_event_loop().time() < end:
                try:
                    r = await asyncio.wait_for(ws.receive_json(), timeout=2)
                except asyncio.TimeoutError:
                    continue
                if r.get("type") == "event":
                    print("EVENT:", json.dumps(r["event"]["data"], indent=1,
                                               default=str), flush=True)


if __name__ == "__main__":
    payload = json.loads(sys.argv[1])
    asyncio.run(main(payload.get("cmds", []), payload.get("event"),
                     payload.get("secs", 0)))
