# 🤖 Telegram Live Join Bot

## ✅ Features
- Multiple Telegram account add/login (Telethon)
- Account list view
- Live join with selected number of accounts (joined **muted** automatically)
- Live leave with one button click
- Owner-only access
- Runs on Railway

---

## 📦 Setup Steps

### Step 1 — BotFather থেকে Bot Token নিন
1. Telegram এ @BotFather খুলুন
2. `/newbot` পাঠান
3. নাম ও username দিন
4. **Token** কপি করে রাখুন

### Step 2 — Telegram API credentials নিন
1. https://my.telegram.org এ যান
2. Login করুন → **API development tools** এ ক্লিক করুন
3. App তৈরি করুন → **api_id** ও **api_hash** কপি করুন

### Step 3 — Owner ID নিন
1. Telegram এ **@userinfobot** তে `/start` পাঠান
2. আপনার **User ID** নোট করুন

### Step 4 — Railway তে Deploy করুন
1. [railway.app](https://railway.app) এ যান → New Project
2. **Deploy from GitHub repo** বেছে নিন (অথবা "Deploy from local" দিয়ে এই folder আপলোড করুন)
3. **Variables** সেকশনে গিয়ে এই 4টি variable যোগ করুন:

| Variable   | Value                          |
|------------|-------------------------------|
| BOT_TOKEN  | আপনার bot token               |
| OWNER_ID   | আপনার telegram user ID        |
| API_ID     | my.telegram.org থেকে         |
| API_HASH   | my.telegram.org থেকে         |

4. Deploy করুন → **Worker** service হিসেবে চলবে

---

## 🚀 Bot ব্যবহার

### `/start`
Main menu আসবে তিনটি বাটন সহ:

```
[ ➕ ADD ACCOUNT ]  [ 📋 ACCOUNT LIST ]
       [ 🟢 JOIN ON LIVE ]
```

### ➕ Account Add
- নাম্বার দিন (+8801XXXXXXXXX)
- OTP কোড দিন
- যদি 2FA থাকে তাহলে password দিন
- ✅ Login Success!

### 📋 Account List
সব সেভ করা account দেখাবে নাম ও নাম্বার সহ।

### 🟢 Join On Live
1. Live link দিন (যেমন: `@channelname`)
2. কতটি account join করাবেন বলুন
3. প্রতিটি account **আসল UDP/WebRTC media connection** দিয়ে join করবে (ntgcalls engine — ঠিক যেমন Telegram app করে), সাথে **muted** থাকবে
4. **🔴 LIVE LEAVE** বাটন চাপলে সব account leave করবে

### 👑 Admin System (শুধু owner এই কমান্ডগুলো চালাতে পারবেন)
- `/add user_id` — অন্য কাউকে **admin** বানাবে; সে আপনার মতোই বটের সব feature (account add, live join/leave) ব্যবহার করতে পারবে
- `/rm user_id` — কারো **admin-ship বাদ** দেবে
- `/admins` — বর্তমান owner ও admin তালিকা দেখাবে

> ⚠️ `/add` ও `/rm` **শুধু owner** ব্যবহার করতে পারবেন — admin এই দুটো command চালাতে পারবে না।
> user_id জানতে যাকে admin দিতে চান, তাকে **@userinfobot** এ পাঠান।
> admin list `admins.json` ফাইলে সেভ থাকে — bot restart হলেও থেকে যাবে।

---

## 🛠️ কীভাবে auto-leave ঠেকানো হয়েছে
- পুরনো উপায়ে (শুধু fake join request) সার্ভার কিছুক্ষণ পর account বের করে দিচ্ছিল
- এখন **py-tgcalls (ntgcalls)** ব্যবহার হয় — প্রতিটি account আসল ধরনের call connection রাখে, তাই real human-এর মতো লিস্টে থেকে যায়
- সাথে **৪ সেকেন্ড** পর পর watchdog চেক করে, connection গেলে নিজে থেকেই আবার ঢুকে দেয়
- admin unmute করলেও account নিজে থেকেই **muted** হয়ে যাবে

**লগে যা দেখবেন:**
- `[WATCHDOG] +8801... 🟢 started (every 4.0s)`
- `[WATCHDOG] +8801... ✅ live-এ আছে (15 checks)` → প্রতি ~১ মিনিট
- `[WATCHDOG] +8801... 🔄 connection গেছে → আবার join হচ্ছে` → কখনো drop হলে নিজে থেকেই ফিরে আসবে

---

## ⚠️ গুরুত্বপূর্ণ নোট
- শুধুমাত্র **Owner** ব্যবহার করতে পারবে
- Account data `accounts.json` ফাইলে সেভ হয় (Railway volume বা persistent storage ব্যবহার করুন)
- Live join করার পর admin unmute করলেও account নিজে থেকে **muted** থাকবে
- Account live-এ যতক্ষণ থাকবে ততক্ষণ **আসল UDP call connection** চালু থাকবে (auto-leave হবে না)
- একই সাথে অনেক account ধরে রাখলে Railway-এর RAM/CPU বেশি লাগে — দরকার হলে plan বাড়িয়ে নিন
