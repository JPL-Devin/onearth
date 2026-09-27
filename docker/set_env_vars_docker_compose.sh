source version.sh

# Images are built for the host architecture (amd64 and arm64 are supported).
# Set DOCKER_PLATFORM (e.g. linux/amd64 or linux/arm64) to target a different platform.
export DOCKER_PLATFORM_OPTION=${DOCKER_PLATFORM:-}
export USE_SSL=true 
export SERVER_NAME=localhost
export ONEARTH_DEPS_TAG=nasagibs/onearth-deps:${ONEARTH_VERSION}
export START_ONEARTH_TOOLS_CONTAINER=0