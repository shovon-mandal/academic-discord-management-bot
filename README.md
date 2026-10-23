# 🎓 Academic Discord Management Bot

A Discord bot for running a university course server. It builds the whole server from one config file, verifies students by section, keeps class lists per course and section, takes attendance in voice channels, and can be controlled from a private admin channel.

It was built for CSE courses at **Northern University of Business and Technology Khulna (NUBTK)**. Any teacher can reuse it by editing `config.json`.

```
PC (edit + test)  ──git push──▶  GitHub  ──!update──▶  Tinker Board / Raspberry Pi / any Linux (runs 24/7)
```

---

## ✨ Features

### 🏗️ Builds the server for you
- **`!setup_server`** creates every role, category and channel from `config.json`, and sets all permissions.
- **Safe to run again.** It only creates what is missing, renames channels if you changed their names, and deletes channels you removed from the config.
- **`!reset_server confirm`** deletes everything and builds a fresh server. Use it the first time if the server already has channels.
- Every channel name has an icon, for example `📢┃announcement` and `💬┃general-text`.
- **The member list is grouped by role:** Teacher first, then each section (Sec-2C, Sec-2D, …), each in its own color.

### 👋 Easy student registration
- New members get a welcome ping and a DM with simple steps.
- The **welcome channel has one button per section**. A student clicks a button, a form opens asking for **Student ID** and **full name**, and they press Submit.
- **Backup method:** students can type `2C 11250121964 Full Name` (any order) in `🪪┃id-name-section`.
- After verification the bot:
  - gives the section role, which opens the section, course group and All Students channels
  - sets the nickname to `1964_2C_Full Name` (students cannot change it)
  - hides the welcome area from that student
  - cleans up their messages in the ID channel
  - sends them a **"👉 Start here"** link to their section's pinned welcome message

### 📌 Pinned welcome message in every section
Each section's `📢┃announcement` channel gets a pinned message with:
- the section's courses
- a short welcome from the instructor
- a **copyable invite link**, so students can invite classmates
- **instructor details and contact information**, taken from `config.json`

### 🔐 Permissions
| Where | Students can |
|---|---|
| `💬┃general-text`, `🎞️┃presentation-slide` | write, attach files, create threads |
| All other text channels | read history and react only |
| Voice channels | talk and share screen |
| `🔒 ADMIN` | nothing. Only the teacher can see it |

### 🛡️ Safety checks
- **Duplicate Student ID:** if a second Discord account uses the same ID, the teacher gets **Approve / Reject** buttons.
- **Official roster (optional):** upload the class list, see who hasn't joined yet (`!missing 2C`), and optionally hold back IDs that are not on the list.
- **Rejoin:** a student who leaves and comes back gets their section and nickname back automatically.
- Messages sent while the bot was offline are processed when it starts again.

### 📂 Separate data file for each course and section
```
data/students/CSE1205_2C.csv   data/students/CSE1205_2C.json
data/students/CSE1206_2C.csv   ...
```
The CSV files open directly in Excel. The JSON files are for scripts.
**`!excel`** sends one `.xlsx` file with a Summary sheet and one sheet for each course and section (`!excel 2C` or `!excel CSE1205` for just one).

### 🗓️ Voice attendance
- `!att_start 2C 15` opens attendance for 15 minutes. Students who join a voice channel of that section are marked present.
- When it stops, the bot posts a CSV with Present and Absent.
- `!att_report 2C` gives the percentage for each student over the semester and shows who is below 60%.

### 📢 Messages
- `!announce 2C …`, `!announce 2C:lab …`, `!announce CSE1205 …`, `!announce all …` post an announcement and ping the students (attachments are included).
- `!dm 2C …` sends a DM to every student in the section.
- `!invite` gives the invite link and a ready-made join guide for Facebook or WhatsApp groups.

### ⚙️ Remote control and backups
- **Auto-update:** every 5 minutes the bot checks GitHub. If you pushed new code, it pulls it, checks it, and restarts by itself. If `config.json` changed, the welcome messages are re-posted automatically.
- `!update` does the same right away.
- `!restart`, `!status` (uptime, CPU temperature, RAM, disk, git version) and `!logs`.
- A **daily backup** zip is posted to `📜┃bot-logs`, and `!backup` makes one any time.

---

## 🗂️ Server layout

```
👋 WELCOME            (only students who are not verified yet)
   👋┃welcome   🪪┃id-name-section
🏫 ALL STUDENTS       (every verified student)
   📌┃notice-board   🔊┃lounge
🌐 CSE 1205/1206 · DLD · 2C, 2D                 ┐
🌐 CSE 2109/2110 · OOP II · 3D, 3E               ├ shared by all sections of a course
🌐 CSE 2217 · Computer Architecture · 4B, 4P     ┘   📢┃course-announcement 📚┃course-resources 🔊┃combined class
📘 2C · CSE 1205/1206 · DLD
   📢┃announcement  💬┃general-text ✍️  📚┃resources  📊┃marks-sheet
   🎞️┃presentation-slide ✍️  🧪┃lab  🔊┃voice channel
📘 2D · 3D · 3E      (same, with 🧪┃lab)
📘 4B · 4P (Evening) (same, no lab channel)
🔒 ADMIN             (teacher only)
   🛠️┃admin   📜┃bot-logs
```
✍️ = students can write here.

### Courses (Fall 2026)
| Code | Course | Credit | Sections |
|---|---|---|---|
| CSE 1205 | Digital Logic Design | 3 | 2C, 2D |
| CSE 1206 | Digital Logic Design Lab | 1 | 2C, 2D |
| CSE 2109 | Object-Oriented Programming II | 3 | 3D, 3E |
| CSE 2110 | Object-Oriented Programming II Lab | 1 | 3D, 3E |
| CSE 2217 | Computer Architecture | 3 | 4B, 4P (Evening) |

---

## 🚀 Setup

### 1. Discord Developer Portal
1. Open https://discord.com/developers/applications, create an application, and go to **Bot**.
2. Turn ON **Server Members Intent** and **Message Content Intent**, then click **Save**.
3. Copy the bot token. Keep it secret.
4. Invite the bot with Administrator permission:
   `https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&permissions=8&scope=bot`
5. In your server, go to **Server Settings → Roles** and drag the bot's role to the **top**.

### 2. Get the files
```bash
git clone https://github.com/shovon-mandal/academic-discord-management-bot.git
cd academic-discord-management-bot
```
Copy `.env.example` to `.env` and fill it in:
```
DISCORD_TOKEN=your_bot_token
GUILD_ID=your_server_id        # Developer Mode → right-click server → Copy Server ID
ADMIN_IDS=                     # optional, your user ID. The server owner is always admin
```
> `.env` is in `.gitignore`. **Never commit it.**

### 3a. Run on Windows (for testing)
1. Install Python 3.11+ from python.org and tick **"Add to PATH"**.
2. Double-click `scripts\setup_windows.bat` (one time only).
3. Double-click `scripts\run_windows.bat`. Wait until you see `Logged in as ...`.

### 3b. Run on Tinker Board / Raspberry Pi / Linux (24/7)
```bash
git clone https://github.com/shovon-mandal/academic-discord-management-bot.git ~/discord-bot
cd ~/discord-bot
bash scripts/install_tinkerboard.sh      # venv + packages + systemd service
nano .env                                # or copy it from your PC: scp .env user@board-ip:~/discord-bot/
sudo systemctl start nubtk-bot
journalctl -u nubtk-bot -f               # live log (Ctrl+C to exit)
```
- The bot starts again by itself after a reboot or a crash.
- If the system Python is older than 3.9, the script installs Python 3.11 with `uv`. Nothing else on the system changes.
- It uses about 60–100 MB of RAM, so it can run next to other projects on the same board.
- Shortcuts: `bash scripts/bot.sh status | log | restart | update`
- ⚠️ Run the bot in **one place only**. With the same token running on your PC and the board, every action happens twice.

### 4. Build the server
In any channel, type:
```
!setup_server            # or, if the server already has channels:  !reset_server confirm
```
After 1–3 minutes, go to `🛠️┃admin` and type `!help`. Then run `!invite` and share the link with your students.

---

## 🛠️ Admin commands
Commands work only in `🛠️┃admin` or `📜┃bot-logs`, and only for the teacher.

| Command | What it does |
|---|---|
| `!help` | Shows all commands |
| `!setup_server` | Creates or updates all roles and channels |
| `!reset_server confirm` | ⚠️ Deletes everything and builds a fresh server |
| `!setup_welcome` | Re-posts the welcome buttons and section welcome messages |
| `!invite` | Invite link and join guide to share |
| `!stats` | Student count per section and course |
| `!list 2C` | All students of a section |
| `!find <id or name>` | Searches for a student |
| `!verify @user 2C <id> <name>` | Verifies a student manually |
| `!unverify @user` | Removes access and the record |
| `!move @user 3D` | Changes a student's section |
| `!rename @user <name>` | Fixes a student's name and nickname |
| `!pending` | Requests waiting for approval |
| `!notify_unverified` | Reminds unverified members by DM and mention |
| `!kick_unverified <days> [confirm]` | Lists or kicks members who never verified |
| `!roster_upload 2C` (+ CSV file) | Uploads the official class list |
| `!missing 2C` | Students on the roster who haven't joined |
| `!announce <target> <text>` | Announcement with ping. Target: `all`, `2C`, `2C:lab`, `CSE1205` |
| `!announce_silent <target> <text>` | The same, without the ping |
| `!dm <target> <text>` | DM to every student of the target |
| `!att_start 2C [CSE1206] [minutes]` | Starts voice attendance |
| `!att_stop 2C` / `!att_status` | Stops attendance and gets the CSV / shows running sessions |
| `!att_report 2C [course]` | Attendance percentage for the semester |
| `!excel [all \| 2C \| CSE1205 \| CSE1205 2C]` | Student list as one Excel file |
| `!export [all \| 2C \| CSE1205 \| CSE1205 2C]` | Downloads student files (CSV/JSON) |
| `!backup` | Zip of all data |
| `!status` / `!logs [n]` | Bot health / last log lines |
| `!update` / `!restart` | Pulls from GitHub and restarts / restarts only |

---

## 🔄 Updating (from your PC)
```bash
# edit code or config.json, then:
git add .
git commit -m "what changed"
git push
```
That's it. Within 5 minutes the bot pulls the change and restarts by itself (or type **`!update`** to do it now). If you changed sections or channels, also type **`!setup_server`**.

---

## ⚙️ Configuration (`config.json`)
| Key | Meaning |
|---|---|
| `semester`, `teacher`, `instructor` | Shown in welcome messages |
| `invite_link` | Invite link shown to students. Leave empty and the bot creates a permanent one |
| `courses` | Code, title, short name, `type` (`theory` or `lab`), linked `lab` course |
| `sections` | Section name, list of courses, role color, `evening` |
| `channels` | Channel names and icons. `student_write: true` means students can write there |
| `duplicate_id_policy` | `admin_approval` (default), `block` or `allow` |
| `roster_check` | `true` sends IDs that are not on the roster to the teacher for approval |
| `nickname_format` | Default `{last4}_{section}_{name}` (`{id}` also works) |
| `daily_backup.time` | Backup time (local time, see `utc_offset_hours`) |
| `auto_update` | `enabled` and `check_minutes` for automatic updates from GitHub |

To add a section or course next semester, edit `config.json`, push, then run `!update` and `!setup_server`.

---

## 📁 Project structure
```
bot.py                  entry point
config.json             server layout, courses, sections, texts (safe to share)
.env                    token, server ID (secret, not in git)
core/                   config, storage, parsing, system helpers
cogs/
  server_setup.py       !setup_server, !reset_server
  verification.py       welcome, buttons, form, verification, section welcome
  admin.py              admin commands
  attendance.py         voice attendance
  maintenance.py        backup, status, logs, update, restart
scripts/                Windows .bat files, Linux installer, systemd helper
data/                   student records, attendance, roster (private, not in git)
```

## 🔒 Privacy
Student data (`data/`), logs and backups stay on the machine that runs the bot. They are listed in `.gitignore`, so they never go to GitHub.

## 🧰 Troubleshooting
| Problem | Fix |
|---|---|
| `PrivilegedIntentsRequired` | Turn on both intents (Setup step 1.2) |
| Commands get no reply | Use `🛠️┃admin` and check `ADMIN_IDS` in `.env` |
| Bot can't give roles | Drag the bot role to the top of the role list |
| Channels appear twice | Run `!reset_server confirm` once |
| Board can't connect | Check the time with `date`, then look at `journalctl -u nubtk-bot -n 50` |

---

Made by **Shovon Mandal (SM)**, Lecturer, Dept. of CSE, NUBTK · Built with [discord.py](https://github.com/Rapptz/discord.py)
