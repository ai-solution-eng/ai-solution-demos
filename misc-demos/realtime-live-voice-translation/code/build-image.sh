#!/usr/bin/env bash
# build-image.sh — build (and optionally push) the docker-mcp image.
#
# Usage:
#   ./build-image.sh -t ghcr.io/ai-solution-eng/docker-mcp:v0.6.0
#   ./build-image.sh --tag ghcr.io/ai-solution-eng/docker-mcp:v0.6.0 --push
#   ./build-image.sh -t <ref> --no-push        # explicit skip
#
# Notes:
#   * Proxy build-args are ALWAYS passed (the RUN steps need them on the
#     dsh-dind daemon; harmless elsewhere). Override via env:
#     HTTP_PROXY/HTTPS_PROXY/NO_PROXY.
#   * DOCKER_HOST is honored if set (e.g. the dsh-dind service); otherwise
#     the default docker context is used.
#   * --push requires registry auth (docker login / existing config.json).
set -euo pipefail

TAG=""
PUSH=0
PLATFORM="linux/amd64"
DOCKERFILE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -t|--tag)   TAG="${2:?--tag needs a value}"; shift 2 ;;
    --push)     PUSH=1; shift ;;
    --no-push)  PUSH=0; shift ;;
    --platform) PLATFORM="${2:?--platform needs a value}"; shift 2 ;;
    -f|--file)  DOCKERFILE="${2:?--file needs a value}"; shift 2 ;;
    -h|--help)  grep "^#" "$0" | sed "s/^# \{0,1\}//"; exit 0 ;;
    *) echo "unknown arg: $1 (see --help)" >&2; exit 2 ;;
  esac
done

if [[ -z "$TAG" ]]; then
  echo "usage: $0 -t|--tag <registry/repo:tag> [--push] [--platform X] [-f FILE]" >&2
  exit 2
fi

# defaults for the proxy args (override via env)
HTTP_PROXY_ARG="${HTTP_PROXY:-http://hpeproxy.its.hpecorp.net:8080}"
HTTPS_PROXY_ARG="${HTTPS_PROXY:-http://hpeproxy.its.hpecorp.net:8080}"
NO_PROXY_ARG="${NO_PROXY:-127.0.0.1,localhost,.cluster.local,.svc,10.0.0.0/8,172.0.0.0/8}"

# the repo dir is where this script lives (so it runs from anywhere)
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

ARGS=(
  buildx build
  --platform "$PLATFORM"
  --network host 
  --build-arg "HTTP_PROXY=$HTTP_PROXY_ARG"
  --build-arg "HTTPS_PROXY=$HTTPS_PROXY_ARG"
  --build-arg "NO_PROXY=$NO_PROXY_ARG"
  -t "$TAG"
  -f "$DOCKERFILE"
)
[[ "$PUSH" == "1" ]] && ARGS+=(--push)
ARGS+=(.)

echo ">> docker ${ARGS[*]}"
exec docker "${ARGS[@]}"
