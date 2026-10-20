# tg-message-relay

把 **Facebook Messenger、Instagram DM、LINE、Discord** 的個人帳號訊息轉到 **Telegram**。四個平台的聊天集中在 Telegram 一個地方看、一個地方回。

```
FB Messenger ─┐                                             ┌─ 🔵 王小明
Instagram DM ─┼─ mautrix bridges ─ Synapse ─ relay ─ Telegram 群組（每個聊天對象一個「主題」）─ 🟣 Amy
LINE ─────────┤     （全部跑在你自己的電腦，Docker）         ├─ 🟢 家族群組
Discord ──────┘                                             └─ 👾 某伺服器 #general
```

- 每個聊天對象（或群組）會自動變成 Telegram 群組裡的一個**主題 (Topic)**。對方傳訊息，就會出現在那個主題裡。
- 在主題裡打字、傳圖或傳檔案，就會用你的身分回覆到 FB、IG、LINE 或 Discord。
- 支援文字、圖片、影片、語音、檔案、回覆（引用）、編輯，以及別人按的表情回應。
- 編輯只有在文字真的改了才會顯示「✏️ 已編輯」。Discord 更新連結預覽（標題、價格）時 bridge 也會送「編輯」，這種文字沒變的不會轉過來。
- 圖片、影片、語音附說明文字時，如果平台不支援（LINE 會拒絕、IG 會直接丟掉文字），relay 會自動拆成兩則：先送檔案，再送文字。
- 你回覆之後，會順便把該聊天標成已讀。
- 只有 `TELEGRAM_OWNER_ID` 本人傳的訊息會被轉出去，群組裡其他人講話都會被忽略。
- 不需要對外開 port，也不需要網域。電腦只要能連上網路就好，因為 Telegram 是用 long polling 主動去拉訊息。

## ⚠️ 先看風險

這個專案設計給一個人用：自己的帳號，只有自己看。它跟 Meta、LINE、Discord、Telegram 官方都沒有關係，帳號被停權或其他後果請自行承擔。

1. **這是非官方做法**。Meta 和 LINE 都沒有開放個人帳號的 API，這裡的 bridge 是模擬官方 App 或網頁的協定，違反服務條款。帳號有機率被要求重新驗證、強制登出，嚴重的話會暫時被鎖。建議不要用太重要的帳號，也不要大量發訊息。
2. **Discord 一樣禁止用程式操作個人帳號**（self-bot）。只轉發少數頻道、不要大量發訊息，風險跟 FB / IG 差不多。
3. **公司或學校的規定**：如果那裡的網路擋了 FB、IG，多半是資安政策，用這套繞過去可能違反規定，請自行評估。在公司電腦裝 Telegram 可能本身就違規，電腦上的監控軟體也可能看得到訊息內容。不要用這個群組傳公司的檔案或截圖。
4. **Telegram 帳號就是所有帳號的鑰匙**。能進你 Telegram 的人（帳號被盜，或用了你沒鎖的電腦），就能用你的名義在 FB、IG、LINE、Discord 看訊息、回訊息。請開 Telegram 的兩步驟驗證（Two-Step Verification），電腦版設本機密碼（Local passcode），離開座位要鎖螢幕。不想讓旁邊的人看到內容，可以在電腦版的通知設定關掉訊息預覽。
5. **這台電腦存著所有登入和訊息**。`data/` 裡有各平台的登入憑證和完整的訊息紀錄（含照片），`.env` 裡有 bot token，被拿到就等於帳號被拿走。電腦請開磁碟加密；專案資料夾不要放在會同步到雲端的位置（例如 iCloud 的「桌面與文件」、OneDrive、Dropbox）；`.env` 和 `data/` 不要傳給別人，也不要 commit 進 git（`.gitignore` 已經排除）。bot token 外流的話，到 @BotFather 用 `/revoke` 換一個新的，改好 `.env` 再執行 `docker compose up -d relay`。
6. **原本的端對端加密就沒了**。Messenger、LINE 的私訊原本只有你和對方的裝置解得開；經過這套之後，在這台電腦上是明文，到了 Telegram 也不是端對端加密，Telegram 看得到內容。對方傳來的證件、驗證碼、私密照片，也會多存在這兩個地方。
7. **訊息可能漏接**。平台改了協定，bridge 就可能收不到訊息。每天的登入檢查抓得到登出和斷線，但抓不到「bridge 以為還連著、其實收不到」的情況。重要的事不要只靠這套。
8. **LINE 同時只能有一個「Chrome 擴充功能」登入**。bridge 用的就是這個登入位置，所以開了 bridge 就不能再用 LINE Chrome 擴充功能。手機 App 不受影響。

## 需要的東西

**電腦**

- 一台不關機、能連上網路的電腦：macOS、Linux，或 Windows（WSL 2）都可以。
- 記憶體：整套平常約 600 MB 到 1 GB，可以跟其他服務共用同一台電腦。
- **有開 LINE 的話**：LINE bridge 要從原始碼編譯，編譯時 Docker 需要約 **8 GB 記憶體**（Docker Desktop：設定 → Resources → Memory）。編譯完可以調回來。
- 硬碟：預留幾 GB 給 Docker image（LINE 編譯時會再多用一些）。

**軟體**

- `git`
- **Docker** 和 **Docker Compose v2**（`docker compose` 指令）：裝 [Docker Desktop](https://www.docker.com/products/docker-desktop/) 就都有了，也可以用 OrbStack、Colima 等其他 Docker 環境。
- **有開 LINE 的話還要 buildx**：Docker Desktop 內建；用 Homebrew 裝的 Docker 要另外 `brew install docker-buildx`。
- 只會在本機開 `127.0.0.1:8008`（給除錯用），不需要對外開任何 port。

**帳號**

- Telegram 帳號（下面會建立 bot 和群組）。
- FB / IG：帳號密碼，或能在電腦瀏覽器登入以取得 cookie。
- LINE：帳號要先綁定 Email 和密碼（手機 LINE → 設定 → 我的帳號 → 電子郵件帳號），登入時要用手機 LINE 輸入 PIN 碼。
- Discord：手機 Discord App（掃 QR code 登入）。

## 安裝步驟

### 1. 準備 Telegram

1. 在 Telegram 找 **@BotFather**，用 `/newbot` 建立一個 bot，記下 **bot token**。
2. 建一個**新群組**，只放你自己和這個 bot。
3. 到群組設定，打開 **「主題 (Topics)」**。
4. 把 bot 設成**管理員**，並勾選「**管理主題**」和「**刪除訊息**」。

### 2. 安裝

```bash
git clone https://github.com/m930406/tg-message-relay.git
cd tg-message-relay
cp .env.example .env
open -e .env        # 填 TELEGRAM_BOT_TOKEN，並把 MATRIX_PASSWORD 改成一組隨機長密碼
./setup.sh
```

`setup.sh` 會做這些事：下載或建置 image（有開 LINE 的話，LINE bridge 要從原始碼編譯，第一次大約 5 到 10 分鐘）、產生所有設定檔、建立本機 Matrix 帳號，最後啟動全部服務。之後要重跑也沒關係，它會保留已經存在的設定。

### 3. 取得群組 ID

如果 `.env` 裡的 `TELEGRAM_CHAT_ID` 和 `TELEGRAM_OWNER_ID` 還是空的：

1. 在 Telegram 群組裡隨便講一句話，bot 會回覆兩行 ID。
2. 把那兩行填進 `.env`。
3. 執行 `docker compose up -d relay`。

之後群組裡會自動出現每個平台的管理主題，例如「🤖 FB 登入 / 管理」、「🤖 IG 登入 / 管理」（有開 LINE、Discord 才會有它們的管理主題）。

### 4. 登入各平台

到對應的主題裡打字，跟 bridge bot 對話就能登入。bot 出現「Hello, I'm a … bridge bot」或你輸入 `help` 有回應，就代表準備好了。只要 bot 在要求輸入密碼、cookie 或驗證碼，**你輸入的那則訊息送出後會自動從 Telegram 刪掉**。

| 平台 | 指令 | 說明 |
|---|---|---|
| FB | `login messenger-lite` | 用帳號密碼登入（模擬 Messenger iOS）。不行的話試試 `login messenger-lite-android` |
| FB | `login messenger` | 改用 cookie 登入：在電腦瀏覽器登入 messenger.com，依 bot 的說明貼上 cookie |
| IG | `login android` | 用帳號密碼登入（模擬 IG Android App） |
| IG | `login instagram` | 改用 cookie 登入：在電腦瀏覽器登入 instagram.com，依 bot 的說明貼上 cookie |
| LINE | `login` | 依序輸入 Email、密碼，再到**手機 LINE 輸入畫面上的 PIN 碼**。帳號必須先綁定 Email（手機 LINE → 設定 → 我的帳號 → 電子郵件帳號） |
| Discord | `login` | bot 會傳一張 QR code，用**手機 Discord → 設定 → 掃描 QR 碼**掃它 |
| Discord | `login-token user <token>` | QR code 不行的話（例如被要求驗證）改用 token：在電腦瀏覽器登入 discord.com，從開發者工具取得。這則訊息送出後會自動刪掉 |

其他常用指令：`help`、`logout`、`list-logins`、`cancel`（取消進行中的登入）。

登入成功後，**新進來的訊息**就會開始轉發。舊的聊天紀錄不會倒進 Telegram。

## 日常操作

```bash
docker compose ps                  # 看狀態
docker compose logs -f relay       # 看轉發 log
docker compose logs -f fb          # 看 FB bridge log（ig / line / synapse 同理）
docker compose restart fb          # 重啟某個 bridge

# 更新所有元件（有開 LINE 的話會重新編譯 LINE，Docker 可用記憶體要先調到 8 GB）
docker compose pull --ignore-buildable && docker compose build --pull && docker compose up -d

# 只更新 LINE（重跑 setup.sh 不會重新編譯 LINE，要更新時才執行這行）
docker compose build line && docker compose up -d line
```

**每日登入檢查**：每天早上 9 點（台灣時間）relay 會檢查各平台登入狀態，結果發到 General。全部正常時是靜音訊息，需要重新登入時會跳通知並告訴你去哪個主題輸入 `login`。一天只送一次；如果 relay 在檢查時間剛好沒在跑，一小時內啟動會補送。隨時想查，在 General 輸入 `/status`。要改時間就改 `.env` 的 `LOGIN_CHECK_TIME`（留空表示不檢查），然後 `docker compose up -d relay`。

**只在通知時段收訊息**：在 `.env` 設 `NOTIFY_HOURS`（台灣時間，例如 `08:00-22:00`，也可以跨夜）和 `NOTIFY_DAYS`（星期幾，例如 `1-5`、`1,3,5`，留空 = 每天）。這段時間以外收到的訊息**先存在 relay 裡、不送進 Telegram**，所以完全不會跳通知；通知時段一到，照順序補送到各自的主題（補送的訊息是靜音的），最後發一則「📬 靜音期間有 N 則新訊息」的摘要。登入 / 管理主題、系統警告不受影響。兩個都留空（預設）就是隨時都收。改完執行 `docker compose up -d relay`。國定假日不會自動判斷。

**通知時段前提早載入**：通知時段開始前 1 小時（例如通知時段從 08:00 開始，就是 07:00），就先把累積的訊息照順序補送進來，一樣是靜音送達。這樣放完長假，通知時段一開始訊息就已經都在主題裡，不用等它慢慢補。這段時間新進來的訊息也會直接（靜音）送進來。要改時間就改 `.env` 的 `PRELOAD_MINUTES`（分鐘），設 `0` 就是通知時段到了才補送。注意：Telegram 的靜音訊息在手機上可能還是會出現橫幅，只是沒有聲音。

**放假**：在 General 或任何「🤖 登入 / 管理」主題輸入：

- `/off 10/10 國慶日`：那天整天放假，訊息先存著，下一個通知時段（前 1 小時）補送。日期後面的字是備註，可以不寫。
- `/off 10/9-10/12 出國`：連續放好幾天（一次最多 60 天，也可以寫 `10/9~10/12`）。
- `/on 10/10`：取消那天的假。
- `/off`：列出已設定的假日，依月份分組，連續而且備註一樣的日子會合成一段（例如 `10/09（五）～10/12（一）  出國`）。
- 已設定的日子再 `/off` 一次並寫新備註，就會更新備註；沒寫備註則保留原本的。

日期可以加年份，例如 `/off 2027/1/2`、`/off 2026/12/30-2027/1/2`（也可以寫 `2027-01-02`）。沒寫年份時，已經過了的日期會當成明年。`/status` 也會顯示今天是不是放假。

**群組成員防護**：Telegram 群組裡的每個人都看得到所有主題，所以群組只能有你和 bot。relay 每分鐘檢查一次人數，有人加入時也會立刻檢查；超過 `GROUP_MAX_MEMBERS`（預設 2）就暫停轉發，新訊息（包括管理主題）先存著，所有指令和按鈕只回「暫停中」。把多的人移出群組後自動恢復，管理主題的訊息馬上補送，聊天訊息照通知時段的規則補送。已經在群組裡的舊訊息擋不住，必要時要自己刪除。

**今天先靜音**：在 General 或任何「🤖 登入 / 管理」主題輸入 `/mute`，今天接下來的訊息也先存著，明天到了通知時段再補送。

- `/inbox`：馬上把累積的訊息送出來看（之後仍維持靜音）。
- `/unmute`：取消今天的靜音；如果在通知時段內，會立刻補送累積的訊息。
- `/status` 會顯示目前有幾則訊息等著補送。

**讓電腦不要睡**：關閉自動睡眠。macOS 是系統設定 → 電池 / 能源，把「顯示器關閉時防止自動進入睡眠」打開，或執行 `sudo pmset -a sleep 0`。
**開機自動啟動**：在 Docker Desktop（或你用的 Docker 環境）設定裡勾選開機啟動。容器都設了 `restart: unless-stopped`，會跟著一起起來。

### 開關平台（例如之後要加 LINE）

`.env.example` 預設只開 FB 和 IG（`COMPOSE_PROFILES=fb,ig`）。把 `.env` 的 `COMPOSE_PROFILES` 改成想要的組合，例如加上 LINE 和 Discord 就改成 `COMPOSE_PROFILES=fb,ig,line,discord`，再執行一次 `./setup.sh`。沒列到的平台不會啟動。

### LINE 只轉發你加入的聊天（白名單）

LINE 的官方帳號、廣告帳號很多，所以 LINE 預設**只轉發你加入白名單的聊天**。沒加入的聊天完全不會出現在 Telegram，也不會跳出任何提示。

在「🤖 LINE 登入 / 管理」主題輸入：

- `/chats`：列出還沒加入的 LINE 聊天，按「➕」就加入。可以加關鍵字縮小範圍，例如 `/chats 媽`。
  還沒聊過的好友也會列在後面，標成「👤 名字（好友，還沒聊過）」；按下去會建好私訊並開好主題，就能直接從 Telegram 主動傳訊息給對方。
- `/added`：列出已加入的聊天，按「➖」就移除。

加入時，會把加入前最近收到的訊息（最多 20 則）補送過來。

想讓其他平台也用白名單，或完全關掉，改 `.env` 的 `APPROVE_NEW_CHATS`（例如 `APPROVE_NEW_CHATS=line,ig`，或留空表示全部直接轉發）後執行 `docker compose up -d relay`。

### IG 只轉發你追蹤的人（訊息請求不轉）

IG 會把「你沒追蹤的人」傳來的私訊放進「訊息請求」。relay 預設不轉發 IG 的訊息請求，也不會開主題或跳任何提示，只默默保留最近 20 則：

- 你追蹤的人、以前已經接受過的聊天：照常轉發。
- 想讓某個訊息請求轉過來：在 IG App 裡接受它（或直接回覆），relay 會自動補送先前保留的訊息，之後照常轉發。

FB 也有訊息請求（不是朋友的人傳來的）。想一起擋，把 `.env` 的 `HOLD_MESSAGE_REQUESTS` 改成 `ig,fb`；想全部照常轉發就留空。改完執行 `docker compose up -d relay`。

### Discord：只轉發你挑的頻道

Discord 一律只轉發你挑的頻道，私訊也一樣要挑。設定分兩步，都在「🤖 Discord 登入 / 管理」主題：

1. `/servers`：列出你加入的伺服器，按「➕」把伺服器接進來。這時候頻道只是建好，還不會轉發。
2. `/chats 伺服器名稱`：列出那個伺服器的頻道，按「➕」的頻道才會轉發。主題名稱會是「👾 伺服器名稱 #頻道」。

`/added` 列出正在轉發的頻道，按「➖」就停止。不要的伺服器在 `/servers` 按「➖」整個移除。

### 想直接看 Matrix 那一層（除錯用）

Synapse 只在本機開了 `http://localhost:8008`。可以用 [Element](https://element.io/download) 桌面版登入：伺服器填 `http://localhost:8008`，帳密就是 `.env` 裡的 `MATRIX_USER` / `MATRIX_PASSWORD`。

## 疑難排解

| 狀況 | 處理方式 |
|---|---|
| Telegram 沒有任何反應 | `docker compose logs relay`。檢查 bot token、群組是否開了主題、bot 是不是管理員 |
| 群組裡出現「⚠️ 轉發設定有問題」 | relay 每次啟動都會檢查群組設定，照訊息裡列的項目修正後執行 `docker compose restart relay` |
| `.env` 裡的 `TELEGRAM_OWNER_ID` 是 `1087968824` | 那是「匿名管理員」的共用 ID。關掉自己的「保持匿名」，把 `TELEGRAM_OWNER_ID` 清空後 `docker compose up -d relay`，再講一句話重新取得 |
| 自己傳的訊息都沒有轉出去 | 群組設定裡把你自己的「保持匿名」關掉。匿名發言時 Telegram 不會告訴 bot 是誰傳的，relay 會當成陌生人忽略 |
| bot 回覆 ID 之後就沒動靜 | 把 ID 填進 `.env` 後，記得 `docker compose up -d relay` |
| 不小心刪掉某個主題 | 不用處理，下次那個聊天有新訊息時會自動重建 |
| FB / IG 突然收不到訊息 | 多半是 cookie 失效或被要求重新驗證。到管理主題輸入 `login` 重新登入，並先用手機 App 確認帳號沒被鎖 |
| LINE 登入說密碼錯誤 | 先確認可以用 Email 和密碼登入 LINE PC 版。PIN 碼要在手機 LINE 上輸入 |
| 管理主題還沒出現 | bridge 第一次啟動比較慢，relay 每 15 秒會自動重試，等一下就會出現。可以用 `docker compose logs relay` 看進度 |

## 檔案結構

```
docker-compose.yml   # synapse + fb + ig + line + discord + relay
setup.sh             # 一鍵初始化
.env.example         # 設定範本
relay/relay.py       # Matrix <-> Telegram 轉發程式（Python，只依賴 aiohttp）
LICENSE              # MIT 授權
data/                # 執行後產生：所有設定、資料庫、登入狀態（已加入 .gitignore，請勿上傳）
```

使用的開源專案：[mautrix-meta](https://github.com/mautrix/meta)（FB、IG）、[mautrix-discord](https://github.com/mautrix/discord)（Discord）、[beeper/line](https://github.com/beeper/line)（LINE）、[Synapse](https://github.com/element-hq/synapse)。

## 授權

[MIT](LICENSE)。上面這些開源專案是安裝時另外下載的，各自有自己的授權，不包含在這個 repo 裡。
