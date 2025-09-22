# gradio_app_conversations_multi.py
import os
import json
import contextvars
from typing import TypedDict, Optional, Literal

# Bootstrap env (OpenAI, SendGrid, Stripe, SITE_URL, etc.)
import config  # side-effect: sets env on import

import gradio as gr
from agents import Agent, Runner, function_tool, OpenAIConversationsSession

from base_prompt import BasePrompt
from llc_prompt import LLCPrompt
from corp_prompt import CorpPrompt
from payment_prompt import PaymentPrompt
from otp_service import OTPService

# Use the real PaymentService
from payment_service import PaymentService


# ========= GLOBAL CONTEXT =========
CURRENT_SESSION = contextvars.ContextVar("CURRENT_SESSION", default=None)


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

# Fallback fees (same as your table)
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
    'Louisiana':      {'llc': 100, 's-corp': 75,  'c-corp': 75 },
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
    'Rhode Island':   {'llc': 150, 's-corp': 230, 'c-corp': 230},
    'South Carolina': {'llc': 110, 's-corp': 125, 'c-corp': 125},
    'South Dakota':   {'llc': 150, 's-corp': 150, 'c-corp': 150},
    'Tennessee':      {'llc': 300, 's-corp': 100,  'c-corp': 100},
    'Texas':          {'llc': 300, 's-corp': 300,  'c-corp': 300},
    'Utah':           {'llc': 70,  's-corp': 70,  'c-corp': 70 },
    'Vermont':        {'llc': 125, 's-corp': 125, 'c-corp': 125},
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

    checkout_url = None
    checkout_id = None

    try:
        conv_id = getattr(sess, "conversation_id", None)
        out = PaymentService.create_payment_link(
            product_name=quote["productName"],
            price=quote["price"],
            billing_cycle=quote["billingCycle"],
            state_fee=quote["stateFilingFee"],
            total_due_now=quote["totalDueNow"],
            session_id=conv_id,
        )
        checkout_id = out.get("id")
        checkout_url = out.get("url")
        setattr(sess, "payment_checkout_url", checkout_url)
        setattr(sess, "payment_checkout_id", checkout_id)
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
        print(f"[TOOL LOG] 🧾 checkPaymentStatus (PaymentService) -> {status}")
        return status

    status = getattr(sess, "payment_status", None)
    norm = status if status in ("completed", "pending", "failed") else "unknown"
    print(f"[TOOL LOG] 🧾 checkPaymentStatus (session) -> {norm}")
    return norm


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
        "- Do NOT answer LLC- or Corp-specific questions here; ask to choose entity and set it via `setEntityType` first."
    ),
    tools=[sendEmailOtp, verifyEmailOtp, setEntityType]
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
    setattr(s, "entity_type", "BASE")  # BASE | LLC | C-CORP | S-CORP | PAYMENT
    setattr(s, "awaiting_payment", False)
    setattr(s, "payment_status", None)
    print("[AGENT LOG] 🧭 init_session -> entity_type = BASE")
    return s


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
        gr.update(visible=False, value=""),  # pay_panel (unused)
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

    # ---- PATCH: broaden detection so *any* phrasing like "I have done my payment" triggers a status check
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
                    result = loop.run_until_complete(Runner.run(current_agent, message, session=session))
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

    # ---- PATCH: put the actual Checkout URL directly into the chat when it exists
    checkout_url = getattr(session, "payment_checkout_url", None)

    # Case A: Payment agent has just said the gateway is open → append the URL
    popup_triggered = response_content.strip().startswith("_Your secure payment gateway is now open.")
    # Case B: User explicitly asks to show the link later
    user_wants_link = any(
        phrase in lower_msg
        for phrase in [
            "show me the link", "payment link", "checkout link", "stripe link",
            "open payment", "open the payment", "show link", "show me link", "link please", "give me the link"
        ]
    )

    if (popup_triggered or user_wants_link) and checkout_url:
        # Only add once
        if "Secure payment link:" not in response_content:
            response_content += f"\n\n**Secure payment link:** {checkout_url}"
        print("[UI] 🔗 Payment link appended to chat text")

    # (Optional placeholder replacement if your prompt includes it)
    if checkout_url and "[CHECKOUT_URL_FROM_SESSION]" in response_content:
        response_content = response_content.replace("[CHECKOUT_URL_FROM_SESSION]", checkout_url)
        print(f"[UI] 🔗 Payment URL inserted via placeholder")

    # No popup panel; we keep UI simple (chat only)
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

def end_session(history, session: Optional[OpenAIConversationsSession]):
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

    # Simple HTML block reserved (not used in this patch, kept for compatibility)
    pay_panel = gr.HTML(visible=False, value="")

    st_session = gr.State()

    # Wire events (include pay_panel in outputs)
    demo.load(fn=on_load, outputs=[chat, st_session, conv_banner, conv_id_in, start_btn, pay_panel])
    start_btn.click(fn=start_or_resume, inputs=[conv_id_in, st_session],
                    outputs=[chat, st_session, conv_banner, conv_id_in, start_btn, pay_panel])
    msg.submit(fn=respond, inputs=[msg, chat, st_session],
               outputs=[chat, st_session, conv_banner, conv_id_in, start_btn, pay_panel])
    msg.submit(lambda: "", None, msg)

    def clear_chat():
        print("[UI LOG] 🧹 clear_chat -> cleared visible chat, session state preserved")
        return []

    clear_btn.click(fn=clear_chat, outputs=[chat])
    end_btn.click(fn=end_session, inputs=[chat, st_session],
                  outputs=[chat, st_session, conv_banner, conv_id_in, start_btn, pay_panel])

if __name__ == "__main__":
    print("🌐 SITE_URL:", os.getenv("SITE_URL"))
    demo.queue().launch()
