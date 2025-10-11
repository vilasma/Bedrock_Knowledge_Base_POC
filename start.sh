#!/bin/bash

# Bedrock Knowledge Base POC - Startup Script

echo "🚀 Starting Bedrock Knowledge Base POC..."
echo ""

# Check if .env exists
if [ ! -f .env ]; then
    echo "❌ Error: .env file not found"
    echo "👉 Please copy .env.example to .env and configure it:"
    echo "   cp .env.example .env"
    echo "   # Then edit .env with your AWS credentials"
    exit 1
fi

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
echo "🔧 Activating virtual environment..."
source venv/bin/activate

# Install/update dependencies
echo "📥 Installing dependencies..."
pip install -q -r requirements.txt

# Check AWS credentials
echo "🔐 Checking AWS credentials..."
python3 -c "import boto3; boto3.client('sts').get_caller_identity()" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "⚠️  Warning: AWS credentials not configured or invalid"
    echo "👉 Make sure AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are set in .env"
fi

echo ""
echo "✅ Environment ready!"
echo ""
echo "🌐 Starting application..."
echo "   - Gradio UI will be available at: http://localhost:7860"
echo "   - FastAPI will be available at: http://localhost:8000"
echo ""

# Run the application
python app.py
