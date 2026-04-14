#!/bin/bash

# Configuration
DOCKER_HUB_USER="erdincka"
PROJECT_PREFIX="defence-ops"
VERSION=${1:-latest}

# List of services to build and push
SERVICES=(
    "app-ui"
    "video-service"
    "llm-service"
    "kafka-service"
)

echo "🚀 Building and publishing microservices to Docker Hub..."
echo "👤 User: $DOCKER_HUB_USER"
echo "🔖 Version: $VERSION"

# Loop through services
for SERVICE in "${SERVICES[@]}"; do
    IMAGE_NAME="$DOCKER_HUB_USER/$PROJECT_PREFIX-$SERVICE"
    echo "----------------------------------------"
    echo "📦 Building $IMAGE_NAME..."
    
    # Run docker build from the root context using specific Dockerfile
    docker build --platform linux/amd64 -t "$IMAGE_NAME:$VERSION" -t "$IMAGE_NAME:latest" -f "$SERVICE/Dockerfile" . --push
    
    if [ $? -ne 0 ]; then
        echo "❌ Build failed for $SERVICE"
        exit 1
    fi
    
done

echo "----------------------------------------"
echo "✅ All microservices published successfully!"
