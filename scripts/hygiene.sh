#!/usr/bin/env bash
# =============================================================================
# hygiene.sh  —  FlightDeck App Health & Hygiene
#
# Reads apps.json, derives each repo's root directory, then for every repo:
#
#   CHECK 1  Git repo & remote configured
#   CHECK 2  .gitignore covers the required patterns
#   CHECK 3  No secret/credential files tracked in git
#   CHECK 4  No credential patterns in tracked source files
#   CHECK 5  README.md exists and is non-trivial (>5 lines)
#   CHECK 6  No build artefacts tracked (__pycache__, node_modules, .venv, dist)
#   CHECK 7  No large files tracked (>500 KB)
#   CHECK 8  Working tree is clean (untracked/modified files flagged)
#   CHECK 9  Branch is not behind its remote
#
#   FIX      Adds any missing .gitignore entries, commits + pushes the fix
#
# Usage:
#   chmod +x scripts/hygiene.sh
#   ./scripts/hygiene.sh              # full run (fixes + push)
#   ./scripts/hygiene.sh --dry-run    # report only, no writes
# =============================================================================

set -euo pipefail

# ── Colour helpers ─────────────────────────────────────────────────────────────
RED='\033[0;31m';  GREEN='\033[0;32m';  YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BLUE='\033[0;34m';  BOLD='\033[1m';  DIM='\033[2m'; RESET='\033[0m'
TICK="${GREEN}✓${RESET}"; CROSS="${RED}✗${RESET}"; WARN="${YELLOW}⚠${RESET}"; FIX="${CYAN}⚙${RESET}"

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERR]${RESET}   $*"; }
fixed()   { echo -e "${CYAN}[FIX]${RESET}   $*"; }
section() { echo -e "\n${BOLD}━━━ $* ━━━${RESET}"; }

# ── Config ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLIGHTDECK_ROOT="$(dirname "$SCRIPT_DIR")"
APPS_JSON="${FLIGHTDECK_ROOT}/backend/apps.json"
GITHUB_BASE="${HOME}/Library/CloudStorage/OneDrive-Personal/Projects/GitHub"

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

# Patterns that MUST appear in .gitignore (literal or glob)
REQUIRED_GITIGNORE_PATTERNS=(
    ".env"
    "*.pem"
    "*.key"
    ".venv"
    "__pycache__"
    ".DS_Store"
    "*.log"
    "secrets.*"
    "credentials.*"
)

# Credential patterns to scan for in tracked source files
# Format: "description|regex"
CREDENTIAL_PATTERNS=(
    "AWS access key|AKIA[0-9A-Z]{16}"
    "Private key block|-----BEGIN.{0,20}PRIVATE KEY-----"
    "GitHub PAT (classic)|ghp_[a-zA-Z0-9]{36}"
    "GitHub PAT (fine-grained)|github_pat_[a-zA-Z0-9_]{82}"
    "Generic password assignment|password\s*=\s*['\"][^'\"]{6,}['\"]"
    "Generic secret assignment|secret\s*=\s*['\"][^'\"]{8,}['\"]"
    "Generic API key assignment|api_key\s*=\s*['\"][^'\"]{8,}['\"]"
    "Generic token assignment|access_token\s*=\s*['\"][^'\"]{8,}['\"]"
    "Slack token|xox[baprs]-[0-9A-Za-z\-]{10,48}"
    "Stripe key|sk_live_[0-9a-zA-Z]{24}"
)

# File extensions to skip in credential scan (binary, compiled, etc.)
SKIP_EXTENSIONS="jpg|jpeg|png|gif|svg|ico|woff|woff2|ttf|eot|mp3|m4b|epub|pdf|zip|tar|gz|db|sqlite|sqlite3|pyc|so|o|a|class|jar|lock"

# Files whose names suggest they hold secrets (tracked = bad)
SECRET_FILE_PATTERNS=(
    ".env"
    "*.pem"
    "*.key"
    "*.p12"
    "*.pfx"
    "id_rsa"
    "id_ed25519"
    "secrets.json"
    "secrets.yaml"
    "secrets.yml"
    "credentials.json"
    "credentials.yaml"
)

# Patterns that are SAFE to track even if they look like secret files
# (.env.example, .env.sample etc. are intentional templates)
SECRET_FILE_ALLOWLIST=(
    ".env.example"
    ".env.sample"
    ".env.template"
    ".env.test"
)

# Build artefacts that should never be tracked
ARTEFACT_DIRS=(".venv" "venv" "node_modules" "__pycache__" ".pytest_cache" ".mypy_cache")

# ── Counters ───────────────────────────────────────────────────────────────────
TOTAL_APPS=0
PASS_APPS=0
WARN_APPS=0
FAIL_APPS=0
declare -A APP_RESULTS   # id → PASS | WARN | FAIL
declare -a APP_IDS=()    # ordered list, built as we go

# ── Helpers ────────────────────────────────────────────────────────────────────

# Find the git repo root by walking up from a given path
git_root_for() {
    local path="$1"
    git -C "$path" rev-parse --show-toplevel 2>/dev/null || echo ""
}

# Check if a pattern is already covered by .gitignore
gitignore_covers() {
    local repo="$1"
    local pattern="$2"
    [[ -f "${repo}/.gitignore" ]] && grep -qF "${pattern}" "${repo}/.gitignore" 2>/dev/null
}

# Add a pattern to .gitignore under a section header
append_gitignore() {
    local repo="$1"
    local pattern="$2"
    local section_header="$3"
    local gi="${repo}/.gitignore"

    if ! grep -qF "# ${section_header}" "$gi" 2>/dev/null; then
        printf "\n# %s\n" "${section_header}" >> "$gi"
    fi
    echo "${pattern}" >> "$gi"
}

# ── Per-app check function ─────────────────────────────────────────────────────
check_app() {
    local app_id="$1"
    local repo="$2"

    local issues=0
    local warnings=0
    local fixes_made=()

    echo ""
    echo -e "${BOLD}▶ ${app_id}${RESET}  ${DIM}${repo}${RESET}"

    # ── CHECK 1: Git repo & remote ─────────────────────────────────────────────
    if [[ ! -d "${repo}/.git" ]]; then
        echo -e "  ${WARN} Not a git repository — skipping (run 'git init' to enable tracking)"
        APP_RESULTS[$app_id]="WARN"
        (( WARN_APPS++ )) || true
        return
    fi

    local remote
    remote=$(git -C "$repo" remote get-url origin 2>/dev/null || echo "")
    if [[ -z "$remote" ]]; then
        echo -e "  ${WARN} No remote 'origin' configured — cannot push fixes"
        (( warnings++ )) || true
    else
        echo -e "  ${TICK} Git repo with remote: ${DIM}${remote}${RESET}"
    fi

    # ── CHECK 2: .gitignore completeness ──────────────────────────────────────
    local missing_patterns=()
    for pattern in "${REQUIRED_GITIGNORE_PATTERNS[@]}"; do
        if ! gitignore_covers "$repo" "$pattern"; then
            missing_patterns+=("$pattern")
        fi
    done

    if [[ ${#missing_patterns[@]} -eq 0 ]]; then
        echo -e "  ${TICK} .gitignore covers all required patterns"
    else
        echo -e "  ${WARN} .gitignore missing: ${missing_patterns[*]}"
        if [[ "$DRY_RUN" == "false" ]]; then
            for pat in "${missing_patterns[@]}"; do
                append_gitignore "$repo" "$pat" "Security / hygiene (added by FlightDeck hygiene.sh)"
                fixes_made+=("Added '${pat}' to .gitignore")
            done
        fi
        (( warnings++ )) || true
    fi

    # ── CHECK 3: No secret files tracked ──────────────────────────────────────
    local tracked_secrets=()
    for spat in "${SECRET_FILE_PATTERNS[@]}"; do
        while IFS= read -r match; do
            if [[ -n "$match" ]]; then
                # Check against allowlist
                local allowed=false
                for allowed_pat in "${SECRET_FILE_ALLOWLIST[@]}"; do
                    [[ "$(basename "$match")" == "$allowed_pat" ]] && allowed=true && break
                done
                [[ "$allowed" == "false" ]] && tracked_secrets+=("$match")
            fi
        done < <(git -C "$repo" ls-files "$spat" 2>/dev/null || true)
    done

    if [[ ${#tracked_secrets[@]} -eq 0 ]]; then
        echo -e "  ${TICK} No secret files tracked in git"
    else
        echo -e "  ${CROSS} Secret files are tracked in git — REMOVE IMMEDIATELY:"
        for f in "${tracked_secrets[@]}"; do
            echo -e "       ${RED}${f}${RESET}"
        done
        echo -e "       Fix: git rm --cached <file> && add to .gitignore"
        (( issues++ )) || true
    fi

    # ── CHECK 4: Credential pattern scan in tracked source files ──────────────
    local cred_hits=()
    # Build list of tracked text files (skip binary extensions)
    local text_files
    text_files=$(git -C "$repo" ls-files 2>/dev/null \
        | grep -Ev "\.(${SKIP_EXTENSIONS})$" || true)

    if [[ -n "$text_files" ]]; then
        for entry in "${CREDENTIAL_PATTERNS[@]}"; do
            local desc="${entry%%|*}"
            local regex="${entry##*|}"
            while IFS= read -r f; do
                [[ -n "$f" ]] && cred_hits+=("${desc}: ${f}")
            done < <(echo "$text_files" \
                | xargs -I{} sh -c 'grep -PlI "'"${regex}"'" "${1}" 2>/dev/null && true' -- "${repo}/{}" \
                | sed "s|${repo}/||" \
                | head -3 || true)
        done
    fi

    if [[ ${#cred_hits[@]} -eq 0 ]]; then
        echo -e "  ${TICK} No credential patterns found in tracked files"
    else
        echo -e "  ${CROSS} Possible credentials in tracked source files:"
        # Deduplicate
        local prev=""
        for hit in $(printf '%s\n' "${cred_hits[@]}" | sort -u); do
            echo -e "       ${RED}${hit}${RESET}"
        done
        (( issues++ )) || true
    fi

    # ── CHECK 5: README.md ────────────────────────────────────────────────────
    if [[ ! -f "${repo}/README.md" ]]; then
        echo -e "  ${WARN} README.md missing"
        (( warnings++ )) || true
    else
        local readme_lines
        readme_lines=$(wc -l < "${repo}/README.md" | tr -d ' ')
        if [[ "$readme_lines" -lt 6 ]]; then
            echo -e "  ${WARN} README.md is very short (${readme_lines} lines) — consider expanding"
            (( warnings++ )) || true
        else
            echo -e "  ${TICK} README.md present (${readme_lines} lines)"
        fi
    fi

    # ── CHECK 6: Build artefacts tracked ─────────────────────────────────────
    local tracked_artefacts=()
    for artefact in "${ARTEFACT_DIRS[@]}"; do
        # Check if any tracked files start with this directory name
        local count
        count=$(git -C "$repo" ls-files "${artefact}/" 2>/dev/null | wc -l | tr -d ' ')
        if [[ "$count" -gt 0 ]]; then
            tracked_artefacts+=("${artefact}/ (${count} files)")
        fi
    done

    if [[ ${#tracked_artefacts[@]} -eq 0 ]]; then
        echo -e "  ${TICK} No build artefacts tracked"
    else
        echo -e "  ${CROSS} Build artefacts tracked in git:"
        for a in "${tracked_artefacts[@]}"; do
            echo -e "       ${RED}${a}${RESET}"
        done
        echo -e "       Fix: git rm -r --cached <dir> && add to .gitignore"
        (( issues++ )) || true
    fi

    # ── CHECK 7: Large files ──────────────────────────────────────────────────
    local large_files=()
    while IFS= read -r line; do
        [[ -n "$line" ]] && large_files+=("$line")
    done < <(git -C "$repo" ls-files -z 2>/dev/null \
        | xargs -0 -I{} bash -c 'f="${repo}/{}"; [[ -f "$f" ]] && sz=$(du -k "$f" | cut -f1); [[ "$sz" -gt 500 ]] && echo "$sz KB: {}"' \
        repo="$repo" 2>/dev/null | sort -rn | head -5 || true)

    if [[ ${#large_files[@]} -eq 0 ]]; then
        echo -e "  ${TICK} No large files (>500 KB) tracked"
    else
        echo -e "  ${WARN} Large tracked files:"
        for lf in "${large_files[@]}"; do
            echo -e "       ${YELLOW}${lf}${RESET}"
        done
        (( warnings++ )) || true
    fi

    # ── CHECK 8: Working tree clean ───────────────────────────────────────────
    local dirty_count
    dirty_count=$(git -C "$repo" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
    if [[ "$dirty_count" -eq 0 ]]; then
        echo -e "  ${TICK} Working tree is clean"
    else
        local dirty_files
        dirty_files=$(git -C "$repo" status --porcelain 2>/dev/null | head -6)
        echo -e "  ${WARN} Working tree has ${dirty_count} uncommitted change(s):"
        while IFS= read -r dline; do
            echo -e "       ${DIM}${dline}${RESET}"
        done <<< "$dirty_files"
        [[ "$dirty_count" -gt 6 ]] && echo -e "       ${DIM}… and $((dirty_count - 6)) more${RESET}"
        (( warnings++ )) || true
    fi

    # ── CHECK 9: Branch not behind remote ────────────────────────────────────
    if [[ -n "$remote" ]]; then
        git -C "$repo" fetch origin --quiet 2>/dev/null || true
        local branch
        branch=$(git -C "$repo" branch --show-current 2>/dev/null || echo "")
        if [[ -n "$branch" ]]; then
            local behind
            behind=$(git -C "$repo" rev-list --count "HEAD..origin/${branch}" 2>/dev/null || echo "0")
            local ahead
            ahead=$(git -C "$repo" rev-list --count "origin/${branch}..HEAD" 2>/dev/null || echo "0")
            if [[ "$behind" -gt 0 ]]; then
                echo -e "  ${WARN} Branch '${branch}' is ${behind} commit(s) behind origin — consider pulling"
                (( warnings++ )) || true
            elif [[ "$ahead" -gt 0 ]]; then
                echo -e "  ${WARN} Branch '${branch}' is ${ahead} commit(s) ahead of origin — consider pushing"
                (( warnings++ )) || true
            else
                echo -e "  ${TICK} Branch '${branch}' is up to date with origin"
            fi
        fi
    fi

    # ── Commit & push fixes ────────────────────────────────────────────────────
    if [[ "$DRY_RUN" == "false" && ${#fixes_made[@]} -gt 0 ]]; then
        echo ""
        # Only stage the .gitignore so we don't accidentally commit unrelated work
        git -C "$repo" add .gitignore
        local commit_msg="chore: hygiene — add missing .gitignore entries

Applied by FlightDeck hygiene.sh:
$(printf '  - %s\n' "${fixes_made[@]}")"
        git -C "$repo" commit -m "$commit_msg" --quiet
        fixed "Committed: ${fixes_made[*]}"

        if [[ -n "$remote" ]]; then
            git -C "$repo" push origin HEAD --quiet
            fixed "Pushed to origin"
        else
            warn "No remote — skipping push"
        fi
    fi

    # ── Determine overall result ───────────────────────────────────────────────
    if [[ "$issues" -gt 0 ]]; then
        APP_RESULTS[$app_id]="FAIL"
        (( FAIL_APPS++ )) || true
    elif [[ "$warnings" -gt 0 ]]; then
        APP_RESULTS[$app_id]="WARN"
        (( WARN_APPS++ )) || true
    else
        APP_RESULTS[$app_id]="PASS"
        (( PASS_APPS++ )) || true
    fi
}

# ── Derive repo root from apps.json entry ─────────────────────────────────────
#
# Rules (in order):
#   1. script is a directory  →  use it directly, then walk up to git root
#   2. script is a file       →  walk up to git root from its parent
#   3. script is null         →  $GITHUB_BASE/$id  (docker apps)
#
derive_repo() {
    local app_id="$1"
    local script="$2"

    if [[ "$script" == "null" || -z "$script" ]]; then
        # Docker-type or no script — scan $GITHUB_BASE for a directory whose
        # lowercased name matches the app id (handles AudiobookConstructor etc.)
        local id_lower="${app_id,,}"
        local found=""
        while IFS= read -r candidate; do
            local cname
            cname="$(basename "$candidate")"
            if [[ "${cname,,}" == "$id_lower" ]]; then
                found="$candidate"
                break
            fi
        done < <(find "$GITHUB_BASE" -maxdepth 1 -type d 2>/dev/null)
        echo "$found"
        return
    fi

    local base
    if [[ -d "$script" ]]; then
        base="$script"
    else
        base="$(dirname "$script")"
    fi

    # Try to find git root; if not found, return the base dir itself
    # (check_app will then report "not a git repo" gracefully)
    local root
    root=$(git -C "$base" rev-parse --show-toplevel 2>/dev/null || echo "")
    echo "${root:-$base}"
}

# ── Main ──────────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════╗"
echo -e "║   FlightDeck — App Health & Hygiene              ║"
[[ "$DRY_RUN" == "true" ]] && \
echo -e "║   ${YELLOW}DRY RUN — no files will be modified${RESET}${BOLD}             ║"
echo -e "╚══════════════════════════════════════════════════╝${RESET}"
echo ""

command -v jq >/dev/null 2>&1 || { error "jq is required (brew install jq)"; exit 1; }
[[ -f "$APPS_JSON" ]] || { error "apps.json not found at ${APPS_JSON}"; exit 1; }

# Load app list as tab-separated "id\tscript" pairs (keeps nulls aligned)
readarray -t APP_ENTRIES < <(jq -r '.[] | [.id, (.script // "null")] | @tsv' "$APPS_JSON")

info "Checking ${#APP_ENTRIES[@]} apps from apps.json"
[[ "$DRY_RUN" == "true" ]] && info "Dry-run mode — reporting only"

for entry in "${APP_ENTRIES[@]}"; do
    app_id="$(echo "$entry" | cut -f1)"
    script="$(echo "$entry" | cut -f2)"

    repo=$(derive_repo "$app_id" "$script")

    TOTAL_APPS=$(( TOTAL_APPS + 1 ))
    APP_IDS+=("$app_id")

    if [[ -z "$repo" ]]; then
        echo ""
        echo -e "${BOLD}▶ ${app_id}${RESET}"
        echo -e "  ${CROSS} Could not locate repo directory (script=${script})"
        APP_RESULTS[$app_id]="FAIL"
        (( FAIL_APPS++ )) || true
        continue
    fi

    check_app "$app_id" "$repo"
done

# ── Summary ────────────────────────────────────────────────────────────────────
section "Summary"
# APP_IDS is built dynamically in the loop above
echo ""
printf "  %-30s  %s\n" "App" "Result"
printf "  %-30s  %s\n" "──────────────────────────────" "──────"
for app_id in "${APP_IDS[@]}"; do
    result="${APP_RESULTS[$app_id]:-SKIP}"
    case "$result" in
        PASS) badge="${GREEN}PASS${RESET}" ;;
        WARN) badge="${YELLOW}WARN${RESET}" ;;
        FAIL) badge="${RED}FAIL${RESET}" ;;
        *)    badge="${DIM}SKIP${RESET}" ;;
    esac
    printf "  %-30s  " "$app_id"
    echo -e "$badge"
done

echo ""
echo -e "  Total: ${TOTAL_APPS}  ${GREEN}Pass: ${PASS_APPS}${RESET}  ${YELLOW}Warn: ${WARN_APPS}${RESET}  ${RED}Fail: ${FAIL_APPS}${RESET}"
echo ""

if [[ "$DRY_RUN" == "true" ]]; then
    echo -e "  ${DIM}Run without --dry-run to apply .gitignore fixes and push.${RESET}"
fi

echo ""

[[ "$FAIL_APPS" -gt 0 ]] && exit 1 || exit 0
