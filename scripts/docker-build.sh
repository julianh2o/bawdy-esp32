#!/bin/bash
# Build (and optionally push) the bawdy Docker images to Docker Hub.
#
#   julianh2o/bawdy-daemon   lighting engine + REST API   (Dockerfile.daemon, target: daemon)
#   julianh2o/bawdy-web      browser sequencer UI         (Dockerfile.web)
#
# Usage: ./scripts/docker-build.sh [--push] [--version VERSION] [--only daemon|web]
#   PLATFORM=linux/amd64,linux/arm64 ./scripts/docker-build.sh --push   # multi-arch (push only)

set -e

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Check for uncommitted changes
if [ -n "$(git status --porcelain)" ]; then
    echo "Error: Working directory has uncommitted changes."
    echo "Please commit your changes before building a Docker image."
    echo ""
    git status --short
    exit 1
fi

REGISTRY_USER="${REGISTRY_USER:-julianh2o}"
PLATFORM="${PLATFORM:-linux/amd64}"

# Parse arguments
PUSH=false
ONLY=""
VERSION=$(git describe --tags --always --dirty 2>/dev/null || echo "dev")

while [[ $# -gt 0 ]]; do
    case $1 in
        --push)
            PUSH=true
            shift
            ;;
        --version)
            VERSION="$2"
            shift 2
            ;;
        --only)
            ONLY="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--push] [--version VERSION] [--only daemon|web]"
            exit 1
            ;;
    esac
done

if [ "$PUSH" = false ] && [[ "$PLATFORM" == *,* ]]; then
    echo "Error: multi-platform builds can't be loaded locally; use --push or a single PLATFORM."
    exit 1
fi

BUILD_DATE=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
VCS_REF=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
OUTPUT=$([ "$PUSH" = true ] && echo "--push" || echo "--load")

# name | dockerfile | target
IMAGES=(
    "bawdy-daemon|Dockerfile.daemon|daemon"
    "bawdy-web|Dockerfile.web|"
)

for spec in "${IMAGES[@]}"; do
    IFS='|' read -r NAME DOCKERFILE TARGET <<< "$spec"
    if [ -n "$ONLY" ] && [ "$NAME" != "bawdy-$ONLY" ]; then
        continue
    fi
    IMAGE_NAME="$REGISTRY_USER/$NAME"

    echo "Building $IMAGE_NAME:$VERSION for $PLATFORM"
    echo "  Dockerfile: $DOCKERFILE${TARGET:+ (target: $TARGET)}"
    echo "  Build date: $BUILD_DATE"
    echo "  VCS ref: $VCS_REF"

    docker buildx build \
        --platform "$PLATFORM" \
        -f "$DOCKERFILE" \
        ${TARGET:+--target "$TARGET"} \
        --build-arg VERSION="$VERSION" \
        --build-arg BUILD_DATE="$BUILD_DATE" \
        --build-arg VCS_REF="$VCS_REF" \
        -t "$IMAGE_NAME:$VERSION" \
        -t "$IMAGE_NAME:latest" \
        $OUTPUT \
        .

    echo "Build complete: $IMAGE_NAME:$VERSION"
    echo ""
done

if [ "$PUSH" = true ]; then
    echo "Images pushed to Docker Hub"
else
    echo "To push to Docker Hub, run: ./scripts/docker-build.sh --push"
fi
