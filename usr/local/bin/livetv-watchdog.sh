#!/bin/bash
# Multi-channel watchdog: per-channel freshness + A/V sync check; restarts unhealthy channels.
# Runs every 15s via livetv-watchdog.timer.

HLS_ROOT="/var/www/hls"
MAX_AV_SKEW=5     # sec; |audio-video| start_time gap above this = broken
MAX_AGE=20        # sec; newest segment older than this = stalled
LOG_TAG="livetv-watchdog"

restart() {
    logger -t "$LOG_TAG" "channel $1 UNHEALTHY ($2) — restarting"
    systemctl restart "livetv-channel@$1.service"
}

for dir in "$HLS_ROOT"/*/; do
    [ -d "$dir" ] || continue
    CH=$(basename "$dir")
    UNIT="livetv-channel@${CH}.service"

    # only watch channels that are supposed to be running
    systemctl is-enabled --quiet "$UNIT" 2>/dev/null || continue
    if ! systemctl is-active --quiet "$UNIT"; then
        logger -t "$LOG_TAG" "$UNIT not active — starting"
        systemctl start "$UNIT"
        continue
    fi

    mapfile -t SEGS < <(ls -t "$dir"*.ts 2>/dev/null)
    [ "${#SEGS[@]}" -lt 2 ] && continue          # warming up

    AGE=$(( $(date +%s) - $(stat -c %Y "${SEGS[0]}") ))
    if [ "$AGE" -gt "$MAX_AGE" ]; then
        restart "$CH" "stalled ${AGE}s"
        continue
    fi

    SEG="${SEGS[1]}"   # 2nd newest (fully written)
    VID=$(ffprobe -v error -select_streams v:0 -show_entries stream=start_time -of default=nw=1:nk=1 "$SEG" 2>/dev/null)
    AUD=$(ffprobe -v error -select_streams a:0 -show_entries stream=start_time -of default=nw=1:nk=1 "$SEG" 2>/dev/null)
    [ -z "$VID" ] || [ -z "$AUD" ] && continue

    BAD=$(awk -v a="$VID" -v b="$AUD" -v m="$MAX_AV_SKEW" 'BEGIN{d=a-b;if(d<0)d=-d;print(d>m)?1:0}')
    [ "$BAD" = "1" ] && restart "$CH" "A/V desync"
done
exit 0
