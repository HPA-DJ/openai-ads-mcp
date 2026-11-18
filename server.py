#!/usr/bin/env python3
"""Local MCP server for the OpenAI Ads (ChatGPT Ads) Advertiser API.

Standard library only, stdio transport. Reads are always available.
Writes are off unless OPENAI_ADS_ALLOW_WRITES=1, and are two-step:
ads_propose_change saves a preview, ads_apply_change sends it.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time
from pathlib import Path

from client import AdsClient, AdsError

HERE = Path(__file__).resolve().parent
CHANGES = HERE / "changes"
AUDIT = HERE / "audit.jsonl"
MAX_CHARS = 40_000
CHANGE_TTL_SECONDS = 30 * 60
ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,80}$")

client = AdsClient()
WRITES_ON = os.environ.get("OPENAI_ADS_ALLOW_WRITES", "") == "1"

RESOURCES = {
    "campaigns": "/campaigns",
    "ad_groups": "/ad_groups",
    "ads": "/ads",
    "custom_audiences": "/custom_audiences",
}
INSIGHT_PATHS = {
    "ad_account": "/ad_account/insights",
    "campaign": "/campaigns/{id}/insights",
    "ad_group": "/ad_groups/{id}/insights",
    "ad": "/ads/{id}/insights",
}
# action -> (POST path, GET path used to capture the "before" state, needs id, sends idempotency key)
ACTIONS = {
    "campaign_create": ("/campaigns", None, False, True),
    "campaign_update": ("/campaigns/{id}", "/campaigns/{id}", True, False),
    "campaign_pause": ("/campaigns/{id}/pause", "/campaigns/{id}", True, False),
    "campaign_activate": ("/campaigns/{id}/activate", "/campaigns/{id}", True, False),
    "ad_group_create": ("/ad_groups", None, False, True),
    "ad_group_update": ("/ad_groups/{id}", "/ad_groups/{id}", True, False),
    "ad_group_pause": ("/ad_groups/{id}/pause", "/ad_groups/{id}", True, False),
    "ad_group_activate": ("/ad_groups/{id}/activate", "/ad_groups/{id}", True, False),
    "ad_create": ("/ads", None, False, True),
    "ad_update": ("/ads/{id}", "/ads/{id}", True, False),
    "ad_pause": ("/ads/{id}/pause", "/ads/{id}", True, False),
    "ad_activate": ("/ads/{id}/activate", "/ads/{id}", True, False),
    "account_daily_spend_limit_set": ("/ad_account/daily_spend_limit", "/ad_account", False, False),
}


def _check_id(value: str) -> str:
    if not isinstance(value, str) or not ID_RE.match(value):
        raise ValueError(f"Invalid id: {value!r}")
    return value


def _audit(entry: dict) -> None:
    entry["at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with AUDIT.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


# ---------- read tools ----------

def ads_account(args: dict):
    return client.get("/ad_account")


def ads_list(args: dict):
    resource = args["resource"]
    if resource not in RESOURCES:
        raise ValueError(f"resource must be one of {sorted(RESOURCES)}")
    params = dict(args.get("params") or {})
    params.setdefault("limit", args.get("limit", 50))
    if args.get("after"):
        params["after"] = args["after"]
    return client.get(RESOURCES[resource], params)


def ads_get(args: dict):
    resource = args["resource"]
    if resource not in RESOURCES:
        raise ValueError(f"resource must be one of {sorted(RESOURCES)}")
    return client.get(f"{RESOURCES[resource]}/{_check_id(args['id'])}", args.get("params"))


def ads_insights(args: dict):
    level = args.get("level", "ad_account")
    if level not in INSIGHT_PATHS:
        raise ValueError(f"level must be one of {sorted(INSIGHT_PATHS)}")
    path = INSIGHT_PATHS[level]
    if "{id}" in path:
        path = path.replace("{id}", _check_id(args.get("id", "")))
    return client.get(path, args.get("params"))


def ads_conversion_insights(args: dict):
    # POST, but a report query: it changes nothing in the account.
    return client.post("/conversions/insights", args["body"])


def ads_audit_logs(args: dict):
    return client.get("/audit_logs", args.get("params"))


def ads_spend_limits(args: dict):
    return {"ad_account": client.get("/ad_account"),
            "spend_limit_windows": client.get("/ad_account/spend_limit_windows")}


def ads_audience_operations(args: dict):
    return client.get(f"/custom_audiences/{_check_id(args['id'])}/operations", args.get("params"))


# ---------- write tools ----------

def ads_propose_change(args: dict):
    action = args["action"]
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {sorted(ACTIONS)}")
    post_path, before_path, needs_id, idem = ACTIONS[action]
    target = args.get("id")
    if needs_id:
        target = _check_id(target or "")
        post_path = post_path.replace("{id}", target)
    if before_path:
        before_path = before_path.replace("{id}", target or "")
    body = args.get("body") or {}
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    if action.endswith(("_pause", "_activate")) and body:
        raise ValueError("pause/activate take no body")
    before = client.get(before_path) if before_path else None
    change_id = "chg_" + secrets.token_hex(6)
    CHANGES.mkdir(exist_ok=True)
    record = {"change_id": change_id, "action": action, "path": post_path, "body": body,
              "reason": args.get("reason", ""), "before": before,
              "idempotency_key": change_id if idem else None,
              "created": time.time(), "status": "proposed"}
    (CHANGES / f"{change_id}.json").write_text(json.dumps(record, indent=2))
    _audit({"event": "proposed", "change_id": change_id, "action": action, "path": post_path})
    return {"change_id": change_id, "status": "proposed, NOT sent",
            "will_send": {"method": "POST", "path": post_path, "body": body},
            "current_state": before,
            "next": "Show this to the user. Call ads_apply_change only after they approve this exact change.",
            "expires_in_minutes": CHANGE_TTL_SECONDS // 60}


def ads_apply_change(args: dict):
    change_id = _check_id(args["change_id"])
    path = CHANGES / f"{change_id}.json"
    if not path.exists():
        raise ValueError("Unknown change_id")
    record = json.loads(path.read_text())
    if record["status"] != "proposed":
        raise ValueError(f"Change is already {record['status']}")
    if time.time() - record["created"] > CHANGE_TTL_SECONDS:
        record["status"] = "expired"
        path.write_text(json.dumps(record, indent=2))
        raise ValueError("Change expired; propose it again")
    try:
        result = client.post(record["path"], record["body"], record["idempotency_key"])
    except AdsError as err:
        record.update(status="failed", error=str(err))
        path.write_text(json.dumps(record, indent=2))
        _audit({"event": "failed", "change_id": change_id, "error": str(err)[:500]})
        raise
    record.update(status="applied", applied=time.time(), result=result)
    path.write_text(json.dumps(record, indent=2))
    _audit({"event": "applied", "change_id": change_id, "action": record["action"], "path": record["path"]})
    return {"change_id": change_id, "status": "applied", "result": result,
            "rollback_hint": "Previous state is saved in the change file under 'before'."}


OBJ = {"type": "object"}
STR = {"type": "string"}


def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


READ_TOOLS = [
    ("ads_account", ads_account,
     "Ad account details: id, name, status, timezone, currency, review status.", _schema({})),
    ("ads_list", ads_list,
     "List campaigns, ad_groups, ads or custom_audiences. Use params for filters such as campaign_id or ad_group_id. "
     "Page with the last id of the previous page as 'after'.",
     _schema({"resource": {"type": "string", "enum": sorted(RESOURCES)}, "limit": {"type": "integer"},
              "after": STR, "params": OBJ}, ["resource"])),
    ("ads_get", ads_get, "Get one campaign, ad group, ad or custom audience by id.",
     _schema({"resource": {"type": "string", "enum": sorted(RESOURCES)}, "id": STR, "params": OBJ},
             ["resource", "id"])),
    ("ads_insights", ads_insights,
     "Delivery insights (impressions, clicks, spend, ctr, cpc, cpm). level sets the endpoint scope; id is needed "
     "unless level is ad_account. params are passed as query parameters: time_granularity (hourly|daily|monthly|none), "
     "aggregation_level, fields (list), segments (list), limit, after, and time_ranges / filters / sort as lists of "
     "objects (they are JSON-encoded for you).",
     _schema({"level": {"type": "string", "enum": sorted(INSIGHT_PATHS)}, "id": STR, "params": OBJ})),
    ("ads_conversion_insights", ads_conversion_insights,
     "Goal conversion totals and attributed events. body needs aggregation_level and time_ranges (a list holding one "
     "JSON-encoded time-range string). Read-only report.",
     _schema({"body": OBJ}, ["body"])),
    ("ads_audit_logs", ads_audit_logs, "Recorded changes to resources in the ad account.", _schema({"params": OBJ})),
    ("ads_spend_limits", ads_spend_limits, "Account-wide spending limits and spend limit windows.", _schema({})),
    ("ads_audience_operations", ads_audience_operations,
     "Processing operations for one custom audience (upload and match progress).",
     _schema({"id": STR, "params": OBJ}, ["id"])),
]
WRITE_TOOLS = [
    ("ads_propose_change", ads_propose_change,
     "Step 1 of a write. Saves a change and returns a preview with the current state. Sends nothing to the ad "
     "account. Money values are in micros (1,000,000 micros = 1 unit of account currency).",
     _schema({"action": {"type": "string", "enum": sorted(ACTIONS)}, "id": STR, "body": OBJ, "reason": STR},
             ["action"])),
    ("ads_apply_change", ads_apply_change,
     "Step 2 of a write. Sends a proposed change. Only call after the user has approved that exact change.",
     _schema({"change_id": STR}, ["change_id"])),
]
TOOLS = READ_TOOLS + (WRITE_TOOLS if WRITES_ON else [])
HANDLERS = {name: fn for name, fn, _, _ in TOOLS}


def _text(payload) -> str:
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=1)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + f"\n... truncated at {MAX_CHARS} characters; narrow the request or page."
    return text


def handle(msg: dict):
    method, msg_id = msg.get("method"), msg.get("id")
    if method == "initialize":
        return {"protocolVersion": msg.get("params", {}).get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "openai-ads-local", "version": "0.1.0"},
                "instructions": "OpenAI Ads (ChatGPT Ads) account access. Writes are "
                                + ("ON and two-step: propose, show the user, then apply."
                                   if WRITES_ON else "OFF (read-only).")}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{"name": n, "description": d, "inputSchema": s} for n, _, d, s in TOOLS]}
    if method == "tools/call":
        params = msg.get("params", {})
        name = params.get("name")
        if name not in HANDLERS:
            return {"content": [{"type": "text", "text": f"Unknown tool {name}"}], "isError": True}
        try:
            return {"content": [{"type": "text", "text": _text(HANDLERS[name](params.get("arguments") or {}))}]}
        except (AdsError, ValueError, KeyError) as err:
            return {"content": [{"type": "text", "text": f"{type(err).__name__}: {err}"}], "isError": True}
    if msg_id is None:
        return None  # notification
    raise LookupError(method)


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            result = handle(msg)
            if msg.get("id") is None:
                continue
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        except LookupError as err:
            reply = {"jsonrpc": "2.0", "id": msg.get("id"),
                     "error": {"code": -32601, "message": f"Method not found: {err}"}}
        except Exception as err:  # keep the server alive on unexpected errors
            reply = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32603, "message": str(err)}}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
