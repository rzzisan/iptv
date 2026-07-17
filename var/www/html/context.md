# Live TV Server — Context & Documentation

> এই সার্ভারের সম্পূর্ণ সেটআপ, কনফিগ ও সমস্যা-সমাধানের রেকর্ড।
> সর্বশেষ আপডেট: 2026-07-17 (SQLite migration + স্ট্যাটিস্টিক)

---

## 1. উদ্দেশ্য (Goal)

একটি HDMI Encoder থেকে লাইভ টিভি স্ট্রিম নিয়ে এই সার্ভারকে **লাইভ টিভি সার্ভার** বানানো হয়েছে।

- **সমস্যা যা সমাধান করা হয়েছে:** একসাথে ৫ জন সরাসরি এনকোডার থেকে দেখলে এনকোডারের CPU high হয়ে যেত।
- **সমাধান:** সার্ভার এনকোডারে **মাত্র ১টি কানেকশন** রাখে (FFmpeg), সেটিকে HLS-এ রূপান্তর করে nginx দিয়ে **১০০-২০০+ ইউজারকে** serve করে।

### Architecture (Multi-channel)

```
একাধিক Source (HDMI Encoder TS / RTSP / HLS)
      │  প্রতি চ্যানেলে ১টি FFmpeg relay (livetv-channel@<id>)
      ▼
Ubuntu Server (10.255.255.2)
      │  প্রতি চ্যানেল → re-encode → /var/www/hls/<id>/  (ch1, ch2, ...)
      │  Python backend (admin.py, :8088) — চ্যানেল CRUD, auth, systemd ম্যানেজ
      ▼
  Nginx + njs (cookie auth)  ──  /api,/admin → backend proxy
      ▼
Viewers  →  http://10.255.255.2/        (player + চ্যানেল লিস্ট)
Admin    →  http://10.255.255.2/admin/   (চ্যানেল/auth ম্যানেজমেন্ট)
```

> **মাল্টি-চ্যানেল ও অ্যাডমিন প্যানেল সম্পূর্ণ — দ্রষ্টব্য #11।** আগের single-channel `livetv-stream.service` এখন **disabled**; ch1 হিসেবে `livetv-channel@ch1`-এ migrate করা হয়েছে।

---

## 2. সার্ভার তথ্য (Server Specs)

| বিষয় | মান |
|------|-----|
| OS | Ubuntu 24.04.1 LTS (Noble) |
| RAM | 15 GiB |
| Disk free | ~45 GB |
| CPU | Intel Xeon X5650, 24 cores (অনেকগুলো offline, ~14 online) |
| Server IP | 10.255.255.2 |
| Encoder IP | 172.155.255.2 |

**ইনস্টল করা প্যাকেজ:** `nginx` (1.24), `ffmpeg` (6.1.1), `libnginx-mod-http-js` (njs 0.8.2), `curl`

---

## 3. এনকোডার তথ্য (Source)

- **চিপ:** HiSilicon **Hi3516EV300** (দুর্বল entry-level IPC SoC, RAM ~72MB)। ভারী/খারাপ ইনপুটে CPU ~৯২% saturate হয় — তখন অডিও থ্রেড পিছিয়ে যেতে পারে।
- **Input:** 1920×1080i@50 (interlaced)। ডিইন্টারলেস+স্কেল এই চিপের প্রধান CPU-ভার।
- **বর্তমান output (main/output=0):** 1280×720 @ 25fps, **baseline** profile, **4000 kbit**, CBR। audio AAC, 48000 Hz (same-as-input)।
- **Pull URL (ব্যবহৃত):** `http://172.155.255.2/0.ts`  (continuous MPEG-TS)। HLS/RTSP/FLV আউটপুট **বন্ধ** (অপ্রয়োজনীয়, CPU বাঁচাতে)।
- **Substream:** output=1,2,3 এ ৩টি 640×480@30 substream factory-default-এ চালু; firmware API দিয়ে disable করা যায়নি (শুধু main framerate 31-60 হলে auto-disable হয়, যা আমরা চাই না)। এগুলো SW-encoded, CPU খায়।

### এনকোডার Web Admin API (reference)
- লগইন: HTTP Basic `admin` / `admin`, ওয়েব UI `http://172.155.255.2/`
- **পড়া:** `GET /get_output?input=0&output=N` (XML), `GET /get_input`, `GET /get_status` (cpuusage, samplerate ইত্যাদি)
- **লেখা:** `GET /set_output?input=0&output=0&venc_profile=..&venc_bitrate=..&...` (পুরো প্যারাম সেট লাগে; বদলানোর আগে get করে সব মান নিয়ে শুধু target ফিল্ড পাল্টে পাঠাতে হয়)। response `succeed`।
- profile মান: 0=baseline, 1=main, 2=high। codec: 96=H.264, 265=H.265, 1002=MJPEG। rc_mode: 0=cbr, 1=vbr, 5=strong cbr।
- অডিও set: `GET /set_output?aenc_codec=0&aenc_bitrate=128000&aenc_input=0&digital_vol=0&samplerate_same_as_input=1`

---

## 4. ফাইল ও ডিরেক্টরি কাঠামো

| পাথ | কাজ |
|-----|-----|
| `/etc/livetv/livetv.db` | **একক সোর্স-অফ-ট্রুথ** — SQLite: settings/secrets, categories, চ্যানেল, viewer sessions (perm 600, WAL) — দ্রষ্টব্য #11 |
| *(retired)* `/etc/livetv/config.json.migrated.bak` | পুরনো JSON config, migration-এর পর রোলব্যাক রেফারেন্স হিসেবে রাখা, আর পড়া হয় না |
| `/etc/livetv/channels/<id>.env` | প্রতি চ্যানেলের URL/type (backend লেখে) |
| `/opt/livetv/db.py` | SQLite schema, migration, সব CRUD/stats query — একমাত্র persistence layer |
| `/opt/livetv/admin.py` | Python backend — HTTP routing, auth, systemd ম্যানেজ (:8088); persistence-এর জন্য `db.py` কল করে |
| `/opt/livetv/channel-stream.sh` | প্রতি-চ্যানেল FFmpeg relay স্ক্রিপ্ট |
| `/usr/local/bin/livetv-watchdog.sh` | মাল্টি-চ্যানেল freshness + A/V watchdog |
| `/etc/systemd/system/livetv-channel@.service` | templated per-channel relay (`@ch1`, `@ch2`…) |
| `/etc/systemd/system/livetv-admin.service` | backend সার্ভিস |
| `/etc/systemd/system/livetv-watchdog.{service,timer}` | watchdog (প্রতি ১৫s) |
| `/var/www/hls/<id>/` | প্রতি চ্যানেলের HLS (live.m3u8, seg*.ts) |
| `/var/www/livetv/index.html` | প্লেয়ার (HLS.js + চ্যানেল লিস্ট + login modal) |
| `/var/www/admin/index.html` | অ্যাডমিন প্যানেল (SPA) |
| `/var/www/logos/` | চ্যানেল লোগো |
| `/etc/nginx/njs/auth.js` | cookie validate (backend **জেনারেট করে**, perm 600) |
| `/etc/nginx/sites-available/livetv` | nginx সাইট কনফিগ |
| `/var/www/html/context.md` | এই ডকুমেন্ট |
| *(disabled)* `/usr/local/bin/livetv-stream.sh`, `livetv-stream.service` | পুরনো single-channel (rollback রেফারেন্স) |

---

## 5. সার্ভিসসমূহ (Services)

| সার্ভিস | কাজ | boot-এ enabled |
|---------|-----|:-:|
| `nginx` | HLS serve + auth + প্লেয়ার + proxy | ✅ |
| `livetv-admin` | Python backend (চ্যানেল/auth ম্যানেজ, :8088) | ✅ |
| `livetv-channel@<id>` | প্রতি চ্যানেলের FFmpeg relay (যেমন `@ch1`) | ✅ |
| `livetv-watchdog.timer` | প্রতি ১৫s স্বাস্থ্য পরীক্ষা (সব চ্যানেল) | ✅ |

সবগুলো সার্ভার রিবুটের পরে স্বয়ংক্রিয়ভাবে চালু হয়। নতুন চ্যানেল যোগ করলে backend তার `livetv-channel@<id>` enable+start করে।

---

## 6. FFmpeg Relay (গুরুত্বপূর্ণ অংশ)

`/usr/local/bin/livetv-stream.sh` একটি `while true` লুপে FFmpeg চালায়। মূল প্যারামিটার:

- **Input:** `-fflags +discardcorrupt+genpts`, `-rw_timeout 15000000`, `-reconnect 1` → এনকোডার drop/stall হলে ১৫s-এ নিজে ধরে আবার কানেক্ট করে।
- **Video filter:** `scale=1920:1080,fps=25` + `-fps_mode cfr` → 720p সোর্সকে 1080p-তে আপস্কেল, CFR 25fps। **সোর্সের নিজস্ব timestamp সংরক্ষিত** (index-based regeneration সরানো হয়েছে — দ্রষ্টব্য #7)।
- **Audio filter:** `aresample=async=1` → সোর্সের টাইমলাইনে অডিও sync রাখে (gap সামান্য compensate)।
- **Video encode:** `libx264 -preset veryfast -b:v 6000k -maxrate 6500k -bufsize 12000k -profile:v high -level 4.0 -g 50 -threads 8`
- **Audio encode:** `aac -b:a 128k -ar 44100 -ac 2`
- **HLS:** `-hls_time 2 -hls_list_size 6 -hls_flags delete_segments+append_list+discont_start`

**ফল:** সার্ভার আউটপুট 1080p High @ ~6 Mbps (720p সোর্স আপস্কেল করা), FFmpeg CPU ~৪.৩ core (পুরনো Xeon X5650-এ real-time, হেডরুম আছে)। আপলিংক **10 Gbps** — 200 viewer × 6 Mbps ≈ 1.2 Gbps = মাত্র ~১২%, প্রচুর হেডরুম (~১৬০০ viewer পর্যন্ত সম্ভব)।

> **কোয়ালিটির লিভার ও আর্কিটেকচার সিদ্ধান্ত:** এনকোডার (দুর্বল Hi3516EV300) হালকা রাখা হয়েছে — **baseline + 4000k @ 720p** — কারণ সার্ভার এমনিতেই **high profile-এ re-encode** করে চূড়ান্ত কোয়ালিটি ঠিক করে। এনকোডারকে high/8000k করলে CPU saturate হয় (lesson #7), অথচ দর্শকের কোয়ালিটি একই থাকে। তাই হালকা এনকোডার + শক্তিশালী সার্ভার re-encode = সেরা ভারসাম্য। এনকোডারের bitrate viewer-bandwidth-এ প্রভাব ফেলে না (১টি pull); সার্ভার আউটপুট bitrate × viewer = মোট bandwidth।

---

## 7. "Black Screen" সমস্যা — ইতিহাস ও স্থায়ী সমাধান ⭐

**লক্ষণ:** কিছুক্ষণ ঠিক চলার পর ব্রাউজারে কালো স্ক্রিন, যদিও এনকোডারের TS লিংক লোকাল প্লেয়ারে (VLC) ঠিক চলত।

**মূল কারণ:** এনকোডার পর্যায়ক্রমে তার internal ঘড়িতে বড় লাফ দেয় (33-bit PTS প্রতি ~২৬ ঘন্টায় wrap, time-sync, রেজোলিউশন বদল)। এতে HLS সেগমেন্টের timestamp ভেঙে যায়; ব্রাউজারের HLS.js কঠোর হওয়ায় render করতে পারে না (VLC সহনশীল বলে চলে)।

একটি গুরুতর রূপ: **অডিও ও ভিডিওর timestamp আলাদা টাইমলাইনে চলে যায়** (একবার ~৯৪,০০০ সেকেন্ড পার্থক্য দেখা গেছে) → A/V desync → কালো স্ক্রিন।

**সমাধানের বিবর্তন:**

1. **পুরনো ভাঙা সোর্সের সময় (index-based regen):** এনকোডারের timestamp ফেলে দিয়ে অডিও+ভিডিও দুটোরই timestamp index দিয়ে নতুন করে তৈরি করা হতো (`setpts=N/(25*TB)`, `asetpts=N/SR/TB`)। এটা কালো-স্ক্রিন ঠেকাত, কিন্তু A/V-কে জোর করে index-0-তে মেলানোয় সোর্সের আসল lip-sync নষ্ট হতো (~0.5s অফসেট)।
   > পরিত্যক্ত: `-use_wallclock_as_timestamps 1` (A/V আলাদা করে দিত)।

2. **বর্তমান (পরিষ্কার সোর্স, natural timestamps):** সোর্স ঠিক হওয়ার পর index-based regen **সরিয়ে দেওয়া হয়েছে**। এখন `fps=25` + `aresample=async=1` সোর্সের নিজস্ব (সঠিক) timestamp সংরক্ষণ করে → lip-sync ঠিক (A/V skew ~60ms)।
   > ⚠️ ট্রেড-অফ: সোর্স যদি আবার timestamp-জাম্প করা ভাঙা সোর্স হয়, কালো-স্ক্রিন ফিরতে পারে। তখন #7.1-এর index-based ফিল্টার আবার বসাতে হবে। watchdog স্থূল desync ধরবে।

3. **Watchdog (safety net):** প্রতি ১৫s `livetv-watchdog.sh` পরীক্ষা করে — A/V skew > 5s, অথবা newest segment > 20s পুরনো (stall), অথবা সার্ভিস বন্ধ → স্বয়ংক্রিয় রিস্টার্ট।

> **🔑 শিক্ষা:** "slow audio" ও বারবার কালো-স্ক্রিনের আসল কারণ ছিল **এনকোডারে দেওয়া খারাপ HDMI সোর্স** (অডিও real-time-এর ~৬৫% রেটে আসছিল, ai_samplerate ভুল 44100 ডিটেক্ট হচ্ছিল, ভিডিওতে PPS error)। ব্যবহারকারী **সোর্স পরিবর্তন** করার পর সব ঠিক হয় (নতুন সোর্সে audio সঠিক 48000)। এনকোডার সেটিং/সার্ভার কনফিগ সমস্যা ছিল না।

---

## 8. পাসওয়ার্ড প্রোটেকশন (Auth)

**লগইন যাচাই** (HLS গেটিং) দ্রুত পথে njs করে; **লগইন ইস্যু ও সব সেটিং** Python backend করে (settings এখন `livetv.db`-তে — দ্রষ্টব্য #11)।

| বিষয় | মান |
|------|-----|
| ভিউয়ার পাসওয়ার্ড | অ্যাডমিন প্যানেলে (`/admin/`) সেট করা আছে — এখানে প্লেইনটেক্সটে লেখা হয় না (নিরাপত্তার কারণে) |
| সেশন | ২৪ ঘন্টা |
| কুকি | HMAC-SHA256 সাইনড (`session=<exp>.<sig>`), viewer_secret দিয়ে |
| লগইন | প্লেয়ারের ভেতরে modal → `POST /api/login` |

**ফ্লো:** প্লেয়ার পেজ public; HLS (`/hls/*`) njs `auth.validate` দিয়ে গেটেড। auth চালু + কুকি নেই → 403 → প্লেয়ার login modal দেখায় → `/api/login` সঠিক পাসওয়ার্ডে signed cookie দেয় → njs কুকি যাচাই করে। জাল/মেয়াদোত্তীর্ণ কুকি → 403।

**সুরক্ষা:** `auth.js` শুধু `validate()` রাখে (backend জেনারেট করে; `AUTH_ENABLED` flag + `SECRET` baked-in)। auth on/off বা secret বদলালে backend `auth.js` rewrite করে `nginx -s reload` করে। ভিউয়ার পাসওয়ার্ড njs-এ লাগে না (শুধু backend লগইনে), তাই পাসওয়ার্ড বদলাতে reload লাগে না।

**পরিবর্তন:** সব কিছু **অ্যাডমিন প্যানেল** (`/admin/`) থেকে — পাসওয়ার্ড, auth on/off (দ্রষ্টব্য #10)।

---

## 9. দরকারি কমান্ড (Operations)

```bash
# সার্ভিস স্ট্যাটাস
systemctl status livetv-stream nginx livetv-watchdog.timer

# রিস্টার্ট
systemctl restart livetv-stream

# লাইভ লগ
journalctl -u livetv-stream -f

# watchdog কখন রিস্টার্ট করেছে
journalctl -t livetv-watchdog

# A/V sync যাচাই (video ও audio start_time কাছাকাছি হওয়া উচিত)
ls /var/www/hls/*.ts | tail -1 | xargs ffprobe -v error \
  -show_entries stream=codec_type,start_time,duration -of default=noprint_wrappers=1

# এনকোডার CPU/স্ট্যাটাস ও আউটপুট কনফিগ পড়া
curl -s -u admin:admin "http://172.155.255.2/get_status"  | grep -E "cpuusage|samplerate"
curl -s -u admin:admin "http://172.155.255.2/get_output?input=0&output=0" | grep -E "venc_profile|venc_bitrate|venc_framerate"

# সোর্সের A/V real-time রেট যাচাই (audio দৈর্ঘ্য ≈ video দৈর্ঘ্য হওয়া উচিত)
timeout 40 ffmpeg -v error -i http://172.155.255.2/0.ts -map 0 -c copy -t 60 /tmp/t.ts; \
ffprobe -v error -show_entries stream=codec_type,duration -of default=nw=1 /tmp/t.ts

# এন্ডপয়েন্ট
#   প্লেয়ার   : http://10.255.255.2/         (লগইন লাগে)
#   লগইন      : http://10.255.255.2/login
#   স্ট্যাটাস  : http://10.255.255.2/status    (পাবলিক)
#   HLS       : http://10.255.255.2/hls/live.m3u8  (লগইন লাগে)
```

---

## 10. মাল্টি-চ্যানেল ও অ্যাডমিন প্যানেল ⭐

**অ্যাডমিন প্যানেল:** `http://10.255.255.2/admin/` — অ্যাডমিন পাসওয়ার্ড দিয়ে লগইন (`livetv.db`-এর `settings.admin_password`)।

প্যানেল থেকে যা করা যায়:
- **চ্যানেল যোগ:** নাম + সোর্স URL (TS/HLS/RTSP) + টাইপ + লোগো (URL বা আপলোড) + ঐচ্ছিক ক্যাটাগরি(সমূহ) দিলেই নতুন চ্যানেল তৈরি — backend `livetv-channel@<id>` সার্ভিস বানিয়ে চালু করে।
- **চ্যানেল এডিট/ডিলিট।**
- **ক্যাটাগরি (multi-select):** অ্যাডমিন প্যানেলে নতুন ক্যাটাগরি তৈরি/এডিট/ডিলিট করা যায় (`id` `cat<N>`)। চ্যানেল তৈরি/এডিটের সময় চেকবক্স দিয়ে **একাধিক** ক্যাটাগরি বেছে নেওয়া যায় (কোনোটিই বেছে না নিলে uncategorized)। প্রতিটি চ্যানেলের `categories` config-এ একটি array (আগে একক `category` স্ট্রিং ছিল; পুরনো ডেটা backend `load()`-এ স্বয়ংক্রিয়ভাবে array-তে migrate হয়ে যায়)। ক্যাটাগরি ডিলিট করলে শুধু সেই ক্যাটাগরিটি চ্যানেলগুলোর array থেকে সরে যায়, চ্যানেল বা অন্য ক্যাটাগরি বহাল থাকে। প্লেয়ারে (`/`) ভিডিওর নিচে ক্যাটাগরি ট্যাব (All + প্রতিটি ক্যাটাগরি) দিয়ে চ্যানেল ফিল্টার করা যায়; ডিফল্ট **All**।
- **লগইন on/off** (টগল) — auth বন্ধ করলে কেউ পাসওয়ার্ড ছাড়াই দেখতে পারবে। (backend `auth.js` পুনঃজেনারেট করে nginx reload করে।)
- **ভিউয়ার ও অ্যাডমিন পাসওয়ার্ড পরিবর্তন।**
- **চ্যানেল চালু/বন্ধ (enabled/disable):** প্রতিটি চ্যানেলের পাশে টগল সুইচ — বন্ধ করলে `systemctl disable --now livetv-channel@<id>` চলে এবং চ্যানেলটি পাবলিক তালিকা থেকে সরে যায়; চালু করলে relay আবার শুরু হয়। *(পূর্বে এই ফিচার UI-তে ছিল না — ফর্ম সেভ করলে `enabled` ফিল্ড না পাঠানোয় ব্যাকএন্ড সবসময় `True` ধরে নিত, ফলে বন্ধ করা যেত না। এখন `POST /api/admin/channel/toggle` দিয়ে আলাদাভাবে ঠিক করা হয়েছে — নিচে দ্রষ্টব্য #11।)*
- **পরিসংখ্যান (📊):** এখন কতজন সর্বমোট ও প্রতি-চ্যানেলে দেখছে (লাইভ, প্রতি ১৫s রিফ্রেশ), এবং তারিখ-রেঞ্জ (দৈনিক/মাসিক) অনুযায়ী ইউনিক দর্শক + মোট দেখার সময়। ডিলিট করা চ্যানেলও পুরনো পরিসংখ্যানে "(মুছে ফেলা হয়েছে)" ট্যাগসহ দেখা যায় — দ্রষ্টব্য #11।

**প্লেয়ার:** `http://10.255.255.2/` — ভিডিওর নিচে হরিজন্টাল স্ক্রলযোগ্য চ্যানেল লিস্ট; ক্লিক করে চ্যানেল সুইচ। auth চালু থাকলে 403-এ login modal দেখায়।

### Backend API (`admin.py`, nginx `/api/` proxy → 127.0.0.1:8088)
| Endpoint | কাজ |
|----------|-----|
| `GET /api/channels` | public — চ্যানেল লিস্ট + auth_enabled (প্লেয়ারের জন্য) |
| `POST /api/login` | viewer login → signed cookie (njs যাচাই করে) |
| `POST /api/admin/login` | admin login → admin_session cookie |
| `GET /api/admin/state` | পূর্ণ config (admin only) |
| `POST /api/admin/channel` | add/edit (id দিলে edit), body-তে ঐচ্ছিক `categories` (cat id-এর array) |
| `POST /api/admin/channel/delete` | সফট-ডিলিট (relay বন্ধ + পাবলিক লিস্ট থেকে বাদ, কিন্তু পরিসংখ্যানের জন্য DB row থেকে যায়) |
| `POST /api/admin/channel/toggle` | `{id, enabled}` — শুধু চালু/বন্ধ টগল করে, relay start/stop করে |
| `POST /api/admin/category` | ক্যাটাগরি add/edit (id দিলে rename) |
| `POST /api/admin/category/delete` | ক্যাটাগরি ডিলিট (আশ্রিত চ্যানেলের `categories` array থেকে শুধু ওই id বাদ যায়) |
| `POST /api/admin/auth` | `{auth_enabled}` টগল |
| `POST /api/admin/password` | ভিউয়ার পাসওয়ার্ড |
| `POST /api/admin/adminpassword` | অ্যাডমিন পাসওয়ার্ড |
| `POST /api/admin/logo?filename=x.png` | লোগো আপলোড (raw body) |
| `POST /api/heartbeat` | public — প্লেয়ার প্রতি ১৫s `{vid, channel}` পাঠায় (viewer tracking) |
| `GET /api/admin/stats/live` | এখন কে কোথায় দেখছে (open sessions) |
| `GET /api/admin/stats/range?from&to` | তারিখ-রেঞ্জ অনুযায়ী দৈনিক/মাসিক পরিসংখ্যান |

### কনফিগ ও নোট
- **সব কিছু SQLite-এ: `/etc/livetv/livetv.db`** (settings, categories, channels, viewer sessions — দ্রষ্টব্য #11)। পুরনো `config.json` প্রথমবার migrate হয়ে `config.json.migrated.bak` নামে থেকে যায় (রোলব্যাক রেফারেন্স, আর ব্যবহার হয় না)। হাতে DB এডিট করার দরকার হলে backend-এর `db.py` module ব্যবহার করুন, সরাসরি SQL না লেখাই ভালো (id-জেনারেশন/লক লজিক আছে)। যেকোনো পরিবর্তনের পর `systemctl restart livetv-admin`।
- **চ্যানেল মোড (CPU-র মূল লিভার) — প্রতি চ্যানেলে `mode`:**
  - `auto` (ডিফল্ট): সোর্স probe করে — **H.264 → copy** (re-encode ছাড়া, CPU ~০), নাহলে transcode।
  - `copy`: শুধু remux (video copy + AAC audio)। CPU প্রায় শূন্য। H.264 সোর্সের জন্য।
  - `transcode`: পূর্ণ libx264 re-encode (720p @ 3000k veryfast)। H.265/অসামঞ্জস্যপূর্ণ/ভাঙা-timestamp সোর্সের জন্য — **প্রতি চ্যানেল ~১.৫ core**।
  - ⚡ **শিক্ষা:** ১১টি চ্যানেল transcode = ১৫.৪ core (CPU saturate)। auto/copy-তে = মাত্র ~২.২ core। তাই বেশি চ্যানেলে অবশ্যই copy/auto ব্যবহার করুন।
  - কোনো চ্যানেল copy-তে কালো স্ক্রিন দেখালে (ভাঙা timestamp/H.265) → অ্যাডমিন প্যানেলে সেটিকে **Transcode** করুন।
- **নিরাপত্তা:** backend root হিসেবে চলে, **শুধু 127.0.0.1**-এ bound (nginx proxy)। admin অংশ admin-cookie দিয়ে সুরক্ষিত।
- cookie signing: HMAC-SHA256, viewer/admin আলাদা secret; Python ও njs একই অ্যালগরিদম।

> ⚠️ **নীতিগত নোট:** শুধু **নিজের বৈধ সোর্স/এনকোডার** যোগ করা হবে। তৃতীয় পক্ষের টিভি সাইট (যেমন redforce.live) থেকে কপিরাইটেড চ্যানেল টেনে re-stream করা হবে না — অননুমোদিত পুনঃবিতরণ (copyright)। আগে এই অনুরোধ প্রত্যাখ্যান করা হয়েছে।

---

## 11. SQLite migration ও ভিউয়ার স্ট্যাটিস্টিক ⭐

**কেন:** অ্যাডমিন চেয়েছিলেন লাইভ/দৈনিক/মাসিক ভিউয়ার-সংখ্যা ও প্রতি-চ্যানেল দেখার সময়, এবং চ্যানেল ডিলিট হলেও সেই ইতিহাস যেন থেকে যায়। JSON ফাইলে এসব রাখা/query করা অসুবিধাজনক, তাই পুরো সিস্টেম (settings/categories/channels + নতুন viewer sessions) একটাই SQLite DB-তে migrate করা হয়েছে।

### আর্কিটেকচার
- **`/opt/livetv/db.py`** (নতুন module) — schema, এক-বারের JSON→SQLite migration, সব CRUD ও stats query। **`/opt/livetv/admin.py`** এখন শুধু HTTP routing + systemd/nginx glue; কোনো persistence logic নেই, সব `db.py`-কে কল করে।
- **`/etc/livetv/livetv.db`** (perm 600, WAL mode) — টেবিল: `settings` (key-value), `categories`, `channels` (soft-delete: `deleted_at` কলাম — ডিলিট করলে relay বন্ধ হয় ও পাবলিক লিস্ট থেকে বাদ যায়, কিন্তু row থেকে যায় যাতে পুরনো পরিসংখ্যানে নাম রিজলভ করা যায়), `channel_categories` (many-to-many), `sessions` (viewer heartbeat log)।
- **প্রথম রিস্টার্টে migration:** `livetv.db` না থাকলে backend পুরনো `/etc/livetv/config.json` থেকে সব ডেটা import করে, তারপর ফাইলটার নাম বদলে `config.json.migrated.bak` করে দেয় (ডিলিট করা হয় না — রোলব্যাক রেফারেন্স)। এরপরের রিস্টার্টে এই ধাপ স্কিপ হয়ে যায় (`livetv.db` থাকলে আর touch করে না)। **এই migration-এ কোনো লাইভ স্ট্রিম রিস্টার্ট হয় না** — `startup()` শুধু env ফাইল লেখে, `systemctl restart` করে না।

### ভিউয়ার ট্র্যাকিং (heartbeat মডেল)
HLS সরাসরি nginx সার্ভ করে (Python backend প্রক্সি করে না — দ্রষ্টব্য #4/#10-এর nginx কনফিগ), তাই প্রতি-রিকোয়েস্ট hook করার সুযোগ নেই। এর বদলে প্লেয়ার (`livetv/index.html`) `localStorage`-এ একটা `vid` (UUID) রাখে এবং প্রতি ১৫s + প্রতি চ্যানেল-সুইচে `POST /api/heartbeat {vid, channel}` পাঠায় (viewer-এর কাছে অদৃশ্য)।
- backend viewer-এর open session আপডেট/ক্লোজ/নতুন-ওপেন করে (`db.record_heartbeat`); একটা background sweep thread (প্রতি ২০s) ৪০s-এর বেশি নিরুত্তর session বন্ধ করে দেয়, তাই "এখন কে দেখছে" মানেই `WHERE closed=0`।
- **watch duration** = `last_seen_ts - start_ts`, আলাদা bookkeeping লাগে না।
- **টাইমজোন:** সার্ভার UTC-তে চলে কিন্তু দর্শক/অ্যাডমিন বাংলাদেশে — তাই দৈনিক/মাসিক বাকেটিং-এ ফিক্সড **+৬ঘন্টা (BDT)** অফসেট যোগ করা হয় (system localtime-এর উপর নির্ভর না করে), যাতে "আজ" মানে সঠিক বাংলাদেশ ক্যালেন্ডার-দিন হয়।
- **Lock:** viewer heartbeat-এর জন্য আলাদা `STATS_LOCK` (settings/channel mutation-এর `CONFIG_LOCK` থেকে আলাদা) — তাই অনেক viewer-এর ঘনঘন heartbeat কখনো ধীরগতির চ্যানেল-সেভ (যেটা `systemctl` কল করে) এর পেছনে আটকে থাকে না।

### চ্যানেল চালু/বন্ধ বাগ ফিক্স
আগে অ্যাডমিন UI-তে চ্যানেল বন্ধ করার কোনো নিয়ন্ত্রণ ছিল না — ফর্ম সেভ করলে `enabled` ফিল্ড কখনো পাঠানো হতো না, ব্যাকএন্ড ডিফল্ট `True` ধরে নিত, ফলে এডিট করলেও বন্ধ-করা চ্যানেল আবার চালু হয়ে যেত। এখন প্রতি চ্যানেলের পাশে আলাদা টগল সুইচ (`POST /api/admin/channel/toggle`) — মূল ফর্ম সেভ এখন বিদ্যমান enabled/disabled অবস্থা বদলায় না।
