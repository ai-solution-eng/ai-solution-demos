"""Debug: dump every WS frame during an agent turn."""
import asyncio
import json
import sys
import time
import uuid

import websockets


async def main(url: str, token: str) -> None:
    async with websockets.connect(url, max_size=2 ** 22, open_timeout=20) as ws:
        await ws.send(json.dumps({
            "type": "req", "id": "c1", "method": "connect",
            "params": {
                "minProtocol": 3, "maxProtocol": 3,
                "client": {"id": "gateway-client", "version": "dbg", "platform": "linux",
                           "mode": "backend"},
                "auth": {"token": token},
                "role": "operator", "scopes": ["operator.admin"],
            },
        }))
        deadline = time.time() + 30
        while time.time() < deadline:
            m = json.loads(await asyncio.wait_for(ws.recv(), 30))
            print("FRAME:", json.dumps(m)[:400], flush=True)
            if m.get("type") == "res" and m.get("id") == "c1":
                break
        tid = "a1"
        await ws.send(json.dumps({
            "type": "req", "id": tid, "method": "agent",
            "params": {"message": "Reply with exactly: WS-DBG-OK",
                       "sessionId": "slvd-dbg", "timeout": 60,
                       "idempotencyKey": uuid.uuid4().hex},
        }))
        deadline = time.time() + 120
        while time.time() < deadline:
            m = json.loads(await asyncio.wait_for(ws.recv(), 120))
            print("FRAME:", json.dumps(m)[:600], flush=True)
            if m.get("type") == "res" and m.get("id") == tid:
                print("=== got final res for", tid, "===")
                break
    print("DONE")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
