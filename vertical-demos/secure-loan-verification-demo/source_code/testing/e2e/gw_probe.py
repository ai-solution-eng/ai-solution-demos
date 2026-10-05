"""Probe the NemoClaw gateway WS handshake from within the slvd namespace.
Verifies: connect -> hello-ok -> agent turn -> payloads.
Usage: python3 gw_probe.py <url> <token>
"""
import asyncio
import json
import sys

import websockets


async def main(url: str, token: str) -> int:
    async with websockets.connect(url, max_size=2**22, open_timeout=15) as ws:
        # 1) connect
        await ws.send(json.dumps({
            "type": "req", "id": "probe-1", "method": "connect",
            "params": {
                "minProtocol": 3, "maxProtocol": 3,
                "client": {"id": "gateway-client", "version": "probe",
                           "platform": "linux", "mode": "backend"},
                "auth": {"token": token},
                "role": "operator", "scopes": ["operator.admin"],
            },
        }))
        # read until hello-ok (may be an event or a res frame)
        hello = None
        for _ in range(20):
            msg = json.loads(await asyncio.wait_for(ws.recv(), 15))
            print("FRAME:", json.dumps(msg)[:300])
            if msg.get("type") == "res" and msg.get("id") == "probe-1":
                hello = msg
                break
        if hello is None:
            print("NO hello-ok")
            return 1
        if hello.get("ok") is False or "error" in hello:
            print("CONNECT FAILED:", json.dumps(hello)[:500])
            return 2

        # 2) agent turn
        await ws.send(json.dumps({
            "type": "req", "id": "probe-2", "method": "agent",
            "params": {"message": "Reply with exactly: WS-PROBE-OK",
                       "sessionId": "slvd-probe", "timeout": 60,
                       "idempotencyKey": "probe-" + str(__import__("time").time())},
        }))
        for _ in range(40):
            msg = json.loads(await asyncio.wait_for(ws.recv(), 90))
            if msg.get("type") == "event":
                continue  # streaming events
            if msg.get("type") == "res" and msg.get("id") == "probe-2":
                print("AGENT RESULT:", json.dumps(msg)[:800])
                r = msg.get("result", {})
                payloads = r.get("payloads", [])
                print("PAYLOADS:", [p.get("text", "")[:100] for p in payloads])
                return 0
        print("NO agent res")
        return 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2])))
