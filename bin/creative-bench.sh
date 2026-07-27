#!/usr/bin/env bash
# Benchmark local GGUF models against ONE creative-writing prompt.
#
# Walks every model you pick, runs it with thinking and/or without, saves one
# result file per run, then writes a ready-to-paste SUMMARIZE.md that inlines
# every result for a comparison pass in a fresh session.
#
# Usage:
#   ~/Apps/bin/creative-bench.sh                     # fully interactive
#   ~/Apps/bin/creative-bench.sh --system s.txt --user u.txt --temp 1.1 \
#       --models "Fable,APEX" --mode both            # non-interactive
#   ~/Apps/bin/creative-bench.sh --help
#
# Options:
#   --system FILE     system prompt from a file (skips the prompt)
#   --user FILE       user prompt from a file (skips the prompt)
#   --temp N          temperature (default 1.0; skips the prompt)
#   --models "a,b"    comma-separated substrings matched against the GGUF paths
#                     (skips the per-model menu). A pattern may carry its own
#                     mode as "a:thinking,b:nothinking,c:both" — bare patterns
#                     fall back to --mode/MODE.
#   --mode MODE       thinking (default) | nothinking | both — default mode for
#                     any --models pattern that doesn't specify its own.
#                     No-thinking is no longer offered in the interactive menu:
#                     it lost to thinking on every judge in the first real
#                     session. Still selectable here, or with MODE= in the env.
#   --yes             don't ask for confirmation before running the queue
#   --no-card-settings  ignore the per-model HuggingFace card sampler settings and
#                     run every model on one identical profile (controlled compare)
#   --summarize       after the benchmark, have EVERY benchmarked model read
#                     SUMMARIZE.md and write its own judgement of all the
#                     results, saved as summary_<stamp>_<model>.md
#   --no-summarize    skip that question and stop after SUMMARIZE.md
#   --meta-summary DIR --meta-model PATTERN --system FILE --user FILE
#                     Round 3: run ONE model (first GGUF path match for
#                     PATTERN) against the given prompt, appending the result
#                     into the existing session DIR as
#                     round3_<stamp>_<model>.md. Meant to be fed a prompt that
#                     asks the model to synthesize the Round 2 judge verdicts —
#                     Roundtable builds that prompt from its own computed
#                     consensus table, not from raw judge prose.
#

# Env knobs (all optional):
#   TEMP TOP_P TOP_K MIN_P REPEAT_PENALTY PRESENCE_PENALTY FREQUENCY_PENALTY
#   DRY_MULTIPLIER DRY_BASE DRY_ALLOWED_LENGTH DRY_PENALTY_LAST_N
#   MAX_TOKENS=8192       cap on tokens generated per run
#   SEED=<n>              pin the shared seed instead of picking one at random
#   CTX=<n>               pin context instead of --fit auto-sizing
#   FIT_TARGET FIT_MIN_CTX N_GPU_LAYERS CACHE_TYPE_K CACHE_TYPE_V
#   REASONING_BUDGET=-1   thinking-token cap for thinking runs (-1 = unlimited)
#   SUMMARY_MODE=thinking      thinking|nothinking for the summary pass
#   SUMMARY_TEMP=0.7           temperature for the summary pass
#   SUMMARY_MAX_TOKENS=<n>     token cap for each summary (defaults to MAX_TOKENS)
#   CHAT_TEMPLATE_FILE    'auto' (default) = the GGUF's embedded template; set a
#                         path to force one. Unlike code-stack.sh this does NOT
#                         swap in the tool-calling template — that one is tuned
#                         for agentic coding, not prose.
#   OUTDIR=~/Apps/creative-bench   where session dirs are written
#   PORT=11436            llama-server port (deliberately not code-stack's 11435)
set -euo pipefail

# llama-server: an explicit $LLAMA_BIN always wins; otherwise prefer one already
# on PATH (what most people who built llama.cpp will have), and only then fall
# back to the author's own build location as a last guess.
LLAMA_BIN="${LLAMA_BIN:-$(command -v llama-server 2>/dev/null || true)}"
LLAMA_BIN="${LLAMA_BIN:-$HOME/Apps/llama.cpp-src/build/bin/llama-server}"
LM_MODELS="${LM_MODELS:-$HOME/.lmstudio/models}"
OUTDIR="${OUTDIR:-$HOME/Apps/creative-bench}"
PORT="${PORT:-11436}"

# --- creative sampling profile ----------------------------------------------
# Deliberately NOT code-stack.sh's coding profile:
#   temp 1.0 (vs 0.7)          — prose wants the extra spread
#   presence_penalty 0         — code-stack keeps 0.1 to stop agentic loops;
#                                here it would just punish recurring names
#   DRY OFF                    — I turned DRY on here originally, reasoning that
#                                prose wants anti-repetition. That was wrong, and
#                                Jason caught it: same model, same prompt, clean
#                                prose in LM Studio, while this bench's APEX
#                                output dropped articles and pronouns ("wasn
#                                asking permission. Setting course. nodded,
#                                choice slipping"). Confirmed 2026-07-23 by
#                                re-running with DRY_MULTIPLIER=0: fluent again.
#                                DRY penalizes any token that would EXTEND a
#                                token sequence already seen anywhere in the
#                                context (allowed_length 2, penalty_last_n -1 =
#                                whole context). In long prose the recurring
#                                n-grams are ordinary phrasing, so it strips
#                                function words rather than the recycled imagery
#                                it was aimed at. It also mangled repeated model
#                                names in the judge pass. No DRY setting appears
#                                anywhere in Jason's LM Studio configs, i.e. he
#                                has never turned it on there — I have NOT
#                                verified what LM Studio's own default is.
TEMP_USER_SET="${TEMP+1}"
TOP_P_USER_SET="${TOP_P+1}"; TOP_K_USER_SET="${TOP_K+1}"; MIN_P_USER_SET="${MIN_P+1}"
REPEAT_PENALTY_USER_SET="${REPEAT_PENALTY+1}"; PRESENCE_PENALTY_USER_SET="${PRESENCE_PENALTY+1}"
TEMP="${TEMP:-1.0}"
TOP_P="${TOP_P:-0.95}"
TOP_K="${TOP_K:-40}"
MIN_P="${MIN_P:-0.05}"
REPEAT_PENALTY="${REPEAT_PENALTY:-1.0}"   # 1.0 = off. Jason's LM Studio per-model
                                          # configs vary: 1.1 enabled on Fable/heretic-v2,
                                          # present-but-disabled on APEX.
PRESENCE_PENALTY="${PRESENCE_PENALTY:-0}"
FREQUENCY_PENALTY="${FREQUENCY_PENALTY:-0}"
DRY_MULTIPLIER="${DRY_MULTIPLIER:-0}"   # OFF — see the note above. Set 0.8 to try it again.
DRY_BASE="${DRY_BASE:-1.75}"
DRY_ALLOWED_LENGTH="${DRY_ALLOWED_LENGTH:-2}"
DRY_PENALTY_LAST_N="${DRY_PENALTY_LAST_N:--1}"
MAX_TOKENS="${MAX_TOKENS:-8192}"

# --- server settings (same proven values as code-stack.sh) ------------------
CTX="${CTX:-auto}"
FIT_TARGET="${FIT_TARGET:-1024}"
FIT_MIN_CTX="${FIT_MIN_CTX:-32768}"
N_GPU_LAYERS="${N_GPU_LAYERS:-999}"
CACHE_TYPE_K="${CACHE_TYPE_K:-q8_0}"
CACHE_TYPE_V="${CACHE_TYPE_V:-q8_0}"
REASONING_BUDGET="${REASONING_BUDGET:--1}"
READY_TIMEOUT="${READY_TIMEOUT:-300}"
REQUEST_TIMEOUT="${REQUEST_TIMEOUT:-3600}"


# --- per-model sampler settings from the HuggingFace model cards ---------------
# Verified 2026-07-23 by reading each card. A single shared profile cannot satisfy
# these — Gemma4's author explicitly says its numbers "aren't the stock Gemma
# defaults", and the Qwen cards give DIFFERENT settings for thinking vs instruct
# mode, so benchmarking both modes on one profile silently handicaps one of them.
# CARD_SETTINGS=0 (or --no-card-settings) turns this off for a controlled run
# where every model gets identical samplers. Anything pinned via env/--temp always
# wins over the card.
#
# The settings themselves live in presets/model-cards.json (bundled) merged
# with ~/.config/roundtable/model-cards.json (edited on the roundtable web
# page's "New run" form) -- not hardcoded here -- so editing a card on the page
# changes what the next bench run actually uses. See roundtable/model_cards.py.
CARD_SETTINGS="${CARD_SETTINGS:-1}"
ROUNDTABLE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

card_settings() {  # card_settings <model-path> <thinking|nothinking>
  # sets C_TEMP C_TOP_P C_TOP_K C_MIN_P C_REP C_PRES C_PROFILE C_THINKS
  local m="${1,,}" mode="$2"
  C_TEMP=""; C_TOP_P=""; C_TOP_K=""; C_MIN_P=""; C_REP=""; C_PRES=""
  C_PROFILE="script defaults (no published card settings)"
  C_THINKS=1
  local out
  out="$(PYTHONPATH="$ROUNDTABLE_ROOT" python3 -c '
import shlex, sys
from roundtable import model_cards

name, mode = sys.argv[1], sys.argv[2]
card = model_cards.match(name)
if not card:
    sys.exit(0)

profile = card["thinking"] if mode == "thinking" else card["nothinking"]


def emit(key, value):
    print("%s=%s" % (key, "" if value is None else value))


emit("C_TEMP", profile.get("temp"))
emit("C_TOP_P", profile.get("top_p"))
emit("C_TOP_K", profile.get("top_k"))
emit("C_MIN_P", profile.get("min_p"))
emit("C_REP", profile.get("repeat"))
emit("C_PRES", profile.get("presence"))
print("C_THINKS=%d" % (1 if card.get("thinks", True) else 0))

label = card.get("title", card["id"])
same = card["thinking"] == card["nothinking"]
text = ("%s card" % label if same
        else "%s card / %s" % (label, "thinking-general" if mode == "thinking"
                                else "instruct-general"))
print("C_PROFILE=%s" % shlex.quote(text))
' "$m" "$mode")"
  [[ -n "$out" ]] && eval "$out"
}

# --- summary pass -----------------------------------------------------------
# After the benchmark, optionally hand SUMMARIZE.md back to each model that was
# benchmarked and save its judgement as summary_<stamp>_<model>.md.
# Thinking is ON by default here regardless of how the model was benchmarked:
# ranking N samples against 7 criteria is exactly the analytical work a
# scratchpad helps with, which is a different job from writing the prose.
# TEMP 0.7 (not the creative 1.0, not 0.2 — which is known to break these Qwen3.6
# thinking models, see project_code_stack_ctx_mtp) for a steadier judge.
SUMMARY_MODE="${SUMMARY_MODE:-thinking}"
SUMMARY_TEMP="${SUMMARY_TEMP:-0.7}"
SUMMARY_MAX_TOKENS="${SUMMARY_MAX_TOKENS:-16384}"   # not $MAX_TOKENS: on 2026-07-23 the
                                   # Gemma4 judge spent all 8192 inside its reasoning trace and
                                   # emitted an EMPTY verdict. Judges need room for think+answer.

SYS_ARG=""; USER_ARG=""; MODELS_ARG=""; MODE_ARG=""; ASSUME_YES=0; SUMMARIZE_ARG=""
SUMMARIZE_ONLY_DIR=""
META_SUMMARY_DIR=""; META_MODEL_ARG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --summarize)    SUMMARIZE_ARG=1; shift ;;
    --no-summarize) SUMMARIZE_ARG=0; shift ;;
    --summarize-only) SUMMARIZE_ONLY_DIR="${2:?--summarize-only needs a session dir}"; shift 2 ;;
    --meta-summary) META_SUMMARY_DIR="${2:?--meta-summary needs a session dir}"; shift 2 ;;
    --meta-model)   META_MODEL_ARG="${2:?--meta-model needs a substring pattern}"; shift 2 ;;
    --system) SYS_ARG="${2:?--system needs a file}"; shift 2 ;;
    --user)   USER_ARG="${2:?--user needs a file}"; shift 2 ;;
    --temp)   TEMP="${2:?--temp needs a number}"; TEMP_USER_SET=1; shift 2 ;;
    --models) MODELS_ARG="${2:?--models needs a list}"; shift 2 ;;
    --mode)   MODE_ARG="${2:?--mode needs a value}"; shift 2 ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --no-card-settings) CARD_SETTINGS=0; shift ;;
    --no-blind) export BLIND=0; shift ;;
    -h|--help)
      sed -n "2,$(($(grep -n '^set -euo pipefail' "$0" | cut -d: -f1) - 1))p" "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

[[ -x "$LLAMA_BIN" ]] || { echo "llama-server not found at: $LLAMA_BIN
Build llama.cpp (https://github.com/ggml-org/llama.cpp) and either put llama-server
on your PATH or point LLAMA_BIN at it, e.g. LLAMA_BIN=/path/to/llama-server. See the
README (\"The bench runner\") for LLAMA_BIN / LM_MODELS." >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 required" >&2; exit 1; }

if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
  echo "port $PORT already in use — another creative-bench.sh running?" >&2
  exit 1
fi

if [[ -n "$META_SUMMARY_DIR" ]]; then
  [[ -n "$META_MODEL_ARG" ]] || { echo "--meta-summary needs --meta-model PATTERN" >&2; exit 1; }
  [[ -n "$SYS_ARG" && -n "$USER_ARG" ]] || { echo "--meta-summary needs --system and --user (the synthesis prompt, not the original session's)" >&2; exit 1; }
fi

if [[ -n "$SUMMARIZE_ONLY_DIR" ]]; then
  # Judge-only mode: reuse a finished session's results, skip the benchmark.
  SDIR="${SUMMARIZE_ONLY_DIR%/}"
  [[ -d "$SDIR" ]] || { echo "no such session dir: $SDIR" >&2; exit 1; }
elif [[ -n "$META_SUMMARY_DIR" ]]; then
  # Round 3: reuse a finished session's dir too. system-prompt.txt/user-prompt.txt
  # below are left untouched — SYS_ARG/USER_ARG here are the synthesis prompt,
  # not Round 1's, and must never overwrite the record of what Round 1 ran.
  SDIR="${META_SUMMARY_DIR%/}"
  [[ -d "$SDIR" ]] || { echo "no such session dir: $SDIR" >&2; exit 1; }
  mkdir -p "$SDIR/logs"
else
  SESSION="$(date +%Y%m%d-%H%M%S)"
  SDIR="$OUTDIR/$SESSION"
  mkdir -p "$SDIR/logs"
fi

# --- helper: read a prompt (file path, or multi-line paste ending in ".") ----
read_prompt() {   # read_prompt <label> <dest-file> <preset-file-or-empty>
  local label="$1" dest="$2" preset="${3:-}"
  if [[ -n "$preset" ]]; then
    [[ -r "$preset" ]] || { echo "can't read $preset" >&2; exit 1; }
    cp "$preset" "$dest"
    echo "$label: $preset ($(wc -c <"$dest") bytes)"
    return
  fi
  if [[ ! -t 0 ]]; then
    echo "non-interactive stdin and no --${label%% *} given" >&2
    exit 1
  fi
  echo
  echo "$label — paste it, then a single '.' on its own line to finish."
  echo "(or type a file path on the first line and press Enter)"
  : > "$dest"
  local first=1 line
  while IFS= read -r line; do
    if [[ $first -eq 1 ]]; then
      first=0
      # A lone existing path on line 1 means "use this file".
      if [[ -n "$line" && -f "${line/#\~/$HOME}" ]]; then
        cp "${line/#\~/$HOME}" "$dest"
        echo "  loaded ${line/#\~/$HOME} ($(wc -c <"$dest") bytes)"
        return
      fi
    fi
    [[ "$line" == "." ]] && break
    printf '%s\n' "$line" >> "$dest"
  done
  echo "  $(wc -c <"$dest") bytes captured"
}

if [[ -z "$SUMMARIZE_ONLY_DIR" && -z "$META_SUMMARY_DIR" ]]; then
read_prompt "system prompt" "$SDIR/system-prompt.txt" "$SYS_ARG"
read_prompt "user prompt"   "$SDIR/user-prompt.txt"   "$USER_ARG"

if [[ ! -s "$SDIR/user-prompt.txt" ]]; then
  echo "user prompt is empty — nothing to benchmark" >&2
  exit 1
fi
fi

# --- temperature ------------------------------------------------------------
if [[ -z "$SUMMARIZE_ONLY_DIR" && -z "$META_SUMMARY_DIR" && -z "$TEMP_USER_SET" && -t 0 ]]; then
  read -r -p "temperature [default $TEMP]: " t || t=""
  t="${t:-$TEMP}"
  if [[ "$t" =~ ^[0-9]*\.?[0-9]+$ ]] && awk "BEGIN{exit !($t >= 0 && $t <= 2)}"; then
    TEMP="$t"
  else
    echo "  invalid temperature '$t' — keeping $TEMP" >&2
  fi
fi

SEED="${SEED:-$RANDOM$RANDOM}"   # one shared seed for the whole session, so the
                                 # comparison between models is apples-to-apples

# --- build the run queue ----------------------------------------------------
mapfile -t ALL_MODELS < <(find "$LM_MODELS" -type f -name '*.gguf' ! -name '*mmproj*' | sort)
(( ${#ALL_MODELS[@]} )) || { echo "no .gguf files found under $LM_MODELS" >&2; exit 1; }

name_of() {  # path -> just the model name, no folder, no extension, no size
  local n; n="$(basename "$1" .gguf)"
  printf '%s' "$n"
}

slug() {  # same name, filesystem-safe — no folder prefix, the timestamp in the
          # filename already makes runs unique
  printf '%s' "$(name_of "$1" | tr -c 'A-Za-z0-9._-' '-')"
}

QUEUE_MODEL=(); QUEUE_MODE=()

# Judge-only mode: rebuild the model list from the existing result files'
# model_path frontmatter rather than from a menu.
if [[ -n "$SUMMARIZE_ONLY_DIR" ]]; then
  mapfile -t QUEUE_MODEL < <(
    grep -h '^model_path:' "$SDIR"/[0-9]*.md 2>/dev/null |
      sed 's/^model_path: *"\(.*\)"$/\1/' | sort -u)
  (( ${#QUEUE_MODEL[@]} )) || { echo "no result files with a model_path in $SDIR" >&2; exit 1; }
  for _ in "${QUEUE_MODEL[@]}"; do QUEUE_MODE+=("thinking"); done
  OK_RUNS_PRESET="$(grep -lc '^error: null' "$SDIR"/[0-9]*.md 2>/dev/null | wc -l)"
  echo "judge-only mode: ${#QUEUE_MODEL[@]} models, $OK_RUNS_PRESET usable results in $SDIR"
fi

enqueue() {  # enqueue <model> <t|n|b>
  local key="$2"
  # 'both' on a model with no reasoning mode would queue the same run twice:
  # Cydonia is Mistral-based, --reasoning on/off is a no-op there, and with one
  # shared seed the two outputs came back byte-identical (verified 2026-07-23).
  if [[ "$key" == b ]]; then
    card_settings "$1" thinking
    if [[ "$C_THINKS" == "0" ]]; then
      echo "  note: $(name_of "$1") has no thinking mode — queuing one run, not two" >&2
      key=t
    fi
  fi
  case "$key" in
    t) QUEUE_MODEL+=("$1"); QUEUE_MODE+=("thinking") ;;
    n) QUEUE_MODEL+=("$1"); QUEUE_MODE+=("nothinking") ;;
    b) QUEUE_MODEL+=("$1"); QUEUE_MODE+=("thinking")
       QUEUE_MODEL+=("$1"); QUEUE_MODE+=("nothinking") ;;
  esac
}

# Thinking is the only advertised mode as of 2026-07-23: across the first real
# 9-run session, four of five judge models ranked every thinking output above
# every no-thinking one (planning the restructure is most of the job on
# editorial prompts). MODE=nothinking / --mode nothinking still work as an
# escape hatch — worth re-testing on pure prose-generation prompts, where the
# planning step may matter less.
DEFAULT_MODE_KEY=t
case "${MODE:-thinking}" in
  nothinking) DEFAULT_MODE_KEY=n ;;
  both)       DEFAULT_MODE_KEY=b ;;
esac

if [[ -n "$SUMMARIZE_ONLY_DIR" || -n "$META_SUMMARY_DIR" ]]; then
  :   # queue already built (summarize-only), or built separately below (round 3)
elif [[ -n "$MODELS_ARG" ]]; then
  case "${MODE_ARG:-${MODE:-thinking}}" in
    thinking) DEFAULT_M=t ;; nothinking) DEFAULT_M=n ;; both) DEFAULT_M=b ;;
    *) echo "invalid --mode '$MODE_ARG' (thinking|nothinking|both)" >&2; exit 1 ;;
  esac
  IFS=',' read -r -a PATTERNS <<< "$MODELS_ARG"
  for pat in "${PATTERNS[@]}"; do
    pat="$(echo "$pat" | sed 's/^ *//;s/ *$//')"
    # A pattern may carry its own mode as "pattern:mode" (Roundtable sends one
    # per model, since the same session can mix thinking-on and thinking-off
    # defaults per model); bare patterns fall back to --mode/MODE as before.
    m="$DEFAULT_M"
    if [[ "$pat" == *:* ]]; then
      pmode="${pat##*:}"; pat="${pat%:*}"
      case "$pmode" in
        thinking) m=t ;; nothinking) m=n ;; both) m=b ;;
        *) echo "invalid mode '$pmode' in --models pattern (thinking|nothinking|both)" >&2
           exit 1 ;;
      esac
    fi
    hit=0
    for mp in "${ALL_MODELS[@]}"; do
      if [[ "${mp,,}" == *"${pat,,}"* ]]; then enqueue "$mp" "$m"; hit=1; fi
    done
    (( hit )) || echo "  no model matched '$pat'" >&2
  done
else
  [[ -t 0 ]] || { echo "non-interactive stdin — pass --models/--mode" >&2; exit 1; }
  echo
  echo "─── pick models (${#ALL_MODELS[@]} found) ───"
  echo "  [y] run it   [s] skip (default)   [q] stop asking and run the selection"
  echo
  for mp in "${ALL_MODELS[@]}"; do
    read -r -p "$(name_of "$mp")  (y/s) " ans || ans="s"
    case "${ans,,}" in
      y|t) enqueue "$mp" "$DEFAULT_MODE_KEY" ;;
      n)   enqueue "$mp" n ;;   # still reachable if you type it, not advertised
      b)   enqueue "$mp" b ;;
      q) break ;;
      *) : ;;
    esac
  done
fi

if [[ -z "$META_SUMMARY_DIR" ]] && (( ${#QUEUE_MODEL[@]} == 0 )); then
  echo "nothing selected — exiting"; rm -rf "$SDIR"; exit 0
fi

if [[ -z "$SUMMARIZE_ONLY_DIR" && -z "$META_SUMMARY_DIR" ]]; then
echo
echo "─────────────────────────────────────────────────────────────"
echo "queue: ${#QUEUE_MODEL[@]} runs   temp=$TEMP   seed=$SEED   max_tokens=$MAX_TOKENS"
for i in "${!QUEUE_MODEL[@]}"; do
  printf '  %2d. %-55s %s\n' "$((i+1))" "$(name_of "${QUEUE_MODEL[$i]}")" "${QUEUE_MODE[$i]}"
done
echo "output: $SDIR"
echo "─────────────────────────────────────────────────────────────"
if [[ $ASSUME_YES -eq 0 && -t 0 ]]; then
  read -r -p "run? [Y/n] " go || go="y"
  [[ "${go:-y}" =~ ^[Yy]?$ ]] || { echo "aborted"; rm -rf "$SDIR"; exit 0; }
fi
fi

# Every model judges once, so the judge count is the queue with modes collapsed.
# Computed here rather than in pass 2 because the progress bar needs the total
# before the first run starts, not after the last one.
UNIQ_MODELS=(); for m in "${QUEUE_MODEL[@]}"; do
  seen=0; for u in ${UNIQ_MODELS[@]+"${UNIQ_MODELS[@]}"}; do [[ "$u" == "$m" ]] && seen=1; done
  (( seen )) || UNIQ_MODELS+=("$m")
done

# How many runs are coming. A run in flight has written no result file yet, so
# this is the only way the report can say "3 of 6" instead of just "3" — see
# session.load(). Judges are only knowable up front when --summarize/--no-summarize
# said so on the command line (the worker always passes one); left unwritten
# when the question is still going to be asked interactively after pass 1, and
# written for real when pass 2 starts.
if [[ -z "$SUMMARIZE_ONLY_DIR" && -z "$META_SUMMARY_DIR" ]]; then
  printf '%s\n' "${#QUEUE_MODEL[@]}" > "$SDIR/.expected-runs"
fi
if [[ -z "$META_SUMMARY_DIR" ]]; then      # Round 3 appends to a finished session
  case "$SUMMARIZE_ARG" in
    1) printf '%s\n' "${#UNIQ_MODELS[@]}" > "$SDIR/.expected-judges" ;;
    0) printf '0\n' > "$SDIR/.expected-judges" ;;
  esac
fi

# --- the request/writer helper ----------------------------------------------
HELPER="$SDIR/.call.py"
cat > "$HELPER" <<'PYEOF'
import json, os, re, sys, time, urllib.request, urllib.error

E = os.environ
def rd(p):
    with open(p) as f: return f.read()

msgs = []
sysp = rd(E["SYS_FILE"]).strip()
if sysp:
    msgs.append({"role": "system", "content": sysp})
msgs.append({"role": "user", "content": rd(E["USER_FILE"]).strip()})

body = {
    "model": "bench",
    "messages": msgs,
    "stream": False,
    "seed": int(E["SEED"]),
    "max_tokens": int(E["MAX_TOKENS"]),
    "temperature": float(E["TEMP"]),
    "top_p": float(E["TOP_P"]),
    "top_k": int(E["TOP_K"]),
    "min_p": float(E["MIN_P"]),
    "presence_penalty": float(E["PRESENCE_PENALTY"]),
    "frequency_penalty": float(E["FREQUENCY_PENALTY"]),
    "repeat_penalty": float(E["REPEAT_PENALTY"]),
    "dry_multiplier": float(E["DRY_MULTIPLIER"]),
    "dry_base": float(E["DRY_BASE"]),
    "dry_allowed_length": int(E["DRY_ALLOWED_LENGTH"]),
    "dry_penalty_last_n": int(E["DRY_PENALTY_LAST_N"]),
}

req = urllib.request.Request(
    "http://127.0.0.1:%s/v1/chat/completions" % E["PORT"],
    data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json"},
)

t0 = time.time()
err = None; content = ""; reasoning = ""; ntok = 0
try:
    with urllib.request.urlopen(req, timeout=float(E["REQUEST_TIMEOUT"])) as r:
        data = json.load(r)
    msg = data["choices"][0]["message"]
    content = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or ""
    ntok = (data.get("usage") or {}).get("completion_tokens", 0) or 0
except urllib.error.HTTPError as e:
    err = "HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:2000])
except Exception as e:
    err = "%s: %s" % (type(e).__name__, e)
elapsed = time.time() - t0
tps = round(ntok / elapsed, 2) if elapsed > 0 and ntok else 0

# --- ranking-line retry ------------------------------------------------------
# Judges are asked to end with "RANKING: {{A}} > {{B}} > ...", and some never
# do: across the first ten sessions one judge missed it every single time, so a
# fifth of all verdicts were being recovered by regex from prose instead of
# read from a line the judge wrote on purpose. Salvage parsers are where the
# silent errors live -- one of them dropped a first-place vote for months.
#
# So: if the line is missing or short, ask again. Same loaded model, same
# conversation, one extra call of a few hundred tokens -- no reload. The retry
# asks for the line ONLY; the judging already happened in the first reply and
# is not revisited. When it works, the line is spliced in and the file records
# ranking_retry: true, because a number obtained on the second ask should not
# look identical to one obtained on the first.
RANKING_RE = re.compile(r"^\s*RANKING\s*:\s*(.+)$", re.M | re.I)


def ranking_labels(text):
    m = RANKING_RE.search(text or "")
    return set(re.findall(r"\{\{\s*([A-Z])\s*\}\}", m.group(1))) if m else set()


want = {l.strip() for l in E.get("REQUIRE_LABELS", "").split(",") if l.strip()}
retried = False
retry_tokens = 0
retry_elapsed = 0.0
if want and not err and ranking_labels(content) != want:
    tags = " > ".join("{{%s}}" % l for l in sorted(want))
    follow = ("Reply with ONE line and nothing else — no preamble, no "
              "explanation, no code fence. Put the %d outputs you just judged "
              "in order, best first, using the tags exactly as written with "
              "braces, each tag exactly once:\n\nRANKING: %s"
              % (len(want), tags))
    retry_body = dict(body)
    retry_body["messages"] = msgs + [
        {"role": "assistant", "content": content},
        {"role": "user", "content": follow},
    ]
    # Enough room for a thinking model to reason its way to one line.
    retry_body["max_tokens"] = 1024
    t1 = time.time()
    try:
        rr = urllib.request.Request(
            "http://127.0.0.1:%s/v1/chat/completions" % E["PORT"],
            data=json.dumps(retry_body).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(rr, timeout=float(E["REQUEST_TIMEOUT"])) as r:
            rdata = json.load(r)
        rmsg = rdata["choices"][0]["message"]
        # A thinking model sometimes states the line in its reasoning and then
        # answers in prose; either place is the judge's own answer.
        for text in (rmsg.get("content") or "", rmsg.get("reasoning_content") or ""):
            if ranking_labels(text) == want:
                line = "RANKING: " + RANKING_RE.search(text).group(1).strip()
                # Drop the malformed original, or the reader finds it first.
                content = RANKING_RE.sub("", content).rstrip() + "\n\n" + line
                # Counted apart from the judging run, never folded into it:
                # tokens/tokens_per_sec describe the verdict's own generation,
                # and the report compares models on those numbers.
                retry_tokens = (rdata.get("usage") or {}).get("completion_tokens", 0) or 0
                retry_elapsed = time.time() - t1
                retried = True
                break
    except Exception:
        pass                       # a failed retry is the status quo, not a failure

def esc(s):
    return json.dumps(s)  # safe YAML scalar: JSON strings are valid YAML

with open(E["OUT_FILE"], "w") as f:
    f.write("---\n")
    f.write("model: %s\n"        % esc(E["MODEL_NAME"]))
    f.write("model_path: %s\n"   % esc(E["MODEL_PATH"]))
    f.write("thinking: %s\n"     % ("true" if E["MODE"] == "thinking" else "false"))
    f.write("temperature: %s\n"  % E["TEMP"])
    f.write("sampler_profile: %s\n" % esc(E.get("SAMPLER_PROFILE", "?")))
    f.write("samplers: %s\n" % esc("top_p %s, top_k %s, min_p %s, repeat %s, presence %s, dry %s" % (
        E.get("TOP_P"), E.get("TOP_K"), E.get("MIN_P"),
        E.get("REPEAT_PENALTY"), E.get("PRESENCE_PENALTY"), E.get("DRY_MULTIPLIER"))))
    f.write("seed: %s\n"         % E["SEED"])
    f.write("context: %s\n"      % (E.get("ACTUAL_CTX") or "unknown"))
    f.write("tokens: %d\n"       % ntok)
    f.write("tokens_per_sec: %s\n" % tps)
    f.write("elapsed_sec: %d\n"  % round(elapsed))
    f.write("error: %s\n"        % (esc(err) if err else "null"))
    if retried:
        f.write("ranking_retry: true\n")
        f.write("ranking_retry_tokens: %d\n" % retry_tokens)
        f.write("ranking_retry_sec: %d\n" % round(retry_elapsed))
    f.write("---\n\n")
    if err:
        f.write("## Error\n\n```\n%s\n```\n" % err)
    else:
        f.write("## Thinking\n\n%s\n\n" % (reasoning.strip() or "(none)"))
        f.write("## Output\n\n%s\n" % content.strip())

if err:
    print("ERR %s" % err.replace("\n", " ")[:160]); sys.exit(1)
print("OK %d tokens, %ss, %s tok/s" % (ntok, round(elapsed), tps))
PYEOF

# --- run loop ---------------------------------------------------------------
LLAMA_PID=""; TICK_PID=""

# Live elapsed counter for the long waits (model load, generation) — these run
# for minutes with nothing to print, and a frozen terminal is indistinguishable
# from a hung one.
tick_start() {  # tick_start <label>
  local label="$1"
  [[ -t 1 ]] || return 0   # piped/redirected: \r ticking would just be noise
  ( t=0
    while :; do
      sleep 5; t=$((t+5))
      printf '\r  → %s  %dm%02ds' "$label" $((t/60)) $((t%60))
    done ) &
  TICK_PID=$!
}
tick_stop() {
  if [[ -n "$TICK_PID" ]]; then
    kill "$TICK_PID" 2>/dev/null || true
    wait "$TICK_PID" 2>/dev/null || true
    TICK_PID=""
    printf '\r\033[K'   # wipe the ticker line so the result line lands clean
  fi
  return 0
}

vram_used() {  # MiB in use on the dGPU, or empty if nvidia-smi isn't around
  nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | head -1
}

cleanup() {
  tick_stop
  if [[ -n "$LLAMA_PID" ]] && kill -0 "$LLAMA_PID" 2>/dev/null; then
    kill -- -"$LLAMA_PID" 2>/dev/null || true
    wait "$LLAMA_PID" 2>/dev/null || true
  fi
}
trap 'cleanup; echo; echo "interrupted — completed results kept in $SDIR"; exit 130' INT TERM
trap cleanup EXIT

OK_RUNS=0; FAIL_RUNS=0

# do_run <model> <mode> <out-file> <log> <sys-file> <user-file> <temp> <max-tokens>
# One complete cycle: load the model, send one prompt, write the result file,
# unload. Used for both the benchmark pass and the summary pass, so the two
# always run under identical server settings.
do_run() {
  local MODEL="$1" MODE="$2" OUT_FILE="$3" LOG="$4"
  local RUN_SYS="$5" RUN_USER="$6" RUN_TEMP="$7" RUN_MAX_TOKENS="$8"
  local USE_CARDS="${9:-0}"
  local SLUG; SLUG="$(slug "$MODEL")"

  # Resolve this run's samplers: card values, unless the user pinned them.
  local R_TEMP="$RUN_TEMP" R_TOP_P="$TOP_P" R_TOP_K="$TOP_K" R_MIN_P="$MIN_P"
  local R_REP="$REPEAT_PENALTY" R_PRES="$PRESENCE_PENALTY" R_PROFILE="script defaults"
  if [[ "$USE_CARDS" == "1" && "$CARD_SETTINGS" == "1" ]]; then
    card_settings "$MODEL" "$MODE"
    R_PROFILE="$C_PROFILE"
    [[ -n "$C_TEMP"  && -z "$TEMP_USER_SET"             ]] && R_TEMP="$C_TEMP"
    [[ -n "$C_TOP_P" && -z "$TOP_P_USER_SET"            ]] && R_TOP_P="$C_TOP_P"
    [[ -n "$C_TOP_K" && -z "$TOP_K_USER_SET"            ]] && R_TOP_K="$C_TOP_K"
    [[ -n "$C_MIN_P" && -z "$MIN_P_USER_SET"            ]] && R_MIN_P="$C_MIN_P"
    [[ -n "$C_REP"   && -z "$REPEAT_PENALTY_USER_SET"   ]] && R_REP="$C_REP"
    [[ -n "$C_PRES"  && -z "$PRESENCE_PENALTY_USER_SET" ]] && R_PRES="$C_PRES"
  fi

  # Qwen3.6 + MTP detection (same probe as code-stack.sh: the filename is a fast
  # path, the GGUF architecture key is the real test — derivatives that drop
  # "Qwen" from the name, e.g. Ornith-1.0-35B, report qwen35moe and would
  # otherwise bench without MTP, which skews tok/s against their twins. The
  # nextn layers are probed too: many quants drop them and llama-server aborts
  # if asked for a draft it can't build).
  local QWEN36=0 HAS_MTP=0 KEYS=""
  KEYS="$(head -c 64000000 "$MODEL" | strings -n 6 \
            | grep -oiE '^[a-z0-9._]+\.(block_count|nextn_predict_layers)' || true)"
  if [[ "${MODEL,,}" =~ (qwen|qwopus)[-._\ ]*3[-._\ ]*6 ]] \
     || grep -qiE '^qwen3[5-9](moe|vl|vlmoe)?\.' <<<"$KEYS"; then
    QWEN36=1
    if grep -qi 'nextn_predict_layers' <<<"$KEYS"; then
      HAS_MTP=1
    fi
  fi

  local ARGS=(
    --model "$MODEL" --alias bench
    --host 127.0.0.1 --port "$PORT" --parallel 1
    --n-gpu-layers "$N_GPU_LAYERS" --flash-attn on
    --cache-type-k "$CACHE_TYPE_K" --cache-type-v "$CACHE_TYPE_V"
    --seed "$SEED"
    --temp "$R_TEMP" --top-p "$R_TOP_P" --top-k "$R_TOP_K" --min-p "$R_MIN_P"
    --repeat-penalty "$R_REP"
    --presence-penalty "$R_PRES" --frequency-penalty "$FREQUENCY_PENALTY"
    --dry-multiplier "$DRY_MULTIPLIER" --dry-base "$DRY_BASE"
    --dry-allowed-length "$DRY_ALLOWED_LENGTH" --dry-penalty-last-n "$DRY_PENALTY_LAST_N"
    --jinja --metrics
    --reasoning-format deepseek     # thinking lands in reasoning_content, so the
                                    # prose in the result file stays clean
  )
  if [[ "$CTX" == "auto" ]]; then
    ARGS+=( --fit on --fit-target "$FIT_TARGET" --fit-ctx "$FIT_MIN_CTX" )
  else
    ARGS+=( --ctx-size "$CTX" --fit off )
  fi
  if [[ "$MODE" == "nothinking" ]]; then
    ARGS+=( --reasoning off --chat-template-kwargs '{"enable_thinking":false}' )
  else
    ARGS+=( --reasoning on )
    [[ "$REASONING_BUDGET" != "-1" ]] && ARGS+=( --reasoning-budget "$REASONING_BUDGET" )
  fi
  if [[ "$HAS_MTP" == "1" ]]; then
    ARGS+=( --spec-type draft-mtp --spec-draft-n-max "${SPEC_DRAFT_N_MAX:-2}" )
    export GGML_CUDA_GRAPH_OPT=1 GGML_CUDA_FORCE_CUBLAS_COMPUTE_16F=1
  fi
  if [[ "${CHAT_TEMPLATE_FILE:-auto}" != "auto" && "${CHAT_TEMPLATE_FILE}" != "0" ]]; then
    ARGS+=( --chat-template-file "$CHAT_TEMPLATE_FILE" )
  fi

  local MTP_DESC="mtp off"
  [[ "$HAS_MTP" == "1" ]] && MTP_DESC="mtp on"
  echo "  → loading model onto the GPU ($MTP_DESC, $(du -h --apparent-size "$MODEL" 2>/dev/null | cut -f1) of weights)…"
  local T_LOAD=$SECONDS

  setsid "$LLAMA_BIN" "${ARGS[@]}" > "$LOG" 2>&1 &
  LLAMA_PID=$!

  tick_start "loading…"
  local READY=0
  for _ in $(seq "$READY_TIMEOUT"); do
    if curl -sf "http://127.0.0.1:$PORT/v1/models" >/dev/null 2>&1; then READY=1; break; fi
    sleep 1
    kill -0 "$LLAMA_PID" 2>/dev/null || break
  done
  tick_stop

  if [[ "$READY" -ne 1 ]]; then
    # A model that won't load must not kill the batch — record it and move on.
    echo "  ✗ server never became ready (see $LOG)"
    {
      echo "---"
      echo "model: \"$SLUG\""
      echo "model_path: \"$MODEL\""
      echo "thinking: $([[ "$MODE" == thinking ]] && echo true || echo false)"
      echo "temperature: $R_TEMP"
      echo "sampler_profile: \"$R_PROFILE\""
      echo "seed: $SEED"
      echo "context: unknown"
      echo "tokens: 0"
      echo "tokens_per_sec: 0"
      echo "elapsed_sec: 0"
      echo "error: \"llama-server failed to start\""
      echo "---"
      echo
      echo "## Error"
      echo
      echo '```'
      tail -25 "$LOG"
      echo '```'
    } > "$OUT_FILE"
    cleanup; LLAMA_PID=""
    return 1
  fi

  local ACTUAL_CTX
  ACTUAL_CTX="$(grep -oP 'new slot, n_ctx = \K[0-9]+' "$LOG" | head -1 || true)"
  echo "  ✓ model loaded in $((SECONDS - T_LOAD))s  (ctx ${ACTUAL_CTX:-?}$([[ -n "$(vram_used)" ]] && echo ", $(vram_used) MiB VRAM"))"

  echo "  → running prompt ($R_PROFILE: temp $R_TEMP, top_p $R_TOP_P, top_k $R_TOP_K, min_p $R_MIN_P, rep $R_REP, pres $R_PRES)…"
  tick_start "generating…"
  local RESULT RC
  set +e
  RESULT="$(
    OUT_FILE="$OUT_FILE" MODEL_NAME="$SLUG" MODEL_PATH="$MODEL" MODE="$MODE" \
    SYS_FILE="$RUN_SYS" USER_FILE="$RUN_USER" \
    PORT="$PORT" SEED="$SEED" TEMP="$R_TEMP" MAX_TOKENS="$RUN_MAX_TOKENS" \
    TOP_P="$R_TOP_P" TOP_K="$R_TOP_K" MIN_P="$R_MIN_P" SAMPLER_PROFILE="$R_PROFILE" \
    PRESENCE_PENALTY="$R_PRES" FREQUENCY_PENALTY="$FREQUENCY_PENALTY" \
    REPEAT_PENALTY="$R_REP" DRY_MULTIPLIER="$DRY_MULTIPLIER" \
    DRY_BASE="$DRY_BASE" DRY_ALLOWED_LENGTH="$DRY_ALLOWED_LENGTH" \
    DRY_PENALTY_LAST_N="$DRY_PENALTY_LAST_N" \
    ACTUAL_CTX="${ACTUAL_CTX:-}" REQUEST_TIMEOUT="$REQUEST_TIMEOUT" \
    REQUIRE_LABELS="${REQUIRE_LABELS:-}" \
    python3 "$HELPER" 2>&1
  )"
  RC=$?
  set -e
  tick_stop
  if [[ $RC -eq 0 ]]; then
    echo "  ✓ generation done — ${RESULT#OK }"
  else
    echo "  ✗ ${RESULT#ERR }"
  fi
  echo "  → wrote $(basename "$OUT_FILE")"

  echo "  → unloading model…"
  cleanup; LLAMA_PID=""
  # let the GPU actually release before the next model loads
  local VRAM_AFTER
  for _ in $(seq 30); do
    ss -ltn 2>/dev/null | grep -q ":$PORT " || break
    sleep 1
  done
  VRAM_AFTER="$(vram_used)"
  echo "  ✓ model unloaded${VRAM_AFTER:+  (${VRAM_AFTER} MiB VRAM still in use)}"
  return $RC
}

# --- pass 1: the benchmark --------------------------------------------------
if [[ -n "$SUMMARIZE_ONLY_DIR" ]]; then
  OK_RUNS="$OK_RUNS_PRESET"      # results already on disk from the earlier run
fi
for i in $([[ -n "$SUMMARIZE_ONLY_DIR" ]] || echo "${!QUEUE_MODEL[@]}"); do
  MODEL="${QUEUE_MODEL[$i]}"
  MODE="${QUEUE_MODE[$i]}"
  N="$(printf '%02d' $((i+1)))"
  SLUG="$(slug "$MODEL")"
  STAMP="$(date +%Y%m%d-%H%M%S)"

  echo
  echo "[$((i+1))/${#QUEUE_MODEL[@]}] $(name_of "$MODEL")  ($MODE)"

  if do_run "$MODEL" "$MODE" \
       "$SDIR/${N}_${STAMP}_${SLUG}_${MODE}.md" \
       "$SDIR/logs/${N}_${SLUG}_${MODE}.log" \
       "$SDIR/system-prompt.txt" "$SDIR/user-prompt.txt" \
       "$TEMP" "$MAX_TOKENS" 1; then
    OK_RUNS=$((OK_RUNS+1))
  else
    FAIL_RUNS=$((FAIL_RUNS+1))
  fi
done

# --- Round 3: one model synthesises the Round 2 verdicts ---------------------
# A completely separate path from the queue above: exactly one model, appended
# into the SAME session dir as a round3_*.md file. system-prompt.txt and
# user-prompt.txt (Round 1's) are never touched — $SYS_ARG/$USER_ARG here are
# whatever synthesis prompt the caller built (Roundtable builds it from its
# own computed consensus table, not from raw judge prose).
if [[ -n "$META_SUMMARY_DIR" ]]; then
  META_MATCH=""
  for mp in "${ALL_MODELS[@]}"; do
    if [[ "${mp,,}" == *"${META_MODEL_ARG,,}"* ]]; then META_MATCH="$mp"; break; fi
  done
  [[ -n "$META_MATCH" ]] || { echo "no model matched '$META_MODEL_ARG'" >&2; exit 1; }

  META_MODE="${MODE_ARG:-thinking}"
  META_SLUG="$(slug "$META_MATCH")"
  META_STAMP="$(date +%Y%m%d-%H%M%S)"
  echo
  echo "[round 3] $(name_of "$META_MATCH")  ($META_MODE)  synthesising the panel's verdicts"

  if do_run "$META_MATCH" "$META_MODE" \
       "$SDIR/round3_${META_STAMP}_${META_SLUG}.md" \
       "$SDIR/logs/round3_${META_SLUG}.log" \
       "$SYS_ARG" "$USER_ARG" "$TEMP" "$MAX_TOKENS" 0; then
    RC=0
  else
    RC=1
  fi
  rm -f "$HELPER"
  exit $RC
fi

# --- SUMMARIZE.md -----------------------------------------------------------
echo
echo "→ writing SUMMARIZE.md…"
# The judges, as slugs, so the builder can give each one its own running order
# with its own entries last. Empty in --no-summarize runs: no judges, one doc.
JUDGE_SLUGS=""
if [[ "${SUMMARIZE_ARG:-}" != "0" ]]; then
  for m in "${UNIQ_MODELS[@]}"; do JUDGE_SLUGS+="${JUDGE_SLUGS:+,}$(slug "$m")"; done
fi
export JUDGE_SLUGS
SDIR="$SDIR" TEMP="$TEMP" SEED="$SEED" BLIND="${BLIND:-1}" \
  JUDGE_SLUGS="${JUDGE_SLUGS:-}" python3 - <<'PYEOF'
import os, glob, random

sdir = os.environ["SDIR"]
files = sorted(f for f in glob.glob(os.path.join(sdir, "*.md"))
               if not os.path.basename(f).startswith(("SUMMARIZE", "summary_")))

def rd(p):
    try:
        with open(p) as f: return f.read().strip()
    except OSError:
        return ""

def split_front(text):
    """-> (dict of frontmatter, body)"""
    meta, body = {}, text
    if text.startswith("---\n"):
        end = text.find("\n---\n", 3)
        if end != -1:
            for line in text[4:end].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip().strip('"')
            body = text[end + 5:].lstrip("\n")
    return meta, body

# Blind judging: outputs are relabelled A/B/C..., shuffled, and stripped of model
# name, mode and perf stats. Reason (2026-07-23): Cydonia produced BYTE-IDENTICAL
# text in its thinking and no-thinking runs, and all four judges still ranked the
# copy labelled "no thinking" lower — one by eight places. They were grading the
# label, not the prose. The mapping lives in SUMMARIZE-KEY.md. BLIND=0 restores
# the old labelled document.
blind = os.environ.get("BLIND", "1") == "1"
parsed = [(f,) + split_front(rd(f)) for f in files]
if blind:
    import random
    random.Random(int(os.environ["SEED"])).shuffle(parsed)   # kill position bias, reproducibly
# Labels are wrapped in {{ }} so de-anonymising is an exact search-and-replace.
# A bare "A" collides with the article and "I" with the pronoun, which forced the
# de-anon pass to guess. Braces have no markdown meaning (asterisks get rewritten
# by judges) and don't clash with the [square brackets] these prompts ask for.
labels = {rec[0]: "{{%s}}" % chr(65 + i) for i, rec in enumerate(parsed)}

def render(records, letters):
    """One judge's document: the same text, in the order that judge sees it.

    ``letters`` maps a result file to the tag it carries *in this document*.
    Tags are assigned by presentation position, so a judge's "{{A}}" is always
    the entry it read first -- position and tag stay welded together, which is
    what lets one counterbalanced assignment neutralise both at once.
    """
    out = [
        "# Compare these creative-writing runs",
        "",
        ("Below are outputs from several local models answering the SAME prompt at the"
         if blind else
         "Below are outputs from several local models answering the SAME prompt, some"),
        ("same temperature and seed. They are anonymised and shuffled: judge the text only."
         if blind else
         "with thinking enabled and some without, all at the same temperature and seed."),
        "",
        "Judge them on:",
        "",
        "1. **Prose quality** — sentence rhythm, imagery, restraint, whether it reads like",
        "   a human wrote it or like an LLM performing 'good writing'.",
        "2. **Instruction adherence** — did it actually do what the system + user prompt asked?",
        "3. **Voice and originality** — distinct point of view, or generic/cliché filler?",
        "4. **Coherence** — internal consistency, structure, a real ending.",
        "5. **Repetition** — recycled phrasings, padded paragraphs, looping.",
        "6. **Sameness** — if two outputs look identical or near-identical, say so",
        "   explicitly rather than ranking them apart.",
        "",
        "Give me: a ranked table (rank, output letter, one-line verdict), then a short",
        "paragraph per output, then a final recommendation of which output I should",
        "prefer for this kind of writing — and say plainly if the differences are too",
        "small to call. Refer to each output ONLY by its tag, copied exactly as",
        "written, braces included — {{A}}, {{B}} and so on. Never drop the braces.",
        "",
        "Finish your reply with one final line in exactly this form, best first, so",
        "the ranking can be read without parsing your prose:",
        "",
        "RANKING: {{A}} > {{B}} > {{C}}",
        "",
        "---",
        "",
        "## The prompt every model was given",
        "",
        "### System prompt",
        "",
        "```",
        rd(os.path.join(sdir, "system-prompt.txt")) or "(none)",
        "```",
        "",
        "### User prompt",
        "",
        "```",
        rd(os.path.join(sdir, "user-prompt.txt")),
        "```",
        "",
        "Shared settings: temperature %s, seed %s (identical across all runs)."
            % (os.environ["TEMP"], os.environ["SEED"]),
        "",
        "---",
        "",
        "## Results (%d runs)" % len(files),
        "",
    ]

    for path, meta, body in records:
        mode = "thinking" if meta.get("thinking") == "true" else "no thinking"
        if blind:
            # No name, no mode, no tok/s — throughput alone identifies a model.
            out.append("### Output %s" % letters[path])
            out.append("")
        else:
            out.append("### %s (%s)" % (meta.get("model", os.path.basename(path)), mode))
            out.append("")
            out.append("`tokens: %s | tok/s: %s | elapsed: %ss | ctx: %s | error: %s`"
                       % (meta.get("tokens", "?"), meta.get("tokens_per_sec", "?"),
                          meta.get("elapsed_sec", "?"), meta.get("context", "?"),
                          meta.get("error", "null")))
            out.append("")
        if blind:
            # Show ONLY the deliverable. The "## Thinking" section is a dead giveaway:
            # a no-thinking run prints "(none)" there while a thinking run prints a
            # 1000-word trace, which re-leaks the mode the blinding just removed.
            tail = body.split("## Output", 1)
            out.append((tail[1] if len(tail) > 1 else body).lstrip("\n"))
        else:
            # Demote the result file's own "## Thinking"/"## Output" headings so they
            # don't outrank the "### model" heading they live under.
            out.append("\n".join(("##" + l) if l.startswith("## ") else l
                                 for l in body.splitlines()))
        out.append("")
        out.append("---")
        out.append("")
    return "\n".join(out)


def key_table(records, letters, title, extra=""):
    key = [title, "",
           "| Output | Model | Mode | tokens | tok/s | elapsed | file |",
           "|---|---|---|---|---|---|---|"]
    for path, meta, body in sorted(records, key=lambda r: letters[r[0]]):
        key.append("| %s | %s | %s | %s | %s | %ss | %s |" % (
            letters[path], meta.get("model", "?"),
            "thinking" if meta.get("thinking") == "true" else "no thinking",
            meta.get("tokens", "?"), meta.get("tokens_per_sec", "?"),
            meta.get("elapsed_sec", "?"), os.path.basename(path)))
    if extra:
        key += ["", extra]
    return "\n".join(key) + "\n"


def counterbalance(records, judges, rng):
    """One presentation order per judge. -> ({judge slug: [record, ...]}, repeats)

    Two rules, in priority order.

    A judge's own entries go LAST. Its vote on itself is discarded by the
    scorer anyway, so the slot a late position penalises is spent on a vote
    nobody counts — the penalty is thrown away rather than landing on some
    other model.

    Everything else is placed so each entry occupies each position AT MOST
    ONCE across the judges whose votes do count for it. That is the difference
    between removing the position effect and averaging it down: with six
    judges, leaving it to chance leaves a lot of imbalance standing. Built by
    randomised backtracking rather than a fixed rotation, so the running order
    — and any effect of what an entry is read next to — doesn't repeat session
    after session.

    ``repeats`` is how many position repeats survived, 0 when the design is
    exact. It can't always be 0: a judge that ran in both modes owns two
    entries, which leaves the other judges one fewer slot to spread over.
    """
    owner = {r[0]: (r[1].get("model") or "") for r in records}
    paths = [r[0] for r in records]

    counted_by = {p: [j for j in judges if owner[p] != j] for p in paths}
    used = {p: set() for p in paths}          # positions this entry has taken
    design = {}

    def place(idx):
        if idx == len(judges):
            return True
        judge = judges[idx]
        mine = [p for p in paths if owner[p] == judge]
        others = [p for p in paths if owner[p] != judge]
        order = list(others)
        for _ in range(60):                   # tries for this row, then give up
            rng.shuffle(order)
            if any(i + 1 in used[p] for i, p in enumerate(order)):
                continue
            for i, p in enumerate(order):
                used[p].add(i + 1)
            rng.shuffle(mine)
            design[judge] = order + mine
            if place(idx + 1):
                return True
            for i, p in enumerate(order):
                used[p].discard(i + 1)
            del design[judge]
        return False

    if not place(0):
        # No exact design exists (or wasn't found): fall back to plain random
        # per-judge orders, which still beats one order shared by everyone.
        design.clear()
        for judge in judges:
            mine = [p for p in paths if owner[p] == judge]
            others = [p for p in paths if owner[p] != judge]
            rng.shuffle(others); rng.shuffle(mine)
            design[judge] = others + mine

    repeats = 0
    for p in paths:
        seen = [design[j].index(p) for j in counted_by[p]]
        repeats += len(seen) - len(set(seen))

    by_path = {r[0]: r for r in records}
    return {j: [by_path[p] for p in order] for j, order in design.items()}, repeats


with open(os.path.join(sdir, "SUMMARIZE.md"), "w") as f:
    f.write(render(parsed, labels))

if blind:
    with open(os.path.join(sdir, "SUMMARIZE-KEY.md"), "w") as f:
        f.write(key_table(parsed, labels, "# Key for SUMMARIZE.md",
                          "Judges see none of this — they get lettered, "
                          "shuffled text only."))

    # Per-judge documents: same six outputs, a different running order each.
    judges = [j for j in os.environ.get("JUDGE_SLUGS", "").split(",") if j]
    if judges:
        rng = random.Random(int(os.environ["SEED"]) ^ 0x5EED)
        design, imbalance = counterbalance(parsed, judges, rng)
        for slug, order in design.items():
            letters = {rec[0]: "{{%s}}" % chr(65 + i) for i, rec in enumerate(order)}
            with open(os.path.join(sdir, "SUMMARIZE-%s.md" % slug), "w") as f:
                f.write(render(order, letters))
            # Maps this judge's tags onto the canonical ones the report uses.
            rows = ["# Key for SUMMARIZE-%s.md" % slug, "",
                    "This judge read the outputs in its own order, so its {{A}} is "
                    "not the canonical {{A}}.", "",
                    "| This judge's tag | Canonical | Model | Mode |",
                    "|---|---|---|---|"]
            for i, (path, meta, _b) in enumerate(order):
                rows.append("| {{%s}} | %s | %s | %s |" % (
                    chr(65 + i), labels[path], meta.get("model", "?"),
                    "thinking" if meta.get("thinking") == "true" else "no thinking"))
            with open(os.path.join(sdir, "SUMMARIZE-KEY-%s.md" % slug), "w") as f:
                f.write("\n".join(rows) + "\n")
        print("counterbalanced %d judge documents (%s)"
              % (len(design), "exact" if imbalance == 0 else
                 "%d position repeat(s)" % imbalance))
PYEOF

echo "  ✓ wrote SUMMARIZE.md ($(wc -c <"$SDIR/SUMMARIZE.md") bytes)"

# --- pass 2: let every model judge the results ------------------------------
# Feeds SUMMARIZE.md back to each model that was benchmarked, so you get one
# comparison per judge rather than trusting a single model's taste.
# UNIQ_MODELS was computed before pass 1, so the progress bar knew the total.

RUN_SUMMARY="$SUMMARIZE_ARG"
if [[ -z "$RUN_SUMMARY" && -t 0 ]]; then
  echo
  read -r -p "run the summary now — have each of the ${#UNIQ_MODELS[@]} models judge all $OK_RUNS results? [y/N] " s || s="n"
  [[ "${s,,}" == "y" || "${s,,}" == "yes" ]] && RUN_SUMMARY=1 || RUN_SUMMARY=0
fi
RUN_SUMMARY="${RUN_SUMMARY:-0}"
# Not judging settles the total too — otherwise the report sits at "0 of 6
# judges, 6 to go" forever on a session that is actually finished. Covers both
# declining the prompt and having no usable result for a judge to read.
if [[ "$RUN_SUMMARY" != "1" || "$OK_RUNS" -eq 0 ]]; then
  printf '0\n' > "$SDIR/.expected-judges"
fi

if [[ "$RUN_SUMMARY" == "1" && "$OK_RUNS" -gt 0 ]]; then
  echo
  echo "─── summary pass: ${#UNIQ_MODELS[@]} judges, $SUMMARY_MODE, temp $SUMMARY_TEMP ───"
  # Answered interactively after pass 1, so this may be the first time the
  # judge total is known; harmlessly rewrites the same number otherwise.
  printf '%s\n' "${#UNIQ_MODELS[@]}" > "$SDIR/.expected-judges"
  : > "$SDIR/.summary-system.txt"   # no system prompt: SUMMARIZE.md is self-contained
  # The tags this session used. Set only for the judge pass: it turns on the
  # helper's ranking-line retry, and only a judge is asked for a ranking line.
  REQUIRE_LABELS=""
  if [[ -f "$SDIR/SUMMARIZE-KEY.md" ]]; then
    REQUIRE_LABELS="$(grep -oE '\{\{[A-Z]\}\}' "$SDIR/SUMMARIZE-KEY.md" \
                        | tr -d '{}' | sort -u | paste -sd, -)"
    [[ -n "$REQUIRE_LABELS" ]] && echo "  (judges will be re-asked if they skip the RANKING line: $REQUIRE_LABELS)"
  fi
  S_OK=0; S_FAIL=0
  for j in "${!UNIQ_MODELS[@]}"; do
    M="${UNIQ_MODELS[$j]}"
    SLUG="$(slug "$M")"
    STAMP="$(date +%Y%m%d-%H%M%S)"
    echo
    echo "[judge $((j+1))/${#UNIQ_MODELS[@]}] $(name_of "$M")  ($SUMMARY_MODE)"
    # Its own document if the builder made one — same outputs, its own order,
    # its own entries last. Falls back to the shared one (older sessions,
    # re-judging a directory built before per-judge documents existed).
    JUDGE_DOC="$SDIR/SUMMARIZE-${SLUG}.md"
    [[ -f "$JUDGE_DOC" ]] || JUDGE_DOC="$SDIR/SUMMARIZE.md"
    if do_run "$M" "$SUMMARY_MODE" \
         "$SDIR/summary_${STAMP}_${SLUG}.md" \
         "$SDIR/logs/summary_${SLUG}.log" \
         "$SDIR/.summary-system.txt" "$JUDGE_DOC" \
         "$SUMMARY_TEMP" "$SUMMARY_MAX_TOKENS"; then
      S_OK=$((S_OK+1))
    else
      S_FAIL=$((S_FAIL+1))
    fi
  done
  rm -f "$SDIR/.summary-system.txt"
  echo
  echo "summaries: $S_OK ok, $S_FAIL failed"

  # Judges saw lettered outputs; put the model names back so the verdicts are
  # readable. Non-destructive: the lettered originals stay for the scorer.
  if [[ "$S_OK" -gt 0 && -f "$SDIR/SUMMARIZE-KEY.md" ]]; then
    echo
    echo "de-anonymising verdicts:"
    python3 "$(dirname "$0")/creative-bench-deanon.py" "$SDIR" ||       echo "  (de-anonymise failed — lettered verdicts are still in $SDIR)"
  fi
fi

rm -f "$HELPER"

echo
echo "─────────────────────────────────────────────────────────────"
echo "done: $OK_RUNS ok, $FAIL_RUNS failed"
echo "results:   $SDIR"
if [[ "${S_OK:-0}" -gt 0 ]]; then
  echo "summaries: $SDIR/summary_*.md  ($S_OK judges)"
  [[ -d "$SDIR/deanon" ]] && echo "de-anon:   $SDIR/deanon/  (model names restored)"
else
  echo "next step: $SDIR/SUMMARIZE.md  (paste it into a fresh session)"
fi
echo "─────────────────────────────────────────────────────────────"
