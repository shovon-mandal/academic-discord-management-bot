# NUBTK Discord Class Bot

এটা Shovon Mandal (SM)-এর CSE class-এর Discord server চালানোর bot, NUBTK, Fall 2026।
Code আছে GitHub-এ, বানানো আর test করা হয় PC-তে, আর live চলে Tinkerboard-এ।

```
PC (edit + test)  ──git push──▶  GitHub (private repo)  ──!update──▶  Tinkerboard (24/7 live)
```

---

## Features

| Feature | কী করে |
|---|---|
| **Auto server build** | `!setup_server` দিলে config.json দেখে সব role, category আর channel বানায়, সাথে permission-ও সেট করে। যতবার ইচ্ছা আবার চালানো যায়, কিছু duplicate হয় না। |
| **Welcome + section buttons** | কেউ join করলেই welcome channel-এ ping আর DM পায়। Section button চাপলে একটা form খোলে, সেখানে Student ID আর নাম দিতে হয়। |
| **Manual fallback** | `#id-name-section` channel-এ `2C 11250121964 John Doe` লিখলেও চলে, order যেকোনো হতে পারে। |
| **Verification-এর পরে** | Section role দেয়, ফলে section category, course group আর All Students খুলে যায়। Nickname হয় `1964_2C_Name`, welcome area লুকিয়ে যায়, আর ID channel-এর পুরনো message মুছে যায়। |
| **Course-section আলাদা file** | `data/students/CSE1205_2C.json` আর `.csv`, প্রতিটা course আর section-এর জন্য আলাদা (CSV Excel-এ খোলা যায়)। |
| **Duplicate ID ধরা** | একই Student ID দিয়ে দুইটা account verify করতে চাইলে admin channel-এ Approve/Reject button আসে। |
| **Roster check (optional)** | Official list upload করলে `!missing 2C` দিয়ে দেখা যায় কে এখনো join করেনি। চাইলে ভুল ID block-ও করা যায়। |
| **Rejoin auto-restore** | কেউ leave করে আবার join করলে আগের section আর nickname আপনাআপনি ফিরে পায়। |
| **Voice attendance** | `!att_start 2C 15` দিলে ১৫ মিনিটের একটা session চালু হয়। এর মধ্যে যারা voice-এ join করে তারা present হয়। শেষে CSV পাওয়া যায়, আর `!att_report` দেখায় পুরো semester-এর %। |
| **Announce / DM** | `!announce 2C ...`, `!announce CSE1205 ...`, `!announce all ...`, `!dm 2C ...` |
| **Remote control** | `!update` দিলে GitHub থেকে pull করে restart হয়। আরও আছে `!restart`, `!status` (CPU temp, RAM), `!logs` |
| **Daily backup** | প্রতিদিন 23:30-এ data-র zip `#bot-logs`-এ চলে আসে (SD card নষ্ট হলেও data থাকবে)। |

Server-এর গঠন:
```
👋 WELCOME            (শুধু unverified দের দেখায়)
   👋┃welcome  🪪┃id-name-section
🏫 ALL STUDENTS       (সব verified)
🌐 CSE 1205/1206 · DLD · 2C, 2D
🌐 CSE 2109/2110 · OOP II · 3D, 3E
🌐 CSE 2217 · CA · 4B, 4P
📘 2C · CSE 1205/1206 · DLD     📢┃announcement, 💬┃general-text ✍️, 📚┃resources, 📊┃marks-sheet,
📘 2D · …                        🎞️┃presentation-slide ✍️, 🧪┃lab, 🔊┃voice channel
📘 3D · CSE 2109/2110 · OOP II   (lab channel শুধু DLD/OOP II section-এ)
📘 3E · …
📘 4B · CSE 2217 · CA           (lab নেই)
✍️ = student শুধু এখানেই লিখতে, file দিতে আর thread খুলতে পারে। বাকি সব channel-এ শুধু পড়া আর react করা যায়।
📘 4P · CSE 2217 · CA · Evening
🔒 ADMIN              (শুধু তুমি)   🛠️┃admin  📜┃bot-logs
```

---

## Step 1: Security (সবার আগে)
1. https://discord.com/developers/applications খোলো, তোমার app-এ যাও, তারপর **Bot**-এ গিয়ে **Reset Token** চাপো। পুরনো token leak হয়ে গেছে, ওটা আর ব্যবহার করা যাবে না।
2. একই পেজে **Privileged Gateway Intents**-এর নিচে **Server Members Intent** আর **Message Content Intent** দুটোই ON করো।
3. নতুন token শুধু `.env` file-এ রাখবে, কোনোদিন code-এ বা GitHub-এ রাখবে না।

## Step 2: নতুন server আর bot invite
1. Discord-এ **+** চাপো, তারপর **Create My Own**, তারপর **For me and my friends**। একটা খালি server বানাও।
2. Bot invite করো (এখানে Administrator permission দেওয়া আছে):
   `https://discord.com/oauth2/authorize?client_id=1297622424640294922&permissions=8&scope=bot`
3. **Server Settings**-এর **Roles**-এ গিয়ে bot-এর role টেনে **সবার উপরে** রাখো।
4. Discord Settings-এর **Advanced**-এ গিয়ে **Developer Mode** ON করো। তারপর server icon-এ right-click করে **Copy Server ID** নাও, আর নিজের নামে right-click করে **Copy User ID** নাও।

## Step 3: PC-তে চালানো (Windows)
1. Python 3.11+ install করো (python.org থেকে, "Add to PATH" tick দিয়ে)।
2. Folder-এ গিয়ে `scripts\setup_windows.bat` double-click করো। Notepad-এ `.env` খুলবে, সেখানে লেখো:
   ```
   DISCORD_TOKEN=নতুন_token
   GUILD_ID=server_id
   ADMIN_IDS=তোমার_user_id
   ```
3. `scripts\run_windows.bat` চালাও। Window-তে `Logged in as ...` দেখা গেলে bot চালু হয়ে গেছে।
4. Server-এর যেকোনো channel-এ লেখো **`!setup_server`**। ১–৩ মিনিটের মধ্যে সব তৈরি হয়ে যাবে, welcome button-ও post হয়ে যাবে।
5. এরপর থেকে সব command দেবে `🛠️-admin` channel-এ। `!help` লিখলে পুরো list দেখা যাবে।
6. **Test**: অন্য একটা account (বা কোনো বন্ধুর account) দিয়ে join করো, তারপর section button চাপো, form পূরণ করো, আর দেখো channel খুলে যাচ্ছে কিনা।
   (তোমার নিজের owner account-এর nickname Discord কখনো বদলাতে দেয় না, এটা স্বাভাবিক।)

## Step 4: GitHub-এ upload
GitHub-এ একটা **Private** repo বানাও (যেমন `nubtk-discord-bot`)। তারপর project folder-এ:
```bash
git init
git add .
git commit -m "NUBTK class bot"
git branch -M main
git remote add origin https://github.com/<username>/nubtk-discord-bot.git
git push -u origin main
```
`.gitignore` থাকার কারণে `.env`, `data/` (student data), `logs/` আর `backups/` কখনো GitHub-এ যাবে না।

## Step 5: Tinkerboard-এ live করা
> একই Tinkerboard-এ তোমার আগের project-ও চলতে থাকবে, কোনো সমস্যা নেই। Bot আলাদা folder, আলাদা venv আর আলাদা service-এ চলে, RAM লাগে প্রায় ৬০–১০০ MB।

1. **আগে PC-র bot বন্ধ করো।** একই token দিয়ে দুই জায়গায় একসাথে চালালে প্রতিটা কাজ দুইবার হবে।
2. Tinkerboard-এ SSH দিয়ে ঢোকো। প্রথমে `date` দিয়ে দেখো সময় ঠিক আছে কিনা (ভুল থাকলে Discord connect হবে না)।
3. Private repo clone করার জন্য একটা deploy key লাগবে:
   ```bash
   ssh-keygen -t ed25519 -C tinkerboard -f ~/.ssh/github_deploy -N ""
   cat ~/.ssh/github_deploy.pub
   ```
   Output যা আসবে সেটা GitHub repo-র **Settings**-এ **Deploy keys**-এ গিয়ে **Add** করো (read-only)। তারপর:
   ```bash
   printf "Host github.com\n  IdentityFile ~/.ssh/github_deploy\n" >> ~/.ssh/config
   git clone git@github.com:<username>/nubtk-discord-bot.git ~/nubtk-discord-bot
   cd ~/nubtk-discord-bot
   bash scripts/install_tinkerboard.sh
   nano .env                     # PC-র .env-এর মতো একই ৩টা লাইন
   sudo systemctl start nubtk-bot
   journalctl -u nubtk-bot -f    # live log দেখার জন্য
   ```
   Python 3.9-এর চেয়ে পুরনো হলে script নিজে থেকেই `uv` দিয়ে Python 3.11 নামিয়ে নেয়, system-এ কোনো পরিবর্তন করে না।
4. PC-তে test করার সময়ের data রাখতে চাইলে PC-র `data` folder Tinkerboard-এ copy করো:
   `scp -r data <user>@<tinker-ip>:~/nubtk-discord-bot/`

## Step 6: প্রতিদিনের কাজ (PC থেকে control)
- **Code বা config বদলাতে চাইলে**: PC-তে edit করো, তারপর `git commit` আর `git push` করো। এরপর Discord admin channel-এ `!update` দিলেই Tinkerboard নিজে pull করে restart হয়ে যায়।
  Section বা channel বদলালে এর পরে `!setup_server`-ও দিও।
- **নতুন section বা course যোগ করতে**: শুধু `config.json` edit করো, তারপর push, তারপর `!update`, তারপর `!setup_server`।
- Tinkerboard-এ shortcut: `bash scripts/bot.sh status|log|restart|update`

### config.json-এর গুরুত্বপূর্ণ setting
| key | মানে |
|---|---|
| `duplicate_id_policy` | `admin_approval` (default) / `block` / `allow` |
| `roster_check` | `true` করলে roster-এ নেই এমন ID admin approval-এ যায় |
| `nickname_format` | `{last4}_{section}_{name}` (এখানে `{id}`-ও ব্যবহার করা যায়) |
| `daily_backup.time` | Backup-এর সময় (Bangladesh time) |
| `lock_nickname_change` | Student-রা নিজে nickname বদলাতে পারবে না |

---

## Data files (`data/`, GitHub-এ যায় না)
```
data/verified_records.json         master (সব student)
data/students/CSE1205_2C.json/.csv  course + section অনুযায়ী
data/students/CSE1206_2C.json/.csv  ...
data/attendance/CSE1205_2C/2026-10-24_1030.csv
data/roster/2C.csv                 official list (optional)
data/audit_log.jsonl               কে কখন verify/move/unverify হলো
data/server_ids.json               channel/role ID (setup নিজে লেখে)
```

## সমস্যা হলে
| সমস্যা | সমাধান |
|---|---|
| `PrivilegedIntentsRequired` | Step 1.2-এর দুইটা intent ON করো |
| Command দিলে উত্তর আসে না | `🛠️-admin` channel-এ দিচ্ছো তো? `.env`-এ `ADMIN_IDS` ঠিক আছে তো? |
| Role দিতে পারছে না | Bot role সবার উপরে রাখো |
| Tinkerboard-এ connect হচ্ছে না | `date` দিয়ে সময় দেখো, `journalctl -u nubtk-bot -n 50` দিয়ে log দেখো |
