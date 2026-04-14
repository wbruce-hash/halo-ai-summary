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
    resp.raise_for_status()
    return resp.json()


def build_ticket_text(ticket_id):
    ticket = halo_get(f"/api/Tickets/{ticket_id}")
    actions = halo_get("/api/Actions", params={"ticket_id": ticket_id})

    parts = []
    parts.append(f"Ticket ID: {ticket.get('id')}")
    parts.append(f"Summary: {ticket.get('summary', '')}")
    parts.append(f"Details: {ticket.get('details', '')}")

    action_items = actions.get("actions") or actions.get("actionsdetails") or []
    for action in action_items:
        note = action.get("note") or action.get("private_note") or ""
        if note and note.strip():
            parts.append(note)

    return "\n\n".join(parts)


def summarize_ticket(ticket_text):
    prompt = f"""
You are a senior MSP help desk analyst.

Summarize this resolved ticket in this exact format:

Issue Summary:
Root Cause:
Resolution Steps:

Rules:
- Be concise
- Do not make things up
- If root cause is unclear, say Unknown

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
            "note": f"AI Resolution Summary\\n\\n{summary}",
            "hiddenfromuser": True
        }
    ]
    return halo_post("/api/Actions", payload)


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

    ticket_text = build_ticket_text(int(ticket_id))
    summary = summarize_ticket(ticket_text)
    write_summary(int(ticket_id), summary)

    return jsonify({"success": True, "ticket_id": ticket_id})
