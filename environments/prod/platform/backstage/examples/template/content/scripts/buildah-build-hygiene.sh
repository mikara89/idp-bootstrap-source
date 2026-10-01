#!/bin/sh
# The generated service repository keeps its Buildah cache inside the GitLab
# project workspace. It never reaches the Runner's Docker image or volume store.
set -eu

action=${1:-}
project_dir=${CI_PROJECT_DIR:-$(pwd)}
cache_dir=${IDP_BUILD_CACHE_DIR:-"$project_dir/.ci/buildah-storage"}
storage_conf=${CONTAINERS_STORAGE_CONF:-"$project_dir/.ci/containers-storage.conf"}
runroot=${IDP_BUILDAH_RUNROOT:-"$project_dir/.ci/buildah-runroot"}
minimum_available_kib=${IDP_BUILD_MIN_AVAILABLE_KIB:-12582912}

case "$cache_dir" in
  "$project_dir"/.ci/buildah-storage) ;;
  *)
    echo "IDP_BUILD_CACHE_DIR must remain under $project_dir/.ci/buildah-storage" >&2
    exit 2
    ;;
esac

case "$storage_conf" in
  "$project_dir"/.ci/containers-storage.conf) ;;
  *)
    echo "CONTAINERS_STORAGE_CONF must remain under $project_dir/.ci" >&2
    exit 2
    ;;
esac

prepare() {
  command -v buildah >/dev/null 2>&1 || {
    echo "buildah is required for an image build" >&2
    exit 2
  }
  available_kib=$(df -Pk "$project_dir" | awk 'NR == 2 { print $4 }')
  case "$available_kib" in
    ''|*[!0-9]*)
      echo "could not determine available build filesystem space" >&2
      exit 2
      ;;
  esac
  if [ "$available_kib" -lt "$minimum_available_kib" ]; then
    echo "insufficient build disk: ${available_kib}KiB available, ${minimum_available_kib}KiB required" >&2
    exit 1
  fi

  mkdir -p "$cache_dir" "$runroot" "$(dirname "$storage_conf")"
  cat > "$storage_conf" <<EOF
[storage]
driver = "overlay"
runroot = "$runroot"
graphroot = "$cache_dir"

[storage.options.overlay]
mount_program = "/usr/bin/fuse-overlayfs"
EOF
  export CONTAINERS_STORAGE_CONF="$storage_conf"
  buildah info >/dev/null
  echo "Buildah cache prepared at $cache_dir; ${available_kib}KiB available"
}

cleanup() {
  [ -f "$storage_conf" ] || exit 0
  export CONTAINERS_STORAGE_CONF="$storage_conf"
  command -v buildah >/dev/null 2>&1 || exit 0
  # Only the explicit project cache is touched. Do not use Docker pruning here:
  # the Runner may host active platform images and application volumes.
  buildah rm --all >/dev/null 2>&1 || true
  buildah rmi --prune >/dev/null 2>&1 || true
}

case "$action" in
  prepare) prepare ;;
  cleanup) cleanup ;;
  *)
    echo "usage: $0 {prepare|cleanup}" >&2
    exit 2
    ;;
esac
