#!/usr/bin/env python3
"""
Startup script for Incubation AI application
This script ensures all configuration is loaded before starting the app
"""

import os
import sys

def main():
    print("🚀 Starting Incubation AI Application...")
    print("=" * 50)
    
    # Import configuration first
    try:
        import config
        print("✅ Configuration loaded successfully")
    except ImportError as e:
        print(f"❌ Failed to load configuration: {e}")
        sys.exit(1)
    
    # Verify environment variables
    required_vars = ["OPENAI_API_KEY", "SENDGRID_API_KEY", "MAIL_FROM"]
    missing_vars = []
    
    for var in required_vars:
        if not os.getenv(var):
            missing_vars.append(var)
    
    if missing_vars:
        print(f"❌ Missing environment variables: {', '.join(missing_vars)}")
        sys.exit(1)
    
    print("✅ All environment variables configured")
    print("✅ Starting Gradio application...")
    print("=" * 50)
    
    # Import and run the main application
    try:
        import gradio_app_conversations_multi
    except Exception as e:
        print(f"❌ Failed to start application: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
