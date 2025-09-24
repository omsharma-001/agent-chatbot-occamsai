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
