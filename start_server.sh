#!/bin/bash
# Startup script for MWA AI Chatbot

cd "$(dirname "$0")"

# Activate virtual environment
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment with Python 3.11..."
    python3.11 -m venv .venv || python3 -m venv .venv
fi

source .venv/bin/activate

# Check for .env file
if [ ! -f ".env" ]; then
    echo "Warning: .env file not found. Creating template..."
    echo "OPENAI_API_KEY=sk-your-key-here" > .env
    echo "Please edit .env and add your OpenAI API key, then run this script again."
    exit 1
fi

# Check if OpenAI API key is set
source .env
if [ -z "$OPENAI_API_KEY" ] || [ "$OPENAI_API_KEY" = "sk-your-key-here" ]; then
    echo "Error: OPENAI_API_KEY not set in .env file"
    echo "Please edit .env and add your OpenAI API key."
    exit 1
fi

# Start server
echo "Starting MWA AI Chatbot server..."
echo "Server will be available at: http://127.0.0.1:8000"
echo "UI will be available at: http://127.0.0.1:8000/ui/"
echo ""
echo "Press Ctrl+C to stop the server"
echo ""

# Run without --reload to avoid macOS permission issues with file watching
uvicorn app.main:app --host 127.0.0.1 --port 8000

