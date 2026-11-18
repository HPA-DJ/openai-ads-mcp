#!/usr/bin/env python3
"""Offline test: runs server.py against a fake local API. No real account is touched."""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEEN = []
CAMPAIGN = {"id": "cmpn_1", "name": "Test", "status": "active",
            "budget": {"daily_spend_limit_micros": 100000000}}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        SEEN.append(("GET", self.path, self.headers.get("Authorization"), None, None))
        if self.path.startswith("/v1/ad_account"):
            return self._send(200, {"id": "adacct_1", "currency_code": "NZD"})
        if self.path.startswith("/v1/campaigns/cmpn_1"):
            return self._send(200, CAMPAIGN)
        if self.path.startswith("/v1/campaigns"):
            return self._send(200, {"object": "list", "data": [CAMPAIGN], "has_more": False})
        self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
        SEEN.append(("POST", self.path, self.headers.get("Authorization"), body,
                     self.headers.get("Idempotency-Key")))
        self._send(200, {**CAMPAIGN, **body})


def rpc(proc, n, method, params=None):
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": n, "method": method, "params": params or {}}) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


def start(port, writes):
    env = {**os.environ, "OPENAI_ADS_API_KEY": "test-key", "OPENAI_ADS_BASE_URL": f"http://127.0.0.1:{port}/v1"}
    env.pop("OPENAI_ADS_ALLOW_WRITES", None)
    if writes:
        env["OPENAI_ADS_ALLOW_WRITES"] = "1"
    return subprocess.Popen([sys.executable, str(HERE / "server.py")], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True, env=env, cwd=HERE)


def call(proc, n, name, args):
    res = rpc(proc, n, "tools/call", {"name": name, "arguments": args})["result"]
    return res.get("isError", False), res["content"][0]["text"]


def main():
    httpd = HTTPServer(("127.0.0.1", 0), Fake)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    checks = []

    ro = start(port, writes=False)
    init = rpc(ro, 1, "initialize", {"protocolVersion": "2025-06-18"})
    checks.append(("initialize echoes protocol", init["result"]["protocolVersion"] == "2025-06-18"))
    names = [t["name"] for t in rpc(ro, 2, "tools/list")["result"]["tools"]]
    checks.append(("read-only hides write tools", "ads_apply_change" not in names and "ads_account" in names))
    err, text = call(ro, 3, "ads_account", {})
    checks.append(("ads_account works", not err and "adacct_1" in text))
    err, text = call(ro, 4, "ads_list", {"resource": "campaigns", "limit": 5})
    checks.append(("ads_list works", not err and "cmpn_1" in text))
    checks.append(("limit sent as query", any("limit=5" in p for _, p, *_ in SEEN)))
    checks.append(("bearer key sent", all(a == "Bearer test-key" for _, _, a, *_ in SEEN)))
    err, text = call(ro, 5, "ads_get", {"resource": "campaigns", "id": "../ad_account"})
    checks.append(("bad id rejected", err))
    err, text = call(ro, 6, "ads_propose_change", {"action": "campaign_pause", "id": "cmpn_1"})
    checks.append(("write tool unavailable when off", err))
    ro.stdin.close(); ro.wait(timeout=5)

    rw = start(port, writes=True)
    rpc(rw, 1, "initialize")
    posts_before = sum(1 for m, *_ in SEEN if m == "POST")
    err, text = call(rw, 2, "ads_propose_change", {
        "action": "campaign_update", "id": "cmpn_1",
        "body": {"budget": {"daily_spend_limit_micros": 200000000}}, "reason": "peak days"})
    proposal = json.loads(text)
    checks.append(("propose returns preview + before", not err and proposal["current_state"]["id"] == "cmpn_1"))
    checks.append(("propose sends no POST", sum(1 for m, *_ in SEEN if m == "POST") == posts_before))
    err, text = call(rw, 3, "ads_apply_change", {"change_id": proposal["change_id"]})
    last = SEEN[-1]
    checks.append(("apply POSTs the saved body", not err and last[0] == "POST" and last[1] == "/v1/campaigns/cmpn_1"
                   and last[3] == {"budget": {"daily_spend_limit_micros": 200000000}}))
    err, text = call(rw, 4, "ads_apply_change", {"change_id": proposal["change_id"]})
    checks.append(("apply twice refused", err))
    err, text = call(rw, 5, "ads_propose_change", {"action": "campaign_create", "body": {"name": "x"}})
    err2, text2 = call(rw, 6, "ads_apply_change", {"change_id": json.loads(text)["change_id"]})
    checks.append(("create carries idempotency key", not err2 and SEEN[-1][4] == json.loads(text)["change_id"]))
    rw.stdin.close(); rw.wait(timeout=5)

    for change in (HERE / "changes").glob("chg_*.json"):
        change.unlink()
    failed = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print("PASS" if ok else "FAIL", name)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
