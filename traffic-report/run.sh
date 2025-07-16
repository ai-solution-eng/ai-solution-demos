#!/bin/bash

# Smart Traffic Management Demo Runner

echo "🚦 Starting Smart Traffic Management Demo..."

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
source venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt

# Check for environment variables
if [ -z "$YOLO_ENDPOINT" ] || [ -z "$YOLO_API_KEY" ] || [ -z "$QWEN_ENDPOINT" ] || [ -z "$QWEN_API_KEY" ]; then
    echo "⚠️  Warning: Environment variables not set!"
    echo "Please set the following environment variables:"
    echo "  - YOLO_ENDPOINT"
    echo "  - YOLO_API_KEY"
    echo "  - QWEN_ENDPOINT"
    echo "  - QWEN_API_KEY"
    echo ""
    echo "You can copy .env.example to .env and edit it with your values."
    echo ""
fi

# Run the application
echo "🚀 Launching Streamlit interface..."
streamlit run app.py --server.port=8080 --server.address=0.0.0.0 --server.headless=true