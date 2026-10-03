#!/bin/bash
# Deploy script for Magpie: stop → pull → build → start

set -e  # Exit on any error

echo "🛑 Stopping Docker containers..."
docker compose down

echo "📥 Pulling latest changes..."
git pull

echo "🔨 Building Docker image..."
docker compose build

echo "🚀 Starting services..."
docker compose up -d

echo "✅ Deploy complete!"
echo "Magpie is running at http://localhost:8000"
docker compose logs -f magpie
