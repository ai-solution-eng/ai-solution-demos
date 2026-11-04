#!/bin/bash

# Configuration. IMAGE_PREFIX is the registry and account to push to: a Docker Hub user, or e.g.
# registry.example.com/team. Set the chart's image repositories to match.
DOCKER_HUB_USER="${IMAGE_PREFIX:-erdincka}"
PROJECT_PREFIX="defence-ops"
VERSION=${1:?Usage: $0 <version>, e.g. 1.1.0 (the tag the chart pins in global.image.tag)}

# Only the version tag is pushed. `latest` still serves the 1.0.x multi-pod chart, which these
# single-pod images would break.

# List of services to build and push
SERVICES=(
    "app-ui"
    "video-service"
    "llm-service"
    "kafka-service"
)

echo "🚀 Building and publishing microservices to Docker Hub..."
echo "👤 Prefix: $DOCKER_HUB_USER"
echo "🔖 Version: $VERSION"

# Loop through services
for SERVICE in "${SERVICES[@]}"; do
    IMAGE_NAME="$DOCKER_HUB_USER/$PROJECT_PREFIX-$SERVICE"
    echo "----------------------------------------"
    echo "📦 Building $IMAGE_NAME..."
    
    # Run docker build from the root context using specific Dockerfile
    docker build --platform linux/amd64 -t "$IMAGE_NAME:$VERSION" -f "$SERVICE/Dockerfile" . --push
    
    if [ $? -ne 0 ]; then
        echo "❌ Build failed for $SERVICE"
        exit 1
    fi
    
done

echo "----------------------------------------"
echo "✅ All microservices published successfully!"
