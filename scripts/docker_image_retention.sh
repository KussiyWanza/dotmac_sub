#!/usr/bin/env bash
# Prune old deployment images while keeping rollback headroom.
set -euo pipefail

IMAGE_REPO="${IMAGE_REPO:-ghcr.io/michaelayoade/dotmac_sub}"
RETAIN_IMAGES="${RETAIN_IMAGES:-5}"
DRY_RUN="${DRY_RUN:-0}"

if ! [[ "${RETAIN_IMAGES}" =~ ^[0-9]+$ ]] || [[ "${RETAIN_IMAGES}" -lt 1 ]]; then
  echo "RETAIN_IMAGES must be a positive integer" >&2
  exit 1
fi
if [[ "${DRY_RUN}" != "0" && "${DRY_RUN}" != "1" ]]; then
  echo "DRY_RUN must be 0 or 1" >&2
  exit 1
fi

declare -A used_ids=()
if ! container_ids="$(docker ps -aq)"; then
  echo "Image retention failed: container inventory is unavailable" >&2
  exit 1
fi
while IFS= read -r container_id; do
  [[ -n "${container_id}" ]] || continue
  if ! image_id="$(docker inspect --format '{{.Image}}' "${container_id}" 2>/dev/null)"; then
    echo "Image retention failed: cannot identify image for container ${container_id}" >&2
    exit 1
  fi
  used_ids["${image_id}"]=1
done < <(sort -u <<<"${container_ids}")

if ! image_inventory="$(
  docker image ls --no-trunc "${IMAGE_REPO}" \
    --format '{{.CreatedAt}}\t{{.ID}}\t{{.Repository}}:{{.Tag}}'
)"; then
  echo "Image retention failed: repository image inventory is unavailable" >&2
  exit 1
fi
mapfile -t image_rows < <(sort -r <<<"${image_inventory}")

kept_unused=0
removed_unused=0
discovered_unused=0
discovered_images=0
declare -A seen_ids=()
for row in "${image_rows[@]}"; do
  [[ -n "${row}" ]] || continue
  image_id="$(cut -f2 <<<"${row}")"
  image_ref="$(cut -f3 <<<"${row}")"
  [[ -n "${image_id}" ]] || continue
  [[ -z "${seen_ids[$image_id]:-}" ]] || continue
  seen_ids["${image_id}"]=1
  discovered_images=$((discovered_images + 1))
  if ! full_id="$(docker image inspect --format '{{.Id}}' "${image_id}" 2>/dev/null)"; then
    echo "Image retention failed: cannot inspect ${image_ref} (${image_id})" >&2
    exit 1
  fi

  if [[ -n "${used_ids[$full_id]:-}" ]]; then
    echo "Keeping in-use image: ${image_ref} (${full_id})"
    continue
  fi

  discovered_unused=$((discovered_unused + 1))
  if (( kept_unused < RETAIN_IMAGES )); then
    echo "Keeping rollback image: ${image_ref} (${full_id})"
    kept_unused=$((kept_unused + 1))
    continue
  fi

  if [[ "${DRY_RUN}" == "1" ]]; then
    echo "Would remove old unused image: ${image_ref} (${full_id})"
  else
    echo "Removing old unused image: ${image_ref} (${full_id})"
    docker image rm "${full_id}"
    removed_unused=$((removed_unused + 1))
  fi
done

if [[ "${DRY_RUN}" == "1" ]]; then
  echo "Image retention preview: discovered=${discovered_images} unused=${discovered_unused} keep=${kept_unused} remove=$((discovered_unused - kept_unused)) target=${RETAIN_IMAGES}"
  exit 0
fi

EXPECTED_UNUSED=${discovered_unused}
if [[ "${EXPECTED_UNUSED}" -gt "${RETAIN_IMAGES}" ]]; then
  EXPECTED_UNUSED=${RETAIN_IMAGES}
fi
if ! remaining_inventory="$(
  docker image ls --no-trunc "${IMAGE_REPO}" \
    --format '{{.CreatedAt}}\t{{.ID}}\t{{.Repository}}:{{.Tag}}'
)"; then
  echo "Image retention verification failed: repository inventory is unavailable" >&2
  exit 1
fi
remaining_unused=0
declare -A remaining_ids=()
while IFS=$'\t' read -r _created_at remaining_id _remaining_ref; do
  [[ -n "${remaining_id}" ]] || continue
  [[ -z "${remaining_ids[$remaining_id]:-}" ]] || continue
  remaining_ids["${remaining_id}"]=1
  if [[ -z "${used_ids[$remaining_id]:-}" ]]; then
    remaining_unused=$((remaining_unused + 1))
  fi
done <<<"${remaining_inventory}"
if [[ "${remaining_unused}" -ne "${EXPECTED_UNUSED}" ]]; then
  echo "Image retention verification failed: expected_unused=${EXPECTED_UNUSED} actual_unused=${remaining_unused}" >&2
  exit 1
fi
for used_id in "${!used_ids[@]}"; do
  if ! docker image inspect "${used_id}" >/dev/null 2>&1; then
    echo "Image retention verification failed: in-use image ${used_id} is missing" >&2
    exit 1
  fi
done
echo "Image retention verified: discovered=${discovered_images} in_use=${#used_ids[@]} rollback=${EXPECTED_UNUSED} removed=${removed_unused} target=${RETAIN_IMAGES}"
