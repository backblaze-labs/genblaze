#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────
# Genblaze Maintainer — Launcher
# ─────────────────────────────────────────────────────────
# Launches the Genblaze Maintainer as a Claude Code sub-agent.
#
# Usage:
#   .claude/agents/genblaze-maintainer/run.sh                    # Full audit
#   .claude/agents/genblaze-maintainer/run.sh --domain security  # Security only
#   .claude/agents/genblaze-maintainer/run.sh --fix              # Auto-fix P0/P1
#   .claude/agents/genblaze-maintainer/run.sh --report-only      # Read-only
#
# Or invoke directly in Claude Code:
#   @genblaze-maintainer run a full maintenance audit
#
# Requirements:
#   - Claude Code CLI (`claude`) must be installed and authenticated
#   - Run from the genblaze repo root
# ─────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
AGENT_MD="$SCRIPT_DIR/../genblaze-maintainer.md"
CONFIG="$SCRIPT_DIR/config.json"

# ── Parse arguments ──────────────────────────────────────
DOMAIN=""
FIX_MODE=false
REPORT_ONLY=false
MODEL="sonnet"

while [[ $# -gt 0 ]]; do
    case $1 in
        --domain)
            DOMAIN="$2"
            shift 2
            ;;
        --fix)
            FIX_MODE=true
            shift
            ;;
        --report-only)
            REPORT_ONLY=true
            shift
            ;;
        --model)
            MODEL="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Genblaze Maintainer — autonomous repo guardian"
            echo ""
            echo "Options:"
            echo "  --domain DOMAIN   Focus on specific domain:"
            echo "                    functional, security, code-quality,"
            echo "                    documentation, agent-standards, dependencies"
            echo "  --fix             Enable auto-fix mode (will modify files)"
            echo "  --report-only     Read-only assessment, no file changes"
            echo "  --model MODEL     Claude model to use (default: sonnet)"
            echo "  -h, --help        Show this help message"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# ── Validate environment ─────────────────────────────────
if ! command -v claude &> /dev/null; then
    echo "Error: 'claude' CLI not found. Install Claude Code first."
    echo "  npm install -g @anthropic-ai/claude-code"
    exit 1
fi

if [[ ! -f "$REPO_ROOT/Makefile" ]]; then
    echo "Error: Must be run from the genblaze repo root."
    echo "  cd /path/to/genblaze && .claude/agents/genblaze-maintainer/run.sh"
    exit 1
fi

# ── Build the prompt ─────────────────────────────────────
PROMPT="You are the Genblaze Maintainer. Follow the Execution Protocol in your agent instructions."

if [[ -n "$DOMAIN" ]]; then
    PROMPT+="\n\nFOCUS: Only audit the '$DOMAIN' domain. Read the checklist at .claude/agents/genblaze-maintainer/checklists/${DOMAIN}.md and work through every item."
fi

if [[ "$FIX_MODE" == true ]]; then
    PROMPT+="\n\nMODE: Fix mode enabled. You are authorized to make changes to fix P0 and P1 issues. Run make test after every change. Create one commit per logical fix."
elif [[ "$REPORT_ONLY" == true ]]; then
    PROMPT+="\n\nMODE: Report-only mode. Do NOT modify any files. Only read, analyze, and produce a maintenance report."
else
    PROMPT+="\n\nMODE: Standard audit. Produce a maintenance report. Ask before making any changes."
fi

TODAY=$(date +%Y-%m-%d)
PROMPT+="\n\nToday's date: $TODAY"

# ── Launch the agent ─────────────────────────────────────
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Genblaze Maintainer"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Domain:  ${DOMAIN:-all}"
echo "  Mode:    $(if $FIX_MODE; then echo 'fix'; elif $REPORT_ONLY; then echo 'report-only'; else echo 'standard'; fi)"
echo "  Model:   $MODEL"
echo "  Date:    $TODAY"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

cd "$REPO_ROOT"

claude --agent genblaze-maintainer \
    --model "$MODEL" \
    --prompt "$(echo -e "$PROMPT")" \
    --allowedTools "Bash(make*),Bash(pytest*),Bash(python3*),Bash(pip*),Bash(grep*),Bash(find*),Bash(git*),Bash(ruff*),Bash(ls*),Bash(cat*),Read,Write,Edit,Glob,Grep"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Genblaze Maintainer session complete."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
