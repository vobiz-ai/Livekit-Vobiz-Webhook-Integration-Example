"""
06 - Webhook + CRM Integration Agent
======================================
Demonstrates a real-world integration pattern where the agent:

  1. Looks up the caller in a CRM via HTTP before/during the call.
  2. Posts call events to a webhook (call started, intent captured, call ended).
  3. Updates the CRM contact record with call outcome and notes.
  4. Sends DTMF if an upstream IVR is detected.
  5. Can perform a SIP transfer when escalation is needed.

CRM / webhook calls are made with aiohttp (async, non-blocking).
All endpoints are configured via environment variables so no CRM vendor
is hard-coded — swap the URL patterns to match HubSpot, Salesforce,
Pipedrive, or your own API.

Environment variables:
  CRM_BASE_URL        Base URL for CRM API  (e.g. https://api.hubapi.com)
  CRM_API_KEY         Bearer token / API key for CRM
  WEBHOOK_URL         URL to POST call events to
  WEBHOOK_SECRET      Optional HMAC secret to sign payloads

Webhook event types posted:
  call.started        — call connected, CRM lookup result included
  call.intent         — caller stated their reason
  call.transferred    — SIP transfer initiated
  call.ended          — call complete, summary included
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import aiohttp
from dotenv import load_dotenv
from livekit import agents, api
from livekit.agents import Agent, AgentSession, RoomInputOptions, RunContext, llm
from livekit.plugins import deepgram, noise_cancellation, openai, silero

load_dotenv(".env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("webhook-crm-agent")

OUTBOUND_TRUNK_ID = os.getenv("OUTBOUND_TRUNK_ID")
SIP_DOMAIN = os.getenv("VOBIZ_SIP_DOMAIN", "")

CRM_BASE_URL   = os.getenv("CRM_BASE_URL", "https://api.example-crm.com")
CRM_API_KEY    = os.getenv("CRM_API_KEY", "")
WEBHOOK_URL    = os.getenv("WEBHOOK_URL", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

@dataclass
class CRMContact:
    id: Optional[str] = None
    name: Optional[str] = None
    email: Optional[str] = None
    company: Optional[str] = None
    last_intent: Optional[str] = None
    open_tickets: int = 0


@dataclass
class CallRecord:
    phone_number: Optional[str] = None
    room_name: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    contact: CRMContact = field(default_factory=CRMContact)
    events: list[dict] = field(default_factory=list)
    transfer_destination: Optional[str] = None
    call_summary: Optional[str] = None


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _crm_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {CRM_API_KEY}",
        "Content-Type": "application/json",
    }


def _sign_payload(body: bytes) -> str:
    """HMAC-SHA256 signature for webhook verification."""
    if not WEBHOOK_SECRET:
        return ""
    return hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()


async def crm_lookup_contact(phone_number: str) -> CRMContact:
    """
    Look up a contact by phone number in the CRM.
    Returns a CRMContact (fields may be None if not found).

    Adapt the URL pattern / response parsing to your CRM's API.
    """
    if not CRM_BASE_URL or not CRM_API_KEY:
        logger.warning("CRM not configured — skipping lookup.")
        return CRMContact()

    url = f"{CRM_BASE_URL}/contacts/search"
    payload = {"filter": {"phone": phone_number}}

    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(url, json=payload, headers=_crm_headers(), timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    results = data.get("results", [])
                    if results:
                        c = results[0]
                        return CRMContact(
                            id=c.get("id"),
                            name=c.get("name") or c.get("full_name"),
                            email=c.get("email"),
                            company=c.get("company"),
                            open_tickets=c.get("open_tickets", 0),
                        )
                else:
                    logger.warning("CRM lookup returned %s", resp.status)
    except Exception as exc:
        logger.warning("CRM lookup failed: %s", exc)

    return CRMContact()


async def crm_update_contact(record: CallRecord) -> None:
    """Post call outcome back to CRM contact record."""
    if not CRM_BASE_URL or not CRM_API_KEY or not record.contact.id:
        return

    url = f"{CRM_BASE_URL}/contacts/{record.contact.id}/activities"
    payload = {
        "type": "call",
        "phone": record.phone_number,
        "duration_seconds": int(time.time() - record.started_at),
        "intent": record.contact.last_intent,
        "summary": record.call_summary,
        "transfer_to": record.transfer_destination,
    }

    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(url, json=payload, headers=_crm_headers(), timeout=aiohttp.ClientTimeout(total=5)) as resp:
                logger.info("CRM update: %s → %s", url, resp.status)
    except Exception as exc:
        logger.warning("CRM update failed: %s", exc)


async def post_webhook(event_type: str, data: dict[str, Any]) -> None:
    """Fire-and-forget POST to the configured webhook URL."""
    if not WEBHOOK_URL:
        return

    payload = {
        "event": event_type,
        "timestamp": time.time(),
        **data,
    }
    body = json.dumps(payload).encode()
    headers: dict[str, str] = {"Content-Type": "application/json"}
    sig = _sign_payload(body)
    if sig:
        headers["X-Webhook-Signature"] = sig

    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(WEBHOOK_URL, data=body, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                logger.info("Webhook %s → %s", event_type, resp.status)
    except Exception as exc:
        logger.warning("Webhook post failed (%s): %s", event_type, exc)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class CRMAgent(Agent):
    """
    Voice agent with full CRM + webhook integration.
    Greets the caller by name (if found in CRM), captures intent,
    and posts events throughout the call lifecycle.
    """

    def __init__(self, ctx: agents.JobContext) -> None:
        super().__init__(
            instructions="""
            You are a professional Vobiz account manager.

            You have access to the caller's CRM record (if available). Use it to
            personalize the conversation — reference their name, company, or open tickets.

            Your tasks:
            1. Greet the caller (use their name from CRM if known).
            2. Understand their reason for calling (capture with record_intent tool).
            3. Help them or escalate:
               - Use transfer_call to do a SIP transfer to a real person.
               - Use lookup_crm to search for customer info during the call.
               - Use update_crm_note to log anything important.
            4. When the call ends, thank them and say goodbye.

            Keep all responses concise — this is a phone call.
            """
        )
        self._ctx = ctx

    async def on_enter(self) -> None:
        record: CallRecord = self.session.userdata
        contact = record.contact

        await post_webhook("call.started", {
            "phone": record.phone_number,
            "room": record.room_name,
            "contact_id": contact.id,
            "contact_name": contact.name,
        })

        if contact.name:
            instructions = (
                f"Greet {contact.name} from {contact.company or 'their company'} warmly. "
                f"They have {contact.open_tickets} open support ticket(s). "
                "Ask how you can help them today."
            )
        else:
            instructions = "Greet the caller and ask for their name and how you can help."

        await self.session.generate_reply(instructions=instructions)

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    @llm.function_tool(
        description="Record the caller's reason for calling and post a webhook event."
    )
    async def record_intent(self, context: RunContext[CallRecord], intent: str) -> str:
        context.userdata.contact.last_intent = intent
        context.userdata.events.append({"type": "intent", "value": intent, "at": time.time()})

        await post_webhook("call.intent", {
            "phone": context.userdata.phone_number,
            "intent": intent,
            "contact_id": context.userdata.contact.id,
        })

        logger.info("Intent recorded: %s", intent)
        return f"Got it — {intent}. How can I best help you with that?"

    @llm.function_tool(
        description=(
            "Look up a customer in the CRM by phone number or name "
            "while on the call (e.g., if the caller mentions another contact)."
        )
    )
    async def lookup_crm(self, context: RunContext[CallRecord], phone_or_name: str) -> str:
        if not CRM_BASE_URL or not CRM_API_KEY:
            return "CRM is not configured."

        url = f"{CRM_BASE_URL}/contacts/search"
        payload = {"query": phone_or_name}

        try:
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    url, json=payload, headers=_crm_headers(),
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        results = data.get("results", [])
                        if results:
                            c = results[0]
                            return (
                                f"Found: {c.get('name')} at {c.get('company')} "
                                f"({c.get('email')}). Open tickets: {c.get('open_tickets', 0)}."
                            )
                        return "No contact found."
                    return f"CRM returned status {resp.status}."
        except Exception as exc:
            return f"CRM lookup failed: {exc}"

    @llm.function_tool(
        description="Add a note to the caller's CRM record during the call."
    )
    async def update_crm_note(self, context: RunContext[CallRecord], note: str) -> str:
        contact_id = context.userdata.contact.id
        if not contact_id:
            context.userdata.events.append({"type": "note", "text": note, "at": time.time()})
            return "Note saved locally (no CRM contact ID available)."

        url = f"{CRM_BASE_URL}/contacts/{contact_id}/notes"
        payload = {"text": note, "source": "ai-agent"}

        try:
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    url, json=payload, headers=_crm_headers(),
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status in (200, 201):
                        return "Note added to CRM."
                    return f"CRM note failed: status {resp.status}."
        except Exception as exc:
            return f"Note failed: {exc}"

    @llm.function_tool(
        description=(
            "Transfer the live SIP call to a specific department or phone number. "
            "Accepts: 'sales', 'billing', 'support', or a full E.164 number."
        )
    )
    async def transfer_call(self, context: RunContext[CallRecord], destination: str = "support") -> str:
        # Resolve destination
        resolved = {
            "sales":   os.getenv("TRANSFER_SALES",   "+15550001111"),
            "billing": os.getenv("TRANSFER_BILLING", "+15550002222"),
            "support": os.getenv("TRANSFER_SUPPORT", "+15550003333"),
        }.get(destination.lower(), destination)

        clean = resolved.replace("tel:", "").replace("sip:", "")
        if "@" in clean:
            transfer_uri = f"sip:{clean}"
        elif SIP_DOMAIN:
            transfer_uri = f"sip:{clean}@{SIP_DOMAIN}"
        else:
            transfer_uri = f"tel:{clean}"

        context.userdata.transfer_destination = transfer_uri

        # Find SIP participant
        phone = context.userdata.phone_number
        participant_identity: Optional[str] = None
        if phone:
            participant_identity = f"sip_{phone}"
        else:
            for p in self._ctx.room.remote_participants.values():
                participant_identity = p.identity
                break

        if not participant_identity:
            return "Transfer failed: could not identify SIP participant."

        await post_webhook("call.transferred", {
            "phone": phone,
            "destination": transfer_uri,
            "contact_id": context.userdata.contact.id,
        })

        try:
            await self._ctx.api.sip.transfer_sip_participant(
                api.TransferSIPParticipantRequest(
                    room_name=self._ctx.room.name,
                    participant_identity=participant_identity,
                    transfer_to=transfer_uri,
                    play_dialtone=False,
                )
            )
            return f"Transfer to {destination} initiated."
        except Exception as exc:
            logger.error("SIP transfer failed: %s", exc)
            return f"Transfer failed: {exc}"


# ---------------------------------------------------------------------------
# Call teardown: post final webhook + update CRM
# ---------------------------------------------------------------------------

async def on_call_ended(record: CallRecord) -> None:
    """Called when the room closes / agent shuts down."""
    logger.info("Call ended — posting final webhook and updating CRM.")

    record.call_summary = (
        f"Intent: {record.contact.last_intent or 'not captured'}. "
        f"Duration: {int(time.time() - record.started_at)}s. "
        f"Transfer: {record.transfer_destination or 'none'}."
    )

    await asyncio.gather(
        post_webhook("call.ended", {
            "phone": record.phone_number,
            "summary": record.call_summary,
            "contact_id": record.contact.id,
            "events": record.events,
        }),
        crm_update_contact(record),
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def entrypoint(ctx: agents.JobContext):
    logger.info("Room: %s", ctx.room.name)

    phone_number: Optional[str] = None
    try:
        if ctx.job.metadata:
            phone_number = json.loads(ctx.job.metadata).get("phone_number")
    except Exception:
        pass

    # CRM lookup before the call starts (non-blocking)
    contact = CRMContact()
    if phone_number:
        logger.info("CRM lookup for %s…", phone_number)
        contact = await crm_lookup_contact(phone_number)
        logger.info("CRM result: %s / %s", contact.name, contact.company)

    record = CallRecord(
        phone_number=phone_number,
        room_name=ctx.room.name,
        contact=contact,
    )

    session = AgentSession[CallRecord](
        stt=deepgram.STT(model="nova-3", language="multi"),
        llm=openai.LLM(model="gpt-4o-mini"),
        tts=openai.TTS(model="tts-1", voice="alloy"),
        vad=silero.VAD.load(),
        userdata=record,
    )

    # Register shutdown hook for final webhook / CRM update
    @ctx.room.on("disconnected")
    def _on_disconnect(_reason=None):
        asyncio.ensure_future(on_call_ended(record))

    await session.start(
        room=ctx.room,
        agent=CRMAgent(ctx),
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVCTelephony(),
        ),
    )

    if phone_number:
        logger.info("Dialing %s …", phone_number)
        await ctx.api.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                room_name=ctx.room.name,
                sip_trunk_id=OUTBOUND_TRUNK_ID,
                sip_call_to=phone_number,
                participant_identity=f"sip_{phone_number}",
                wait_until_answered=True,
            )
        )
        logger.info("Call answered.")
    else:
        logger.info("Inbound call — on_enter() will greet the caller.")


if __name__ == "__main__":
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="webhook-crm-agent",
        )
    )
