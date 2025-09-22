# gradio_app_conversations_multi.py
import os
import contextvars
from typing import TypedDict, Optional, Literal

# Import configuration to set up environment variables (dotenv, etc.)
import config

import gradio as gr
from agents import Agent, Runner, function_tool, OpenAIConversationsSession

from base_prompt import BasePrompt
from llc_prompt import LLCPrompt
from corp_prompt import CorpPrompt
from otp_service import OTPService


# ========= GLOBAL CONTEXT =========
# Expose the current OpenAIConversationsSession to tools safely.
CURRENT_SESSION = contextvars.ContextVar("CURRENT_SESSION", default=None)


# ========= TOOLS =========
class SendEmailOtpArgs(TypedDict):
    email: str

class VerifyEmailOtpArgs(TypedDict):
    email: str
    code: str

class SetEntityArgs(TypedDict):
    entity_type: Literal["BASE", "LLC", "C-CORP", "S-CORP"]

otp = OTPService()

@function_tool
async def sendEmailOtp(args: SendEmailOtpArgs) -> str:
    """Send a verification code to the specified email address."""
    return otp.send_otp_to_user(args)

@function_tool
async def verifyEmailOtp(args: VerifyEmailOtpArgs) -> str:
    """Verify a user-provided OTP code for the given email."""
    return otp.verify_otp_from_user(args)

@function_tool
async def setEntityType(args: SetEntityArgs) -> str:
    """
    Deterministically set the current session's entity type.
    Call this from any agent immediately after the user chooses the entity.
    """
    sess = CURRENT_SESSION.get()
    if not isinstance(sess, OpenAIConversationsSession):
        print("[AGENT LOG] ❌ setEntityType called but no active OpenAIConversationsSession found")
        return "No active session to update."
    new_type = args.get("entity_type", "BASE")
    old_type = getattr(sess, "entity_type", "BASE")
    setattr(sess, "entity_type", new_type)
    print(f"[AGENT LOG] 🔒 setEntityType tool -> {old_type} → {new_type}")
    return f"Entity type set to {new_type}"


# ========= AGENTS (NO HANDOFFS) =========
corp_agent = Agent(
    name="Corp Assistant",
    model="gpt-4o",
    instructions=(
        "🏷️ AGENT IDENTIFICATION: You are the Corporate Formation Assistant. "
        "Always start your responses with '[CORP AGENT]' so users know which agent is responding.\n\n"
        + CorpPrompt.get_mode_prompt()
        + "\n\n"
        "Routing rules:\n"
        "- If the user explicitly asks to switch entity type (e.g., LLC), call the tool `setEntityType` with that type.\n"
        "- Do not answer LLC-specific questions in Corp mode; switch with `setEntityType` when appropriate.\n"
        "- After executing the function show confirmation message to the user that the entity type has been set. and ask to type proceed to start further with the formation process."
    ),
    tools=[setEntityType]  # add sendEmailOtp/verifyEmailOtp here if you want them in Corp mode
)

llc_agent = Agent(
    name="LLC Assistant",
    model="gpt-4o",
    instructions=(
        "🏷️ AGENT IDENTIFICATION: You are the LLC Formation Assistant. "
        "Always start your responses with '[LLC AGENT]' so users know which agent is responding.\n\n"
        + LLCPrompt.get_mode_prompt()
        + "\n\n"
        "Routing rules:\n"
        "- If the user explicitly asks to switch entity type (e.g., C-Corp or S-Corp), call the tool `setEntityType` with that type.\n"
        "- Do not answer corporate-specific questions in LLC mode; switch with `setEntityType` when appropriate.\n"
        "- After executing the function show confirmation message to the user that the entity type has been set. and ask to type proceed to start further with the formation process."
    ),
    tools=[setEntityType]  # add sendEmailOtp/verifyEmailOtp here if you want them in LLC mode
)

base_agent = Agent(
    name="Incubation AI (Base Assistant)",
    model="gpt-4o",
    instructions=(
        "🏷️ AGENT IDENTIFICATION: You are the Base Assistant. "
        "Always start your responses with '[BASE AGENT]' so users know which agent is responding.\n\n"
        + BasePrompt.get_mode_prompt()
        + "\n\n"
        "Routing rules:\n"
        "- When the user chooses an entity type (LLC / C-CORP / S-CORP), call `setEntityType` with that type immediately.\n"
        "- Do NOT answer LLC- or Corp-specific questions here; ask to choose entity and set it via `setEntityType` first.\n"
        "- After executing the function show confirmation message to the user that the entity type has been set. and ask to type proceed to start further with the formation process."
    ),
    tools=[sendEmailOtp, verifyEmailOtp, setEntityType]
)


# ========= HELPERS =========
def banner_for(session: Optional[OpenAIConversationsSession]) -> str:
    cid = getattr(session, "conversation_id", None)
    return f"**Conversation ID:** `{cid}` — keep this if you want to resume later." if cid else ""

def init_session() -> OpenAIConversationsSession:
    s = OpenAIConversationsSession()
    setattr(s, "entity_type", "BASE")  # BASE | LLC | C-CORP | S-CORP
    print("[AGENT LOG] 🧭 init_session -> entity_type = BASE")
    return s

def _agent_for_entity(entity_type: str):
    if entity_type == "LLC":
        return llc_agent, "LLC Agent"
    if entity_type in ("C-CORP", "S-CORP"):
        return corp_agent, "Corp Agent"
    return base_agent, "Base Agent"


# ========= HANDLERS =========
def on_load():
    session = init_session()
    hello = (
        "Hello and welcome! I’m Incubation AI — here to help you turn your business idea into a registered reality.\n\n"
        "What’s needed next: Please share your **full legal name**, **email address**, and **primary phone number** "
        "so we can set up your secure account and get you moving toward launch."
    )
    chat = [{"role": "assistant", "content": hello}]
    return (
        chat,
        session,
        banner_for(session),
        gr.update(interactive=False),
        gr.update(interactive=False),
    )

def start_or_resume(conv_id: str, session: Optional[OpenAIConversationsSession]):
    if isinstance(session, OpenAIConversationsSession):
        info = "A conversation is already active. To resume another ID, click **End Session** first."
        print("[AGENT LOG] ⚠️ Start/Resume requested but session already active")
        return (
            [{"role": "assistant", "content": info}], session, banner_for(session),
            gr.update(), gr.update()
        )

    session = (
        OpenAIConversationsSession(conversation_id=conv_id.strip())
        if conv_id and conv_id.strip()
        else init_session()
    )
    if not hasattr(session, "entity_type"):
        session.entity_type = "BASE"

    msg = "Resumed your conversation. Welcome back! 🎉" if conv_id.strip() else "Started a new conversation."
    print(f"[AGENT LOG] ▶️ start_or_resume -> entity_type = {session.entity_type}")
    chat = [{"role": "assistant", "content": f"{msg}\n\n{banner_for(session)}"}]
    return (
        chat, session, banner_for(session),
        gr.update(interactive=False), gr.update(interactive=False),
    )

def respond(message: str, history, session: Optional[OpenAIConversationsSession]):
    import asyncio, concurrent.futures

    if not isinstance(session, OpenAIConversationsSession):
        print("[AGENT LOG] ⛔ respond called with no active session")
        history = history + [
            {"role": "user", "content": message},
            {"role": "assistant", "content": "Session ended. Click **Start / Resume** to begin, then paste a Conversation ID if you want to resume."}
        ]
        return history, session, banner_for(session), gr.update(), gr.update()

    # Safety default
    if not hasattr(session, "entity_type"):
        session.entity_type = "BASE"

    current_agent, agent_name = _agent_for_entity(session.entity_type)
    print(f"[AGENT LOG] 🚀 Routing to {agent_name} (entity_type = {session.entity_type})")

    try:
        def run_agent():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                # Expose session to tools via context var so setEntityType can mutate it
                token = CURRENT_SESSION.set(session)
                try:
                    print(f"[AGENT LOG] ▶️ Runner.run with {agent_name} | message: {message[:80]!r}")
                    result = loop.run_until_complete(Runner.run(current_agent, message, session=session))
                finally:
                    CURRENT_SESSION.reset(token)

                response = (result.final_output or "").strip()

                # OPTIONAL: If agents include tags, you can keep an audit log here.
                if response.startswith("[LLC AGENT]") and session.entity_type != "LLC":
                    print("[AGENT LOG] ℹ️ Responder tag = LLC AGENT (routing remains deterministic via session.entity_type)")
                elif response.startswith("[CORP AGENT]") and session.entity_type not in ("C-CORP", "S-CORP"):
                    print("[AGENT LOG] ℹ️ Responder tag = CORP AGENT (routing remains deterministic via session.entity_type)")
                elif response.startswith("[BASE AGENT]") and session.entity_type != "BASE":
                    print("[AGENT LOG] ℹ️ Responder tag = BASE AGENT (routing remains deterministic via session.entity_type)")

                print(f"[AGENT LOG] 💬 Response (first 120): {response[:120]!r}")
                print(f"[AGENT LOG] 📌 Persisted entity_type: {session.entity_type}")
                return result
            finally:
                loop.close()

        with concurrent.futures.ThreadPoolExecutor() as executor:
            result = executor.submit(run_agent).result(timeout=60)
            response_content = result.final_output

    except Exception as e:
        response_content = f"I encountered an error processing your message. Please try again. Error: {str(e)[:120]}..."
        print(f"[AGENT LOG] ❌ Exception in respond: {e!r}")

    history = history + [
        {"role": "user", "content": message},
        {"role": "assistant", "content": response_content}
    ]
    return history, session, banner_for(session), gr.update(interactive=False), gr.update(interactive=False)

def end_session(history, session: Optional[OpenAIConversationsSession]):
    end_note = "Session ended. You can now **paste a Conversation ID** (optional) and press **Start / Resume**."
    print("[AGENT LOG] 🛑 end_session -> dropping session, resetting UI controls")
    return (
        [{"role": "assistant", "content": end_note}],
        None,
        "",
        gr.update(interactive=True),
        gr.update(interactive=True),
    )


# ========= BUILD UI =========
with gr.Blocks(theme=gr.themes.Soft()) as demo:
    assert os.getenv("OPENAI_API_KEY"), "Set OPENAI_API_KEY."

    gr.Markdown("## Incubation AI — Multi-User (OpenAI Conversations Memory) — Base ↔ LLC ↔ Corp (No Handoffs)")
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

    st_session = gr.State()

    # Wire events
    demo.load(fn=on_load, outputs=[chat, st_session, conv_banner, conv_id_in, start_btn])
    start_btn.click(fn=start_or_resume, inputs=[conv_id_in, st_session],
                    outputs=[chat, st_session, conv_banner, conv_id_in, start_btn])
    msg.submit(fn=respond, inputs=[msg, chat, st_session],
               outputs=[chat, st_session, conv_banner, conv_id_in, start_btn])
    msg.submit(lambda: "", None, msg)

    def clear_chat():
        print("[AGENT LOG] 🧹 clear_chat -> chat cleared, entity_type unchanged")
        return [],  # Only clears the visible chat; session + entity_type persist

    clear_btn.click(fn=clear_chat, outputs=[chat])
    end_btn.click(fn=end_session, inputs=[chat, st_session],
                  outputs=[chat, st_session, conv_banner, conv_id_in, start_btn])

if __name__ == "__main__":
    demo.queue().launch()
