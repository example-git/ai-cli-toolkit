#!/usr/bin/env bash
# Multi-tool aware statusline for ai-cli wrappers.
set -euo pipefail

input="$(cat 2>/dev/null || true)"
tool="${AI_CLI_TOOL:-claude}"

extract_json_field() {
  local query="$1"
  if command -v jq >/dev/null 2>&1; then
    printf '%s' "$input" | jq -r "$query // empty" 2>/dev/null || true
  fi
}

cwd="$(extract_json_field '.cwd')"
[ -z "$cwd" ] && cwd="$(extract_json_field '.workspace.current_dir')"
[ -z "$cwd" ] && cwd="$(pwd)"

model="$(extract_json_field '.model.display_name')"
[ -z "$model" ] && model="$tool"

remaining="$(extract_json_field '.context_window.remaining_percentage')"

user="$(whoami)"
host="$(hostname -s)"
now="$(date '+%a %b %d, %H:%M')"

session_remaining=""
weekly_remaining=""

read_claude_access_token() {
  local credentials_file="${HOME}/.claude/.credentials.json"
  local credentials_enc_file="${HOME}/.claude/.credentials.json.enc"
  local credentials_key_file="${HOME}/.claude/.credentials.key"
  local token=""

  if [ -f "$credentials_enc_file" ] && [ -f "$credentials_key_file" ] && command -v openssl >/dev/null 2>&1 && command -v jq >/dev/null 2>&1; then
    local decrypted
    decrypted="$(openssl enc -d -aes-256-cbc -pbkdf2 -a -pass "file:${credentials_key_file}" -in "$credentials_enc_file" 2>/dev/null || true)"
    if [ -n "$decrypted" ]; then
      token="$(printf '%s' "$decrypted" | jq -r '.claudeAiOauth.accessToken // empty' 2>/dev/null || true)"
    fi
  fi

  if [ -z "$token" ] && [ -f "$credentials_file" ] && command -v jq >/dev/null 2>&1; then
    token="$(jq -r '.claudeAiOauth.accessToken // empty' "$credentials_file" 2>/dev/null || true)"
  fi

  printf '%s' "$token"
}

refresh_claude_usage_cache() {
  local cache_file="/tmp/ai-cli-claude-usage-cache.json"
  local stale=1

  if [ -f "$cache_file" ]; then
    local now_epoch file_epoch
    now_epoch="$(date +%s)"
    file_epoch="$(date -r "$cache_file" +%s 2>/dev/null || echo 0)"
    if [ $((now_epoch - file_epoch)) -lt 60 ]; then
      stale=0
    fi
  fi

  if [ "$stale" -eq 1 ]; then
    local token
    token="$(read_claude_access_token)"
    if [ -n "$token" ] && command -v curl >/dev/null 2>&1; then
      local marker response code body
      marker="__AI_CLI_HTTP__"
      response="$(curl -s --max-time 5 \
        -H "Authorization: Bearer ${token}" \
        -H "anthropic-beta: oauth-2025-04-20" \
        -H "Content-Type: application/json" \
        -w "\n${marker}%{http_code}" \
        "https://api.anthropic.com/api/oauth/usage" 2>/dev/null || true)"
      code="${response##*${marker}}"
      body="${response%${marker}*}"
      body="${body%$'\n'}"
      if [ "$code" = "200" ] && [ -n "$body" ] && command -v jq >/dev/null 2>&1 && printf '%s' "$body" | jq -e . >/dev/null 2>&1; then
        printf '%s' "$body" > "$cache_file"
      fi
    fi
  fi

  if [ -f "$cache_file" ] && command -v jq >/dev/null 2>&1; then
    local five_hour seven_day
    five_hour="$(jq -r '.five_hour.utilization // empty' "$cache_file" 2>/dev/null || true)"
    seven_day="$(jq -r '.seven_day.utilization // empty' "$cache_file" 2>/dev/null || true)"
    if [ -n "$five_hour" ]; then
      session_remaining="$(awk "BEGIN { printf \"%d\", 100 - $five_hour }")"
    fi
    if [ -n "$seven_day" ]; then
      weekly_remaining="$(awk "BEGIN { printf \"%d\", 100 - $seven_day }")"
    fi
  fi
}

if [ "$tool" = "claude" ]; then
  refresh_claude_usage_cache
fi

blue='\033[0;34m'
bold_blue='\033[1;34m'
green='\033[1;32m'
dark_gray='\033[1;30m'
cyan='\033[0;36m'
white='\033[1;37m'
yellow='\033[0;33m'
magenta='\033[1;35m'
reset='\033[0m'

printf "${bold_blue}┌─[${reset}${green}%s${dark_gray}@${cyan}%s${bold_blue}]${reset} - ${bold_blue}[${white}%s${bold_blue}]${reset} - ${bold_blue}[${yellow}%s${bold_blue}]${reset}" \
  "$user" "$host" "$cwd" "$now"

line2=""
[ -n "$remaining" ] && line2="${line2} ctx:${remaining}%"
if [ "$tool" = "claude" ]; then
  [ -n "$session_remaining" ] && line2="${line2} | sess:${session_remaining}%"
  [ -n "$weekly_remaining" ] && line2="${line2} | week:${weekly_remaining}%"
fi

printf "\n${bold_blue}└─[${magenta}%s${bold_blue}]${reset} [%s]" "$model" "$tool"
[ -n "$line2" ] && printf " %s remaining" "$line2"
printf "${reset}"
