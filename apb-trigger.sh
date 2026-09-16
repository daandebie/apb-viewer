#!/bin/zsh
# Start elke paar minuten een run van bijwerken.yml. Nodig omdat GitHub het schema van 5 minuten
# in een publieke repo niet betrouwbaar afvuurt (16-09-2026: gaten van uren).
# Draait alleen tijdens de APB-dagen; daarbuiten volstaat de dagelijkse run.
export PATH="/opt/homebrew/bin:/usr/bin:/bin"
REPO="daandebie/apb-viewer"
LOG="$HOME/Projects/Claude/apb-viewer/trigger.log"
vandaag=$(date +%F)
uur=$(date +%H)
[[ "$vandaag" > "2026-09-15" && "$vandaag" < "2026-09-19" ]] || exit 0
[[ "$uur" -ge 8 && "$uur" -le 23 ]] || exit 0
# Loopt er al een run, dan niets doen: een tweede zou toch op de concurrency-group wachten.
if gh run list -R "$REPO" -L 1 --json status -q '.[0].status' 2>/dev/null | grep -qE 'in_progress|queued'; then
  echo "$(date '+%F %H:%M:%S') run loopt al" >> "$LOG"
  exit 0
fi
if gh workflow run bijwerken.yml -R "$REPO" 2>>"$LOG"; then
  echo "$(date '+%F %H:%M:%S') run gestart" >> "$LOG"
else
  echo "$(date '+%F %H:%M:%S') STARTEN MISLUKT" >> "$LOG"
fi
