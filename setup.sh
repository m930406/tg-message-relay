#!/usr/bin/env bash
# 一鍵初始化：產生 Synapse / bridge 設定、註冊 appservice、建立 Matrix 帳號，然後全部啟動。
# 可以重複執行（已存在的設定不會被覆蓋，只會重新套用必要的欄位）。
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "找不到 .env，請先執行： cp .env.example .env  然後填好內容"
  exit 1
fi
set -a
# shellcheck disable=SC1091
. ./.env
set +a

SERVER_NAME="${SERVER_NAME:-home.local}"
COMPOSE_PROFILES="${COMPOSE_PROFILES:-fb,ig,line}"
BRIDGES="$(printf '%s' "$COMPOSE_PROFILES" | tr ',' ' ')"
: "${MATRIX_USER:?請在 .env 設定 MATRIX_USER}"
: "${MATRIX_PASSWORD:?請在 .env 設定 MATRIX_PASSWORD}"
export SERVER_NAME MATRIX_USER COMPOSE_PROFILES

DATA="$PWD/data"
mkdir -p "$DATA/synapse/appservices" "$DATA/relay"

yq() {
  docker run --rm --user root -e SERVER_NAME -e MATRIX_USER -e B -e PORT -e AS_FILES -e SECRET \
    -v "$DATA:/workdir" mikefarah/yq "$@"
}

bridge_info() {
  case "$1" in
    fb)   PORT=29319; BIN=/usr/bin/mautrix-meta ;;
    ig)   PORT=29330; BIN=/usr/bin/mautrix-meta ;;
    line) PORT=29322; BIN=/usr/bin/matrix-line ;;
    discord) PORT=29334; BIN=/usr/bin/mautrix-discord ;;
    *) echo "不認識的平台：$1（COMPOSE_PROFILES 只能填 fb,ig,line,discord）"; exit 1 ;;
  esac
}

echo "==> 下載 / 建置 image（第一次會比較久，LINE 要從原始碼編譯）"
docker compose pull synapse
for B in $BRIDGES; do
  if [ "$B" != line ]; then
    docker compose pull "$B"
  elif docker image inspect local/matrix-line:latest >/dev/null 2>&1; then
    # LINE 已經編譯過就沿用，不然每次重跑 setup.sh 只要上游有更新就要重新編譯 5～10 分鐘
    echo "LINE image 已存在，略過編譯（要更新 LINE：docker compose build line && docker compose up -d line）"
  else
    docker compose build line
  fi
done
docker compose build relay

echo "==> Synapse 設定"
if [ ! -f "$DATA/synapse/homeserver.yaml" ]; then
  docker compose run --rm --no-deps \
    -e SYNAPSE_SERVER_NAME="$SERVER_NAME" -e SYNAPSE_REPORT_STATS=no \
    synapse generate
fi

setup_bridgev2() {
  if [ ! -f "$DATA/$B/config.yaml" ]; then
    docker compose run --rm --no-deps --entrypoint "$BIN" "$B" -c /data/config.yaml -e
  fi
  yq -i '
    .homeserver.address = "http://synapse:8008" |
    .homeserver.domain = strenv(SERVER_NAME) |
    .appservice.address = "http://" + strenv(B) + ":" + strenv(PORT) |
    .appservice.hostname = "0.0.0.0" |
    .appservice.port = (strenv(PORT) | tonumber) |
    .appservice.id = strenv(B) |
    .appservice.bot.username = strenv(B) + "bot" |
    .appservice.username_template = strenv(B) + "_{{.}}" |
    .database.type = "sqlite3-fk-wal" |
    .database.uri = "file:/data/bridge.db?_txlock=immediate" |
    .bridge.permissions = {"@" + strenv(MATRIX_USER) + ":" + strenv(SERVER_NAME): "admin"} |
    .bridge.personal_filtering_spaces = false |
    .bridge.command_prefix = "!" + strenv(B) |
    .matrix.federate_rooms = false |
    .logging.min_level = "info" |
    del(.logging.writers[] | select(.type == "file"))
  ' "$B/config.yaml"
  # ↑ log 只輸出到 docker（docker compose logs 看得到），不寫檔：
  #   有些 Docker 環境（例如 Colima）的掛載資料夾不允許 chown，寫 log 檔會讓 bridge 一啟動就結束（exit 12）
}

# Discord bridge 是舊版架構，設定檔欄位位置不一樣
setup_discord() {
  if [ ! -f "$DATA/$B/config.yaml" ]; then
    docker compose run --rm --no-deps --entrypoint sh "$B" \
      -c 'cp /opt/mautrix-discord/example-config.yaml /data/config.yaml'
  fi
  # relay 用這組密碼查 Discord 的登入狀態、伺服器清單
  if [ ! -s "$DATA/relay/discord_provision_secret" ]; then
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$DATA/relay/discord_provision_secret"
  fi
  SECRET="$(cat "$DATA/relay/discord_provision_secret")"
  export SECRET
  yq -i '
    .homeserver.address = "http://synapse:8008" |
    .homeserver.domain = strenv(SERVER_NAME) |
    .appservice.address = "http://" + strenv(B) + ":" + strenv(PORT) |
    .appservice.hostname = "0.0.0.0" |
    .appservice.port = (strenv(PORT) | tonumber) |
    .appservice.id = strenv(B) |
    .appservice.bot.username = strenv(B) + "bot" |
    .appservice.database.type = "sqlite3-fk-wal" |
    .appservice.database.uri = "file:/data/bridge.db?_txlock=immediate" |
    .bridge.username_template = strenv(B) + "_{{.}}" |
    .bridge.channel_name_template = "{{if .GuildName}}{{.GuildName}} #{{.Name}}{{else}}{{.Name}}{{end}}" |
    .bridge.permissions = {"@" + strenv(MATRIX_USER) + ":" + strenv(SERVER_NAME): "admin"} |
    .bridge.command_prefix = "!" + strenv(B) |
    .bridge.federate_rooms = false |
    .bridge.provisioning.shared_secret = strenv(SECRET) |
    .logging.min_level = "info" |
    del(.logging.writers[] | select(.type == "file"))
  ' "$B/config.yaml"
  # ↑ 頻道的聊天室名稱帶上伺服器名稱（例如「我的伺服器 #general」），/chats 才分得出來、也能用伺服器名稱篩選
}

AS_FILES="["
for B in $BRIDGES; do
  bridge_info "$B"
  export B PORT
  echo "==> 設定 bridge：$B"
  mkdir -p "$DATA/$B"
  if [ "$B" = discord ]; then
    setup_discord
  else
    setup_bridgev2
  fi
  if [ ! -f "$DATA/$B/registration.yaml" ]; then
    docker compose run --rm --no-deps --entrypoint "$BIN" "$B" \
      -g -c /data/config.yaml -r /data/registration.yaml
  fi
  cp "$DATA/$B/registration.yaml" "$DATA/synapse/appservices/$B.yaml"
  chmod 644 "$DATA/synapse/appservices/$B.yaml"
  AS_FILES="$AS_FILES\"/data/appservices/$B.yaml\","
done
AS_FILES="${AS_FILES%,}]"
export AS_FILES

yq -i '
  .app_service_config_files = env(AS_FILES) |
  .listeners[0].bind_addresses = ["0.0.0.0"] |
  .listeners[0].resources[0].names = ["client"] |
  .enable_registration = false |
  .federation_domain_whitelist = [] |
  .trusted_key_servers = [] |
  .suppress_key_server_warning = true |
  .presence.enabled = false |
  .rc_message = {"per_second": 1000, "burst_count": 1000} |
  .rc_joins.local = {"per_second": 1000, "burst_count": 1000}
' synapse/homeserver.yaml
# ↑ 這台 Synapse 只有你一個人用，放寬速率限制：預設每 5 秒只能發 1 則、每 10 秒只能加入 1 個聊天室，
#   登入後 bridge 一次建立幾十個聊天室時，relay 會卡好幾分鐘才開始轉發。

echo "==> 啟動 Synapse"
# 強制重建，確保讀到最新的 appservice 註冊（重跑 setup.sh 新增平台時也適用）
docker compose up -d --force-recreate synapse
printf "等待 Synapse 就緒"
until docker compose exec -T synapse curl -fsS http://localhost:8008/health >/dev/null 2>&1; do
  printf "."; sleep 2
done
echo

echo "==> 建立 Matrix 帳號 @$MATRIX_USER:$SERVER_NAME"
if ! docker compose exec -T synapse register_new_matrix_user \
     -u "$MATRIX_USER" -p "$MATRIX_PASSWORD" -a -c /data/homeserver.yaml http://localhost:8008; then
  echo "（帳號可能已存在，略過）"
fi

echo "==> 啟動 bridges 與 relay"
docker compose up -d

echo
echo "完成！接下來："
echo "  1. 確認 Telegram 群組已開啟「主題」，bot 是管理員（含管理主題權限）"
echo "  2. 若 .env 的 TELEGRAM_CHAT_ID / TELEGRAM_OWNER_ID 還沒填，在群組講一句話取得 ID，填好後執行："
echo "       docker compose up -d relay"
echo "  3. 到 Telegram 群組裡各平台的「🤖 … 登入 / 管理」主題輸入 login 開始登入"
echo "  看 log：docker compose logs -f relay"
