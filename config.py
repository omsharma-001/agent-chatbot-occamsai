# config.py
import os

def setup_environment():
    # ==== OpenAI ====
    os.environ["OPENAI_API_KEY"] = (
        "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"
    )

    # ==== SendGrid ====
    os.environ["SENDGRID_API_KEY"] = "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"
    os.environ["MAIL_FROM"] = "himewe5824@dawhe.com"
    os.environ["MAIL_FROM_NAME"] = "Incubation AI"

    # ==== Stripe ====
    os.environ["STRIPE_SECRET_KEY"] = "sk_test_51Rvd2VHuYjA3rOC4kN6KacDh2hRBv511JisiKv63V1e0fSX7LCm81gLXNY0QA8PtnYz5YI4MKuEnyiymgLx4jN5Q0086fPvcMs"
    os.environ["STRIPE_PUBLISHABLE_KEY"] = "pk_test_51Rvd2VHuYjA3rOC4MpiCIBJ6gfBlIcgVj62ix53XQYMCnCkgbQRxKFXtOh7wnFoIi2CszAE4n35LJ1TkJeE9ipsC00iqmmuc3s"
    os.environ["STRIPE_CLIENT_ID"] = os.environ["STRIPE_PUBLISHABLE_KEY"]
    os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_zr6YR1RDEXSlCJOKUiq3UqSMd0TSzyVa"

    # ==== App base URL (Gradio) ====
    # Default to the typical Gradio port. You can override at runtime.
    os.environ.setdefault("SITE_URL", "http://localhost:7860")
    os.environ.setdefault("ENV", "development")

    print("✅ Environment variables configured for Gradio at", os.environ["SITE_URL"])

class Config:
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
    SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
    MAIL_FROM = os.environ.get("MAIL_FROM", "")
    MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "Incubation AI")

    STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
    STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
    STRIPE_CLIENT_ID = os.environ.get("STRIPE_CLIENT_ID", STRIPE_PUBLISHABLE_KEY)
    STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    SITE_URL = os.environ.get("SITE_URL", "http://localhost:7860")
    ENV = os.environ.get("ENV", "development")

# helper (optional): call this after demo.launch(share=True)
def set_site_url_from_gradio_share(share_url: str):
    if share_url:
        os.environ["SITE_URL"] = share_url
        print("🔗 SITE_URL updated to Gradio share URL:", share_url)

setup_environment()
