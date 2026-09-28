#!/bin/sh

set -e

SCRIPT_NAME=$(basename "$0")
TAG="$1"

if [ -z "$TAG" ]; then
  echo "Usage: ${SCRIPT_NAME} TAG" >&2
  exit 1
fi

# Images are built for the host architecture (amd64 and arm64 are supported).
# Set DOCKER_PLATFORM (e.g. linux/amd64 or linux/arm64) to target a different platform.
DOCKER_PLATFORM_OPTION=${DOCKER_PLATFORM:+--platform=$DOCKER_PLATFORM}

rm -rf Dockerfile

#DOCKER_UID=$(id -u)
#DOCKER_GID=$(id -g)

cp ./docker/test/Dockerfile .

docker build \
  $DOCKER_PLATFORM_OPTION \
  --tag "$TAG" \
  ./

rm Dockerfile
