# Vobiz + LiveKit — Webhook & CRM Integration

A LiveKit voice agent that looks the caller up in a CRM before it speaks, posts every stage of the call to your webhook endpoint, and writes the outcome back to the contact record — over a Vobiz SIP trunk.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![LiveKit Agents](https://img.shields.io/badge/LiveKit%20Agents-1.5%2B-1FD5A6)](https://docs.livekit.io/agents/)
[![Docs](https://img.shields.io/badge/Docs-docs.vobiz.ai-0B7285)](https://docs.vobiz.ai)

---

## Overview

A voice agent that cannot see your customer data is a stranger on every call, and one that cannot write anything back leaves no trace that the conversation happened. The integration work — reading the contact before the greeting, emitting events as the call progresses, logging the outcome afterwards — is what turns a demo into something a business can actually operate. This repository is that layer, wired end to end and kept small enough to read in one sitting.

The flow has three phases. **Before the call**, `entrypoint()` takes the dialled number from job metadata and calls `crm_lookup_contact()`, so by the time the agent speaks it already knows the caller's name, company and open-ticket count — and its greeting is built from them. **During the call**, four LLM-callable tools do the integration work: `record_intent` captures why the caller rang and emits an event, `lookup_crm` searches the CRM mid-conversation, `update_crm_note` writes a note to the contact record, and `transfer_call` issues a SIP REFER to a department or an E.164 number. **After the call**, a `disconnected` handler builds a summary, posts a final event, and writes a call activity onto the contact.

Both integration surfaces are deliberately vendor-neutral. The CRM is reached through `CRM_BASE_URL` and a bearer token, with three URL patterns — `/contacts/search`, `/contacts/{id}/notes`, `/contacts/{id}/activities` — that you rewrite in the three helper functions to match HubSpot, Salesforce, Pipedrive or your own service. The webhook side is a single `post_webhook()` function that POSTs JSON to `WEBHOOK_URL`, optionally signed with HMAC-SHA256. Both are guarded: leave the variables unset and the agent runs as a normal voice agent, logging a warning instead of failing.

Be clear-eyed about the delivery guarantees before you build on it, because they shape what your receiver has to do. `post_webhook()` is fire-and-forget with a five-second timeout, and every failure path is caught and logged rather than retried. There is no queue, no backoff, and no delivery identifier in the payload — so delivery is at-most-once, events can be lost when your endpoint is briefly down, and a receiver has no built-in key to deduplicate on. That is a reasonable starting point for an example and a deliberate simplification; the Roadmap lists what closing it looks like, and the Webhook contract section below tells you exactly what arrives so you can build a receiver against it today.

## What you can build with it

- **Personalised inbound reception.** The agent greets a known customer by name and references their open tickets in the first sentence, without the caller identifying themselves.
- **Automatic call logging.** Every call lands on the CRM contact as an activity with duration, captured intent and transfer destination, so nobody has to remember to log it.
- **Real-time pipeline events.** Stream `call.started` / `call.intent` / `call.transferred` / `call.ended` into your own service to drive dashboards, alerting or follow-up automation.
- **Intent-routed escalation.** Capture the reason for the call, then transfer to the right department with the note already written to the record the human is about to open.
- **Lead qualification with write-back.** An outbound agent qualifies a lead and the outcome appears in the CRM before the rep picks up the follow-up.
- **A bridge to existing automation.** If you already run workflows on webhooks, this is the smallest way to make phone calls a first-class trigger alongside forms and emails.

## How it works

```
Job dispatched with {"phone_number": "+15550003333"} in metadata
         ↓
crm_lookup_contact()
  POST {CRM_BASE_URL}/contacts/search   {"filter": {"phone": "+15550003333"}}
  → results[0] → CRMContact(id, name, email, company, open_tickets)
         ↓
AgentSession[CallRecord] starts with CRMAgent
         ↓
Outbound call placed (wait_until_answered=True)
         ↓
CRMAgent.on_enter()
  → POST webhook: call.started   (phone, room, contact_id, contact_name)
  → greeting built from contact.name / contact.company / contact.open_tickets
         ↓
During the call:
  record_intent()     → POST webhook: call.intent
  lookup_crm()        → POST {CRM_BASE_URL}/contacts/search
  update_crm_note()   → POST {CRM_BASE_URL}/contacts/{id}/notes
  transfer_call()     → POST webhook: call.transferred, then SIP REFER
         ↓
Room disconnects
  → on_call_ended() builds the summary string, then in parallel:
      POST webhook: call.ended  (summary, contact_id, events)
      POST {CRM_BASE_URL}/contacts/{id}/activities
```

The session is typed `AgentSession[CallRecord]`, so every tool reaches the same record through `context.userdata`: the phone number, the room name, the start timestamp, the CRM contact, an append-only `events` list, the transfer destination, and the final summary. Nothing is stored anywhere else — the record exists for the lifetime of the job.

The teardown path deserves a note. `@ctx.room.on("disconnected")` is a synchronous callback, so it schedules the async work with `asyncio.ensure_future(on_call_ended(record))` rather than awaiting it. That means the final webhook and CRM write race the worker's own shutdown: they usually complete, but they are not guaranteed to, and nothing retries them if they do not. The handler is also unguarded, so if the event were ever delivered twice, `call.ended` would be posted twice. Both are reasons to make your receiver idempotent, which the contract below explains how to do.

## Architecture

| Component | Responsibility |
|-----------|----------------|
| `agent.py` | The whole worker — state, HTTP helpers, the agent and its tools, teardown, and registration under `agent_name="webhook-crm-agent"`. |
| `CRMContact` (dataclass) | The contact as the agent knows it: `id`, `name`, `email`, `company`, `last_intent`, `open_tickets`. Returned empty when the CRM is unconfigured or the lookup fails. |
| `CallRecord` (dataclass) | Session state — `phone_number`, `room_name`, `started_at`, `contact`, `events`, `transfer_destination`, `call_summary`. Typed into the session and reached via `context.userdata`. |
| `_crm_headers()` | Builds `Authorization: Bearer {CRM_API_KEY}` and the JSON content type for every CRM request. |
| `_sign_payload()` | HMAC-SHA256 over the exact request body. Returns an empty string when `WEBHOOK_SECRET` is unset, in which case no signature header is sent. |
| `crm_lookup_contact()` | Pre-call lookup. `POST /contacts/search` with `{"filter": {"phone": …}}`, reads `results[0]`. Five-second timeout; any failure returns an empty `CRMContact`. |
| `crm_update_contact()` | Post-call write. `POST /contacts/{id}/activities` with type, phone, duration, intent, summary and transfer destination. Silently skipped when there is no contact ID. |
| `post_webhook()` | The single webhook exit. Serialises the payload, signs it, POSTs to `WEBHOOK_URL` with a five-second timeout, logs the status, and swallows any exception. |
| `CRMAgent` | The conversational agent. Posts `call.started` from `on_enter()`, builds a personalised or generic greeting, and exposes the four tools. |
| `on_call_ended()` | Teardown. Builds `call_summary` and runs the `call.ended` post and the CRM activity write concurrently with `asyncio.gather`. |
| `entrypoint()` | Reads job metadata, performs the CRM lookup, builds the record and session, registers the disconnect hook, then dials out — or falls through to the inbound path. |
| `make_call.py` | Dispatch script. Validates E.164, builds a room name, and creates the dispatch with `{"phone_number": …}` as metadata. |
| `.env.example` | Credential, CRM, webhook and transfer-destination template. Copy to `.env`. |
| `requirements.txt` | LiveKit agents and plugins, `python-dotenv`, and `aiohttp>=3.9.0` for the HTTP calls. |

## Prerequisites

- **Python 3.10 or newer**, as required by `livekit-agents` 1.5.
- **A LiveKit Cloud project** (or self-hosted server) for `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`.
- **A Vobiz account with a SIP trunk.** Outbound needs the LiveKit outbound trunk ID (`ST_…`) that points at your Vobiz trunk. See the [Vobiz + LiveKit integration guide](https://docs.vobiz.ai/integrations/livekit).
- **An OpenAI API key** — `gpt-4o-mini` for the conversation, `tts-1` for speech.
- **A Deepgram API key** — `nova-3` in `multi` language mode for speech-to-text.
- **An HTTPS endpoint that can receive POSTs**, if you want to see webhooks. A [webhook.site](https://webhook.site) URL is enough to start; use a tunnel such as `ngrok` to reach a local server.
- **A CRM with a REST API and a token**, optional. Without one the agent runs and still emits every webhook event; only the personalisation and the CRM writes are skipped.

## Setup

1. **Clone the repository.**

   ```bash
   git clone https://github.com/vobiz-ai/Livekit-Vobiz-Webhook-Integration-Example.git
   cd Livekit-Vobiz-Webhook-Integration-Example
   ```

2. **Create and activate a virtual environment.**

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate          # Windows: .venv\Scripts\activate
   ```

3. **Install the dependencies.** `aiohttp` is included in `requirements.txt`; there is nothing extra to install by hand.

   ```bash
   pip install -r requirements.txt
   ```

4. **Create your `.env`.**

   ```bash
   cp .env.example .env
   ```

5. **Fill it in.** Set the LiveKit, OpenAI and Deepgram credentials and your Vobiz `OUTBOUND_TRUNK_ID`. Set `WEBHOOK_URL` to an endpoint you control and `WEBHOOK_SECRET` to a random string. Leave `CRM_BASE_URL` and `CRM_API_KEY` blank for a first run if you have no CRM to point at. Both `agent.py` and `make_call.py` call `load_dotenv(".env")` with a relative path, so `.env` must live in this directory and every command below must be run from here.

6. **Warm the Silero VAD cache** so the first call is not delayed by a model download:

   ```bash
   python agent.py download-files
   ```

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LIVEKIT_URL` | Yes | — | LiveKit server WebSocket URL, e.g. `wss://your-project.livekit.cloud`. Read by `make_call.py` and by the agents CLI at worker registration. |
| `LIVEKIT_API_KEY` | Yes | — | LiveKit API key. Same two consumers. |
| `LIVEKIT_API_SECRET` | Yes | — | LiveKit API secret. Same two consumers. |
| `OPENAI_API_KEY` | Yes | — | Read by the `openai` plugin for `openai.LLM(model="gpt-4o-mini")` and `openai.TTS(model="tts-1", voice="alloy")`. |
| `DEEPGRAM_API_KEY` | Yes | — | Read by the `deepgram` plugin for `deepgram.STT(model="nova-3", language="multi")`. |
| `OUTBOUND_TRUNK_ID` | For outbound | — | LiveKit outbound SIP trunk ID (`ST_…`) for your Vobiz trunk, passed as `sip_trunk_id` to `CreateSIPParticipantRequest`. |
| `VOBIZ_SIP_DOMAIN` | For SIP transfer | `""` (empty) | Your Vobiz SIP domain, e.g. `xxxx.sip.vobiz.ai`. `transfer_call()` uses it to build `sip:<number>@<domain>`; with it empty the tool falls back to a `tel:` URI, which your trunk may not accept. |
| `CRM_BASE_URL` | For CRM features | `https://api.example-crm.com` | Base URL for the CRM REST API. Note the built-in default is a placeholder host that will not resolve — set it, or set it to an empty string to disable CRM calls cleanly. |
| `CRM_API_KEY` | For CRM features | `""` (empty) | Bearer token sent as `Authorization: Bearer …`. Every CRM helper checks it first, so leaving it empty is what actually disables CRM integration. |
| `WEBHOOK_URL` | For webhooks | `""` (empty) | Endpoint that receives every event as a JSON POST. Empty means `post_webhook()` returns immediately and no events are sent. |
| `WEBHOOK_SECRET` | No | `""` (empty) | HMAC-SHA256 key. When set, each request carries `X-Webhook-Signature`; when empty, no signature header is sent at all. |
| `TRANSFER_SALES` | No | `+15550001111` | Destination for `transfer_call("sales")`. Documentation number by default — replace it. |
| `TRANSFER_BILLING` | No | `+15550002222` | Destination for `transfer_call("billing")`. |
| `TRANSFER_SUPPORT` | No | `+15550003333` | Destination for `transfer_call("support")`, and the default when the model supplies no destination. |

`.env.example` also carries a `DEFAULT_TRANSFER_NUMBER` line. The code does not read it — the effective default is the `"support"` argument default on `transfer_call()`, which resolves through `TRANSFER_SUPPORT`.

`.env` is covered by `.gitignore`. Keep it that way.

## Running it

Two terminals, both with the virtual environment activated and both in the repository directory.

**Terminal 1 — start the worker.**

```bash
python agent.py start
```

```
INFO  registered worker  agent_name=webhook-crm-agent
```

**Point `WEBHOOK_URL` somewhere you can watch.** For a first run, open a [webhook.site](https://webhook.site) URL and put it in `.env` before starting the worker:

```bash
WEBHOOK_URL=https://webhook.site/your-unique-id
```

**Terminal 2 — place a call.**

```bash
python make_call.py --to +15550003333
```

```
Agent  : webhook-crm-agent
Calling: +15550003333
Room   : webhook-crm-agent-15550003333-4821
--------------------------------------------------
Dispatched — ID: AD_xxxxxxxxxxxx
Agent is dialing. Watch the agent terminal for logs.
```

The number must be E.164 and start with `+`; `make_call.py` rejects anything else before contacting LiveKit. There is no `--agent` flag — the name is fixed to `webhook-crm-agent` in the script and must match the `agent_name` in `WorkerOptions`.

What you should observe, in order: a CRM lookup line in the worker log, the dial, `Webhook call.started → 200` on your receiver, a greeting that uses the CRM name if one was found, an intent event when you state your reason, and — once you hang up — `call.ended` followed by a CRM activity write.

**Inbound.** Nothing extra to run. Point a Vobiz number at your LiveKit inbound trunk with a dispatch rule targeting `webhook-crm-agent`. With no `phone_number` in metadata there is nothing to look up, so the greeting is the generic one:

```
INFO  Inbound call — on_enter() will greet the caller.
```

### Expected logs

```
INFO  Room: webhook-crm-agent-15550003333-4821
INFO  CRM lookup for +15550003333…
INFO  CRM result: Alex Doe / Acme Corp
INFO  Dialing +15550003333 …
INFO  Call answered.
INFO  Webhook call.started → 200
INFO  Intent recorded: billing_inquiry
INFO  Webhook call.intent → 200
INFO  Call ended — posting final webhook and updating CRM.
INFO  Webhook call.ended → 200
INFO  CRM update: https://api.example-crm.com/contacts/c_12345/activities → 201
```

## Webhook contract

### Transport

Every event is a single `POST` to `WEBHOOK_URL` with `Content-Type: application/json`, sent by `post_webhook()` through a fresh `aiohttp` client session with a five-second total timeout. Nothing is batched; one event is one request.

### Envelope

Two keys are present on every payload, and the event-specific fields are merged in at the top level rather than nested:

| Field | Type | Description |
|-------|------|-------------|
| `event` | string | One of `call.started`, `call.intent`, `call.transferred`, `call.ended`. |
| `timestamp` | float | Unix epoch seconds, from `time.time()` at the moment the payload was built. |

### Events

| Event | Fired from | Fields merged into the envelope |
|-------|-----------|--------------------------------|
| `call.started` | `CRMAgent.on_enter()` — after the session starts, before the greeting is generated | `phone`, `room`, `contact_id`, `contact_name` |
| `call.intent` | `record_intent()` tool, when the model captures a reason for the call | `phone`, `intent`, `contact_id` |
| `call.transferred` | `transfer_call()` tool, immediately *before* the REFER is attempted | `phone`, `destination` (the resolved SIP or tel URI), `contact_id` |
| `call.ended` | `on_call_ended()`, from the room `disconnected` handler | `phone`, `summary`, `contact_id`, `events` |

Two details are easy to get wrong when writing a receiver. `call.transferred` is posted before `transfer_sip_participant()` is called, so it records the *attempt*, not a confirmed transfer — a subsequent failure is logged locally but produces no webhook. And `contact_id` is `null` whenever the CRM is unconfigured or the lookup found nothing, which is the normal state on inbound calls.

**Sample `call.ended` body:**

```json
{
  "event": "call.ended",
  "timestamp": 1771286400.482,
  "phone": "+15550003333",
  "summary": "Intent: billing_inquiry. Duration: 96s. Transfer: none.",
  "contact_id": "c_12345",
  "events": [
    { "type": "intent", "value": "billing_inquiry", "at": 1771286352.117 }
  ]
}
```

`summary` is assembled by `on_call_ended()` as a fixed three-part string — captured intent, duration in seconds, and transfer destination or `none`. It is not model-generated. The `events` array is the append-only list from `CallRecord`; `record_intent()` appends `{"type": "intent", …}` entries, and `update_crm_note()` appends `{"type": "note", …}` entries when there is no CRM contact ID to write the note to.

### Signing and verification

When `WEBHOOK_SECRET` is set, `_sign_payload()` computes HMAC-SHA256 over the exact serialised body and the request carries the hex digest in `X-Webhook-Signature`. When it is unset the header is omitted entirely — so treat a missing signature as a rejection rather than as "unsigned but fine".

Verify against the raw body, before any JSON parsing or re-serialisation, and compare in constant time:

```python
import hmac, hashlib

def verify_webhook(body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

The signature covers the body only — there is no timestamp in the signed material and no replay window, so a captured request stays valid indefinitely. If replay matters for your endpoint, reject payloads whose `timestamp` field is older than a few minutes as well as checking the digest.

### Delivery, retries and idempotency

This is the part to design your receiver around, because the sender is intentionally simple:

- **At most once.** `post_webhook()` awaits a single POST inside a `try`/`except` that logs failures and returns. There is no retry, no backoff and no dead-letter queue, so a five-second timeout or a 500 from your endpoint loses that event permanently.
- **Any non-2xx is treated as delivered.** The status is logged (`Webhook call.started → 502`) but never acted on. Watch the worker logs, or your own receiver metrics, to notice failures.
- **No delivery identifier.** Payloads carry no `id` or `delivery_id`, so there is no key the sender expects you to deduplicate on. Build your own from the fields that are present — `(event, phone, timestamp)` is unique in practice, and `room` is available on `call.started` if you need to group a whole call.
- **Duplicates are possible at teardown.** The `disconnected` handler is not guarded against firing more than once, so make the handling of `call.ended` idempotent — upsert on your own key rather than inserting.
- **Ordering is not guaranteed.** Requests are independent and `on_call_ended()` runs the final webhook concurrently with the CRM write. Order your records by the `timestamp` field, not by arrival.
- **Respond quickly.** The sender gives you five seconds in total. Acknowledge with a 2xx and do the real work asynchronously; slow processing shows up as a lost event, not a delayed one.

## CRM reference

### Endpoints called

| Purpose | Method and path | Body | Called from |
|---------|-----------------|------|-------------|
| Pre-call lookup | `POST {CRM_BASE_URL}/contacts/search` | `{"filter": {"phone": "<E.164>"}}` | `crm_lookup_contact()` |
| Mid-call search | `POST {CRM_BASE_URL}/contacts/search` | `{"query": "<phone or name>"}` | `lookup_crm()` tool |
| Add a note | `POST {CRM_BASE_URL}/contacts/{id}/notes` | `{"text": "…", "source": "ai-agent"}` | `update_crm_note()` tool |
| Log the call | `POST {CRM_BASE_URL}/contacts/{id}/activities` | `{"type": "call", "phone", "duration_seconds", "intent", "summary", "transfer_to"}` | `crm_update_contact()` |

All four send `Authorization: Bearer {CRM_API_KEY}` and use a five-second timeout. Both search calls read `data["results"][0]`, mapping `id`, `name` (falling back to `full_name`), `email`, `company` and `open_tickets`.

### Agent tools

| Tool | Arguments | Effect |
|------|-----------|--------|
| `record_intent` | `intent: str` | Sets `contact.last_intent`, appends an `intent` entry to `events`, posts `call.intent`, and replies to the caller. |
| `lookup_crm` | `phone_or_name: str` | Searches the CRM mid-call and returns a one-line summary of the first result, or `"CRM is not configured."` when the credentials are absent. |
| `update_crm_note` | `note: str` | Writes the note to the contact. With no contact ID it stores the note in `events` instead and says so. |
| `transfer_call` | `destination: str = "support"` | Resolves `sales` / `billing` / `support` through the `TRANSFER_*` variables — or takes a raw E.164 number — builds the URI, posts `call.transferred`, then issues the SIP REFER with `play_dialtone=False`. |

`lookup_crm` is the one worth seeing in context, because it fires mid-conversation when the caller mentions somebody other than themselves:

```
Caller: "Can you look up the account for Acme Corp?"
Agent calls: lookup_crm("Acme Corp")
Returns:     "Found: John Smith at Acme Corp (john@acme.com). Open tickets: 3."
```

### Adapting to your CRM

Nothing is vendor-specific beyond the four URL patterns and the response parsing, all of which live in `crm_lookup_contact()`, `crm_update_contact()`, `lookup_crm()` and `update_crm_note()`.

| CRM | Change needed |
|-----|---------------|
| **HubSpot** | `CRM_BASE_URL=https://api.hubapi.com`; search via `/crm/v3/objects/contacts/search`, and read results from `results[].properties`. |
| **Salesforce** | Put an OAuth2 access token in `CRM_API_KEY` and query `/services/data/v57.0/query`; map `records[]` instead of `results[]`. |
| **Pipedrive** | `CRM_BASE_URL=https://api.pipedrive.com/v1`; search via `/persons/search`, and read `data.items[].item`. |
| **Your own API** | Any REST service works — rewrite the paths and the field mapping, and keep the `CRMContact` shape so the greeting logic is unchanged. |

### Running without a CRM

Leave `CRM_API_KEY` empty. Every CRM helper checks the credentials first and returns early:

```
WARNING  CRM not configured — skipping lookup.
```

The call proceeds normally with a generic greeting, and all four webhook events still fire — with `contact_id` set to `null`.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `make_call.py` prints `ERROR: Missing LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET in .env` | `load_dotenv(".env")` resolves relative to the working directory, so the file was not found. | Run from the repository directory and confirm `.env` exists (`cp .env.example .env`). |
| No webhook requests arrive at all, and no webhook lines appear in the log | `WEBHOOK_URL` is empty, so `post_webhook()` returns before doing anything. | Set `WEBHOOK_URL` in `.env` and restart the worker — it is read once at import. |
| `Webhook post failed (call.started): …` in the log | The POST raised or exceeded its five-second timeout — an unreachable host, a TLS failure, or a slow receiver. Nothing is retried. | Check the URL is reachable from the worker, and make your endpoint acknowledge fast and process asynchronously. |
| `Webhook call.ended → 401` or similar on your receiver | The signature check failed, most often because the body was re-serialised before verification or `WEBHOOK_SECRET` differs between the two sides. | Verify against the raw request body, and confirm the secret matches exactly. |
| `call.ended` never arrives, though earlier events did | The teardown work is scheduled with `asyncio.ensure_future` from the `disconnected` handler and can lose the race against worker shutdown. | Keep the receiver fast, and treat a missing `call.ended` as possible — reconcile from `call.started` plus a timeout rather than relying on it. |
| `WARNING CRM not configured — skipping lookup.` when you expected a lookup | `CRM_API_KEY` is empty, or `CRM_BASE_URL` is. Both are checked before any request. | Set both in `.env` and restart the worker. |
| `CRM lookup returned 401` / `403` | The bearer token is wrong, expired, or lacks read scope on contacts. | Re-issue the token and confirm the scope your CRM requires for contact search. |
| `CRM lookup failed: …` with a DNS or connection error | `CRM_BASE_URL` is still the built-in `https://api.example-crm.com` placeholder, which does not resolve. | Set a real base URL, or set `CRM_BASE_URL=` empty to disable CRM calls cleanly. |
| Lookup returns 200 but the caller is never recognised | The response shape does not match `data["results"][0]` with `id` / `name` / `email` / `company` / `open_tickets`. | Adapt the parsing in `crm_lookup_contact()` to your CRM's response, as described under Adapting to your CRM. |
| Notes come back as `Note saved locally (no CRM contact ID available).` | The pre-call lookup found no contact, so `contact.id` is `None` and there is nothing to attach a note to. | Expected for unknown callers and for inbound calls with no metadata. The note is kept in `CallRecord.events` and ships with `call.ended`. |
| `Transfer failed: could not identify SIP participant.` | `userdata.phone_number` is empty and the room has no remote participant to fall back on — usually a transfer attempted before the SIP leg joined. | Only offer transfer once the caller is connected. |
| Transfer is rejected by the trunk | `VOBIZ_SIP_DOMAIN` is empty, so a `tel:` URI was built instead of a `sip:` one, or the destination is not permitted for REFER on your trunk. | Set `VOBIZ_SIP_DOMAIN`, and check the destination against your trunk configuration. See [Vobiz outbound trunks](https://docs.vobiz.ai/platform/sip/outbound-trunks). |
| Worker runs but no dispatch arrives | The `agent_name` in `WorkerOptions` and in `make_call.py` must match, and both processes must use the same LiveKit project. | Both ship as `webhook-crm-agent`. If you rename one, rename the other, and confirm both use the same `LIVEKIT_URL`. |

## Security notes

- **Two sets of third-party credentials live in `.env`** — your LiveKit and AI keys, and a CRM bearer token that can usually read and write customer records. `.gitignore` excludes `.env`; rotate anything that reaches a commit, the CRM token first.
- **Webhook payloads carry personal data.** Phone numbers, contact IDs, names and stated intent leave your worker on every event. Send them only to an HTTPS endpoint you control, and apply the same retention rules you would to call recordings.
- **Always set `WEBHOOK_SECRET` outside local testing.** Without it no signature header is sent, and any party that learns your URL can post fabricated call events into whatever your receiver drives.
- **Verify, then trust.** Check `X-Webhook-Signature` against the raw body with `hmac.compare_digest`, reject unsigned requests outright, and consider a freshness window on `timestamp` — the signature alone does not prevent replay.
- **Notes and intents are model-generated text** written into your CRM under `"source": "ai-agent"`. Keep that attribution so a human reading the record knows where it came from.
- **Transfer destinations come from the environment.** Anyone who can set `TRANSFER_*` can redirect live calls, so treat `.env` as a controlled configuration surface in a shared deployment.
- **Transcripts pass through third parties** — Deepgram for speech-to-text, OpenAI for the conversation. Confirm this is compatible with your obligations before dialling real customers.

## Roadmap

> Planned improvements to this example. Ideas and pull requests are welcome —
> open an issue to discuss anything here.

- [ ] Add retries with exponential backoff and a bounded queue in `post_webhook()`, so a brief receiver outage no longer drops events.
- [ ] Include a per-delivery `id` in the envelope, giving receivers a natural idempotency key instead of a composite of `event`, `phone` and `timestamp`.
- [ ] Sign the timestamp alongside the body and document a replay window, so a captured request stops being valid indefinitely.
- [ ] Await the teardown work through a job shutdown callback rather than `asyncio.ensure_future`, so `call.ended` and the CRM activity write cannot lose the race against worker shutdown.
- [ ] Reuse a single `aiohttp.ClientSession` across the call instead of opening one per request, cutting connection setup on every event.
- [ ] Emit `call.transfer_failed` when the REFER is rejected, so a failed escalation is visible to the receiver rather than only in local logs.
- [ ] Persist `CallRecord` locally as a fallback when the CRM write fails; today the record is in-memory only and is lost with the job.
- [ ] Add unit tests for `_sign_payload()`, the `post_webhook()` envelope, and the CRM response mapping; there are none today.

## Related examples

- [Livekit-Vobiz-All-feature-Example](https://github.com/vobiz-ai/Livekit-Vobiz-All-feature-Example) — transfer, IVR, AMD, recording and webhooks combined in one agent, and the hub for this series.
- [Livekit-Vobiz-Post-Call-Analysis](https://github.com/vobiz-ai/Livekit-Vobiz-Post-Call-Analysis) — transcription, summarisation and analysis once the call has ended.
- [Vobiz-Livekit-Call-Transfer-Example](https://github.com/vobiz-ai/Vobiz-Livekit-Call-Transfer-Example) — the SIP cold transfer used by `transfer_call()`, on its own.

## Contributing

Issues and pull requests are welcome. Before opening a pull request:

```bash
source .venv/bin/activate
pip install -r requirements.txt
python -m compileall agent.py make_call.py    # syntax check
python agent.py console                       # smoke-test the conversation locally
```

Please keep changes scoped to one concern, avoid committing anything from `.env`, and mask any real phone numbers, contact IDs, and endpoint URLs in examples and log excerpts.

## References

- [aiohttp documentation](https://docs.aiohttp.org/)
- [LiveKit — cold transfer](https://docs.livekit.io/telephony/features/transfers/cold/)
- [LiveKit — Python API reference](https://docs.livekit.io/reference/python/livekit/agents/)
- [Vobiz + LiveKit integration guide](https://docs.vobiz.ai/integrations/livekit)
- [Vobiz outbound trunks](https://docs.vobiz.ai/platform/sip/outbound-trunks)
- [HubSpot contacts API](https://developers.hubspot.com/docs/api/crm/contacts)
- [Salesforce REST API](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/)

## License

Released under the [MIT License](./LICENSE) © Vobiz.

MIT is permissive: you may use, modify, and redistribute this code, including in
closed-source commercial products, provided the copyright notice and licence text
are retained. There is no warranty. If your organisation needs a different
licensing arrangement, contact [piyush@vobiz.ai](mailto:piyush@vobiz.ai).

## Built by Team Vobiz

[Vobiz](https://vobiz.ai) is a programmable voice and SIP-trunking platform for
voice APIs, SIP trunking, and AI voice agents. This repository is built and
maintained by the Vobiz team.

**Maintainer:** Piyush Sahoo — [piyush@vobiz.ai](mailto:piyush@vobiz.ai) · [LinkedIn](https://www.linkedin.com/in/piyush-s713/)

Questions, or want to talk through an integration? Open an issue on this repo,
or reach out directly at [piyush@vobiz.ai](mailto:piyush@vobiz.ai).

**Useful links:** [Docs](https://docs.vobiz.ai) · [API reference](https://docs.vobiz.ai/api-reference) · [Sign up](https://vobiz.ai)
