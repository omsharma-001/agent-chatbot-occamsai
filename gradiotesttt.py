# gradio_app_conversations_multi.py
import os
import json
import uuid
import contextvars
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import TypedDict, Optional, Literal, Dict

# Bootstrap env (OpenAI, SendGrid, Stripe, SITE_URL, etc.)
import config  # side-effect: sets env on import

import gradio as gr
from agents import Agent, Runner, function_tool, OpenAIConversationsSession

from base_prompt import BasePrompt
from llc_prompt import LLCPrompt
from corp_prompt import CorpPrompt
from payment_prompt import PaymentPrompt
from extractor_prompt import ExtractorPrompt
from otp_service import OTPService

# Use the real PaymentService
from payment_service import PaymentService


# ========= GLOBAL CONTEXT =========
CURRENT_SESSION = contextvars.ContextVar("CURRENT_SESSION", default=None)
_SESSION_STORE = {}  # Store actual session objects to preserve conversation history
_ACTIVE_EXTRACTIONS = set()  # Track active extraction sessions


# ========= TOOL ARG TYPES =========
class SendEmailOtpArgs(TypedDict):
    email: str

class VerifyEmailOtpArgs(TypedDict):
    email: str
    code: str

class SetEntityArgs(TypedDict):
    entity_type: Literal["BASE", "LLC", "C-CORP", "S-CORP", "PAYMENT"]

class UpdateEntityTypeArgs(TypedDict):
    entity_type: Literal["LLC", "C-CORP", "S-CORP"]  # used in Payment to switch mid-checkout

class UpdateToPaymentArgs(TypedDict):
    _: Optional[str]

class StateFeeLookupArgs(TypedDict):
    state: str
    entity_type: Literal["LLC", "C-Corp", "S-Corp", "C-CORP", "S-CORP"]

class CreatePaymentLinkArgs(TypedDict):
    productName: Literal["Classic", "Premium", "Elite"]
    price: float
    billingCycle: Optional[Literal["yearly", "monthly"]]
    stateFilingFee: float
    totalDueNow: float

class CheckPaymentStatusArgs(TypedDict):
    productName: Literal["Classic", "Premium", "Elite"]
    price: float
    billingCycle: Optional[Literal["yearly", "monthly"]]


# ========= HELPERS =========
def _normalize_entity_label(s: str) -> str:
    if not s:
        return s
    x = s.strip().lower().replace(".", "").replace("_", "-")
    if x == "llc": return "LLC"
    if x in ("c-corp", "c corp", "ccorp"): return "C-Corp"
    if x in ("s-corp", "s corp", "scorp", "s-ccorp", "s ccorp"): return "S-Corp"
    return s

def _resolve_state_name(input_state: str) -> Optional[str]:
    if not input_state:
        return None
    s = input_state.strip()
    if len(s) == 2:
        codes = {
            "AL":"Alabama","AK":"Alaska","AZ":"Arizona","AR":"Arkansas","CA":"California","CO":"Colorado",
            "CT":"Connecticut","DE":"Delaware","FL":"Florida","GA":"Georgia","HI":"Hawaii","ID":"Idaho",
            "IL":"Illinois","IN":"Indiana","IA":"Iowa","KS":"Kansas","KY":"Kentucky","LA":"Louisiana",
            "ME":"Maine","MD":"Maryland","MA":"Massachusetts","MI":"Michigan","MN":"Minnesota",
            "MS":"Mississippi","MO":"Missouri","MT":"Montana","NE":"Nebraska","NV":"Nevada",
            "NH":"New Hampshire","NJ":"New Jersey","NM":"New Mexico","NY":"New York",
            "NC":"North Carolina","ND":"North Dakota","OH":"Ohio","OK":"Oklahoma","OR":"Oregon",
            "PA":"Pennsylvania","RI":"Rhode Island","SC":"South Carolina","SD":"South Dakota",
            "TN":"Tennessee","TX":"Texas","UT":"Utah","VT":"Vermont","VA":"Virginia",
            "WA":"Washington","WV":"West Virginia","WI":"Wisconsin","WY":"Wyoming","DC":"Washington, DC"
        }
        return codes.get(s.upper())
    low = s.lower().replace(".", "").replace(",", "").strip()
    if low in ("dc","d c","washington dc","washington d c","district of columbia"):
        return "Washington, DC"
    return s


# ========= TOOLS =========
otp = OTPService()

@function_tool
async def sendEmailOtp(args: SendEmailOtpArgs) -> str:
    print(f"[TOOL LOG] ✉️ sendEmailOtp called with email={args.get('email')}")
    return otp.send_otp_to_user(args)

@function_tool
async def verifyEmailOtp(args: VerifyEmailOtpArgs) -> str:
    print(f"[TOOL LOG] 🔐 verifyEmailOtp called for email={args.get('email')} code={args.get('code')}")
    return otp.verify_otp_from_user(args)

@function_tool
async def setEntityType(args: SetEntityArgs) -> str:
    sess = CURRENT_SESSION.get()
    if not isinstance(sess, OpenAIConversationsSession):
        print("[AGENT LOG] ❌ setEntityType called but no active session")
        return "No active session to update."
    new_type = args.get("entity_type", "BASE")
    old_type = getattr(sess, "entity_type", "BASE")
    setattr(sess, "entity_type", new_type)
    print(f"[AGENT LOG] 🔒 setEntityType -> {old_type} → {new_type}")
    return f"Entity type set to {new_type}"

@function_tool
async def updateToPaymentMode(args: UpdateToPaymentArgs) -> str:
    sess = CURRENT_SESSION.get()
    if not isinstance(sess, OpenAIConversationsSession):
        print("[AGENT LOG] ❌ updateToPaymentMode called with no active session")
        return "No active session to update."
    old = getattr(sess, "entity_type", "BASE")
    setattr(sess, "entity_type", "PAYMENT")
    setattr(sess, "awaiting_payment", False)
    setattr(sess, "payment_status", None)
    print(f"[AGENT LOG] 💳 updateToPaymentMode -> {old} → PAYMENT (awaiting_payment=False, payment_status=None)")
    return "Switched to Payment mode."

@function_tool
async def updateEntityType(args: UpdateEntityTypeArgs) -> str:
    sess = CURRENT_SESSION.get()
    if not isinstance(sess, OpenAIConversationsSession):
        print("[AGENT LOG] ❌ updateEntityType (Payment) called with no active session")
        return "No active session to update."
    target = _normalize_entity_label(args.get("entity_type", ""))
    if target not in ("LLC", "C-Corp", "S-Corp"):
        print(f"[AGENT LOG] ❌ updateEntityType (Payment) unsupported -> {target}")
        return "Unsupported entity type."
    old = getattr(sess, "entity_type", "PAYMENT")
    setattr(sess, "entity_type", "LLC" if target == "LLC" else "C-CORP" if target == "C-Corp" else "S-CORP")
    setattr(sess, "awaiting_payment", False)
    setattr(sess, "payment_status", None)
    print(f"[AGENT LOG] 🔁 updateEntityType (Payment) -> {old} → {getattr(sess,'entity_type')} (flags reset)")
    return f"Entity type updated to {target}. We’ll refresh totals and continue."

# Fallback fees
_FALLBACK_FEES = {
    'Alabama':        {'llc': 200, 's-corp': 208, 'c-corp': 208},
    'Alaska':         {'llc': 250, 's-corp': 250, 'c-corp': 250},
    'Arizona':        {'llc': 50,  's-corp': 60,  'c-corp': 60 },
    'Arkansas':       {'llc': 45,  's-corp': 50,  'c-corp': 50 },
    'California':     {'llc': 70,  's-corp': 100, 'c-corp': 100},
    'Colorado':       {'llc': 50,  's-corp': 50,  'c-corp': 50 },
    'Connecticut':    {'llc': 120, 's-corp': 250, 'c-corp': 250},
    'Delaware':       {'llc': 90,  's-corp': 89,  'c-corp': 89 },
    'Florida':        {'llc': 125, 's-corp': 70,  'c-corp': 70 },
    'Georgia':        {'llc': 100, 's-corp': 100, 'c-corp': 100},
    'Hawaii':         {'llc': 50,  's-corp': 50,  'c-corp': 50 },
    'Idaho':          {'llc': 100, 's-corp': 100, 'c-corp': 100},
    'Illinois':       {'llc': 150, 's-corp': 150, 'c-corp': 150},
    'Indiana':        {'llc': 95,  's-corp': 90,  'c-corp': 90 },
    'Iowa':           {'llc': 50,  's-corp': 50,  'c-corp': 50 },
    'Kansas':         {'llc': 160, 's-corp': 90,  'c-corp': 90 },
    'Kentucky':       {'llc': 40,  's-corp': 50,  'c-corp': 50 },
    'Louisiana':      {'llc': 100, 's-corp': 75, 'c-corp': 75 },
    'Maine':          {'llc': 175, 's-corp': 145, 'c-corp': 145},
    'Maryland':       {'llc': 150, 's-corp': 120, 'c-corp': 120},
    'Massachusetts':  {'llc': 500, 's-corp': 275, 'c-corp': 275},
    'Michigan':       {'llc': 50,  's-corp': 60,  'c-corp': 60 },
    'Minnesota':      {'llc': 155, 's-corp': 135, 'c-corp': 135},
    'Mississippi':    {'llc': 50,  's-corp': 50,  'c-corp': 50 },
    'Missouri':       {'llc': 50,  's-corp': 58,  'c-corp': 58 },
    'Montana':        {'llc': 35,  's-corp': 70,  'c-corp': 70 },
    'Nebraska':       {'llc': 100, 's-corp': 60,  'c-corp': 60 },
    'Nevada':         {'llc': 425, 's-corp': 725, 'c-corp': 725},
    'New Hampshire':  {'llc': 100, 's-corp': 100, 'c-corp': 100},
    'New Jersey':     {'llc': 125, 's-corp': 125, 'c-corp': 125},
    'New Mexico':     {'llc': 50,  's-corp': 100,  'c-corp': 100},
    'New York':       {'llc': 200, 's-corp': 125, 'c-corp': 125},
    'North Carolina': {'llc': 125, 's-corp': 125, 'c-corp': 125},
    'North Dakota':   {'llc': 135, 's-corp': 100,  'c-corp': 100},
    'Ohio':           {'llc': 99,  's-corp': 99,  'c-corp': 99 },
    'Oklahoma':       {'llc': 100, 's-corp': 50,  'c-corp': 50 },
    'Oregon':         {'llc': 100, 's-corp': 100, 'c-corp': 100},
    'Pennsylvania':   {'llc': 125, 's-corp': 125, 'c-corp': 125},
    'Rhode Island':   {'llc': 150, 's-corp': 230,  'c-corp': 230},
    'South Carolina': {'llc': 110, 's-corp': 125,  'c-corp': 125},
    'South Dakota':   {'llc': 150, 's-corp': 150,  'c-corp': 150},
    'Tennessee':      {'llc': 300, 's-corp': 100,  'c-corp': 100},
    'Texas':          {'llc': 300, 's-corp': 300,  'c-corp': 300},
    'Utah':           {'llc': 70,  's-corp': 70,  'c-corp': 70 },
    'Vermont':        {'llc': 125, 's-corp': 125,  'c-corp': 125},
    'Virginia':       {'llc': 100, 's-corp': 25,   'c-corp': 25 },
    'Washington':     {'llc': 200, 's-corp': 200,  'c-corp': 200},
    'West Virginia':  {'llc': 100, 's-corp': 50,   'c-corp': 50 },
    'Wisconsin':      {'llc': 130, 's-corp': 100,  'c-corp': 100},
    'Wyoming':        {'llc': 100, 's-corp': 100,  'c-corp': 100},
    'Washington, DC': {'llc': 99,  's-corp': 99,   'c-corp': 99 },
}

@function_tool
async def stateFeeLookup(args: StateFeeLookupArgs) -> str:
    print(f"[TOOL LOG] 🔎 stateFeeLookup called with args={args}")
    state_raw = args.get("state", "")
    ent_raw = args.get("entity_type", "")
    label = _normalize_entity_label(ent_raw)
    state_name = _resolve_state_name(state_raw)

    if not state_name or label not in ("LLC", "C-Corp", "S-Corp"):
        print("[TOOL LOG] 🔎 stateFeeLookup -> missing params")
        return json.dumps({"error": "missing_params", "state": state_raw, "entity_type": ent_raw})

    key = "llc" if label == "LLC" else "c-corp" if label == "C-Corp" else "s-corp"
    fee = _FALLBACK_FEES.get(state_name, {}).get(key)
    if fee is None:
        print(f"[TOOL LOG] 🔎 stateFeeLookup (fallback) -> fee_not_found for {state_name}/{label}")
        return json.dumps({"error": "fee_not_found", "state": state_name, "entity_type": label})
    out = {"state": state_name, "entity_type": label, "stateFilingFee": float(fee)}
    print(f"[TOOL LOG] 🔎 stateFeeLookup (fallback) -> {out}")
    return json.dumps(out)

@function_tool
async def createPaymentLink(args: CreatePaymentLinkArgs) -> str:
    print(f"[TOOL LOG] 🔗 createPaymentLink called with args={args}")
    sess = CURRENT_SESSION.get()
    if not isinstance(sess, OpenAIConversationsSession):
        print("[TOOL LOG] 🔗 createPaymentLink -> no session")
        return "link_error:no_session"

    # ✅ ensure a conversation_id exists
    conv_id = getattr(sess, "conversation_id", None)
    if not conv_id:
        conv_id = str(uuid.uuid4())
        setattr(sess, "conversation_id", conv_id)

    quote = {
        "productName": args.get("productName"),
        "price": float(args.get("price", 0)),
        "billingCycle": args.get("billingCycle"),
        "stateFilingFee": float(args.get("stateFilingFee", 0)),
        "totalDueNow": float(args.get("totalDueNow", 0)),
    }

    setattr(sess, "awaiting_payment", True)
    setattr(sess, "payment_status", "pending")
    setattr(sess, "payment_quote", quote)

    # ✅ SAVE session attributes before creating payment link
    _save_session_attributes(conv_id, sess)

    checkout_url = None
    checkout_id = None

    try:
        SITE_URL = os.getenv("SITE_URL", "http://localhost:7860").rstrip("/")
        out = PaymentService.create_payment_link(
            product_name=quote["productName"],
            price=quote["price"],
            billing_cycle=quote["billingCycle"],
            state_fee=quote["stateFilingFee"],
            total_due_now=quote["totalDueNow"],
            session_id=conv_id,
            success_url=f"{SITE_URL}?conv_id={conv_id}&status=success&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{SITE_URL}?conv_id={conv_id}&status=cancel"
        )
        checkout_id = out.get("id")
        checkout_url = out.get("url")
        setattr(sess, "payment_checkout_url", checkout_url)
        setattr(sess, "payment_checkout_id", checkout_id)

        # ✅ SAVE again with the checkout details
        _save_session_attributes(conv_id, sess)

        print(f"[TOOL LOG] 🔗 Stripe Checkout created id={checkout_id} url={('…'+checkout_url[-24:]) if checkout_url else None}")
    except Exception as e:
        print("[TOOL LOG] 🔗 PaymentService error (non-fatal):", e)

    print(f"[TOOL LOG] 🔗 createPaymentLink -> awaiting_payment=True, status=pending, quote={quote}")
    return "link_created"

@function_tool
async def checkPaymentStatus(args: CheckPaymentStatusArgs) -> str:
    print(f"[TOOL LOG] 🧾 checkPaymentStatus called with args={args}")
    sess = CURRENT_SESSION.get()
    if not isinstance(sess, OpenAIConversationsSession):
        print("[TOOL LOG] 🧾 checkPaymentStatus -> unknown (no session)")
        return "unknown"

    status = PaymentService.check_payment_status(getattr(sess, "conversation_id", None))
    if status in ("completed", "pending", "failed"):
        setattr(sess, "payment_status", status)
        if status == "completed":
            setattr(sess, "awaiting_payment", False)
            setattr(sess, "show_payment_summary", True)  # trigger flag
        print(f"[TOOL LOG] 🧾 checkPaymentStatus (PaymentService) -> {status}")
        return status

    status = getattr(sess, "payment_status", None)
    norm = status if status in ("completed", "pending", "failed") else "unknown"
    print(f"[TOOL LOG] 🧾 checkPaymentStatus (session) -> {norm}")
    return norm


# ========= BACKGROUND EXTRACTION =========
def run_background_extraction(session_id: str, conversation_history: list):
    """Run extraction in background - NON-BLOCKING with overlap protection"""
    
    global _ACTIVE_EXTRACTIONS
    
    # Check if extraction is already running for this session
    if session_id in _ACTIVE_EXTRACTIONS:
        print(f"[EXTRACTOR] ⏸️ Extraction already running for {session_id} - skipping")
        return None
    
    def extract_worker():
        """Worker function that runs in separate thread"""
        try:
            # Mark as active
            _ACTIVE_EXTRACTIONS.add(session_id)
            print(f"[EXTRACTOR] 🔍 Starting extraction for {session_id} (Active: {len(_ACTIVE_EXTRACTIONS)})")
            
            # Extract only assistant messages
            assistant_messages = []
            for exchange in conversation_history:
                agent_response = exchange.get('agent_response', '').strip()
                agent_name = exchange.get('agent_name', 'Unknown Agent')
                timestamp = exchange.get('timestamp', 'Unknown time')
                if agent_response:
                    assistant_messages.append({
                        'response': agent_response,
                        'agent': agent_name,
                        'time': timestamp
                    })
            
            if not assistant_messages:
                print(f"[EXTRACTOR] ⚠️ No assistant messages for {session_id}")
                return
            
            # Prepare analysis text
            analysis_text = f"ASSISTANT RESPONSES FROM SESSION {session_id}:\n\n"
            for i, msg in enumerate(assistant_messages, 1):
                analysis_text += f"Response {i} ({msg['time']}) - {msg['agent']}:\n"
                analysis_text += f"{msg['response']}\n\n"
            
            analysis_text += "Please extract and analyze all key information from these assistant responses."
            
            # Create event loop (same pattern as your other agents)
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            try:
                print(f"[EXTRACTOR] 🤖 Analyzing {len(assistant_messages)} messages...")
                
                # Run extraction
                result = loop.run_until_complete(
                    Runner.run(extractor_agent, analysis_text)
                )
                
                # Print results to terminal
                extraction_result = (result.final_output or "").strip()
                import datetime
                print(f"\n" + "="*70)
                print(f"[EXTRACTOR] 📊 RESULTS - Session: {session_id}")
                print(f"="*70)
                print(f"Messages Analyzed: {len(assistant_messages)}")
                print(f"Completed: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"\n{extraction_result}")
                print(f"="*70 + "\n")
                
            finally:
                loop.close()
                
        except Exception as e:
            print(f"[EXTRACTOR] ❌ Error: {e}")
        finally:
            # Always remove from active set
            _ACTIVE_EXTRACTIONS.discard(session_id)
            print(f"[EXTRACTOR] ✅ Completed for {session_id} (Active: {len(_ACTIVE_EXTRACTIONS)})")
    
    # Submit to thread pool (NON-BLOCKING)
    with ThreadPoolExecutor() as executor:
        future = executor.submit(extract_worker)
        # No .result() call - returns immediately!
        print(f"[EXTRACTOR] 🚀 Queued extraction for {session_id}")
        return future


# ========= AGENTS =========
corp_agent = Agent(
    name="Corp Assistant",
    model="gpt-4o",
    instructions=(
        "🏷️ AGENT IDENTIFICATION: You are the Corporate Formation Assistant. "
        "Always start your responses with '[CORP AGENT]'.\n\n"
        + CorpPrompt.get_mode_prompt()
        + "\n\nRouting rules:\n"
        "- If the user explicitly asks to switch entity type only to (LLC), call `setEntityType` with that type.\n"
        "- Do not call the `setEntityType` if the switching is asked for entity type other than LLC.\n"
        "- Do not answer LLC-specific questions in Corp mode; switch with `setEntityType` when appropriate.\n"
        "- After the exact phrase __I Confirm__, call `updateToPaymentMode` to continue with payment."
    ),
    tools=[setEntityType, updateToPaymentMode]
)

llc_agent = Agent(
    name="LLC Assistant",
    model="gpt-4o",
    instructions=(
        "🏷️ AGENT IDENTIFICATION: You are the LLC Formation Assistant. "
        "Always start your responses with '[LLC AGENT]'.\n\n"
        + LLCPrompt.get_mode_prompt()
        + "\n\nRouting rules:\n"
        "- If the user explicitly asks to switch entity type only to (C-Corp or S-Corp) call `setEntityType` with that type.\n"
        "- Do not call the `setEntityType` if the switching is asked for entity type other than S-Corp or C-Corp.\n"
        "- Do not answer corporate-specific questions in LLC mode; switch with `setEntityType` when appropriate.\n"
        "- After the exact phrase __I Confirm__, call `updateToPaymentMode` to continue with payment."
    ),
    tools=[setEntityType, updateToPaymentMode]
)

payment_agent = Agent(
    name="Payment Assistant",
    model="gpt-4o",
    instructions=PaymentPrompt.getModePrompt(),
    tools=[stateFeeLookup, createPaymentLink, checkPaymentStatus, updateEntityType]
)

base_agent = Agent(
    name="Incubation AI (Base Assistant)",
    model="gpt-4o",
    instructions=(
        "🏷️ AGENT IDENTIFICATION: You are the Base Assistant. "
        "Always start your responses with '[BASE AGENT]'.\n\n"
        + BasePrompt.get_mode_prompt()
        + "\n\nRouting rules:\n"
        "- When the user chooses an entity type (LLC / C-CORP / S-CORP), call `setEntityType` with that type immediately.\n"
        "- Do NOT answer LLC- or Corp-specific questions here; ask to choose entity and set it via `setEntityType` first.\n\n"
        "CRITICAL OTP RULE:\n"
        "- When user provides full name, valid email, and exactly 10-digit phone number, you MUST call sendEmailOtp function immediately.\n"
        "- NEVER say you sent a code without actually calling the sendEmailOtp function.\n"
        "- Only after successfully calling sendEmailOtp should you tell the user the code was sent."
    ),
    tools=[sendEmailOtp, verifyEmailOtp, setEntityType]
)

extractor_agent = Agent(
    name="Conversation Extractor",
    model="gpt-4o",
    instructions=(
        "🏷️ AGENT IDENTIFICATION: You are the Conversation Extractor Agent. "
        "Always start your responses with '[EXTRACTOR AGENT]'.\n\n"
        + ExtractorPrompt.get_mode_prompt()
    ),
    tools=[]
)


# ========= ROUTER =========
def _agent_for_entity(entity_type: str):
    if entity_type == "LLC":
        print("[ROUTER LOG] → Selecting LLC Agent (entity_type=LLC)")
        return llc_agent, "LLC Agent"
    if entity_type in ("C-CORP", "S-CORP"):
        print(f"[ROUTER LOG] → Selecting Corp Agent (entity_type={entity_type})")
        return corp_agent, "Corp Agent"
    if entity_type == "PAYMENT":
        print("[ROUTER LOG] → Selecting Payment Agent (entity_type=PAYMENT)")
        return payment_agent, "Payment Agent"
    print("[ROUTER LOG] → Selecting Base Agent (entity_type=BASE)")
    return base_agent, "Base Agent"


# ========= UI HELPERS =========
def banner_for(session: Optional[OpenAIConversationsSession]) -> str:
    cid = getattr(session, "conversation_id", None)
    return f"**Conversation ID:** `{cid}` — keep this if you want to resume later." if cid else ""

def init_session() -> OpenAIConversationsSession:
    s = OpenAIConversationsSession()
    # ✅ always have a conv_id from the very first render
    if not getattr(s, "conversation_id", None):
        setattr(s, "conversation_id", str(uuid.uuid4()))
    setattr(s, "entity_type", "BASE")  # BASE | LLC | C-CORP | S-CORP | PAYMENT
    setattr(s, "awaiting_payment", False)
    setattr(s, "payment_status", None)
    # ✅ Add persistent conversation history array
    setattr(s, "conversation_history", [])
    print("[AGENT LOG] 🧭 init_session -> entity_type = BASE, conv_id =", getattr(s, "conversation_id"))
    return s


# ========= SESSION PERSISTENCE HELPERS =========
def _save_session_attributes(conv_id: str, session: OpenAIConversationsSession):
    """Save important session attributes to disk for restoration after payment."""
    if not conv_id:
        return
    
    # ✅ Store the actual session object in memory to preserve conversation history
    global _SESSION_STORE
    _SESSION_STORE[conv_id] = session
    
    try:
        session_data = {
            "entity_type": getattr(session, "entity_type", "BASE"),
            "awaiting_payment": getattr(session, "awaiting_payment", False),
            "payment_status": getattr(session, "payment_status", None),
            "payment_quote": getattr(session, "payment_quote", None),
            "payment_checkout_url": getattr(session, "payment_checkout_url", None),
            "payment_checkout_id": getattr(session, "payment_checkout_id", None),
            # ✅ Add conversation history to saved attributes
            "conversation_history": getattr(session, "conversation_history", []),
        }
        sessions_file = "payment_sessions.json"
        try:
            with open(sessions_file, 'r') as f:
                all_data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            all_data = {}
        if "session_attributes" not in all_data:
            all_data["session_attributes"] = {}
        all_data["session_attributes"][conv_id] = session_data
        with open(sessions_file, 'w') as f:
            json.dump(all_data, f, indent=2)
        print(f"[SESSION] 💾 Saved session object and attributes for {conv_id}")
    except Exception as e:
        print(f"[SESSION] ⚠️ Failed to save session attributes: {e}")

def _load_session_attributes(conv_id: str) -> Optional[Dict]:
    """Load session attributes from disk."""
    if not conv_id:
        return None
    try:
        sessions_file = "payment_sessions.json"
        with open(sessions_file, 'r') as f:
            all_data = json.load(f)
        session_attributes = all_data.get("session_attributes", {})
        return session_attributes.get(conv_id)
    except (FileNotFoundError, json.JSONDecodeError, Exception) as e:
        print(f"[SESSION] ⚠️ Could not load session attributes: {e}")
        return None

def _restore_or_create_session(conv_id: str) -> OpenAIConversationsSession:
    """
    Try to restore an existing OpenAI conversation session or create a new one
    with the same conversation_id to preserve context.
    """
    global _SESSION_STORE
    
    # ✅ First try to get the actual session object from memory
    if conv_id in _SESSION_STORE:
        session = _SESSION_STORE[conv_id]
        print(f"[SESSION] ✅ Restored actual session object for {conv_id}")
        return session
    
    # ✅ Fallback: create new session and restore attributes from disk
    try:
        session = OpenAIConversationsSession(conversation_id=conv_id)
        session_data = _load_session_attributes(conv_id)
        if session_data:
            for key, value in session_data.items():
                setattr(session, key, value)
            print(f"[SESSION] ✅ Restored session attributes for {conv_id}")
        else:
            setattr(session, "entity_type", "PAYMENT")
            setattr(session, "awaiting_payment", True)
            # ✅ Initialize conversation history if not present
            setattr(session, "conversation_history", [])
            print(f"[SESSION] 🆕 Created session with defaults for {conv_id}")
        
        # ✅ Ensure conversation_history exists even if not in saved data
        if not hasattr(session, "conversation_history"):
            setattr(session, "conversation_history", [])
        
        # Store in memory for future use
        _SESSION_STORE[conv_id] = session
        return session
    except Exception as e:
        print(f"[SESSION] ⚠️ Error restoring session {conv_id}: {e}")
        session = OpenAIConversationsSession(conversation_id=conv_id)
        setattr(session, "entity_type", "PAYMENT")
        setattr(session, "awaiting_payment", True)
        # ✅ Initialize conversation history
        setattr(session, "conversation_history", [])
        _SESSION_STORE[conv_id] = session
        return session


# ========= CONVERSATION HISTORY HELPERS =========
def get_conversation_history(session: Optional[OpenAIConversationsSession]) -> list:
    """Get the persistent conversation history array for the session."""
    if not isinstance(session, OpenAIConversationsSession):
        return []
    return getattr(session, "conversation_history", [])

def print_conversation_summary(session: Optional[OpenAIConversationsSession]):
    """Print a summary of the conversation history for debugging."""
    if not isinstance(session, OpenAIConversationsSession):
        print("[CONVERSATION] No active session")
        return
    
    history = getattr(session, "conversation_history", [])
    conv_id = getattr(session, "conversation_id", "Unknown")
    
    print(f"[CONVERSATION] 📊 Summary for session {conv_id}:")
    print(f"[CONVERSATION] Total exchanges: {len(history)}")
    
    for i, exchange in enumerate(history, 1):
        timestamp = exchange.get("timestamp", "Unknown time")
        agent_name = exchange.get("agent_name", "Unknown agent")
        user_msg_preview = exchange.get("user_message", "")[:50] + "..." if len(exchange.get("user_message", "")) > 50 else exchange.get("user_message", "")
        agent_msg_preview = exchange.get("agent_response", "")[:50] + "..." if len(exchange.get("agent_response", "")) > 50 else exchange.get("agent_response", "")
        
        print(f"[CONVERSATION] {i}. {timestamp} - {agent_name}")
        print(f"[CONVERSATION]    User: {user_msg_preview}")
        print(f"[CONVERSATION]    Agent: {agent_msg_preview}")

def get_conversation_count(session: Optional[OpenAIConversationsSession]) -> int:
    """Get the total number of conversation exchanges."""
    if not isinstance(session, OpenAIConversationsSession):
        return 0
    history = getattr(session, "conversation_history", [])
    return len(history)

def clear_conversation_history(session: Optional[OpenAIConversationsSession]) -> bool:
    """Clear the conversation history for a session."""
    if not isinstance(session, OpenAIConversationsSession):
        return False
    
    setattr(session, "conversation_history", [])
    conv_id = getattr(session, "conversation_id", None)
    if conv_id:
        _save_session_attributes(conv_id, session)
    
    print(f"[CONVERSATION] 🗑️ Cleared conversation history for session {conv_id}")
    return True

def _preload_sessions_from_disk():
    """Load all saved sessions into memory on app startup to handle restarts."""
    global _SESSION_STORE
    try:
        sessions_file = "payment_sessions.json"
        with open(sessions_file, 'r') as f:
            all_data = json.load(f)
        
        session_attributes = all_data.get("session_attributes", {})
        loaded_count = 0
        
        for conv_id in session_attributes.keys():
            if conv_id not in _SESSION_STORE:
                try:
                    # This will automatically load from disk and add to _SESSION_STORE
                    _restore_or_create_session(conv_id)
                    loaded_count += 1
                    print(f"[STARTUP] 📥 Preloaded session {conv_id}")
                except Exception as e:
                    print(f"[STARTUP] ⚠️ Failed to preload session {conv_id}: {e}")
        
        print(f"[STARTUP] 🚀 Preloaded {loaded_count} sessions from disk into global store")
    except (FileNotFoundError, json.JSONDecodeError):
        print("[STARTUP] 📝 No existing sessions file found")
    except Exception as e:
        print(f"[STARTUP] ⚠️ Error preloading sessions: {e}")


# ========= SUMMARY TRIGGER =========
def _run_payment_completed_summary(session: OpenAIConversationsSession) -> str:
    """Run the Payment Agent (same prompt/tools/session) to show a congratulatory full summary."""
    import asyncio, concurrent.futures
    instruction = (
        "SYSTEM_TRIGGER:PAYMENT_CONFIRMED\n"
        "Payment has been completed. Congratulate the user warmly and present a complete order summary:\n"
        "- Plan name and billing cycle\n"
        "- State and entity type\n"
        "- State filing fee, platform/plan price, and total paid\n"
        "- What happens next, receipts, timelines\n"
        "Be concise, professional, and friendly."
    )
    def run_agent():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            token = CURRENT_SESSION.set(session)
            try:
                print("[RUN LOG] ▶ Running Payment Agent for final summary")
                result = loop.run_until_complete(Runner.run(payment_agent, instruction, session=session))
            finally:
                CURRENT_SESSION.reset(token)
            print("[RUN LOG] ✅ Payment Agent summary generated")
            return (result.final_output or "").strip()
        finally:
            loop.close()
    with concurrent.futures.ThreadPoolExecutor() as ex:
        return ex.submit(run_agent).result(timeout=120)


# ========= HANDLERS =========
def on_load():
    session = init_session()
    hello = (
        "Hello and welcome! I'm Incubation AI — here to help you turn your business idea into a registered reality.\n\n"
        "What's needed next: Please share your **full legal name**, **email address**, and **primary phone number** "
        "so we can set up your secure account and get you moving toward launch."
    )
    chat = [{"role": "assistant", "content": hello}]
    print("[UI LOG] on_load -> Base Assistant greeting sent")
    return (
        chat,
        session,
        banner_for(session),
        gr.update(interactive=False),
        gr.update(interactive=False),
        gr.update(visible=False, value=""),
    )

def start_or_resume(conv_id: str, session: Optional[OpenAIConversationsSession]):
    if isinstance(session, OpenAIConversationsSession):
        info = "A conversation is already active. To resume another ID, click **End Session** first."
        print("[UI LOG] start_or_resume -> session already active; ignoring new conv_id")
        return (
            [{"role": "assistant", "content": info}], session, banner_for(session),
            gr.update(), gr.update(),
            gr.update(visible=False, value=""),
        )

    session = (
        OpenAIConversationsSession(conversation_id=conv_id.strip())
        if conv_id and conv_id.strip()
        else init_session()
    )
    if not hasattr(session, "entity_type"):
        session.entity_type = "BASE"

    msg = "Resumed your conversation. Welcome back! 🎉" if conv_id.strip() else "Started a new conversation."
    print(f"[UI LOG] start_or_resume -> {msg} | entity_type={session.entity_type} | conv_id={getattr(session,'conversation_id',None)}")
    chat = [{"role": "assistant", "content": f"{msg}\n\n{banner_for(session)}"}]
    return (
        chat, session, banner_for(session),
        gr.update(interactive=False), gr.update(interactive=False),
        gr.update(visible=False, value=""),
    )

def respond(message: str, history, session: Optional[OpenAIConversationsSession]):
    import asyncio, concurrent.futures

    if not isinstance(session, OpenAIConversationsSession):
        print("[UI LOG] respond -> no active session")
        history = history + [
            {"role": "user", "content": message},
            {"role": "assistant", "content": "Session ended. Click **Start / Resume** to begin, then paste a Conversation ID if you want to resume."}
        ]
        return history, session, banner_for(session), gr.update(), gr.update(), gr.update(visible=False, value="")

    if not hasattr(session, "entity_type"):
        session.entity_type = "BASE"
    
    # ✅ Initialize conversation_history if not present
    if not hasattr(session, "conversation_history"):
        setattr(session, "conversation_history", [])

    lower_msg = (message or "").lower().strip()
    payment_return_substrings = [
        "i'm back", "back", "done", "completed", "paid",
        "finished payment", "payment done", "just paid",
        "payment successful", "payment complete",
        "i have paid", "i've paid", "i have done my payment",
        "payment finished", "payment completed"
    ]
    if (
        any(p in lower_msg for p in payment_return_substrings)
        and getattr(session, "awaiting_payment", False)
        and session.entity_type == "PAYMENT"
    ):
        print("[UI LOG] 🔍 Auto-triggering payment status check...")
        message = "Please check my payment status"
    elif (
        any(p in lower_msg for p in ["check payment", "payment status", "verify payment"])
        and getattr(session, "awaiting_payment", False)
        and session.entity_type == "PAYMENT"
    ):
        print("[UI LOG] 🔍 Payment status check requested...")
        message = "Please check my payment status"

    current_agent, agent_name = _agent_for_entity(session.entity_type)
    print(f"[RUN LOG] ▶ Routing message to {agent_name} | entity_type={session.entity_type}")
    print(f"[RUN LOG] 📨 User message (first 120): {message[:120]!r}")

    try:
        def run_agent():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                token = CURRENT_SESSION.set(session)
                try:
                    print(f"[RUN LOG] 🔧 Runner.run({agent_name}) starting…")
                    print(f"[DEBUG] Available tools: {[getattr(tool, 'name', str(tool)) for tool in current_agent.tools] if hasattr(current_agent, 'tools') else 'No tools'}")
                    result = loop.run_until_complete(Runner.run(current_agent, message, session=session))
                    print(f"[DEBUG] Agent result: {result}")
                finally:
                    CURRENT_SESSION.reset(token)
                print(f"[RUN LOG] ✅ Runner.run({agent_name}) finished")
                return result
            finally:
                loop.close()

        with concurrent.futures.ThreadPoolExecutor() as executor:
            result = executor.submit(run_agent).result(timeout=120)
            response_content = (result.final_output or "").strip()
            print(f"[RUN LOG] 💬 {agent_name} response (first 160): {response_content[:160]!r}")

    except Exception as e:
        response_content = f"I encountered an error processing your message. Please try again. Error: {str(e)[:120]}..."
        print(f"[RUN LOG] ❌ Exception in respond: {e!r}")

    # ✅ Add conversation exchange to persistent history array
    import datetime
    conversation_exchange = {
        "timestamp": datetime.datetime.now().isoformat(),
        "user_message": message,
        "agent_response": response_content,
        "agent_type": session.entity_type,
        "agent_name": agent_name
    }
    
    # Get current conversation history
    conversation_history = getattr(session, "conversation_history", [])
    conversation_history.append(conversation_exchange)
    setattr(session, "conversation_history", conversation_history)
    
    print(f"[CONVERSATION] 📝 Added exchange to history. Total exchanges: {len(conversation_history)}")
    
    # ✅ Save session attributes to persist the conversation history
    conv_id = getattr(session, "conversation_id", None)
    if conv_id:
        _save_session_attributes(conv_id, session)

    # 🆕 Trigger background extraction (NON-BLOCKING)
    if conv_id and len(conversation_history) > 0:
        run_background_extraction(conv_id, conversation_history)

    if session.entity_type == "PAYMENT" and getattr(session, "show_payment_summary", False):
        try:
            summary = _run_payment_completed_summary(session)
            response_content = (response_content + "\n\n" + summary).strip() if response_content else summary
        finally:
            setattr(session, "show_payment_summary", False)

    checkout_url = getattr(session, "payment_checkout_url", None)
    if response_content.strip().startswith("_Your secure payment gateway is now open.") and checkout_url:
        import re
        match = re.search(r'Total due now: \$?([0-9.,]+).*Plan: ([^—]+)—([^+]+)\+.*State filing fees: \$?([0-9.,]+)', response_content)
        if match:
            total_due = match.group(1)
            plan_name = match.group(2).strip()
            billing_cycle = match.group(3).strip()
            state_fee = match.group(4)
            response_content = f"""🔗 **Your secure payment link is ready!**

**Total Due Now: ${total_due}**
- Plan: {plan_name} — {billing_cycle}
- State filing fees: ${state_fee}

**Click here to complete your payment:**
{checkout_url}

Once you complete payment, return here and I'll automatically verify your payment status."""
            print(f"[UI] 🔗 Payment trigger converted to full response with URL")
        else:
            response_content = response_content + f"\n\n**Payment Link:** {checkout_url}"
            print(f"[UI] 🔗 Payment URL appended to response")

    panel_update = gr.update(visible=False, value="")

    history = history + [
        {"role": "user", "content": message},
        {"role": "assistant", "content": response_content}
    ]
    return (
        history, session, banner_for(session),
        gr.update(interactive=False), gr.update(interactive=False),
        panel_update
    )

def process_url_params(qs: str, session: Optional[OpenAIConversationsSession], chat):
    """(Legacy) Handle Stripe redirects; kept for compatibility if you wire it up."""
    from urllib.parse import parse_qs
    try:
        params = parse_qs((qs or "").lstrip("?"))
        conv_id = (
            (params.get("conv_id") or params.get("conversation_id") or params.get("conv") or params.get("cid") or [None])
        )[0]
        status_hint = (
            (params.get("status") or params.get("payment_status") or params.get("payment") or [None])
        )[0]
        checkout_id = (
            (params.get("checkout_session") or params.get("session_id") or params.get("cs") or [None])
        )[0]

        if not conv_id:
            return chat, session, banner_for(session), gr.update(), gr.update(), gr.update(visible=False, value="")

        if not isinstance(session, OpenAIConversationsSession) or getattr(session, "conversation_id", None) != conv_id:
            session = OpenAIConversationsSession(conversation_id=conv_id)

        setattr(session, "entity_type", "PAYMENT")
        setattr(session, "awaiting_payment", True)

        if checkout_id:
            setattr(session, "payment_checkout_id", checkout_id)
            try:
                PaymentService._store_checkout_session_id(conv_id, checkout_id)
            except Exception as e:
                print("[UI LOG] process_url_params: failed to persist mapping:", e)

        st = PaymentService.check_payment_status(conv_id)
        setattr(session, "payment_status", st)

        if st == "completed":
            setattr(session, "awaiting_payment", False)
            summary = _run_payment_completed_summary(session)
            new_msg_block = summary
        elif st == "pending":
            new_msg_block = "[PAYMENT AGENT]\n\nℹ️ Your payment is still pending confirmation. If you just paid, this can take a moment."
        else:
            new_msg_block = "[PAYMENT AGENT]\n\n❌ Payment not completed. You can try the link again from your conversation."

        chat = (chat or []) + [{"role": "assistant", "content": new_msg_block}]
        print(f"[UI LOG] process_url_params -> conv_id={conv_id}, inferred_status={st}, status_hint={status_hint}")

        return (
            chat, session, banner_for(session),
            gr.update(interactive=False), gr.update(interactive=False),
            gr.update(visible=False, value="")
        )
    except Exception as e:
        print("[UI LOG] process_url_params error:", e)
        return chat, session, banner_for(session), gr.update(), gr.update(), gr.update(visible=False, value="")

def boot(qs: str = "", stored_cid: str = ""):
    """
    Single entry on first paint.
    If returning from Stripe or refresh, resume the SAME conversation_id
    (from URL or localStorage) in PAYMENT agent and show summary.
    Otherwise, start fresh.
    """
    from urllib.parse import parse_qs

    print(f"[BOOT] 🔄 boot() called with qs='{qs}', stored_cid='{stored_cid}'")

    params = parse_qs((qs or "").lstrip("?"))
    conv_id = (
        (params.get("conv_id") or params.get("conversation_id") or
         params.get("conv")    or params.get("cid") or [None])
    )[0]
    status_hint = (
        (params.get("status") or params.get("payment_status") or
         params.get("payment") or [None])
    )[0]
    checkout_id = (
        (params.get("checkout_session") or params.get("session_id") or
         params.get("cs") or [None])
    )[0]

    # ✅ Fallback to localStorage value if URL doesn't have conv_id
    # Handle case where stored_cid might be string 'None' or empty
    if not conv_id and stored_cid and stored_cid != 'None' and stored_cid.strip():
        conv_id = stored_cid
        print(f"[BOOT] 💾 Using stored conv_id from localStorage: {conv_id}")

    # First-time load (no URL params and no stored cid): create initial session
    if not conv_id:
        print("[BOOT] 🆕 First load - creating initial session")
        session = init_session()
        hello = (
            "Hello and welcome! I'm Incubation AI — here to help you turn your business idea into a registered reality.\n\n"
            "What's needed next: Please share your **full legal name**, **email address**, and **primary phone number** "
            "so we can set up your secure account and get you moving toward launch."
        )
        chat = [{"role": "assistant", "content": hello}]
        return (
            chat, session, banner_for(session),
            gr.update(interactive=False), gr.update(interactive=False),
            gr.update(visible=False, value=""),
            getattr(session, "conversation_id", "")  # return conv_id for localStorage
        )

    # Stripe/refresh return — restore existing session
    session = _restore_or_create_session(conv_id)

    # Ensure we're in Payment mode for status checking
    setattr(session, "entity_type", "PAYMENT")
    setattr(session, "awaiting_payment", True)
    if checkout_id:
        setattr(session, "payment_checkout_id", checkout_id)
        try:
            PaymentService._store_checkout_session_id(conv_id, checkout_id)
        except Exception as e:
            print("[BOOT] ⚠️ could not persist checkout mapping:", e)

    # Check payment now and show the right message immediately
    st = PaymentService.check_payment_status(conv_id)
    setattr(session, "payment_status", st)

    if st == "completed":
        setattr(session, "awaiting_payment", False)
        summary = _run_payment_completed_summary(session)
        chat = [{"role": "assistant", "content": summary}]
    elif st == "pending":
        chat = [{"role": "assistant", "content":
                "[PAYMENT AGENT]\n\nℹ️ Your payment is still pending confirmation. If you just paid, this can take a moment."}]
    else:
        chat = [{"role": "assistant", "content":
                "[PAYMENT AGENT]\n\n❌ Payment not completed. You can try the link again from your conversation."}]

    print(f"[BOOT] conv_id={conv_id} status_hint={status_hint} inferred={st}")
    return (
        chat, session, banner_for(session),
        gr.update(interactive=False), gr.update(interactive=False),
        gr.update(visible=False, value=""),
        conv_id  # return conv_id for localStorage
    )


def end_session(history, session: Optional[OpenAIConversationsSession]):
    global _SESSION_STORE
    
    # ✅ Clear the stored session from memory
    if isinstance(session, OpenAIConversationsSession):
        conv_id = getattr(session, "conversation_id", None)
        if conv_id and conv_id in _SESSION_STORE:
            del _SESSION_STORE[conv_id]
            print(f"[SESSION] 🗑️ Cleared stored session for {conv_id}")
    
    end_note = "Session ended. You can now **paste a Conversation ID** (optional) and press **Start / Resume**."
    print("[UI LOG] 🛑 end_session -> dropping session & enabling Start/Resume inputs")
    return (
        [{"role": "assistant", "content": end_note}],
        None,
        "",
        gr.update(interactive=True),
        gr.update(interactive=True),
        gr.update(visible=False, value=""),
    )


# ========= BUILD UI =========
with gr.Blocks(theme=gr.themes.Soft()) as demo:
    assert os.getenv("OPENAI_API_KEY"), "Set OPENAI_API_KEY."

    gr.Markdown("## Incubation AI — Multi-User (OpenAI Conversations Memory) — Base ↔ LLC ↔ Corp ↔ Payment")
    conv_banner = gr.Markdown("")

    with gr.Row():
        conv_id_in = gr.Textbox(
            label="Conversation ID (optional; enabled only after End Session)",
            placeholder="Paste here to resume AFTER ending the current session…",
        )
        start_btn = gr.Button("Start / Resume")

    chat = gr.Chatbot(height=520, type='messages')
    msg = gr.Textbox(placeholder="Type your message…", scale=1)

    with gr.Row():
        end_btn = gr.Button("End Session", variant="stop")
        clear_btn = gr.Button("Clear Chat (keep session)")

    pay_panel = gr.HTML(visible=False, value="")

    st_session = gr.State()

    # Local storage plumbed through hidden components
    LOCAL_KEY = "incubation_conv_id"
    conv_id_out = gr.Textbox(visible=False)  # Python -> Browser (to store)
    qs_in = gr.State()                       # Browser -> Python (query string)
    stored_cid_in = gr.State()               # Browser -> Python (localStorage value)

    # Single loader: pass URL + stored cid; receive conv_id to write to localStorage
    demo.load(
        fn=boot,
        inputs=[qs_in, stored_cid_in],
        outputs=[chat, st_session, conv_banner, conv_id_in, start_btn, pay_panel, conv_id_out],
        js=f"""() => {{
            const qs = window.location.search;
            const stored = localStorage.getItem('{LOCAL_KEY}') || '';
            return [qs, stored];
        }}"""
    )

    # When Python sends back a conv_id, write it to localStorage
    conv_id_out.change(
        fn=lambda cid: None,
        inputs=[conv_id_out],
        outputs=[],
        js=f"(cid) => {{ if (cid) localStorage.setItem('{LOCAL_KEY}', cid); }}"
    )

    start_btn.click(fn=start_or_resume, inputs=[conv_id_in, st_session],
                    outputs=[chat, st_session, conv_banner, conv_id_in, start_btn, pay_panel])

    msg.submit(fn=respond, inputs=[msg, chat, st_session],
               outputs=[chat, st_session, conv_banner, conv_id_in, start_btn, pay_panel])
    msg.submit(lambda: "", None, msg)

    def clear_chat():
        print("[UI LOG] 🧹 clear_chat -> cleared visible chat, session state preserved")
        return []

    clear_btn.click(fn=clear_chat, outputs=[chat])

    # Clear localStorage on End Session too
    end_btn.click(
        fn=end_session,
        inputs=[chat, st_session],
        outputs=[chat, st_session, conv_banner, conv_id_in, start_btn, pay_panel],
        js=f"() => localStorage.removeItem('{LOCAL_KEY}')"
    )

if __name__ == "__main__":
    print("🌐 SITE_URL:", os.getenv("SITE_URL"))
    # ✅ Preload existing sessions from disk on startup to handle app restarts
    _preload_sessions_from_disk()
    demo.queue().launch()
