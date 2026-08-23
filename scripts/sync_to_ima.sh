#!/usr/bin/env bash
# sync_to_ima.sh — 将下载的文件同步到 IMA 知识库
#
# 用法:
#   sync_to_ima.sh --file "/path/to/report.pdf" --kb-name "年报季度报知识库"
#   sync_to_ima.sh --file "/path/to/report.pdf" --market a --type annual_report
#   sync_to_ima.sh --dir "/path/to/downloads/" --kb-name "年报季度报知识库"
#
# 环境变量:
#   IMA_OPENAPI_CLIENTID / IMA_OPENAPI_APIKEY 或 ~/.config/ima/ 凭证文件

set -euo pipefail

# ── Resolve paths ──
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"           # unified-downloader skill root
IMA_SKILL_DIR="$SKILL_DIR/../ima"              # ima skill root
IMA_API="$IMA_SKILL_DIR/ima_api.cjs"
PREFLIGHT_CHECK="$IMA_SKILL_DIR/knowledge-base/scripts/preflight-check.cjs"
COS_UPLOAD="$IMA_SKILL_DIR/knowledge-base/scripts/cos-upload.cjs"
SYNC_CONFIG="$SKILL_DIR/ima_sync.yaml"

# ── Defaults ──
FILE=""
DIR=""
KB_NAME=""
MARKET=""
DOC_TYPE=""
FOLDER_ID=""
FORCE=""

# ── Parse args ──
while [[ $# -gt 0 ]]; do
  case $1 in
    --file)     FILE="$2"; shift 2 ;;
    --dir)      DIR="$2"; shift 2 ;;
    --kb-name)  KB_NAME="$2"; shift 2 ;;
    --kb-id)    KB_ID_OVERRIDE="$2"; shift 2 ;;
    --market)   MARKET="$2"; shift 2 ;;
    --type)     DOC_TYPE="$2"; shift 2 ;;
    --folder-id) FOLDER_ID="$2"; shift 2 ;;
    --force)    FORCE="1"; shift ;;
    *)          echo "[error] Unknown argument: $1" >&2; exit 1 ;;
  esac
done

# ── Resolve credentials ──
resolve_creds() {
  if [[ -n "${IMA_OPENAPI_CLIENTID:-}" && -n "${IMA_OPENAPI_APIKEY:-}" ]]; then
    CLIENT_ID="$IMA_OPENAPI_CLIENTID"
    API_KEY="$IMA_OPENAPI_APIKEY"
  elif [[ -f ~/.config/ima/client_id && -f ~/.config/ima/api_key ]]; then
    CLIENT_ID="$(cat ~/.config/ima/client_id)"
    API_KEY="$(cat ~/.config/ima/api_key)"
  else
    echo '{"code":-100,"msg":"IMA 凭证未配置，请设置 IMA_OPENAPI_CLIENTID/APIKEY 或 ~/.config/ima/ 文件"}' >&2
    exit 1
  fi
}

# ── IMA API helper ──
ima_api() {
  local api_path="$1"
  local body="$2"
  local opts
  opts=$(printf '{"clientId":"%s","apiKey":"%s"}' "$CLIENT_ID" "$API_KEY")
  local resp err_json err_code err_msg
  if ! resp=$(node "$IMA_API" "$api_path" "$body" "$opts" 2>/tmp/ima_sync_err); then
    err_json=$(cat /tmp/ima_sync_err 2>/dev/null || echo '{}')
    err_code=$(echo "$err_json" | jq -r '.code // empty' 2>/dev/null)
    err_msg=$(echo "$err_json" | jq -r '.msg // "unknown error"' 2>/dev/null)
    echo "[error] API $api_path failed (code=$err_code): $err_msg" >&2
    return 1
  fi
  echo "$resp"
}

# ── Resolve kb_id from kb_name ──
# 2026-08-13: 优先读 ima_sync.yaml 里的 kb_ids 硬编码映射（用户要求写死 ID），
# 找不到才回退到 API search_knowledge_base 按名称搜索。
resolve_kb_id() {
  local name="$1"

  # 优先：从 yaml kb_ids 硬编码映射取 ID
  local hard_id
  hard_id=$(python3 -c "
import yaml, sys
cfg = yaml.safe_load(open('$SYNC_CONFIG'))
kb_ids = cfg.get('kb_ids', {})
print(kb_ids.get('$name', ''))
" 2>/dev/null)
  if [[ -n "$hard_id" ]]; then
    echo "$hard_id"
    return 0
  fi

  # 回退：按名称 API 搜索
  local resp
  resp=$(ima_api "openapi/wiki/v1/search_knowledge_base" "{\"query\":\"$name\",\"limit\":20}") || return 1
  local code
  code=$(echo "$resp" | jq -r '.code')
  if [[ "$code" != "0" ]]; then
    local msg
    msg=$(echo "$resp" | jq -r '.msg')
    echo "[error] search_knowledge_base: $msg" >&2
    return 1
  fi
  # Try exact match first, then partial
  local kb_id
  kb_id=$(echo "$resp" | jq -r --arg n "$name" '.data.info_list[] | select(.kb_name == $n) | .kb_id' | head -1)
  if [[ -z "$kb_id" ]]; then
    kb_id=$(echo "$resp" | jq -r --arg n "$name" '.data.info_list[] | select(.kb_name | contains($n)) | .kb_id' | head -1)
  fi
  if [[ -z "$kb_id" ]]; then
    echo "[error] 未找到知识库「$name」，可用知识库：" >&2
    echo "$resp" | jq -r '.data.info_list[] | "  - \(.kb_name)"' >&2
    return 1
  fi
  echo "$kb_id"
}

# ── Resolve kb_name from config ──
resolve_kb_name_from_config() {
  local m="$1" t="$2"
  if [[ ! -f "$SYNC_CONFIG" ]]; then
    echo "[error] 配置文件不存在: $SYNC_CONFIG" >&2
    return 1
  fi
  # Use python to parse yaml (portable enough)
  python3 -c "
import yaml, sys
cfg = yaml.safe_load(open('$SYNC_CONFIG'))
name = cfg.get('defaults', {}).get('$m', {}).get('$t', '')
if not name:
    sys.exit(1)
print(name)
" 2>/dev/null
}

# ── Upload a single file to IMA ──
upload_file() {
  local file_path="$1"
  local kb_id="$2"
  local file_name
  file_name="$(basename "$file_path")"

  echo "  📤 $file_name"

  # Step 1: preflight-check
  local preflight
  preflight=$(node "$PREFLIGHT_CHECK" --file "$file_path" 2>/dev/null) || {
    local pass
    pass=$(echo "$preflight" | jq -r '.pass // false' 2>/dev/null)
    if [[ "$pass" != "true" ]]; then
      local reason
      reason=$(echo "$preflight" | jq -r '.reason // "不支持该文件类型"' 2>/dev/null)
      echo "  ⏭️  跳过 $file_name: $reason"
      # 2026-07-23 fix: signal caller that we skipped (not uploaded)
      # via last_action + non-zero rc. The previous `return 0` was
      # indistinguishable from real upload success, which made
      # download_manager._trigger_ima_sync falsely mark ima_synced=1.
      last_action="skip"
      return 2
    fi
  }
  local pass
  pass=$(echo "$preflight" | jq -r '.pass // false' 2>/dev/null)
  if [[ "$pass" != "true" ]]; then
    local reason
    reason=$(echo "$preflight" | jq -r '.reason // "不支持该文件类型"' 2>/dev/null)
    echo "  ⏭️  跳过 $file_name: $reason"
    last_action="skip"
    return 2
  fi

  local file_ext media_type content_type file_size
  file_ext=$(echo "$preflight" | jq -r '.file_ext // ""')
  media_type=$(echo "$preflight" | jq -r '.media_type // 0')
  content_type=$(echo "$preflight" | jq -r '.content_type // "application/octet-stream"')
  file_size=$(echo "$preflight" | jq -r '.file_size // 0')

  # Step 2: check_repeated_names
  local dup_policy
  dup_policy=$(python3 -c "
import yaml
cfg = yaml.safe_load(open('$SYNC_CONFIG'))
print(cfg.get('duplicate_policy', 'skip'))
" 2>/dev/null || echo "skip")

  local check_resp
  check_resp=$(ima_api "openapi/wiki/v1/check_repeated_names" "{
    \"params\": [{\"name\": \"$file_name\", \"media_type\": $media_type}],
    \"knowledge_base_id\": \"$kb_id\"
    $([[ -n "$FOLDER_ID" ]] && echo ",\"folder_id\": \"$FOLDER_ID\"" || echo "")
  }") || return 1

  local is_repeated
  # 2026-07-23 fix: the actual API response uses `.data.results[]`
  # not `.data.params[]`. The wrong path always evaluated to
  # `false`, so the duplicate-name short-circuit was permanently
  # bypassed and we could upload the same file_name to the same
  # KB over and over.
  is_repeated=$(echo "$check_resp" | jq -r '.data.results[0].is_repeated // .data.params[0].is_repeated // false' 2>/dev/null)
  if [[ "$is_repeated" == "true" && -z "$FORCE" ]]; then
    if [[ "$dup_policy" == "skip" ]]; then
      echo "  ⏭️  跳过 $file_name: 已存在同名文件"
      # 2026-07-23 fix: distinguish skip from real upload success.
      last_action="skip"
      return 2
    else
      # keep_both: append timestamp
      local ts
      ts=$(date +%Y%m%d%H%M%S)
      local base="${file_name%.*}"
      local ext="${file_name##*.}"
      file_name="${base}_${ts}.${ext}"
      echo "  📝 重命名为 $file_name (保留两者)"
    fi
  fi

  # Step 3: create_media
  local create_resp
  create_resp=$(ima_api "openapi/wiki/v1/create_media" "{
    \"file_name\": \"$file_name\",
    \"file_size\": $file_size,
    \"content_type\": \"$content_type\",
    \"knowledge_base_id\": \"$kb_id\",
    \"file_ext\": \"$file_ext\"
  }") || return 1

  local create_code
  create_code=$(echo "$create_resp" | jq -r '.code')
  if [[ "$create_code" != "0" ]]; then
    local create_msg
    create_msg=$(echo "$create_resp" | jq -r '.msg')
    echo "  ❌ create_media 失败: $create_msg"
    return 1
  fi

  local media_id cos_url
  local cos_secret_id cos_secret_key cos_token cos_bucket cos_region cos_key cos_start cos_expired
  media_id=$(echo "$create_resp" | jq -r '.data.media_id')
  cos_url=$(echo "$create_resp" | jq -r '.data.url')
  cos_secret_id=$(echo "$create_resp" | jq -r '.data.cos_credential.secret_id')
  cos_secret_key=$(echo "$create_resp" | jq -r '.data.cos_credential.secret_key')
  cos_token=$(echo "$create_resp" | jq -r '.data.cos_credential.token')
  cos_bucket=$(echo "$create_resp" | jq -r '.data.cos_credential.bucket_name')
  cos_region=$(echo "$create_resp" | jq -r '.data.cos_credential.region')
  cos_key=$(echo "$create_resp" | jq -r '.data.cos_credential.cos_key')
  cos_start=$(echo "$create_resp" | jq -r '.data.cos_credential.start_time')
  cos_expired=$(echo "$create_resp" | jq -r '.data.cos_credential.expired_time')

  # Step 4: COS upload
  node "$COS_UPLOAD" \
    --file "$file_path" \
    --secret-id "$cos_secret_id" \
    --secret-key "$cos_secret_key" \
    --token "$cos_token" \
    --bucket "$cos_bucket" \
    --region "$cos_region" \
    --cos-key "$cos_key" \
    --content-type "$content_type" \
    --start-time "$cos_start" \
    --expired-time "$cos_expired" \
    --timeout 300000 2>/dev/null || {
    echo "  ❌ COS 上传失败: $file_name"
    return 1
  }

  # Step 5: add_knowledge
  local add_resp
  add_resp=$(ima_api "openapi/wiki/v1/add_knowledge" "{
    \"media_type\": $media_type,
    \"media_id\": \"$media_id\",
    \"title\": \"$file_name\",
    \"knowledge_base_id\": \"$kb_id\",
    \"file_info\": {
      \"cos_key\": \"$cos_key\",
      \"file_size\": $file_size,
      \"file_name\": \"$file_name\"
    }
    $([[ -n "$FOLDER_ID" ]] && echo ",\"folder_id\": \"$FOLDER_ID\"" || echo "")
  }") || return 1

  local add_code
  add_code=$(echo "$add_resp" | jq -r '.code')
  if [[ "$add_code" != "0" ]]; then
    local add_msg
    add_msg=$(echo "$add_resp" | jq -r '.msg')
    echo "  ❌ add_knowledge 失败: $add_msg"
    return 1
  fi

  echo "  ✅ $file_name 已添加到知识库"
  return 0
}

# ── Main ──
main() {
  resolve_creds

  # Resolve kb_id
  local kb_id=""
  if [[ -n "${KB_ID_OVERRIDE:-}" ]]; then
    kb_id="$KB_ID_OVERRIDE"
  elif [[ -n "$KB_NAME" ]]; then
    kb_id=$(resolve_kb_id "$KB_NAME") || exit 1
  elif [[ -n "$MARKET" && -n "$DOC_TYPE" ]]; then
    KB_NAME=$(resolve_kb_name_from_config "$MARKET" "$DOC_TYPE") || {
      echo "[error] 配置中未找到 market=$MARKET type=$DOC_TYPE 的知识库映射" >&2
      exit 1
    }
    echo "📋 根据配置映射到知识库: $KB_NAME"
    kb_id=$(resolve_kb_id "$KB_NAME") || exit 1
  else
    echo "[error] 请指定 --kb-name 或 --market + --type" >&2
    exit 1
  fi

  echo "📚 目标知识库: $KB_NAME (kb_id: $kb_id)"

  # Collect files
  local -a files=()
  if [[ -n "$FILE" ]]; then
    files+=("$FILE")
  elif [[ -n "$DIR" ]]; then
    while IFS= read -r -d '' f; do
      files+=("$f")
    done < <(find "$DIR" -type f \( -name "*.pdf" -o -name "*.doc" -o -name "*.docx" -o -name "*.xls" -o -name "*.xlsx" -o -name "*.ppt" -o -name "*.pptx" -o -name "*.txt" -o -name "*.md" -o -name "*.csv" -o -name "*.epub" -o -name "*.html" -o -name "*.htm" \) -print0 2>/dev/null)
  else
    echo "[error] 请指定 --file 或 --dir" >&2
    exit 1
  fi

  if [[ ${#files[@]} -eq 0 ]]; then
    echo "📂 没有找到可上传的文件"
    exit 0
  fi

  echo "📂 共 ${#files[@]} 个文件待同步"
  echo "---"

  local success=0 skipped=0 failed=0
  for f in "${files[@]}"; do
    # 2026-07-23 fix: upload_file now returns 0=uploaded, 1=failed,
    # 2=skipped. Distinguish all three so skipped files do not get
    # counted as either successes or failures.
    upload_file "$f" "$kb_id"
    local rc=$?
    if [[ $rc -eq 0 ]]; then
      success=$((success + 1))
    elif [[ $rc -eq 2 ]]; then
      skipped=$((skipped + 1))
    else
      failed=$((failed + 1))
    fi
  done

  echo "---"
  echo "📊 同步完成: ✅ 成功 $success | ⏭️ 跳过 $skipped | ❌ 失败 $failed"
}

main
