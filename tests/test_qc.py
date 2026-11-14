#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["signalwire-sdk>=3.7", "requests>=2.31", "httpx"]
# ///
"""Checks for the QC monitor: its config, findings arriving from the sidecar,
tool uses reported to it, and the report. Run with: uv run tests/test_qc.py"""

import base64
import os
import sys
import tempfile
from pathlib import Path

os.environ.update(SWML_BASIC_AUTH_USER="u", SWML_BASIC_AUTH_PASSWORD="p", CALL_LOG_DIR=tempfile.mkdtemp(),
                  QC_MONITOR="1", SWML_PROXY_URL_BASE="https://pumpkin.example.com")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pumpkin_agent as pa  # noqa: E402
import qc_monitor as qc  # noqa: E402
import qc_report  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from signalwire import AgentServer  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    fails += not cond
    print(("ok   " if cond else "FAIL ") + name)


config = qc.sidecar_config("https://pumpkin.example.com", "u", "p", ["pumpkin"])
check("the sidecar posts back with the basic-auth pair", config["url"] == "https://u:p@pumpkin.example.com/qc/events")
check("it's report-only", config["params"]["act_on_channel"] is False)
check("both legs, the visitor as the customer",
      config["direction"] == ["remote-caller", "local-caller"] and config["customer_role"] == "remote-caller")
check("it never gets the pumpkin's prompt", "Who You Are" not in str(config["prompt"]))

check("tool args from the ai-style parsed list",
      qc.tool_call({"function": "f", "argument": {"parsed": [{"a": 1}]}}) == ("f", {"a": 1}))
check("tool args from a JSON string", qc.tool_call({"function": "f", "argument": '{"a": 1}'}) == ("f", {"a": 1}))

pokes = []
qc.poke = lambda call_id, text: pokes.append((call_id, text)) or None
server = AgentServer(port=0)
server.register(pa.agent)
pa.add_eyes_routes(server, None)
web = TestClient(server.app)
auth = {"authorization": "Basic " + base64.b64encode(b"u:p").decode()}

r = web.post("/qc/swaig", json={"function": "review_answer", "argument": {"parsed": [{"actual": "x", "verdict": "deviation"}]}})
check("findings need the basic-auth pair", r.status_code == 401)
r = web.post("/qc/swaig", headers=auth, json={
    "function": "predict_answer", "call_info": {"call_id": "c1"},
    "argument": {"parsed": [{"visitor_said": "Is that real fire?", "expected": "A grand boast about burning."}]}})
web.post("/qc/swaig", headers=auth, json={
    "function": "review_answer", "call_info": {"call_id": "c1"},
    "argument": {"parsed": [{"in_reply_to": "Is that real fire?", "actual": "I am an AI.", "verdict": "broke_character",
                             "notes": "Admitted being a machine."}]}})
web.post("/qc/events", headers=auth, json={"call_info": {"call_id": "c1"},
                                           "sidecar_event": {"type": "error", "error_reason": "llm_error"}})
check("a finding is acknowledged", r.json() == {"response": "Recorded."})

pa.agent._debug_event("x", {"call_info": {"call_id": "c1"},
                            "swaig_call": {"command_name": "tell_a_story", "command_arg": '{"question": "favorite costume"}'}})
pa.agent._debug_event("x", {"call_info": {"call_id": "c1"}, "swaig_call": {"command_name": "get_visual_input"}})
pa.agent.qc_pokes.queue.join()
check("a tool use is reported to the sidecar", pokes and pokes[0][0] == "c1" and "tell_a_story" in pokes[0][1])
check("looking isn't", len(pokes) == 1)
pa.agent._debug_event("x", {"call_info": {"call_id": "c1"}, "context_change": {"to_context": "gentle"}})
pa.agent.qc_pokes.queue.join()
check("a switch to gentle mode is reported too", "[MODE] Gentle mode" in pokes[-1][1])

entries = web.get("/eyes-qc").json()
kinds = [e["kind"] for e in entries]
check("the log has the prediction, review, error, tool use, and mode",
      kinds == ["predict_answer", "review_answer", "sidecar_error", "tool_used", "mode"])
check("a review is filed under the line it answered", entries[1]["visitor_said"] == "Is that real fire?")
text = qc_report.report(entries)
check("the report pairs the expectation with what was said",
      "Broke character (1)" in text and "A grand boast about burning." in text and "I am an AI." in text)

failing = qc.Poker(pa.agent.qc_log, send=lambda call_id, text: "500 nope")
failing.GAP_SECONDS = 0
failing.send("c1", "x")
failing.queue.join()
check("a failed poke is logged where it's seen", web.get("/eyes-qc").json()[-1]["kind"] == "poke_failed")

r = pa.agent.tell_a_story({"question": "What's your favorite costume tonight?"}, {"global_data": {}}).to_dict()
check("a favorite-costume question gets the costume answer, not a story", "Say it in this shape" in r["response"])
r = pa.agent.tell_a_story({"question": "Why is the sky dark?"}, {"global_data": {}}).to_dict()
check("other questions still get a story", "Tell a story" in r["response"])

sys.exit(fails)
