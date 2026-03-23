# 06 — Webhook + CRM Integration Agent

Demonstrates a **production integration pattern** where the agent:

- Looks up the caller in a CRM **before** the call starts
- Greets them by name and references their open tickets
- Posts webhook events throughout the call lifecycle
- Logs notes to the CRM in real time
- Transfers to staff when needed
- Posts a final summary + updates the CRM contact on call end

No CRM vendor is hard-coded — swap URL patterns to match HubSpot, Salesforce, Pipedrive, or your own API.

---

## Call Lifecycle

```
Job dispatched with phone_number in metadata
         ↓
CRM lookup: GET /contacts/search?phone=+91...
  → returns name, company, open_tickets
         ↓
Outbound call placed
         ↓
Call answered
  → POST webhook: call.started (includes CRM contact)
         ↓
Agent greets by name:
  "Hi Rahul, I can see you have 2 open tickets…"
         ↓
During call:
  → record_intent()    → POST webhook: call.intent
  → lookup_crm()       → search CRM for another contact
  → update_crm_note()  → POST /contacts/:id/notes
         ↓
If escalation:
  → transfer_call()    → SIP REFER + POST webhook: call.transferred
         ↓
Call ends (room disconnects)
  → POST webhook: call.ended (summary, duration, events)
  → PATCH /contacts/:id/activities (outcome logged)
```

---

## Webhook Events

| Event | When fired | Payload |
|-------|-----------|---------|
| `call.started` | Call connected | contact_id, contact_name, phone, room |
| `call.intent` | Caller states reason | intent, contact_id |
| `call.transferred` | SIP transfer initiated | destination, reason |
| `call.ended` | Call complete | summary, duration, events list |

All payloads are HMAC-SHA256 signed via `X-Webhook-Signature` header if `WEBHOOK_SECRET` is set.

---

## Environment Variables

```bash
# LiveKit Cloud
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=APIxxxxxxxxxx
LIVEKIT_API_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxx

# OpenAI
OPENAI_API_KEY=sk-xxxx

# Deepgram
DEEPGRAM_API_KEY=xxxx

# Vobiz SIP
VOBIZ_SIP_DOMAIN=xxxx.sip.vobiz.ai
OUTBOUND_TRUNK_ID=ST_xxxxxxxxxxxx
DEFAULT_TRANSFER_NUMBER=+91XXXXXXXXXX

# CRM Integration
CRM_BASE_URL=https://api.hubapi.com          # or your CRM base URL
CRM_API_KEY=pat-xxxxxxxxxxxxxxxxxxxx         # CRM bearer token

# Webhook
WEBHOOK_URL=https://your-server.com/webhooks/calls
WEBHOOK_SECRET=your-hmac-secret             # optional, for request signing

# Transfer
TRANSFER_SALES=+15550001111
TRANSFER_BILLING=+15550002222
TRANSFER_SUPPORT=+15550003333
```

---

## Setup

```bash
source ".venv/bin/activate"
cd 06-webhook-crm-agent
pip install aiohttp   # extra dep for HTTP calls (included in root requirements.txt)
```

---

## Running

### Step 1 — Start the agent worker

```bash
python agent.py start
```

```
INFO  registered worker  agent_name=webhook-crm-agent
```

### Step 2 — Test with a webhook receiver

During development, use [webhook.site](https://webhook.site) or `ngrok` + a local server:

```bash
# Set in .env:
WEBHOOK_URL=https://webhook.site/your-unique-id
```

### Step 3 — Place a call

```bash
python ../make_call.py --to +91XXXXXXXXXX --agent webhook-crm-agent
```

The agent will:
1. Try to look up `+91XXXXXXXXXX` in your CRM (if configured)
2. Post `call.started` to your webhook URL
3. Greet the caller (with their name if CRM lookup succeeded)

---

## CRM Integration

### Lookup (before call)

```python
async def crm_lookup_contact(phone_number: str) -> CRMContact:
    url = f"{CRM_BASE_URL}/contacts/search"
    payload = {"filter": {"phone": phone_number}}

    async with aiohttp.ClientSession() as http:
        resp = await http.post(url, json=payload, headers=_crm_headers())
        data = await resp.json()
        c = data["results"][0]
        return CRMContact(
            id=c["id"],
            name=c["name"],
            email=c["email"],
            company=c["company"],
            open_tickets=c["open_tickets"],
        )
```

### Adapting to your CRM

| CRM | Change needed |
|-----|--------------|
| **HubSpot** | `CRM_BASE_URL=https://api.hubapi.com` → search via `/crm/v3/objects/contacts/search` |
| **Salesforce** | Use OAuth2 token in `CRM_API_KEY`, endpoint `/services/data/v57.0/query` |
| **Pipedrive** | `CRM_BASE_URL=https://api.pipedrive.com/v1`, search via `/persons/search` |
| **Custom API** | Any REST API — just update the URL patterns in `crm_lookup_contact()` |

---

## Webhook Security

Payloads are signed with HMAC-SHA256:

```python
def _sign_payload(body: bytes) -> str:
    return hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
```

Header sent: `X-Webhook-Signature: <hex-digest>`

Verify on your server:
```python
import hmac, hashlib

def verify_webhook(body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

---

## Agent Tools

### `record_intent(intent: str)`
Captures why the caller is calling and fires the `call.intent` webhook:
```python
# Called automatically when caller states their reason
await post_webhook("call.intent", {"intent": "billing_question", ...})
```

### `lookup_crm(phone_or_name: str)`
Mid-call CRM search — useful when caller mentions a colleague or company:
```
Caller: "Can you look up the account for Acme Corp?"
Agent calls: lookup_crm("Acme Corp")
Returns: "Found: John Smith at Acme Corp (john@acme.com). Open tickets: 3"
```

### `update_crm_note(note: str)`
Logs a note to the CRM contact record in real time:
```python
# Agent logs important details during the call
await http.post(f"{CRM_BASE_URL}/contacts/{contact_id}/notes",
                json={"text": note, "source": "ai-agent"})
```

### `transfer_call(destination: str)`
SIP REFER to sales/billing/support/number + fires `call.transferred` webhook.

---

## Call Teardown

When the room disconnects, the agent automatically:
1. Builds a call summary string
2. POSTs `call.ended` webhook with full event list
3. PATCHes the CRM contact with outcome + duration

```python
@ctx.room.on("disconnected")
def _on_disconnect(_reason=None):
    asyncio.ensure_future(on_call_ended(record))

async def on_call_ended(record: CallRecord):
    await asyncio.gather(
        post_webhook("call.ended", {...}),
        crm_update_contact(record),
    )
```

---

## Testing Without a Real CRM

Leave `CRM_BASE_URL` and `CRM_API_KEY` empty. The agent gracefully falls back:

```
WARNING  CRM not configured — skipping lookup.
```

The agent still works fully — it just won't personalise the greeting or log to CRM. All webhook events still fire.

---

## CallRecord Structure

```python
@dataclass
class CallRecord:
    phone_number:         Optional[str]
    room_name:            Optional[str]
    started_at:           float              # unix timestamp
    contact:              CRMContact         # populated from CRM lookup
    events:               list[dict]         # all events during call
    transfer_destination: Optional[str]
    call_summary:         Optional[str]      # LLM-generated post-call summary
```

---

## Expected Logs

```
INFO  CRM lookup for +91XXXXXXXXXX…
INFO  CRM result: Rahul Sharma / Acme Corp
INFO  Dialing +91XXXXXXXXXX…
INFO  Call answered.
INFO  Webhook call.started → 200
INFO  Intent recorded: billing_inquiry
INFO  Webhook call.intent → 200
INFO  Note added to CRM.
INFO  Call ended — posting final webhook and updating CRM.
INFO  Webhook call.ended → 200
INFO  CRM update: /contacts/c_12345/activities → 201
```

---

## Docs

- [aiohttp](https://docs.aiohttp.org/)
- [LiveKit SIP Transfers](https://docs.livekit.io/telephony/features/transfers/cold/)
- [HubSpot Contacts API](https://developers.hubspot.com/docs/api/crm/contacts)
- [Salesforce REST API](https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/)
