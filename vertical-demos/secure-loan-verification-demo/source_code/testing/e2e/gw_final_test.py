"""Verify expectFinal behavior: agent res 'accepted' then a second res with result."""
import asyncio
import json
import os
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
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), 30))
            if m.get("type") == "res" and m.get("id") == "c1":
                print("hello-ok:", m.get("ok"), flush=True)
                break
        tid = "a1"
        await ws.send(json.dumps({
            "type": "req", "id": tid, "method": "agent",
            "params": {"message": "Reply with exactly: WS-DBG2-OK",
                       "sessionId": "slvd-dbg2", "timeout": 90,
                       "idempotencyKey": uuid.uuid4().hex},
        }))
        deadline = time.time() + 150
        n_events = 0
        while time.time() < deadline:
            m = json.loads(await asyncio.wait_for(ws.recv(), 150))
            if m.get("type") == "event":
                n_events += 1
                pl = m.get("payload", {})
                print(f"EVENT {n_events}: {m.get('event')} "
                      f"{json.dumps(pl)[:250]}", flush=True)
                continue
            if m.get("type") == "res" and m.get("id") == tid:
                st = (m.get("payload") or {}).get("status")
                print("RES status=", st, "ok=", m.get("ok"), flush=True)
                if st == "accepted":
                    continue  # expectFinal: keep waiting
                print("FINAL PAYLOAD:", json.dumps(m.get("payload", {}))[:1500], flush=True)
                break
    print("DONE")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
