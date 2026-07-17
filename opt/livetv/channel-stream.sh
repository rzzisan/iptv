#!/bin/bash
# Per-channel live relay. MODE: auto (default) | copy | transcode
#   copy      = remux only (video copy + AAC audio) — near-zero CPU; needs H.264 source
#   transcode = full libx264 re-encode — heavy CPU; for H.265/incompatible/broken sources
#   auto      = probe source; H.264 -> copy, otherwise transcode
# Usage: channel-stream.sh <channel_id>; reads /etc/livetv/channels/<id>.env

CH="$1"
[ -z "$CH" ] && { echo "no channel id"; exit 1; }
ENVFILE="/etc/livetv/channels/${CH}.env"
[ -f "$ENVFILE" ] || { echo "no env for $CH"; exit 1; }
# shellcheck disable=SC1090
. "$ENVFILE"

HLS_DIR="/var/www/hls/${CH}"
mkdir -p "$HLS_DIR"

MODE="${MODE:-auto}"
RES="${RES:-1280x720}"
VBITRATE="${VBITRATE:-3000k}"
MAXRATE="${MAXRATE:-3500k}"
BUFSIZE="${BUFSIZE:-7000k}"
SCALE="${RES/x/:}"

# Input options by source type
if [ "${CHANNEL_TYPE:-ts}" = "rtsp" ]; then
    IN_OPTS=(-rtsp_transport tcp -fflags +discardcorrupt+genpts)
    PROBE_OPTS=(-rtsp_transport tcp)
else
    IN_OPTS=(-fflags +discardcorrupt+genpts -rw_timeout 15000000 -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5)
    PROBE_OPTS=(-rw_timeout 10000000)
fi

HLS_OUT=(-f hls -hls_time 2 -hls_list_size 6
         -hls_flags delete_segments+append_list+discont_start
         -hls_allow_cache 0 -hls_segment_type mpegts
         -hls_segment_filename "${HLS_DIR}/seg%05d.ts" "${HLS_DIR}/live.m3u8")

decide_mode() {
    # echoes "copy" or "transcode"
    if [ "$MODE" = "copy" ]; then echo copy; return; fi
    if [ "$MODE" = "transcode" ]; then echo transcode; return; fi
    # auto: probe video codec
    local vc
    vc=$(ffprobe -v error "${PROBE_OPTS[@]}" -select_streams v:0 \
         -show_entries stream=codec_name -of default=nk=1:nw=1 "$CHANNEL_URL" 2>/dev/null | head -1)
    if [ "$vc" = "h264" ]; then echo copy; else echo transcode; fi
}

while true; do
    rm -f "${HLS_DIR}"/*.ts "${HLS_DIR}"/*.m3u8
    RUNMODE=$(decide_mode)
    echo "[channel ${CH}] mode=${RUNMODE} from ${CHANNEL_URL}"

    if [ "$RUNMODE" = "copy" ]; then
        # Remux only: copy video, ensure browser-safe AAC audio (cheap). Near-zero CPU.
        /usr/bin/ffmpeg -hide_banner -loglevel warning \
            "${IN_OPTS[@]}" -i "${CHANNEL_URL}" \
            -max_muxing_queue_size 1024 \
            -c:v copy -c:a aac -b:a 128k -ar 44100 -ac 2 \
            "${HLS_OUT[@]}"
    else
        # Full re-encode (H.265/incompatible/broken sources)
        /usr/bin/ffmpeg -hide_banner -loglevel warning \
            "${IN_OPTS[@]}" -i "${CHANNEL_URL}" \
            -max_muxing_queue_size 1024 \
            -vf "scale=${SCALE},fps=25" -fps_mode cfr -af "aresample=async=1" \
            -c:v libx264 -preset veryfast -b:v "${VBITRATE}" -maxrate "${MAXRATE}" -bufsize "${BUFSIZE}" \
            -profile:v high -level 4.0 -g 50 -keyint_min 50 -sc_threshold 0 -pix_fmt yuv420p -threads 4 \
            -c:a aac -b:a 128k -ar 44100 -ac 2 \
            "${HLS_OUT[@]}"
    fi

    echo "[channel ${CH}] ffmpeg exited ($?). restarting in 3s..."
    sleep 3
done
