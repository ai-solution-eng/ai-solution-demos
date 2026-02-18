#!/bin/bash
# Build script for Voice Agent
# Builds and pushes all Docker images to :latest (or specified VERSION)

set -e

REGISTRY="${REGISTRY:-liavna}"
TAG="${VERSION:-5.0.0}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}        Voice Agent Docker Build Script (${TAG})${NC}"
echo -e "${BLUE}════════════════════════════════════════════════════════${NC}"
echo ""
echo "Registry: ${REGISTRY}"
echo "Tag:      ${TAG}"
echo ""
echo "Images to build:"
echo "  1) WebSocket Server  (${REGISTRY}/web-socket-server:${TAG})"
echo "  2) Gradio UI         (${REGISTRY}/web-socket-server-ui:${TAG})"
echo "  3) XTTS v2 Server    (${REGISTRY}/xtts-server:${TAG})"
echo "  4) All images"
echo "  5) Exit"
echo ""
read -p "Select option [1-5]: " choice

build_websocket() {
    echo -e "${YELLOW}Building WebSocket Server...${NC}"
    cd docker/websocket-server
    docker buildx build --platform linux/amd64 \
        --no-cache \
        -t ${REGISTRY}/web-socket-server:${TAG} \
        --push .
    cd ../..
    echo -e "${GREEN}✓ WebSocket Server: ${REGISTRY}/web-socket-server:${TAG}${NC}"
}

build_gradio() {
    echo -e "${YELLOW}Building Gradio UI...${NC}"
    cd docker/gradio-ui
    docker buildx build --platform linux/amd64 \
        --no-cache \
        -t ${REGISTRY}/web-socket-server-ui:${TAG} \
        --push .
    cd ../..
    echo -e "${GREEN}✓ Gradio UI: ${REGISTRY}/web-socket-server-ui:${TAG}${NC}"
}

build_xtts() {
    echo -e "${YELLOW}Building XTTS v2 Server (may take a while)...${NC}"
    cd docker/xtts-server
    docker buildx build --platform linux/amd64 \
        --no-cache \
        -t ${REGISTRY}/xtts-server:${TAG} \
        --push .
    cd ../..
    echo -e "${GREEN}✓ XTTS Server: ${REGISTRY}/xtts-server:${TAG}${NC}"
}

case $choice in
    1) build_websocket ;;
    2) build_gradio ;;
    3) build_xtts ;;
    4)
        build_websocket
        build_gradio
        build_xtts
        ;;
    5)
        echo "Exiting..."
        exit 0
        ;;
    *)
        echo -e "${RED}Invalid option${NC}"
        exit 1
        ;;
esac

echo ""
echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}                   Build Complete!${NC}"
echo -e "${GREEN}════════════════════════════════════════════════════════${NC}"
echo ""
echo "Deploy with:"
echo "  helm upgrade voice-agent ."
echo "  # or"
echo "  helm install voice-agent voice-agent-latest.tar.gz"
