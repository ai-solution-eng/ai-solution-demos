#!/bin/bash
# Build and push Docker images to Docker Hub
# Usage: ./build-and-push.sh [version]
# Example: ./build-and-push.sh v1.0.0

set -e

VERSION=${1:-latest}
REGISTRY="erdincka/lawfirm"

echo "🏗️  Building images with version: $VERSION"

# Build backend
echo "📦 Building backend..."
cd backend

# docker buildx build --platform linux/amd64,linux/arm64 -t erdincka/lawfirm-backend:latest --push .
docker buildx build --platform linux/amd64 -f Dockerfile.prod -t ${REGISTRY}-backend:${VERSION} -t ${REGISTRY}-backend:latest --push .
# docker tag ${REGISTRY}-backend:${VERSION} ${REGISTRY}-backend:latest

# Build frontend
echo "📦 Building frontend..."
cd ../frontend
docker buildx build --platform linux/amd64 -f Dockerfile.prod -t ${REGISTRY}-frontend:${VERSION} -t ${REGISTRY}-frontend:latest --push .
# docker tag ${REGISTRY}-frontend:${VERSION} ${REGISTRY}-frontend:latest

cd ..

# echo "✅ Build and push complete!"
# echo ""
# echo "🚀 Pushing images to Docker Hub..."

# # Push backend
# echo "⬆️  Pushing backend..."
# # docker push ${REGISTRY}-backend:${VERSION}
# docker push ${REGISTRY}-backend:latest

# # Push frontend
# echo "⬆️  Pushing frontend..."
# # docker push ${REGISTRY}-frontend:${VERSION}
# docker push ${REGISTRY}-frontend:latest

echo ""
echo "✅ All images pushed successfully!"
echo ""
echo "📝 Images available:"
echo "   - ${REGISTRY}-backend:${VERSION}"
echo "   - ${REGISTRY}-backend:latest"
echo "   - ${REGISTRY}-frontend:${VERSION}"
echo "   - ${REGISTRY}-frontend:latest"
