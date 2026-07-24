#!/usr/bin/env bash
# A stand-in for creative-bench.sh, used by the test suite.
#
# It accepts the same non-interactive flags, writes a session directory with the
# same shape (frontmatter, SUMMARIZE-KEY.md, judge verdicts), and finishes in a
# second or two -- so the queue, the worker, the live report rebuild and the
# report itself can all be exercised without loading a model onto the GPU.
set -euo pipefail

SYS=""; USR=""; TEMP="1.0"; MODE="thinking"; MODELS=""; RUNS="${STUB_RUNS:-2}"
META_DIR=""; META_MODEL=""; DO_SUMMARIZE=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --system) SYS="$2"; shift 2 ;;
    --user)   USR="$2"; shift 2 ;;
    --temp)   TEMP="$2"; shift 2 ;;
    --mode)   MODE="$2"; shift 2 ;;
    --models) MODELS="$2"; shift 2 ;;
    --meta-summary) META_DIR="$2"; shift 2 ;;
    --meta-model)   META_MODEL="$2"; shift 2 ;;
    --summarize)    DO_SUMMARIZE=1; shift ;;
    --no-summarize) DO_SUMMARIZE=0; shift ;;
    --yes|--no-blind) shift ;;
    *) shift ;;
  esac
done

# Round 3: mirrors creative-bench.sh's --meta-summary path -- one model, one
# run, appended into the existing dir, no queue/judge logic touched.
if [[ -n "$META_DIR" ]]; then
  sleep "${STUB_DELAY:-1}"
  STAMP="$(date +%Y%m%d-%H%M%S)"
  cat > "$META_DIR/round3_${STAMP}_${META_MODEL}.md" <<EOF
---
model: "$META_MODEL"
thinking: true
temperature: $TEMP
seed: ${SEED:-4242}
tokens: 300
tokens_per_sec: 60.0
elapsed_sec: 5
error: null
---

## Thinking

(none)

## Output

Stub synthesis by $META_MODEL.
EOF
  echo "round 3: $META_DIR/round3_${STAMP}_${META_MODEL}.md"
  exit 0
fi

OUTDIR="${OUTDIR:?stub runner needs OUTDIR}"
SEED="${SEED:-4242}"
SDIR="$OUTDIR/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$SDIR"
cp "$SYS" "$SDIR/system-prompt.txt" 2>/dev/null || : > "$SDIR/system-prompt.txt"
cp "$USR" "$SDIR/user-prompt.txt"

IFS=',' read -r -a NAMES <<< "${MODELS:-stub-a-7B-Q4_K_M,stub-b-7B-Q4_K_M}"
LETTERS=(A B C D E F G H I J)

n=0
for m in "${NAMES[@]}"; do
  n=$((n+1))
  sleep "${STUB_DELAY:-1}"
  cat > "$SDIR/0${n}_$(date +%Y%m%d-%H%M%S)_${m}_thinking.md" <<EOF
---
model: "$m"
thinking: true
temperature: $TEMP
seed: $SEED
context: 32768
tokens: $((1000 + n * 100))
tokens_per_sec: $((40 + n * 10)).0
elapsed_sec: $((20 + n))
error: null
---

## Thinking

(none)

## Output

Stub output number $n.
EOF
done

if [[ "$DO_SUMMARIZE" != 1 ]]; then
  echo "results:   $SDIR"
  exit 0
fi

# Key: one label per run, in the order they were written.
{
  echo "# Key for SUMMARIZE.md"
  echo
  echo "| Output | Model | Mode | tokens | tok/s | elapsed | file |"
  echo "|---|---|---|---|---|---|---|"
  i=0
  for m in "${NAMES[@]}"; do
    echo "| {{${LETTERS[$i]}}} | $m | thinking | 1000 | 40.0 | 20s | 0$((i+1))_x.md |"
    i=$((i+1))
  done
} > "$SDIR/SUMMARIZE-KEY.md"
: > "$SDIR/SUMMARIZE.md"

# Judges: each ranks the outputs, ending with the machine-readable line.
i=0
for m in "${NAMES[@]}"; do
  i=$((i+1))
  sleep "${STUB_DELAY:-1}"
  order=""
  j=0
  for _ in "${NAMES[@]}"; do
    order+="{{${LETTERS[$j]}}}"
    j=$((j+1))
    [[ $j -lt ${#NAMES[@]} ]] && order+=" > "
  done
  cat > "$SDIR/summary_$(date +%Y%m%d-%H%M%S)_${m}.md" <<EOF
---
model: "$m"
thinking: true
temperature: 0.7
seed: $SEED
tokens: 500
tokens_per_sec: 50.0
elapsed_sec: 10
error: null
---

## Thinking

(none)

## Output

Stub verdict from judge $i.

RANKING: $order
EOF
done

echo "results:   $SDIR"
