#!/bin/zsh
# One-shot moot session: a fresh state directory and a tmux window with the
# hub and your observer seat. The agent sessions join themselves — Claude Code
# through the skill, OpenCode through the plugin — so this script only prints
# how to start them (see docs/MANUAL-TEST.md).
#
#   scripts/session.sh                       # fresh state dir /tmp/moot-<date>-<time>
#   scripts/session.sh --home /tmp/moot-x    # fixed home instead of a timestamped one
#   scripts/session.sh --transcripts DIR     # transcripts here instead of ~/.moot/sessions
#   scripts/session.sh --max-rounds 2        # round budget for this hub (default 24)
#   scripts/session.sh --no-notify           # passed through to the hub
#   scripts/session.sh --observer frank      # your name on the floor (default: $USER, made valid for the hub)
#   scripts/session.sh --full                # observer: do not cut statements to one line
#   scripts/session.sh --width N             # observer: line width (default: terminal)
#
# Stop: Ctrl-C in the hub pane (closes the clients), then `tmux kill-window`.
# The state directory stays on disk; delete it to forget the session. The
# transcripts live outside it, so deleting the home keeps the history.

set -euo pipefail

REPO=${0:A:h:h}
STAMP=$(date +%Y%m%d-%H%M%S)
HOME_DIR=""
TRANSCRIPT_DIR=""
MAX_ROUNDS_FLAG=""
NOTIFY_FLAG=""
OBSERVE_FLAGS=""
OBSERVER_GIVEN=""
# observer name from $USER, made valid for the hub ([A-Za-z0-9][A-Za-z0-9_-]{0,31})
OBSERVER=$(print -r -- "${USER:-observer}" | tr -c 'A-Za-z0-9_-\n' '-' | cut -c1-32)
[[ $OBSERVER == [A-Za-z0-9]* ]] || OBSERVER="observer"

while (( $# )); do
  case $1 in
    --home) HOME_DIR=$2; shift 2 ;;
    --transcripts) TRANSCRIPT_DIR=$2; shift 2 ;;
    --max-rounds) MAX_ROUNDS_FLAG="--max-rounds $2"; shift 2 ;;
    --no-notify) NOTIFY_FLAG="--no-notify"; shift ;;
    --observer) OBSERVER=$2; OBSERVER_GIVEN=1; shift 2 ;;
    --full) OBSERVE_FLAGS="$OBSERVE_FLAGS --full"; shift ;;
    --width) OBSERVE_FLAGS="$OBSERVE_FLAGS --width $2"; shift 2 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if [[ -z $OBSERVER_GIVEN && $OBSERVER != "${USER:-}" ]]; then
  echo "session.sh: seat name '$OBSERVER' (from \$USER '${USER:-}', made valid for the hub — pass --observer to choose)" >&2
fi

if [[ -z $HOME_DIR ]]; then
  # short path: macOS limits AF_UNIX socket paths to 104 bytes, and the
  # control sockets live under $HOME_DIR/ctl/<session>.sock
  HOME_DIR=/tmp/moot-$STAMP
  : ${TRANSCRIPT_DIR:=$HOME/.moot/sessions/$STAMP/transcripts}
else
  # an explicit --home is the caller's choice of where everything lives
  : ${TRANSCRIPT_DIR:=$HOME_DIR/transcripts}
fi
if [[ -e $HOME_DIR/hub.sock ]]; then
  echo "session.sh: $HOME_DIR already has a hub socket — stop that hub first or pick another --home" >&2
  exit 1
fi
mkdir -p "$HOME_DIR" "$TRANSCRIPT_DIR"
command -v tmux >/dev/null || { echo "session.sh: tmux not found" >&2; exit 1; }

WINDOW="moot"

# Pane layout:  hub (top)  /  observer (bottom, where you type).
# Panes are addressed by id (%N), which does not depend on pane-base-index.
if [[ -n ${TMUX:-} ]]; then
  HUB_PANE=$(tmux new-window -P -F '#{pane_id}' -n "$WINDOW" -c "$REPO")
else
  HUB_PANE=$(tmux new-session -d -P -F '#{pane_id}' -s "$WINDOW" -n "$WINDOW" -c "$REPO")
fi
OBS_PANE=$(tmux split-window -P -F '#{pane_id}' -v -t "$HUB_PANE" -c "$REPO")
tmux send-keys -t "$HUB_PANE" "uv run moot serve --home $HOME_DIR --transcripts $TRANSCRIPT_DIR $MAX_ROUNDS_FLAG $NOTIFY_FLAG" C-m
tmux send-keys -t "$OBS_PANE" "sleep 1; uv run moot observe --home $HOME_DIR --name $OBSERVER$OBSERVE_FLAGS" C-m
tmux select-pane -t "$OBS_PANE"   # the observer pane: that is where you type

cat <<EOF

moot session started in tmux window '$WINDOW'  (home: $HOME_DIR)
  top: hub   bottom: observer (type here)
  seat: $OBSERVER  (the name agents address you by; the band shows it as @$OBSERVER)
  transcripts: $TRANSCRIPT_DIR  (also reachable as $HOME_DIR/transcripts)

The hub wrote ~/.moot/current, so the spokes below find this session without
a path. They need \`moot\` on PATH: \`uv tool install --editable $REPO\`
(or prefix every command with $REPO/.venv/bin/).

Claude Code: in your project run  /moot join alpha "<role>"
OpenCode:    start it with        MOOT_NAME=beta MOOT_ROLE="<role>" opencode
Fallback:    pane: moot stream --name <n> --session <n> --inbox $HOME_DIR/<n>.in
             model: moot wait --session <n> --inbox $HOME_DIR/<n>.in  /
                    moot peek --session <n> --inbox $HOME_DIR/<n>.in  /
                    moot say --session <n> @alpha --kind answer "…"

EOF
if [[ -z ${TMUX:-} ]]; then
  echo "attach with:  tmux attach -t $WINDOW"
fi
