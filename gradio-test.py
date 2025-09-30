# gradio_app_conversations_multi.py
import os
import json
import uuid
import contextvars
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import TypedDict, Optional, Literal, Dict
from pydantic import BaseModel

# Bootstrap env (OpenAI, SendGrid, Stripe, SITE_URL, etc.)
import config  # side-effect: sets env on import

import gradio as gr
from agents import (
    Agent, 
    Runner, 
    function_tool, 
    OpenAIConversationsSession,
    GuardrailFunctionOutput,
    InputGuardrailTripwireTriggered,
    RunContextWrapper,
    TResponseInputItem,
    input_guardrail,
)

from base_prompt import BasePrompt
from llc_prompt import LLCPrompt
from corp_prompt import CorpPrompt
from payment_prompt import PaymentPrompt
from extractor_prompt import ExtractorPrompt
from otp_service import OTPService

# Use the real PaymentService
from payment_service import PaymentService


# ========= ENTITY SWITCH GUARDRAIL MODELS =========

class EntitySwitchRequestOutput(BaseModel):
    is_entity_switch_request: bool
    requested_entity_type: Optional[str]
    reasoning: str
    switch_allowed: bool  # Result after checking count

class CheckEntitySwitchCountArgs(TypedDict):
    current_entity_type: str
    requested_entity_type: str

@function_tool
async def checkEntitySwitchCount(args: CheckEntitySwitchCountArgs) -> str:
    """Check if entity switch is allowed based on current switch count and required fields"""
    try:
        sess = CURRENT_SESSION.get()
        if not isinstance(sess, OpenAIConversationsSession):
            print(f"[SWITCH COUNT TOOL] ❌ No valid session found")
            return "false"
        
        current_entity = args.get("current_entity_type", "BASE")
        requested_entity = args.get("requested_entity_type", "BASE")
        
        # If current_entity is empty or "CURRENT", get it from session
        if not current_entity or current_entity == "CURRENT":
            current_entity = getattr(sess, "entity_type", "BASE")
        current_switches = getattr(sess, "entity_switch_count", 0)
        
        print(f"[SWITCH COUNT TOOL] Check: {current_entity} → {requested_entity}, current_switches: {current_switches}")
        
        # Check if this is actually a switch (not initial setting from BASE)
        # inside checkEntitySwitchCount(...)
        if current_entity != "BASE" and current_entity != requested_entity:
                if current_switches >= ENTITY_SWITCH_LIMIT:
                    print(f"[SWITCH COUNT TOOL] ⛔ Switch blocked - limit exceeded ({current_switches}/{ENTITY_SWITCH_LIMIT})")
                    return "false"
                print(f"[SWITCH COUNT TOOL] ✅ Switch allowed (no increment in this tool)")
                return "true" 
        # Not a real switch (initial selection or same entity) - but still check required fields
        if current_entity == "BASE":
            print(f"[SWITCH COUNT TOOL] ✅ Initial entity selection allowed - all required fields present")
        else:
            print(f"[SWITCH COUNT TOOL] ✅ Not a switch (same entity) - allowed")
        return "true"
        
    except Exception as e:
        print(f"[SWITCH COUNT TOOL] ❌ Error in tool: {e}")
        return "false"

entity_switch_guardrail_agent = Agent(
    name="Entity Switch Guardrail Check",
    instructions = (
        "Analyze the user's message to determine if they are requesting to switch entity types.\n\n"
        
        "## TRIGGER CONDITIONS (Return TRUE only if user clearly expresses):\n"
        "- Direct switch requests: 'switch to LLC', 'change to C-Corp', 'I want S-Corp instead'\n"
        "- Entity change requests: 'let's do LLC', 'make it a corporation', 'change entity to'\n"
        "- Comparison requests that imply switching: 'what about LLC instead', 'can we do C-Corp'\n\n"
        
        "## ENTITY TYPES TO DETECT:\n"
        "- LLC, L.L.C., Limited Liability Company\n"
        "- C-Corp, C-Corporation, C Corporation, Corporation\n" 
        "- S-Corp, S-Corporation, S Corporation\n\n"
        
        "## DO NOT TRIGGER FOR:\n"
        "✗ Questions about entity types: 'what is an LLC?', 'how does C-Corp work?'\n"
        "✗ General information requests: 'tell me about corporations'\n"
        "✗ Formation process questions: 'what's next for my LLC?'\n"
        "✗ Business names containing entity words: 'LLC Solutions Inc'\n"
        "✗ Designator selections: 'I choose LLC as designator'\n\n"
        
        "## DECISION CRITERIA:\n"
        "1. The request must be about SWITCHING/CHANGING the current entity type\n"
        "2. The intent must be EXPLICIT and CLEAR\n"
        "3. Must identify the target entity type they want to switch TO\n"
        "4. Be conservative - when in doubt, DO NOT trigger\n\n"
        
        "## SWITCH VALIDATION PROCESS:\n"
        "If you detect an entity switch request:\n"
        "1. Extract and normalize the target entity type to: LLC, C-CORP, or S-CORP\n"
        "2. Call checkEntitySwitchCount tool with:\n"
        "   - current_entity_type: 'CURRENT' (tool will get actual current type from session)\n"
        "   - requested_entity_type: the entity type they want to switch to\n"
        "3. The tool will:\n"
        "   - Check if switch count limit is exceeded\n"
        "   - Increment count if switch is allowed\n"
        "   - Return 'true' if allowed, 'false' if blocked\n"
        "4. Set switch_allowed = true if tool returned 'true', switch_allowed = false if tool returned 'false'\n"
        "5. CRITICAL: Always call the tool - it handles all count logic and returns simple boolean result"
    ),
    output_type=EntitySwitchRequestOutput,
    tools=[checkEntitySwitchCount]
)

# ========= RESTART GUARDRAIL MODELS =========

def _latest_user_text(inp) -> str:
    if isinstance(inp, str):
        return inp or ""
    # inp can be list[TResponseInputItem] or list[dict]
    for item in reversed(inp or []):
        role = (getattr(item, "role", None) or (isinstance(item, dict) and item.get("role")) or "").lower()
        if role == "user":
            return (getattr(item, "content", None) or (isinstance(item, dict) and item.get("content")) or "").strip()
    # fallback: last item content
    last = (inp or [None])[-1]
    return ((getattr(last, "content", None) or (isinstance(last, dict) and last.get("content")) or "") if last else "").strip()


class RestartRequestOutput(BaseModel):
    is_restart_request: bool
    reasoning: str

restart_guardrail_agent = Agent(
    name="Restart Guardrail Check",
    instructions = (
    "Analyze the user's message to determine if they are EXPLICITLY requesting to restart, "
    "start fresh, or begin the entire entity formation process from the beginning.\n\n"
    
    "## TRIGGER CONDITIONS (Return TRUE only if user clearly expresses):\n"
    "- Direct restart requests: 'restart', 'start over', 'begin again', 'start from scratch'\n"
    "- Fresh start requests: 'start fresh', 'fresh start', 'start anew'\n"
    "- Reset requests: 'reset everything', 'reset the process', 'clear and restart'\n"
    "- Complete restart phrases: 'start the entire process again', 'go back to the beginning'\n"
    "- Abandonment + restart: 'forget everything and start over', 'discard this and restart'\n\n"
    
    "## EXPLICIT EXAMPLES TO TRIGGER:\n"
    "✓ 'I want to start fresh'\n"
    "✓ 'Let's start over'\n"
    "✓ 'Can we restart this process?'\n"
    "✓ 'I need to begin again'\n"
    "✓ 'Start everything fresh'\n"
    "✓ 'Reset and start from the beginning'\n"
    "✓ 'Let's restart the formation process'\n"
    "✓ 'Can I start from scratch?'\n"
    "✓ 'I'd like to redo everything'\n\n"
    
    "## DO NOT TRIGGER FOR:\n"
    "✗ Simple acknowledgments: 'ok', 'yes', 'no', 'sure', 'thanks'\n"
    "✗ Personal information: names, emails, phone numbers, addresses\n"
    "✗ Business information: company names, EIN numbers, business details\n"
    "✗ Questions about the process: 'how do I start?', 'what's next?'\n"
    "✗ Continuation phrases: 'let's continue', 'proceed', 'next step'\n"
    "✗ Partial word matches in names: 'Restart LLC', 'Fresh Foods Inc'\n"
    "✗ Editing requests: 'change my name', 'update my email'\n"
    "✗ Starting a specific section: 'start the payment', 'begin filing'\n"
    "✗ Questions containing keywords: 'when can I start my business?'\n\n"
    
    "## DECISION CRITERIA:\n"
    "1. The request must be about restarting the ENTIRE formation process\n"
    "2. The intent must be EXPLICIT and UNAMBIGUOUS\n"
    "3. The message must clearly indicate discarding current progress\n"
    "4. Be VERY conservative - when in doubt, DO NOT trigger\n"
    "5. Ignore keyword matches in business names, personal names, or other contextual data\n\n"
    
    "Return TRUE only when there is crystal-clear, explicit intent to restart the complete "
    "entity formation process from the beginning."
),
    output_type=RestartRequestOutput,
)

@input_guardrail
async def entity_switch_guardrail(
    ctx: RunContextWrapper[None], agent: Agent, input: str | list[TResponseInputItem]
) -> GuardrailFunctionOutput:
    """Check if user is requesting an entity switch and validate against switch limit."""
    text = _latest_user_text(input)
    if not text:
        out = EntitySwitchRequestOutput(
            is_entity_switch_request=False,
            requested_entity_type=None,
            reasoning="No user text found.",
            switch_allowed=True
        )
        return GuardrailFunctionOutput(output_info=out, tripwire_triggered=False)

    # Use the guardrail agent to dynamically analyze the user input
    try:
        result = await Runner.run(entity_switch_guardrail_agent, text)
        print(f"[ENTITY GUARDRAIL] Agent analysis completed")
    except Exception as e:
        print(f"[ENTITY GUARDRAIL] ❌ Error running guardrail agent: {e}")
        # If guardrail agent fails, allow the request to proceed
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    # Not a switch → allow
    if not result.final_output or not result.final_output.is_entity_switch_request:
        print(f"[ENTITY GUARDRAIL] ✅ Not a switch request - allowing")
        return GuardrailFunctionOutput(output_info=result.final_output, tripwire_triggered=False)

    # Blocked? mirror restart handling: DO NOT raise here.
    if hasattr(result.final_output, "switch_allowed") and not result.final_output.switch_allowed:
        new_type = result.final_output.requested_entity_type
        print(f"[ENTITY GUARDRAIL] ⛔ Switch blocked by agent analysis")

        # record context so respond() can show an entity-switch specific message
        sess = CURRENT_SESSION.get()
        try:
            if isinstance(sess, OpenAIConversationsSession):
                setattr(sess, "_tripwire", {"kind": "entity_switch", "target": new_type})
        except Exception:
            pass

        # signal tripwire (Runner will raise InputGuardrailTripwireTriggered)
        return GuardrailFunctionOutput(output_info=result.final_output, tripwire_triggered=True)

    # Allowed → proceed
    print(f"[ENTITY GUARDRAIL] ✅ Switch request allowed by agent analysis")
    return GuardrailFunctionOutput(output_info=result.final_output, tripwire_triggered=False)


@input_guardrail
async def restart_guardrail(
    ctx: RunContextWrapper[None], agent: Agent, input: str | list[TResponseInputItem]
) -> GuardrailFunctionOutput:
    """Stateless: evaluate only the most recent user message."""
    text = _latest_user_text(input)
    if not text:
        out = RestartRequestOutput(is_restart_request=False, reasoning="No user text found.")
        return GuardrailFunctionOutput(output_info=out, tripwire_triggered=False)

    # ❗ Do NOT pass ctx.context or session here
    result = await Runner.run(restart_guardrail_agent, text)

    return GuardrailFunctionOutput(
        output_info=result.final_output,
        tripwire_triggered=bool(result.final_output and result.final_output.is_restart_request),
    )



# ========= GLOBAL CONTEXT =========
CURRENT_SESSION = contextvars.ContextVar("CURRENT_SESSION", default=None)
_SESSION_STORE = {}  # Store actual session objects to preserve conversation history
_ACTIVE_EXTRACTIONS = set()  # Track active extraction sessions
ENABLE_EXTRACTOR = True  # Set to False to disable extractor for debugging

# ========= ENTITY SWITCH LIMITER =========
ENTITY_SWITCH_LIMIT = 2  # Maximum number of entity switches allowed per session


# ========= TOOL ARG TYPES =========
class SendEmailOtpArgs(TypedDict):
    email: str

class VerifyEmailOtpArgs(TypedDict):
    email: str
    code: str

class SetEntityArgs(TypedDict):
    entity_type: Literal["BASE", "LLC", "C-CORP", "S-CORP", "PAYMENT"]


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

class SetLLCDesignatorArgs(TypedDict):
    designator: str

class SetLLCGovernanceArgs(TypedDict):
    governance: str

class SetLLCMembersArgs(TypedDict):
    members: str  # JSON string of member information

class SetRegisteredAgentArgs(TypedDict):
    agent_info: str  # JSON string of agent information

class SetVirtualBusinessAddressArgs(TypedDict):
    address_info: str  # JSON string of address information

class SetUserDataArgs(TypedDict):
    user_name: Optional[str]
    user_email: Optional[str]
    user_phone: Optional[str]
    business_name: Optional[str]
    business_purpose: Optional[str]
    business_state: Optional[str]
    email_verified: Optional[bool]

class SetCorpDesignatorArgs(TypedDict):
    designator: str

class SetAuthorizedSharesArgs(TypedDict):
    shares: str
    par_value: str

class SetIncorporatorsArgs(TypedDict):
    incorporators: str  # JSON string of incorporator information

class SetFormationFlagsArgs(TypedDict):
    _: Optional[str]  # Dummy parameter since we don't need any input

class SetCorpFormationFlagsArgs(TypedDict):
    _: Optional[str]  # Dummy parameter since we don't need any input


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
            "WA":"Washington","WV":"West Virginia","WI":"Wisconsin","WY":"Wyoming","DC":"Washington, DC", "DC":"District of Columbia"
        }
        return codes.get(s.upper())
    low = s.lower().replace(".", "").replace(",", "").strip()
    if low in ("dc","d c","washington dc","washington d c","district of columbia"):
        return "Washington, DC"
    return s


# ========= FORMATION COMPLETION CHECKER =========
def _check_formation_complete(sess, entity_type: str) -> bool:
    """Check if entity formation flow has all required information before allowing payment using completion flags."""
    
    # Base required fields for all entities
    required_base_flags = [
        "user_data_collected",
        "email_verified",
        "otp_verified"  # OTP verification required for formation completion
    ]
    
    # Check base requirements using flags
    missing_base = []
    for flag in required_base_flags:
        if not getattr(sess, flag, False):
            missing_base.append(flag)
    
    if missing_base:
        print(f"[FORMATION CHECK] ❌ Missing base completion flags: {missing_base}")
        return False
    
    # Entity-specific requirements using flags
    if entity_type == "LLC":
        llc_required_flags = [
            "llc_designator_set",      # LLC designator chosen
            "llc_governance_set",      # Governance type chosen
            # "llc_members_set",         # ❌ REMOVED - Member information no longer required
            "registered_agent_set",    # Registered agent setup
            "virtual_address_set"      # Virtual address setup
        ]
        
        missing_llc = []
        for flag in llc_required_flags:
            if not getattr(sess, flag, False):
                missing_llc.append(flag)
        
        if missing_llc:
            print(f"[FORMATION CHECK] ❌ LLC missing completion flags: {missing_llc}")
            return False
                
    elif entity_type in ("C-CORP", "S-CORP"):
        corp_required_flags = [
            "corp_designator_set",     # Corp designator chosen
            "authorized_shares_set",   # Share structure defined
            # "incorporators_set",       # ❌ REMOVED - Incorporator information no longer required
            "registered_agent_set",    # Registered agent setup
            "virtual_address_set"      # Virtual address setup
        ]
        
        missing_corp = []
        for flag in corp_required_flags:
            if not getattr(sess, flag, False):
                missing_corp.append(flag)
        
        if missing_corp:
            print(f"[FORMATION CHECK] ❌ {entity_type} missing completion flags: {missing_corp}")
            return False
    
    print(f"[FORMATION CHECK] ✅ {entity_type} formation complete (all flags set)")
    return True


# ========= DATA COLLECTION TOOLS =========

@function_tool
async def setFormationFlags(args: SetFormationFlagsArgs) -> str:
    """Manually set completion flags for existing formation data"""
    sess = CURRENT_SESSION.get()
    if not isinstance(sess, OpenAIConversationsSession):
        return "No active session"
    
    # Set all LLC formation flags to True based on conversation history
    setattr(sess, "user_data_collected", True)
    setattr(sess, "email_verified", True)
    setattr(sess, "llc_designator_set", True)
    setattr(sess, "llc_governance_set", True)
    # setattr(sess, "llc_members_set", True)  # ❌ REMOVED - No longer required
    setattr(sess, "registered_agent_set", True)
    setattr(sess, "virtual_address_set", True)
    
    print("[MANUAL FLAGS] ✅ All LLC formation completion flags set to True")
    return "All formation completion flags have been set. You can now proceed with 'I Confirm'."

@function_tool
async def setCorpFormationFlags(args: SetCorpFormationFlagsArgs) -> str:
    """Manually set completion flags for existing corporation formation data"""
    sess = CURRENT_SESSION.get()
    if not isinstance(sess, OpenAIConversationsSession):
        return "No active session"
    
    # Set all Corp formation flags to True based on conversation history
    setattr(sess, "user_data_collected", True)
    setattr(sess, "email_verified", True)
    setattr(sess, "corp_designator_set", True)
    setattr(sess, "authorized_shares_set", True)
    # setattr(sess, "incorporators_set", True)  # ❌ REMOVED - No longer required
    setattr(sess, "registered_agent_set", True)
    setattr(sess, "virtual_address_set", True)
    
    print("[MANUAL FLAGS] ✅ All Corp formation completion flags set to True")
    return "All corporation formation completion flags have been set. You can now proceed with 'I Confirm'."

@function_tool
async def setCorpDesignator(args: SetCorpDesignatorArgs) -> str:
    """Set Corporation designator (Inc., Corp., Corporation, Incorporated)"""
    sess = CURRENT_SESSION.get()
    if not isinstance(sess, OpenAIConversationsSession):
        return "No active session"
    
    designator = args.get("designator", "Inc.")
    setattr(sess, "corp_designator", designator)
    setattr(sess, "corp_designator_set", True)  # Set completion flag
    print(f"[CORP DATA] Set designator: {designator} (flag set)")
    return f"Corporation designator set to {designator}"

@function_tool
async def setAuthorizedShares(args: SetAuthorizedSharesArgs) -> str:
    """Set authorized shares and par value for corporation"""
    sess = CURRENT_SESSION.get()
    if not isinstance(sess, OpenAIConversationsSession):
        return "No active session"
    
    shares = args.get("shares", "1000")
    par_value = args.get("par_value", "0.01")
    
    setattr(sess, "authorized_shares", shares)
    setattr(sess, "par_value", par_value)
    setattr(sess, "authorized_shares_set", True)  # Set completion flag
    
    print(f"[CORP DATA] Set authorized shares: {shares} at ${par_value} par value (flag set)")
    return f"Authorized shares set to {shares} shares at ${par_value} par value"

@function_tool
async def setIncorporators(args: SetIncorporatorsArgs) -> str:
    """Set incorporator information for corporation"""
    sess = CURRENT_SESSION.get()
    if not isinstance(sess, OpenAIConversationsSession):
        return "No active session"
    
    incorporators_json = args.get("incorporators", "[]")
    try:
        incorporators = json.loads(incorporators_json) if isinstance(incorporators_json, str) else incorporators_json
    except:
        incorporators = []
    
    setattr(sess, "incorporators", incorporators)
    setattr(sess, "incorporators_set", True)  # Set completion flag
    
    print(f"[CORP DATA] Set incorporators: {len(incorporators)} incorporator(s) (flag set)")
    return f"Corporation incorporators information saved"
@function_tool
async def setLLCDesignator(args: SetLLCDesignatorArgs) -> str:
    """Set LLC designator (LLC, L.L.C., Limited Liability Company)"""
    sess = CURRENT_SESSION.get()
    if not isinstance(sess, OpenAIConversationsSession):
        return "No active session"
    
    designator = args.get("designator", "LLC")
    setattr(sess, "llc_designator", designator)
    setattr(sess, "llc_designator_set", True)  # Set completion flag
    print(f"[LLC DATA] Set designator: {designator} (flag set)")
    return f"LLC designator set to {designator}"

@function_tool  
async def setLLCGovernance(args: SetLLCGovernanceArgs) -> str:
    """Set LLC governance (Member-Managed or Manager-Managed)"""
    sess = CURRENT_SESSION.get()
    if not isinstance(sess, OpenAIConversationsSession):
        return "No active session"
    
    governance = args.get("governance", "Member-Managed")
    setattr(sess, "llc_governance", governance)
    setattr(sess, "llc_governance_set", True)  # Set completion flag
    print(f"[LLC DATA] Set governance: {governance} (flag set)")
    return f"LLC governance set to {governance}"

@function_tool
async def setLLCMembers(args: SetLLCMembersArgs) -> str:
    """Set LLC member information"""
    sess = CURRENT_SESSION.get()
    if not isinstance(sess, OpenAIConversationsSession):
        return "No active session"
    
    members_json = args.get("members", "[]")
    try:
        members = json.loads(members_json) if isinstance(members_json, str) else members_json
    except:
        members = []
    setattr(sess, "llc_members", members)
    setattr(sess, "llc_members_set", True)  # Set completion flag
    print(f"[LLC DATA] Set members: {len(members)} member(s) (flag set)")
    return f"LLC members information saved"

@function_tool
async def setRegisteredAgent(args: SetRegisteredAgentArgs) -> str:
    """Set registered agent information"""
    sess = CURRENT_SESSION.get()
    if not isinstance(sess, OpenAIConversationsSession):
        return "No active session"
    
    agent_info_json = args.get("agent_info", "{}")
    try:
        agent_info = json.loads(agent_info_json) if isinstance(agent_info_json, str) else agent_info_json
    except:
        agent_info = {}
    setattr(sess, "registered_agent", agent_info)
    setattr(sess, "registered_agent_set", True)  # Set completion flag
    print(f"[DATA] Set registered agent: {agent_info} (flag set)")
    return "Registered agent information saved"

@function_tool
async def setVirtualBusinessAddress(args: SetVirtualBusinessAddressArgs) -> str:
    """Set virtual business address"""
    sess = CURRENT_SESSION.get()
    if not isinstance(sess, OpenAIConversationsSession):
        return "No active session"
    
    address_info_json = args.get("address_info", "{}")
    try:
        address_info = json.loads(address_info_json) if isinstance(address_info_json, str) else address_info_json
    except:
        address_info = {}
    setattr(sess, "virtual_business_address", address_info)
    setattr(sess, "virtual_address_set", True)  # Set completion flag
    print(f"[DATA] Set virtual business address: {address_info} (flag set)")
    return "Virtual business address saved"

@function_tool
async def setUserData(args: SetUserDataArgs) -> str:
    """Set user basic information (name, email, phone, business details)"""
    sess = CURRENT_SESSION.get()
    if not isinstance(sess, OpenAIConversationsSession):
        return "No active session"
    
    # Set user data attributes
    if args.get("user_name"):
        setattr(sess, "user_name", args.get("user_name"))
    if args.get("user_email"):
        setattr(sess, "user_email", args.get("user_email"))
    if args.get("user_phone"):
        setattr(sess, "user_phone", args.get("user_phone"))
    if args.get("business_name"):
        setattr(sess, "business_name", args.get("business_name"))
    if args.get("business_purpose"):
        setattr(sess, "business_purpose", args.get("business_purpose"))
    if args.get("business_state"):
        setattr(sess, "business_state", args.get("business_state"))
    if args.get("email_verified"):
        setattr(sess, "email_verified", args.get("email_verified"))
    
    # Set completion flag if we have the essential user data
    if (getattr(sess, "user_name", None) and 
        getattr(sess, "user_email", None) and 
        getattr(sess, "user_phone", None) and
        getattr(sess, "business_name", None) and
        getattr(sess, "business_purpose", None) and
        getattr(sess, "business_state", None)):
        setattr(sess, "user_data_collected", True)
        print(f"[DATA] Set user data: {list(args.keys())} (completion flag set)")
    else:
        print(f"[DATA] Set user data: {list(args.keys())} (partial - flag not set)")
    
    return "User data saved"


# ========= TOOLS =========
otp = OTPService()

@function_tool
async def sendEmailOtp(args: SendEmailOtpArgs) -> str:
    print(f"[TOOL LOG] ✉️ sendEmailOtp called with email={args.get('email')}")
    return otp.send_otp_to_user(args)

@function_tool
async def verifyEmailOtp(args: VerifyEmailOtpArgs) -> str:
    print(f"[TOOL LOG] 🔐 verifyEmailOtp called for email={args.get('email')} code={args.get('code')}")
    result = otp.verify_otp_from_user(args)
    
    # Set OTP verification flag if verification was successful
    sess = CURRENT_SESSION.get()
    if isinstance(sess, OpenAIConversationsSession):
        # Check if verification was successful - OTP service returns "Email verified successfully."
        if "email verified successfully" in result.lower():
            setattr(sess, "otp_verified", True)
            setattr(sess, "email_verified", True)  # Also set email_verified for backward compatibility
            print("[OTP LOG] ✅ OTP verification successful - flags set")
        else:
            print(f"[OTP LOG] ❌ OTP verification failed: {result}")
    
    return result

@function_tool
async def setEntityType(args: SetEntityArgs) -> str:
    sess = CURRENT_SESSION.get()
    if not isinstance(sess, OpenAIConversationsSession):
        print("[AGENT LOG] ❌ setEntityType called but no active session")
        return "No active session to update."

    if not getattr(sess, "otp_verified", False):
        print("[AGENT LOG] ⛔ setEntityType blocked - OTP not verified")
        return "Please verify your email with the OTP code before selecting an entity type."

    new_type = args.get("entity_type", "BASE")
    old_type = getattr(sess, "entity_type", "BASE")

    # real switch?
    if old_type != "BASE" and old_type != new_type:
        current_switches = getattr(sess, "entity_switch_count", 0)

        if current_switches >= ENTITY_SWITCH_LIMIT:
            print(f"[AGENT LOG] ⛔ setEntityType blocked - switch limit exceeded ({current_switches}/{ENTITY_SWITCH_LIMIT})")
            # record for respond() UI copy
            try:
                if isinstance(sess, OpenAIConversationsSession):
                    setattr(sess, "_tripwire", {"kind": "entity_switch", "target": new_type})
            except Exception:
                pass
            # ❗ abort the turn so the LLM cannot continue with a “C-Corp” reply
            raise InputGuardrailTripwireTriggered("entity_switch_limit_exceeded")

        setattr(sess, "entity_switch_count", current_switches + 1)
        print(f"[AGENT LOG] 📊 Entity switch count: {current_switches + 1}/{ENTITY_SWITCH_LIMIT}")

    # only reached if allowed
    setattr(sess, "entity_type", new_type)
    print(f"[AGENT LOG] 🔒 setEntityType -> {old_type} → {new_type} (OTP verified)")
    return f"Entity type set to {new_type}"


@function_tool
async def updateToPaymentMode(args: UpdateToPaymentArgs) -> str:
    sess = CURRENT_SESSION.get()
    if not isinstance(sess, OpenAIConversationsSession):
        print("[AGENT LOG] ❌ updateToPaymentMode called with no active session")
        return "No active session to update."
    
    # ✅ Hard gate: only proceed if the last user message was exactly "I Confirm"
    if not getattr(sess, "ready_for_payment", False):
        print("[AGENT LOG] ⛔ updateToPaymentMode blocked (no explicit 'I Confirm')")
        return "Please type exactly 'I Confirm' to proceed to payment."
    
    # ✅ Check if entity formation flow is complete
    entity_type = getattr(sess, "entity_type", "BASE")
    formation_complete = _check_formation_complete(sess, entity_type)
    
    if not formation_complete:
        print(f"[AGENT LOG] ⛔ updateToPaymentMode blocked - {entity_type} formation not complete")
        return f"I need to collect all {entity_type} formation details before we can proceed to payment. Please provide the missing information first."
    
    # consume the ready_for_payment flag so it is one-shot
    setattr(sess, "ready_for_payment", False)
    
    old = getattr(sess, "entity_type", "BASE")
    setattr(sess, "original_entity_type", old)  # ✅ Track the original entity type for payment processing
    setattr(sess, "entity_type", "PAYMENT")
    setattr(sess, "payment_context", True)  # ✅ Flag that user is in payment flow
    setattr(sess, "awaiting_payment", False)
    setattr(sess, "payment_status", None)
    print(f"[AGENT LOG] 💳 updateToPaymentMode -> {old} → PAYMENT (formation_complete=True, original_entity={old})")
    return "Switching to payment mode."


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
    'District of Columbia': {'llc': 99, 's-corp': 99, 'c-corp': 99}
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
def trigger_extractor_safely(conv_id: str, conversation_history: list):
    """Safely trigger extractor in a completely separate daemon thread"""
    import threading
    
    def extractor_starter():
        try:
            run_background_extraction(conv_id, conversation_history)
        except Exception as e:
            print(f"[EXTRACTOR] ⚠️ Daemon thread extractor error: {e}")
    
    try:
        thread = threading.Thread(target=extractor_starter, daemon=True)
        thread.start()
        print(f"[EXTRACTOR] 🚀 Started daemon thread for {conv_id}")
    except Exception as e:
        print(f"[EXTRACTOR] ⚠️ Failed to start daemon thread: {e}") 

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
    
    # Run extraction directly in daemon thread (TRULY NON-BLOCKING)
    try:
        extract_worker()  # Run directly since we're already in daemon thread
        print(f"[EXTRACTOR] 🚀 Extraction completed for {session_id}")
    except Exception as e:
        print(f"[EXTRACTOR] ⚠️ Failed to run extraction: {e}")
        return None


# ========= AGENTS =========
corp_agent = Agent(
    name="Corp Assistant",
    model="gpt-4o",
    instructions=(
        "🏷️ AGENT IDENTIFICATION: You are the Corporate Formation Assistant.\n\n"
        + CorpPrompt.get_mode_prompt()
        + "\n\nDATA COLLECTION RULES:\n"
        "- When user chooses designator (Inc./Corp./Corporation/Incorporated), call setCorpDesignator\n"
        "- When user provides share structure (authorized shares and par value), call setAuthorizedShares\n"
        "- When collecting incorporator info, call setIncorporators\n"
        "- When setting registered agent, call setRegisteredAgent\n"
        "- When user provides virtual business address, call setVirtualBusinessAddress\n"
        "- When collecting user details, call setUserData\n"
        "- If user has already provided all information but flags aren't set, call setCorpFormationFlags to mark completion\n"
        + "\n\nRouting rules:\n"
        "- CRITICAL: NEVER CALL `setEntityType` FUNCTION unless the user's CURRENT message contains switch language like: 'switch to LLC', 'change to LLC', 'I want to switch to LLC', 'I want LLC instead'.\n"
        "- ABSOLUTELY FORBIDDEN: Do NOT call `setEntityType` for ANY of these words: 'ok', 'proceed', 'yes', 'continue', 'go ahead', 'that sounds good', 'try again', 'let me know', or ANY confirmation language.\n"
        "- IGNORE ALL CONVERSATION HISTORY when deciding whether to call `setEntityType`. Only look at the current message.\n"
        "- Do not answer LLC-specific questions in Corp mode; ask user to explicitly request the switch if needed.\n"
        "- ONLY call `updateToPaymentMode` when user types exactly 'I Confirm' AND all corporation formation completion flags are set.\n"
        "- Never call `updateToPaymentMode` for phrases like 'ok', 'proceed', 'yes' - only 'I Confirm'.\n"
        "- When calling `updateToPaymentMode`, do NOT add extra commentary - just call the tool and let the next agent handle the response.\n"
        "- If formation check fails due to missing flags but user has provided all data, call setCorpFormationFlags first.\n"
        "- If session has 'payment_context=True', user came from payment mode - they can return to payment with 'I Confirm'."
    ),
    tools=[setEntityType, updateToPaymentMode, setCorpDesignator, setAuthorizedShares, setIncorporators, setRegisteredAgent, setVirtualBusinessAddress, setUserData, setCorpFormationFlags],
    input_guardrails=[entity_switch_guardrail, restart_guardrail]
)

llc_agent = Agent(
    name="LLC Assistant",
    model="gpt-4o",
    instructions=(
        "🏷️ AGENT IDENTIFICATION: You are the LLC Formation Assistant.\n\n"
        + LLCPrompt.get_mode_prompt()
        + "\n\nDATA COLLECTION RULES:\n"
        "- When user chooses designator (LLC/L.L.C./etc), call setLLCDesignator\n"
        "- When user chooses governance (Member/Manager-Managed), call setLLCGovernance\n"
        "- When collecting member info, call setLLCMembers\n"
        "- When setting registered agent, call setRegisteredAgent\n"
        "- When user provides virtual business address, call setVirtualBusinessAddress\n"
        "- When collecting user details, call setUserData\n"
        "- If user has already provided all information but flags aren't set, call setFormationFlags to mark completion\n"
        + "\n\nRouting rules:\n"
        "- CRITICAL: NEVER CALL `setEntityType` FUNCTION unless the user's CURRENT message contains switch language like: 'switch to C-Corp', 'switch to S-Corp', 'switch to C Corp', 'switch to S Corp', 'change to C-Corp', 'change to S-Corp', 'I want to switch to C-Corp', 'I want to switch to S-Corp', 'I want to switch to C Corp', 'I want to switch to S Corp'.\n"
        "- ABSOLUTELY FORBIDDEN: Do NOT call `setEntityType` for ANY of these words: 'ok', 'proceed', 'yes', 'continue', 'go ahead', 'that sounds good', 'try again', 'let me know', or ANY confirmation language.\n"
        "- IGNORE ALL CONVERSATION HISTORY when deciding whether to call `setEntityType`. Only look at the current message.\n"
        "- Do not answer Corp-specific questions in LLC mode; ask user to explicitly request the switch if needed.\n"
        "- ONLY call `updateToPaymentMode` when user types exactly 'I Confirm' AND all LLC formation completion flags are set.\n"
        "- Never call `updateToPaymentMode` for phrases like 'ok', 'proceed', 'yes' - only 'I Confirm'.\n"
        "- When calling `updateToPaymentMode`, do NOT add extra commentary - just call the tool and let the next agent handle the response.\n"
        "- If formation check fails due to missing flags but user has provided all data, call setFormationFlags first.\n"
        "- If session has 'payment_context=True', user came from payment mode - they can return to payment with 'I Confirm'."
    ),
    tools=[setEntityType, updateToPaymentMode, setLLCDesignator, setLLCGovernance, setLLCMembers, setRegisteredAgent, setVirtualBusinessAddress, setUserData, setFormationFlags],
    input_guardrails=[entity_switch_guardrail, restart_guardrail]
)

payment_agent = Agent(
    name="Payment Assistant",
    model="gpt-4o",
    instructions=(
        "🏷️ AGENT IDENTIFICATION: You are the Payment Assistant.\n\n"
        
        "CONTEXT DETECTION:\n"
        "- If you receive 'SYSTEM_TRIGGER:PAYMENT_CONFIRMED', this is a payment completion summary request.\n"
        "  Generate a comprehensive congratulatory summary with plan details, total costs, and next steps.\n"
        "  Be warm, professional, and include all payment/formation information.\n\n"
        
        "- If this is your first activation after user transitions to payment mode (no system trigger):\n"
        "  RESPOND WITH EXACTLY: 'here is your secure payment pop up to complete your incorporation'\n"
        "  CRITICAL: Do NOT add any other text. No explanations, no questions, no suggestions, no emojis.\n"
        "  Just the exact phrase above and nothing else.\n\n"
        
        "- For all other payment interactions, provide helpful payment assistance using available tools."
    ),
    tools=[stateFeeLookup, createPaymentLink, checkPaymentStatus],
    input_guardrails=[entity_switch_guardrail, restart_guardrail]
)

base_agent = Agent(
    name="Incubation AI (Base Assistant)",
    model="gpt-4o",
    instructions=(
        "🏷️ AGENT IDENTIFICATION: You are the Base Assistant.\n\n"
        + BasePrompt.get_mode_prompt()
        + "\n\nRouting rules:\n"
        "- ONLY call `setEntityType` when the user EXPLICITLY chooses an entity type like 'LLC', 'C-Corp', or 'S-Corp'.\n"
        "- Do NOT call `setEntityType` for NAICS code selections, numbers, or other non-entity choices.\n"
        "- Do NOT answer LLC- or Corp-specific questions here; ask to choose entity and set it via `setEntityType` first.\n"
        "- After collecting name, email, phone, business name, purpose, state, and NAICS code, ask user to choose entity type.\n\n"
        "CRITICAL OTP RULE:\n"
        "- When user provides full name, valid email, and exactly 10-digit phone number, you MUST call sendEmailOtp function immediately.\n"
        "- NEVER say you sent a code without actually calling the sendEmailOtp function.\n"
        "- Only after successfully calling sendEmailOtp should you tell the user the code was sent.\n"
        "- When collecting user details, call setUserData to store them properly."
    ),
    tools=[sendEmailOtp, verifyEmailOtp, setEntityType, setUserData],
    input_guardrails=[entity_switch_guardrail, restart_guardrail]
)

extractor_agent = Agent(
    name="Conversation Extractor",
    model="gpt-4o",
    instructions=(
        "🏷️ AGENT IDENTIFICATION: You are the Conversation Extractor Agent.\n\n"
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
    # Initialize completion flags
    _initialize_completion_flags(s)
    print("[AGENT LOG] 🧭 init_session -> entity_type = BASE, conv_id =", getattr(s, "conversation_id"))
    return s

def _initialize_completion_flags(session: OpenAIConversationsSession):
    """Initialize all completion flags to False"""
    # Base flags
    setattr(session, "user_data_collected", False)
    setattr(session, "email_verified", False)
    setattr(session, "otp_verified", False)  # OTP verification flag
    
    # Entity switch counter
    setattr(session, "entity_switch_count", 0)  # Initialize entity switch counter
    
    # LLC flags
    setattr(session, "llc_designator_set", False)
    setattr(session, "llc_governance_set", False)
    setattr(session, "llc_members_set", False)
    setattr(session, "registered_agent_set", False)
    setattr(session, "virtual_address_set", False)
    
    # Corp flags
    setattr(session, "corp_designator_set", False)
    setattr(session, "authorized_shares_set", False)
    setattr(session, "incorporators_set", False)

def _check_and_set_existing_data_flags(session: OpenAIConversationsSession):
    """Check existing session data and set completion flags accordingly"""
    # Check user data
    if (getattr(session, "user_name", None) and 
        getattr(session, "user_email", None) and 
        getattr(session, "user_phone", None) and
        getattr(session, "business_name", None) and
        getattr(session, "business_purpose", None) and
        getattr(session, "business_state", None)):
        setattr(session, "user_data_collected", True)
        print("[FLAG CHECK] ✅ user_data_collected flag set based on existing data")
    
    # Check LLC data
    if getattr(session, "llc_designator", None):
        setattr(session, "llc_designator_set", True)
        print("[FLAG CHECK] ✅ llc_designator_set flag set based on existing data")
    
    if getattr(session, "llc_governance", None):
        setattr(session, "llc_governance_set", True)
        print("[FLAG CHECK] ✅ llc_governance_set flag set based on existing data")
    
    if getattr(session, "llc_members", None):
        setattr(session, "llc_members_set", True)
        print("[FLAG CHECK] ✅ llc_members_set flag set based on existing data")
    
    # Check Corp data
    if getattr(session, "corp_designator", None):
        setattr(session, "corp_designator_set", True)
        print("[FLAG CHECK] ✅ corp_designator_set flag set based on existing data")
    
    if getattr(session, "authorized_shares", None) and getattr(session, "par_value", None):
        setattr(session, "authorized_shares_set", True)
        print("[FLAG CHECK] ✅ authorized_shares_set flag set based on existing data")
    
    if getattr(session, "incorporators", None):
        setattr(session, "incorporators_set", True)
        print("[FLAG CHECK] ✅ incorporators_set flag set based on existing data")
    
    # Check shared data (used by both LLC and Corp)
    if getattr(session, "registered_agent", None):
        setattr(session, "registered_agent_set", True)
        print("[FLAG CHECK] ✅ registered_agent_set flag set based on existing data")
    
    if getattr(session, "virtual_business_address", None):
        setattr(session, "virtual_address_set", True)
        print("[FLAG CHECK] ✅ virtual_address_set flag set based on existing data")


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
            setattr(session, "entity_type", "BASE")
            setattr(session, "awaiting_payment", True)
            # ✅ Initialize conversation history if not present
            setattr(session, "conversation_history", [])
            print(f"[SESSION] 🆕 Created session with defaults for {conv_id}")
        
        # ✅ Ensure conversation_history exists even if not in saved data
        if not hasattr(session, "conversation_history"):
            setattr(session, "conversation_history", [])
        
        # ✅ Initialize completion flags if not present
        if not hasattr(session, "user_data_collected"):
            _initialize_completion_flags(session)
        
        # ✅ Initialize entity switch counter if not present (for existing sessions)
        if not hasattr(session, "entity_switch_count"):
            setattr(session, "entity_switch_count", 0)
        
        # ✅ Check existing data and set flags accordingly
        _check_and_set_existing_data_flags(session)
        
        # Store in memory for future use
        _SESSION_STORE[conv_id] = session
        return session
    except Exception as e:
        print(f"[SESSION] ⚠️ Error restoring session {conv_id}: {e}")
        session = OpenAIConversationsSession(conversation_id=conv_id)
        setattr(session, "entity_type", "BASE")
        setattr(session, "awaiting_payment", True)
        # ✅ Initialize conversation history
        setattr(session, "conversation_history", [])
        # ✅ Initialize completion flags
        _initialize_completion_flags(session)
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
    
    # ✅ Only allow switching to PAYMENT when the user typed exactly "I Confirm"
    setattr(session, "ready_for_payment", lower_msg == "i confirm")
    
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

    except InputGuardrailTripwireTriggered as e:
        print(f"[GUARDRAIL] Tripwire triggered: {e!s}")

        # default restart response (same as you already had)
        default_restart = (
            "I understand you'd like to start fresh! 🔄\n\n"
            "To begin a completely new formation process, please:\n\n"
            "1. Click the **End Session** button below\n"
            "2. Then click **Start / Resume** to begin with a clean slate\n\n"
            "This will create a new conversation with a fresh session ID, and you can start over "
            "with your entity formation from the beginning.\n\n"
            "Would you like me to help you with anything else in your current session first?"
        )

        # Check if entity-switch guardrail set a tripwire context
        trip = {}
        try:
            if isinstance(session, OpenAIConversationsSession):
                trip = getattr(session, "_tripwire", {}) or {}
        except Exception:
            trip = {}

        if trip.get("kind") == "entity_switch":
            target = trip.get("target") or "the new entity type"
            response_content = (
                f"I understand you'd like to explore {target} formation.\n\n"
                f"For data integrity and security, we focus on one entity type per session after the initial selection. "
                f"This keeps all your information accurate and properly organized.\n\n"
                f"If you'd like to proceed with {target}, please click **End Session** and then **Start / Resume** to begin "
                f"a new session for a clean slate."
            )
            # clear the flag
            try:
                setattr(session, "_tripwire", {})
            except Exception:
                pass
        else:
            # restart (or any other guardrail without context)
            response_content = default_restart

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

    # Trigger background extraction (NON-BLOCKING)
    print(f"[DEBUG] About to trigger extractor for {conv_id}")
    if ENABLE_EXTRACTOR and conv_id and len(conversation_history) > 0:
        try:
            trigger_extractor_safely(conv_id, conversation_history)
            print(f"[DEBUG] Extractor triggered successfully for {conv_id}")
        except Exception as e:
            print(f"[EXTRACTOR] ⚠️ Failed to start extraction: {e}")
    elif not ENABLE_EXTRACTOR:
        print(f"[DEBUG] Extractor disabled for debugging")
    print(f"[DEBUG] Continuing after extractor trigger")

    if session.entity_type == "PAYMENT" and getattr(session, "show_payment_summary", False):
        try:
            summary = _run_payment_completed_summary(session)
            response_content = (response_content + "\n\n" + summary).strip() if response_content else summary
        finally:
            setattr(session, "show_payment_summary", False)

    # Clean approach: Let the Payment Agent handle formatting via function calls
    # No more static regex parsing - the AI will use formatPaymentLink function instead

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
