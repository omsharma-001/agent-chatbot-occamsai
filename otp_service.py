# otp_service.py  — NO PEPPER, stores OTP in memory/plaintext (not logged)
import os, time, hmac, secrets, ssl
from typing import Any, Dict

# Import configuration to ensure environment variables are set
try:
    import config
except ImportError:
    print("Warning: config.py not found. Using environment variables directly.")

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, Email, To, Content

# Fix SSL certificate issues on Windows
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class OTPService:
    OTP_TTL_SECONDS = 10 * 60
    RESEND_COOLDOWN_SECONDS = 60
    OTP_LENGTH = 6
    MAX_ATTEMPTS = 5
    SESSION_ID = "default-session"   # single-user/process in-memory

    def __init__(self):
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._emails_sent: list[dict] = []
        self._sendgrid_key = os.environ.get("SENDGRID_API_KEY")
        self._from_email = os.environ.get("MAIL_FROM", "test@example.com")
        self._from_name = os.environ.get("MAIL_FROM_NAME", "Incubation AI")
        
        # Debug info
        print(f"[DEBUG] SendGrid API Key: {'✓ Set' if self._sendgrid_key else '✗ Missing'}")
        print(f"[DEBUG] From Email: {self._from_email}")
        print(f"[DEBUG] From Name: {self._from_name}")

    # ---------- session helpers ----------
    def _get_session(self) -> Dict[str, Any]:
        sess = self._sessions.get(self.SESSION_ID)
        if not sess:
            sess = {"id": self.SESSION_ID}
            self._sessions[self.SESSION_ID] = sess
        return sess

    def _now(self) -> int: 
        return int(time.time())

    # ---------- utils ----------
    def _mask_email(self, email: str) -> str:
        try:
            local, domain = email.split("@", 1)
            masked_local = (local[0] + "****") if len(local) <= 2 else (local[0] + "****" + local[-1])
            return f"{masked_local}@{domain}"
        except Exception:
            return "****"

    def _otp_generate(self) -> str:
        return f"{secrets.randbelow(10**self.OTP_LENGTH):0{self.OTP_LENGTH}d}"

    def _send_via_sendgrid(self, to_email: str, code: str) -> None:
        if not self._sendgrid_key:
            # For testing without SendGrid API key
            print(f"[TEST MODE] Would send OTP {code} to {to_email}")
            return
            
        print(f"[DEBUG] Attempting to send OTP {code} to {to_email}")
        print(f"[DEBUG] Using sender: {self._from_email}")
        print(f"[DEBUG] Subject: Your verification code")
        print(f"[DEBUG] OTP Code in email: {code}")
        
        try:
            # Initialize SendGrid client with SSL context
            sg = SendGridAPIClient(self._sendgrid_key)
            
            # Set up SSL context for Windows compatibility
            import ssl
            ssl._create_default_https_context = ssl._create_unverified_context
            
            subject = "Your verification code"
            text_body = f"Your OTP is {code}. It expires in {self.OTP_TTL_SECONDS // 60} minutes."
            html_body = (
                f"<p>Your verification code is:</p>"
                f"<h2 style='letter-spacing:3px; font-family:monospace;'>{code}</h2>"
                f"<p>This code will expire in <strong>{self.OTP_TTL_SECONDS // 60} minutes</strong>.</p>"
                f"<p>If you didn't request this, you can ignore this email.</p>"
            )
            
            msg = Mail(from_email=Email(self._from_email, self._from_name),
                       to_emails=To(to_email), subject=subject)
            msg.add_content(Content("text/plain", text_body))
            msg.add_content(Content("text/html", html_body))
            
            # Disable sandbox mode to send real emails
            # from sendgrid.helpers.mail import MailSettings, SandBoxMode
            # msg.mail_settings = MailSettings()
            # msg.mail_settings.sandbox_mode = SandBoxMode(enable=False)
            
            response = sg.send(msg)
            print(f"[DEBUG] SendGrid response status: {response.status_code}")
            
            if response.status_code in [200, 202]:
                print(f"[DEBUG] Email sent successfully! (Status: {response.status_code})")
            else:
                print(f"[DEBUG] Unexpected status code: {response.status_code}")
                raise Exception(f"SendGrid returned status {response.status_code}")
                
        except Exception as e:
            print(f"[DEBUG] SendGrid error: {str(e)}")
            raise e

    # ---------- API ----------
    def send_otp_to_user(self, args: Dict[str, Any]) -> str:
        email = str(args.get("email", "")).strip()
        if "@" not in email:
            return "Please provide a valid email address."

        sess = self._get_session()
        now = self._now()

        # Cooldown to avoid spamming inbox
        if sess.get("otp_sent_at") and now - sess["otp_sent_at"] < self.RESEND_COOLDOWN_SECONDS:
            wait = self.RESEND_COOLDOWN_SECONDS - (now - sess["otp_sent_at"])
            return f"OTP was just sent. Please wait {wait}s before requesting again."

        code = self._otp_generate()
        print(f"[DEBUG] Generated OTP: {code} for {email}")

        # Store plaintext OTP in memory only (NOT logged)
        sess.update({
            "email": email,
            "otp_code": code,              # <-- plaintext (in memory)
            "otp_sent_at": now,
            "otp_verified": False,
            "otp_attempts": 0,
        })

        try:
            self._send_via_sendgrid(email, code)
            print(f"[DEBUG] OTP sending successful!")
        except Exception as e:
            error_msg = str(e)
            print(f"[DEBUG] OTP sending failed: {error_msg}")
            
            if not self._sendgrid_key:
                # In test mode, continue without actual email sending
                pass
            else:
                # Return more specific error information
                if "401" in error_msg or "Unauthorized" in error_msg:
                    return "SendGrid API key is invalid. Please check your configuration."
                elif "403" in error_msg or "Forbidden" in error_msg:
                    return "SendGrid sender email is not verified. Please verify 'test@example.com' in your SendGrid dashboard or use a verified sender."
                elif "sender" in error_msg.lower() or "from" in error_msg.lower():
                    return f"Email sender '{self._from_email}' is not verified in SendGrid. Please verify your sender domain."
                else:
                    return f"Email sending failed: {error_msg[:150]}..."

        # Minimal audit (masked email, never log OTP)
        self._emails_sent.append({
            "to": self._mask_email(email),
            "subject": "Your verification code",
            "ts": now,
        })
        success_msg = f"OTP sent to {self._mask_email(email)}. It will expire in {self.OTP_TTL_SECONDS // 60} minutes."
        if not self._sendgrid_key:
            success_msg += " (Check console for test mode OTP)"
        else:
            success_msg += " (Real email sent - check your inbox!)"
        
        return success_msg

    def verify_otp_from_user(self, args: Dict[str, Any]) -> str:
        code = str(args.get("code", "")).strip()
        email = str(args.get("email", "")).strip()

        if not code.isdigit() or not (4 <= len(code) <= 8):
            return "Please enter the 4–8 digit code from your email."

        sess = self._get_session()
        if not sess.get("email") or (email and email != sess["email"]):
            return "This email does not match the pending verification request."
        if sess.get("otp_verified", False):
            return "Your email is already verified."

        sent_at = sess.get("otp_sent_at")
        if not sent_at or self._now() - sent_at > self.OTP_TTL_SECONDS:
            return "Your code has expired. Please request a new one."

        expected = sess.get("otp_code", "")
        # Constant-time compare to avoid timing leaks
        if not hmac.compare_digest(code, expected):
            sess["otp_attempts"] = sess.get("otp_attempts", 0) + 1
            if sess["otp_attempts"] >= self.MAX_ATTEMPTS:
                return "Too many attempts. Please request a new code."
            return "That code is incorrect. Please try again."

        # Success: mark verified and erase OTP from memory
        sess["otp_verified"] = True
        sess.pop("otp_code", None)
        sess.pop("otp_sent_at", None)
        sess.pop("otp_attempts", None)
        return "Email verified successfully."
