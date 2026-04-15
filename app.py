import os
import time
import requests
from flask import Flask, request, jsonify
from openai import OpenAI

app = Flask(__name__)

HALO_BASE = os.environ["HALO_BASE"].rstrip("/")
HALO_TOKEN_URL = os.environ["HALO_TOKEN_URL"]
HALO_CLIENT_ID = os.environ["HALO_CLIENT_ID"]
HALO_CLIENT_SECRET = os.environ["HALO_CLIENT_SECRET"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

client = OpenAI(api_key=OPENAI_API_KEY)

token_cache = {"access_token": None, "expires_at": 0}


def get_halo_token():
    now = time.time()
    if token_cache["access_token"] and now < token_cache["expires_at"] - 60:
        return token_cache["access_token"]

    resp = requests.post(
        HALO_TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": HALO_CLIENT_ID,
            "client_secret": HALO_CLIENT_SECRET,
            "scope": "all",
        },
        timeout=30,
    )

    print("TOKEN URL:", HALO_TOKEN_URL, flush=True)
    print("TOKEN STATUS:", resp.status_code, flush=True)
    print("TOKEN HEADERS:", dict(resp.headers), flush=True)
    print("TOKEN BODY:", resp.text[:1000], flush=True)

    try:
        data = resp.json()
    except Exception:
        raise Exception(
            f"Token endpoint did not return JSON. "
            f"Status={resp.status_code}. "
            f"Body={resp.text[:500]}"
        )

    token_cache["access_token"] = data["access_token"]
    token_cache["expires_at"] = now + int(data.get("expires_in", 3600))
    return token_cache["access_token"]


def halo_get(path, params=None):
    token = get_halo_token()
    resp = requests.get(
        f"{HALO_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def halo_post(path, payload):
    token = get_halo_token()
    resp = requests.post(
        f"{HALO_BASE}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )

    print("POST URL:", f"{HALO_BASE}{path}", flush=True)
    print("POST STATUS:", resp.status_code, flush=True)
    print("POST RESPONSE:", resp.text[:2000], flush=True)

    resp.raise_for_status()
    return resp.json() if resp.text.strip() else {}


def build_ticket_text(ticket_id):
    ticket = halo_get(f"/api/Tickets/{ticket_id}")
    client_name = ticket.get("client_name") or "Unknown Client"
    actions = halo_get("/api/Actions", params={"ticket_id": ticket_id})

    print("TICKET JSON:", ticket, flush=True)

    technician = "Unassigned"

    action_items = actions.get("actions") or actions.get("actionsdetails") or []

    # Get last real agent who worked the ticket
    for action in reversed(action_items):
        who = action.get("who")
        who_type = action.get("who_type")

        if who and who_type == 1:
            technician = who
            break

    # Fallback
    if technician == "Unassigned":
        technician = ticket.get("takenby") or "Unassigned"

    parts = []
    parts.append(f"Ticket ID: {ticket.get('id')}")
    parts.append(f"Summary: {ticket.get('summary', '')}")
    parts.append(f"Details: {ticket.get('details', '')}")

    for action in action_items:
        note = action.get("note") or action.get("private_note") or ""
        if note and note.strip():
            parts.append(note)

    return "\n\n".join(parts), technician, client_name


def summarize_ticket(ticket_text):
    prompt = f"""
You are a senior MSP help desk analyst.

Summarize this resolved ticket in this exact format:

Issue Summary: <1-2 sentences>
Root Cause: <1 sentence or Unknown>
Resolution Steps: <short explanation of what was done>

Rules:
- Be concise
- Do not make things up
- If root cause is unclear, say Unknown
- Keep each section on one line

Ticket:
{ticket_text}
"""

    response = client.responses.create(
        model="gpt-5-mini",
        input=prompt
    )

    return response.output_text.strip()


def write_summary(ticket_id, summary):
    payload = [
        {
            "ticket_id": ticket_id,
            "note": f"AI Resolution Summary\n\n{summary}",
            "hiddenfromuser": True,
            "outcome": "Note Added"
        }
    ]
    return halo_post("/api/Actions", payload)

import os
import re
import requests

def send_to_teams(ticket_id, summary, technician, client_name):
    webhook_url = os.environ.get("TEAMS_WEBHOOK_URL")
    if not webhook_url:
        print("No Teams webhook configured", flush=True)
        return

    ticket_url = f"{HALO_BASE}/ticket?id={ticket_id}"

    issue_summary = "Not provided"
    root_cause = "Unknown"
    resolution_steps = "Not provided"

    issue_match = re.search(
        r"Issue Summary:\s*(.*?)(?=\s*Root Cause:|\Z)",
        summary,
        re.DOTALL,
    )
    root_match = re.search(
        r"Root Cause:\s*(.*?)(?=\s*Resolution Steps:|\Z)",
        summary,
        re.DOTALL,
    )
    resolution_match = re.search(
        r"Resolution Steps:\s*(.*)",
        summary,
        re.DOTALL,
    )

    if issue_match:
        issue_summary = issue_match.group(1).strip()

    if root_match:
        root_cause = root_match.group(1).strip()

    if resolution_match:
        resolution_steps = resolution_match.group(1).strip()

    card = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": f"Ticket {ticket_id} Resolved",
                            "weight": "Bolder",
                            "size": "Large",
                            "wrap": True
                        },
                        {
                            "type": "FactSet",
                            "facts": [
                                {"title": "Client", "value": client_name},
                                {"title": "Technician", "value": technician},
                                {"title": "Ticket", "value": str(ticket_id)}
                            ],
                            "spacing": "Small"
                        },
                        {
                            "type": "TextBlock",
                            "text": "Issue Summary",
                            "weight": "Bolder",
                            "spacing": "Medium",
                            "wrap": True
                        },
                        {
                            "type": "TextBlock",
                            "text": issue_summary,
                            "wrap": True
                        },
                        {
                            "type": "TextBlock",
                            "text": "Root Cause",
                            "weight": "Bolder",
                            "spacing": "Medium",
                            "wrap": True
                        },
                        {
                            "type": "TextBlock",
                            "text": root_cause,
                            "wrap": True
                        },
                        {
                            "type": "TextBlock",
                            "text": "Resolution Steps",
                            "weight": "Bolder",
                            "spacing": "Medium",
                            "wrap": True
                        },
                        {
                            "type": "TextBlock",
                            "text": resolution_steps,
                            "wrap": True
                        }
                    ],
                    "actions": [
                        {
                            "type": "Action.OpenUrl",
                            "title": "Open Ticket in Halo",
                            "url": ticket_url
                        }
                    ]
                }
            }
        ]
    }

    try:
        resp = requests.post(webhook_url, json=card, timeout=10)
        print("TEAMS STATUS:", resp.status_code, flush=True)
        print("TEAMS RESPONSE:", resp.text[:1000], flush=True)
    except Exception as e:
        print("Teams send failed:", str(e), flush=True)


@app.route("/")
def home():
    return "Halo AI Summary App is running"


@app.route("/halo-resolved", methods=["POST"])
def halo_resolved():
    body = request.json or {}
    ticket_id = (
        body.get("ticket_id")
        or body.get("object_id")
        or (body.get("ticket") or {}).get("id")
        or body.get("id")
    )

    if not ticket_id:
        return jsonify({"error": "Missing ticket_id"}), 400

    ticket_text, technician, client_name = build_ticket_text(int(ticket_id))
    summary = summarize_ticket(ticket_text)
    write_summary(int(ticket_id), summary)
    send_to_teams(ticket_id, summary, technician, client_name)

    return jsonify({"success": True, "ticket_id": ticket_id})
