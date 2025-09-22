# config.py - Configuration file for Incubation AI application
import os

# Set environment variables programmatically
def setup_environment():
    """Set up all required environment variables"""
    
    # OpenAI API Key
    os.environ["OPENAI_API_KEY"] = "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"
    
    # SendGrid API Key
    os.environ["SENDGRID_API_KEY"] = "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"
    
    # Email settings
    os.environ["MAIL_FROM"] = "himewe5824@dawhe.com"
    os.environ["MAIL_FROM_NAME"] = "Incubation AI"
    
    print("✅ Environment variables configured successfully")

# Configuration constants
class Config:
    # API Keys
    OPENAI_API_KEY = "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"
    SENDGRID_API_KEY = "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"
    
    # Email settings
    MAIL_FROM = "himewe5824@dawhe.com"
    MAIL_FROM_NAME = "Incubation AI"

# Auto-setup when imported
if __name__ == "__main__":
    setup_environment()
else:
    setup_environment()
