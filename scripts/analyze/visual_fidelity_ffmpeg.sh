#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "usage: $0 <original_media> <received_media> <output_dir>" >&2
  exit 2
fi

ORIGINAL="$1"
RECEIVED="$2"
OUTDIR="$3"

mkdir -p "$OUTDIR"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "[visual] ffmpeg not found; cannot compute SSIM/PSNR/VMAF" >&2
  exit 1
fi

COMMON_FILTER="[0:v]setpts=PTS-STARTPTS,format=yuv420p[ref];[1:v]setpts=PTS-STARTPTS,format=yuv420p[dist]"

ffmpeg -hide_banner -y \
  -i "$ORIGINAL" -i "$RECEIVED" \
  -filter_complex "${COMMON_FILTER};[ref][dist]ssim=${OUTDIR}/ssim.log" \
  -f null - >"${OUTDIR}/ffmpeg_ssim.stdout.log" 2>"${OUTDIR}/ffmpeg_ssim.stderr.log"

ffmpeg -hide_banner -y \
  -i "$ORIGINAL" -i "$RECEIVED" \
  -filter_complex "${COMMON_FILTER};[ref][dist]psnr=${OUTDIR}/psnr.log" \
  -f null - >"${OUTDIR}/ffmpeg_psnr.stdout.log" 2>"${OUTDIR}/ffmpeg_psnr.stderr.log"

if ffmpeg -hide_banner -filters 2>/dev/null | grep -q 'libvmaf'; then
  ffmpeg -hide_banner -y \
    -i "$ORIGINAL" -i "$RECEIVED" \
    -filter_complex "${COMMON_FILTER};[ref][dist]libvmaf=log_fmt=json:log_path=${OUTDIR}/vmaf.json" \
    -f null - >"${OUTDIR}/ffmpeg_vmaf.stdout.log" 2>"${OUTDIR}/ffmpeg_vmaf.stderr.log"
  echo "[visual] wrote SSIM, PSNR, and VMAF outputs to ${OUTDIR}"
else
  echo "[visual] ffmpeg does not include libvmaf; wrote SSIM/PSNR only to ${OUTDIR}" >&2
fi
