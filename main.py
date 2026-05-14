import os
import telebot
from telebot import types, apihelper
import json
import io
import chess
import chess.engine
import chess.pgn
import math
import zipfile
from github import Github
from flask import Flask
from threading import Thread
from datetime import datetime
import logging

# للرسم البياني (اختياري)
try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

# إعداد التسجيل
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- 1. الإعدادات الأساسية ---
TOKEN = os.environ.get('TELEGRAM_TOKEN')
apihelper.READ_TIMEOUT = 90
apihelper.CONNECT_TIMEOUT = 90

CONFIG_FILE = "config.json"
STOCKFISH_PATH = "/usr/games/stockfish"
bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# حالات المستخدم
user_steps = {}            # لتخزين mode, file, current_repo
awaiting_pgn = {}          # True إذا كان المستخدم بانتظار إرسال PGN
chess_wait_msg = {}        # message_id لرسالة "انتظار PGN" لتعديلها لاحقاً
error_logs = []            # سجل آخر الأخطاء

start_time = datetime.now()

# --- إعداد قائمة الأوامر التلقائية ---
def setup_commands():
    commands = [
        types.BotCommand("start", "🏠 القائمة الرئيسية"),
        types.BotCommand("help", "❓ مساعدة ودليل الاستخدام"),
        types.BotCommand("check", "♟️ تحليل مباراة شطرنج"),
        types.BotCommand("setup", "⚙️ ضبط حساب GitHub"),
        types.BotCommand("logs", "📋 سجل الأخطاء الأخيرة"),
        types.BotCommand("workflows", "⚡ إدارة workflows (قريباً)")
    ]
    try:
        bot.set_my_commands(commands)
    except Exception as e:
        logger.error(f"فشل تعيين الأوامر: {e}")

# --- 2. وظائف مساعدة ---
def save_config(token, username):
    with open(CONFIG_FILE, 'w') as f:
        json.dump({"token": token, "username": username}, f)

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return None

def clean_txt(text):
    return str(text).replace('_', '\\_').replace('*', '\\*').replace('`', '\\`').replace('[', '\\[')

def clear_user_state(chat_id):
    """مسح جميع الحالات المؤقتة للمستخدم"""
    user_steps.pop(chat_id, None)
    awaiting_pgn.pop(chat_id, None)
    chess_wait_msg.pop(chat_id, None)

def send_long_message(chat_id, text, parse_mode="Markdown", reply_markup=None):
    """تقسيم الرسالة الطويلة إلى عدة أجزاء إذا لزم الأمر"""
    max_len = 4096
    if len(text) <= max_len:
        return bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
    parts = []
    while len(text) > max_len:
        split_idx = text.rfind('\n', 0, max_len)
        if split_idx == -1:
            split_idx = max_len
        parts.append(text[:split_idx])
        text = text[split_idx:].lstrip('\n')
    parts.append(text)
    for i, part in enumerate(parts):
        bot.send_message(chat_id, part, parse_mode=parse_mode, reply_markup=reply_markup if i == len(parts)-1 else None)

def log_error(chat_id, error_msg):
    """تسجيل الخطأ في السجل العالمي"""
    entry = f"{datetime.now().strftime('%H:%M:%S')} | Chat {chat_id} | {clean_txt(str(error_msg))}"
    error_logs.append(entry)
    if len(error_logs) > 10:
        error_logs.pop(0)
    logger.error(entry)

# --- 3. وظائف الشطرنج المطورة ---
def get_estimated_elo(acc):
    if acc >= 98: return 2800 + (acc - 98) * 40
    if acc >= 90: return 2000 + (acc - 90) * 80
    if acc >= 75: return 1400 + (acc - 75) * 40
    return max(100, round(acc * 12))

def calculate_accuracy(loss_list):
    if not loss_list: return 100.0
    avg_loss = sum(loss_list) / len(loss_list)
    return round(100 * math.exp(-0.0035 * avg_loss), 1)

def generate_eval_graph(game, engine):
    """إنشاء رسم بياني لتقييم النقلات باستخدام matplotlib (إن وجدت)"""
    if not MATPLOTLIB_AVAILABLE:
        return None
    try:
        board = game.board()
        scores = []
        for move in game.mainline_moves():
            info = engine.analyse(board, chess.engine.Limit(depth=10))
            score = info["score"].relative.score(mate_score=10000)
            # تحويل score إلى centipawns مع تحديد حدود
            scores.append(max(-1000, min(1000, score / 100.0)))
            board.push(move)

        plt.figure(figsize=(8, 4))
        plt.plot(range(1, len(scores)+1), scores, marker='.', linestyle='-', color='blue')
        plt.axhline(y=0, color='black', linewidth=0.8)
        plt.title('تقييم المباراة (Evaluation)')
        plt.xlabel('رقم النقلة')
        plt.ylabel('التقييم (بيدق)')
        plt.grid(True, alpha=0.3)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=100)
        buf.seek(0)
        plt.close()
        return buf
    except Exception as e:
        logger.warning(f"فشل إنشاء الرسم البياني: {e}")
        return None

# --- 4. واجهة المستخدم (القائمة الرئيسية والمساعدة) ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    clear_user_state(message.chat.id)
    show_main_menu(message.chat.id)

def show_main_menu(chat_id):
    clear_user_state(chat_id)
    config = load_config()
    user_status = f"👤 المتصل: `{config['username']}`" if config else "⚠️ غير متصل بـ GitHub."

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🗂️ إدارة مشاريعي", callback_data="my_projects"),
        types.InlineKeyboardButton("🆕 مشروع جديد (ZIP)", callback_data="create_new_repo")
    )
    markup.add(
        types.InlineKeyboardButton("📊 إحصائيات حسابي", callback_data="account_info"),
        types.InlineKeyboardButton("♟️ تحليل شطرنج", callback_data="start_check")
    )
    markup.add(
        types.InlineKeyboardButton("⚙️ الإعدادات", callback_data="setup_now"),
        types.InlineKeyboardButton("❓ مساعدة", callback_data="help_menu")
    )

    welcome_text = f"🤖 **مدير المهام السحابي الشامل**\n\n{user_status}\n\nاختر من القائمة للبدء:"
    bot.send_message(chat_id, welcome_text, parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "main_menu")
def back_to_main(call):
    bot.answer_callback_query(call.id)
    clear_user_state(call.message.chat.id)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except:
        pass
    show_main_menu(call.message.chat.id)

@bot.message_handler(commands=['help'])
def cmd_help(message):
    show_help_menu(message.chat.id, None)

@bot.callback_query_handler(func=lambda call: call.data == "help_menu")
def callback_help(call):
    bot.answer_callback_query(call.id)
    show_help_menu(call.message.chat.id, call.message.message_id)

def show_help_menu(chat_id, message_id):
    help_text = (
        "📖 **دليل الاستخدام الشامل:**\n\n"
        "**1. GitHub 🐙:**\n"
        "• إنشاء مستودع عبر إرسال ملف ZIP.\n"
        "• تحديث وحذف المستودعات.\n"
        "• عرض حالة Workflows.\n\n"
        "**2. الشطرنج ♟️:**\n"
        "• اضغط (تحليل شطرنج) وأرسل PGN.\n"
        "• تحليل يشمل التضحيات الرائعة والأخطاء مع رسم بياني للتقييم.\n\n"
        "**3. أوامر البوت:**\n"
        "/start - /help - /check - /setup - /logs\n\n"
        "استخدم زر 🏠 للعودة دائماً."
    )
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🏠 العودة للرئيسية", callback_data="main_menu"))
    if message_id:
        bot.edit_message_text(help_text, chat_id, message_id, parse_mode="Markdown", reply_markup=markup)
    else:
        bot.send_message(chat_id, help_text, parse_mode="Markdown", reply_markup=markup)

# --- 5. إدارة GitHub ---
@bot.callback_query_handler(func=lambda call: call.data == "account_info")
def show_account_info(call):
    bot.answer_callback_query(call.id)
    config = load_config()
    if not config:
        bot.edit_message_text("⚠️ يرجى ضبط الإعدادات أولاً.", call.message.chat.id, call.message.message_id)
        return
    bot.edit_message_text("⏳ جاري جلب بيانات حسابك...", call.message.chat.id, call.message.message_id)
    try:
        user = Github(config['token']).get_user()
        info_text = (
            f"👤 **معلومات حساب GitHub:**\n\n"
            f"• **الاسم:** {user.name or user.login}\n"
            f"• **المتابعين:** {user.followers} | **يتابع:** {user.following}\n"
            f"• **المستودعات العامة:** {user.public_repos}\n"
            f"• **المستودعات الخاصة:** {user.total_private_repos if hasattr(user, 'total_private_repos') else 'غير معروف'}\n"
            f"• **الخطة:** {user.plan.name.capitalize() if user.plan else 'مجانية'}\n"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu"))
        bot.edit_message_text(info_text, call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=markup)
    except Exception as e:
        log_error(call.message.chat.id, e)
        markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu"))
        bot.edit_message_text(f"❌ خطأ: {clean_txt(e)}", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "my_projects")
def list_projects(call):
    bot.answer_callback_query(call.id)
    config = load_config()
    if not config:
        bot.edit_message_text("⚠️ يرجى ضبط الإعدادات أولاً.", call.message.chat.id, call.message.message_id)
        return
    bot.edit_message_text("⏳ جاري جلب مشاريعك...", call.message.chat.id, call.message.message_id)
    try:
        repos = Github(config['token']).get_user().get_repos()
        markup = types.InlineKeyboardMarkup(row_width=1)
        # نخزن الأسماء في user_steps
        repo_list = {}
        for repo in repos:
            # نستخدم callback_data قصير مع فهرس أو نعتمد على user_steps
            callback = f"select_repo_{hash(repo.name) % 10000}"  # معرف فريد قصير
            repo_list[callback] = repo.name
            markup.add(types.InlineKeyboardButton(f"📁 {repo.name}", callback_data=callback))
        markup.add(types.InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu"))
        # حفظ القائمة في user_steps
        user_steps[call.message.chat.id] = user_steps.get(call.message.chat.id, {})
        user_steps[call.message.chat.id]['repo_map'] = repo_list
        bot.edit_message_text("🗂️ **مشاريعك:**", call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=markup)
    except Exception as e:
        log_error(call.message.chat.id, e)
        bot.edit_message_text(f"❌ خطأ: {clean_txt(e)}", call.message.chat.id, call.message.message_id)

# معالج اختيار مستودع
@bot.callback_query_handler(func=lambda call: call.data.startswith("select_repo_"))
def repo_selected(call):
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    repo_map = user_steps.get(chat_id, {}).get('repo_map', {})
    repo_name = repo_map.get(call.data)
    if not repo_name:
        bot.edit_message_text("⚠️ حدث خطأ، حاول مجدداً.", chat_id, call.message.message_id)
        return
    user_steps[chat_id]['current_repo'] = repo_name

    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🔄 تحديث (ZIP)", callback_data="cmd_update_repo"),
        types.InlineKeyboardButton("⚡ إدارة Workflows", callback_data="cmd_workflows")
    )
    markup.add(
        types.InlineKeyboardButton("🗑️ حذف نهائي", callback_data="cmd_delete_repo"),
        types.InlineKeyboardButton("🔙 رجوع", callback_data="my_projects")
    )
    markup.add(types.InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu"))

    bot.edit_message_text(
        f"⚙️ **إدارة مشروع:** `{repo_name}`\nاختر العملية:",
        chat_id, call.message.message_id, parse_mode="Markdown", reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data == "cmd_workflows")
def list_workflows(call):
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    repo_name = user_steps.get(chat_id, {}).get('current_repo')
    if not repo_name:
        return
    config = load_config()
    bot.edit_message_text("⏳ جاري فحص الـ Workflows...", chat_id, call.message.message_id)
    try:
        repo = Github(config['token']).get_repo(f"{config['username']}/{repo_name}")
        workflows = repo.get_workflows()
        markup = types.InlineKeyboardMarkup(row_width=1)
        count = 0
        for wf in workflows:
            count += 1
            markup.add(types.InlineKeyboardButton(f"⚙️ {wf.name} ({wf.state})", callback_data="dummy_wf"))
        markup.add(types.InlineKeyboardButton("🔙 رجوع للمشروع", callback_data=f"select_repo_{hash(repo_name) % 10000}"))
        markup.add(types.InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu"))
        if count == 0:
            text = f"⚠️ لا يوجد أي Workflows في `{repo_name}`."
        else:
            text = f"⚡ **الـ Workflows في `{repo_name}`:**\n(التحكم الكامل قيد التطوير)"
        bot.edit_message_text(text, chat_id, call.message.message_id, parse_mode="Markdown", reply_markup=markup)
    except Exception as e:
        log_error(chat_id, e)
        bot.edit_message_text(f"❌ خطأ: {clean_txt(e)}", chat_id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "cmd_delete_repo")
def confirm_delete_repo(call):
    bot.answer_callback_query(call.id)
    repo_name = user_steps.get(call.message.chat.id, {}).get('current_repo')
    if not repo_name:
        return
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("⚠️ نعم، احذف", callback_data="execute_delete_repo"),
        types.InlineKeyboardButton("❌ إلغاء", callback_data=f"select_repo_{hash(repo_name) % 10000}")
    )
    markup.add(types.InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu"))
    bot.edit_message_text(f"⚠️ هل أنت متأكد من حذف `{repo_name}` نهائياً؟", call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "execute_delete_repo")
def execute_delete(call):
    bot.answer_callback_query(call.id)
    repo_name = user_steps.get(call.message.chat.id, {}).get('current_repo')
    config = load_config()
    try:
        Github(config['token']).get_repo(f"{config['username']}/{repo_name}").delete()
        markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu"))
        bot.edit_message_text(f"✅ تم الحذف بنجاح.", call.message.chat.id, call.message.message_id, reply_markup=markup)
    except Exception as e:
        log_error(call.message.chat.id, e)
        bot.edit_message_text(f"❌ فشل: {clean_txt(e)}", call.message.chat.id, call.message.message_id)

# طلب ملف ZIP للتحديث أو الإنشاء
@bot.callback_query_handler(func=lambda call: call.data in ["cmd_update_repo", "create_new_repo"])
def ask_for_zip(call):
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    user_steps[chat_id] = user_steps.get(chat_id, {})
    if call.data == "create_new_repo":
        user_steps[chat_id]['mode'] = 'create'
        text = "📦 لإنشاء مشروع، أرسل ملف ZIP الخاص به الآن:"
    else:
        user_steps[chat_id]['mode'] = 'update'
        repo_name = user_steps[chat_id].get('current_repo', 'المشروع')
        text = f"📦 لتحديث `{repo_name}`، أرسل ملف ZIP الآن:"
    markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🏠 إلغاء والرئيسية", callback_data="main_menu"))
    bot.edit_message_text(text, chat_id, call.message.message_id, parse_mode="Markdown", reply_markup=markup)

@bot.message_handler(content_types=['document'])
def handle_zip(message):
    chat_id = message.chat.id
    config = load_config()
    if not config or chat_id not in user_steps or not message.document.file_name.endswith('.zip'):
        return
    zip_bytes = bot.download_file(bot.get_file(message.document.file_id).file_path)
    mode = user_steps[chat_id].get('mode')
    if mode == 'update':
        repo_name = user_steps[chat_id].get('current_repo')
        try:
            repo = Github(config['token']).get_repo(f"{config['username']}/{repo_name}")
            extract_and_upload(repo, zip_bytes, chat_id)
        except Exception as e:
            log_error(chat_id, e)
            bot.send_message(chat_id, f"❌ خطأ: {clean_txt(e)}")
        finally:
            user_steps[chat_id]['mode'] = None
    elif mode == 'create':
        user_steps[chat_id]['file'] = zip_bytes
        markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu"))
        msg = bot.send_message(chat_id, "✨ ممتاز! أرسل **اسماً للمشروع الجديد** الآن:", reply_markup=markup)
        bot.register_next_step_handler(msg, finalize_create_repo)

def finalize_create_repo(message):
    chat_id = message.chat.id
    repo_name = message.text.strip().replace(" ", "-")
    try:
        repo = Github(load_config()['token']).get_user().create_repo(repo_name)
        extract_and_upload(repo, user_steps[chat_id]['file'], chat_id)
    except Exception as e:
        log_error(chat_id, e)
        bot.send_message(chat_id, f"❌ فشل: {clean_txt(e)}")
    finally:
        user_steps[chat_id]['mode'] = None

def extract_and_upload(repo, zip_bytes, chat_id):
    bot.send_message(chat_id, "📦 جاري فك الضغط ورفع الملفات فرادى...")
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            for file_info in z.infolist():
                if file_info.is_dir():
                    continue
                file_path = file_info.filename
                file_content = z.read(file_path)
                try:
                    repo.create_file(file_path, f"Upload {file_path}", file_content)
                except Exception:
                    try:
                        contents = repo.get_contents(file_path)
                        repo.update_file(contents.path, f"Update {file_path}", file_content, contents.sha)
                    except Exception:
                        pass
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🏠 العودة للرئيسية", callback_data="main_menu"))
        bot.send_message(chat_id, f"✅ تم فك الضغط ورفع الملفات بنجاح!\n🔗 الرابط: {repo.html_url}", reply_markup=markup)
    except Exception as e:
        log_error(chat_id, e)
        bot.send_message(chat_id, f"❌ حدث خطأ أثناء الرفع:\n`{clean_txt(e)}`", parse_mode="Markdown")

# --- إعداد الحساب ---
@bot.callback_query_handler(func=lambda call: call.data == "setup_now")
def callback_setup(call):
    bot.answer_callback_query(call.id)
    start_setup(call.message)

@bot.message_handler(commands=['setup'])
def start_setup(message):
    clear_user_state(message.chat.id)
    markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🏠 إلغاء", callback_data="main_menu"))
    bot.register_next_step_handler(
        bot.send_message(message.chat.id, "🔑 أرسل **GitHub Token** الخاص بك:\n(سيُخزن محلياً، تأكد من أمان جهازك)", reply_markup=markup),
        get_token_step
    )

def get_token_step(message):
    markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🏠 إلغاء", callback_data="main_menu"))
    bot.register_next_step_handler(
        bot.reply_to(message, "👤 الآن أرسل **اسم المستخدم**:", reply_markup=markup),
        lambda m: finish_setup(m, message.text.strip())
    )

def finish_setup(message, token):
    save_config(token, message.text.strip())
    markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu"))
    bot.reply_to(message, "✅ تم حفظ التوكن واسم المستخدم. لن تحتاج لإدخالهما مجدداً!", reply_markup=markup)

# --- 6. نظام تحليل الشطرنج (مُعاد هيكلته) ---
@bot.callback_query_handler(func=lambda call: call.data == "start_check")
def start_chess_check(call):
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    clear_user_state(chat_id)  # يمسح أي حالات سابقة
    markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🏠 إلغاء", callback_data="cancel_chess"))
    msg = bot.edit_message_text(
        "📝 الصق نص الـ PGN هنا للتحليل:",
        chat_id, call.message.message_id, reply_markup=markup
    )
    awaiting_pgn[chat_id] = True
    chess_wait_msg[chat_id] = msg.message_id

@bot.callback_query_handler(func=lambda call: call.data == "cancel_chess")
def cancel_chess(call):
    bot.answer_callback_query(call.id)
    clear_user_state(call.message.chat.id)
    show_main_menu(call.message.chat.id)

@bot.message_handler(commands=['check'])
def handle_check_command(message):
    clear_user_state(message.chat.id)
    data = message.text.replace('/check', '').strip()
    if data:
        process_chess(message, data)
    else:
        markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🏠 إلغاء", callback_data="cancel_chess"))
        msg = bot.reply_to(message, "📝 أرسل نص PGN للتحليل:", reply_markup=markup)
        awaiting_pgn[message.chat.id] = True
        chess_wait_msg[message.chat.id] = msg.message_id

# معالج الرسائل النصية العادية - يلتقط PGN إذا كان المستخدم في حالة انتظار
@bot.message_handler(func=lambda message: awaiting_pgn.get(message.chat.id, False) and message.text)
def receive_pgn(message):
    chat_id = message.chat.id
    # إلغاء حالة الانتظار فوراً
    awaiting_pgn.pop(chat_id, None)
    wait_msg_id = chess_wait_msg.pop(chat_id, None)
    # حذف رسالة الانتظار إن أمكن
    if wait_msg_id:
        try:
            bot.delete_message(chat_id, wait_msg_id)
        except:
            pass
    process_chess(message, message.text)

def process_chess(message, pgn_data):
    try:
        pgn_io = io.StringIO(pgn_data)
        game = chess.pgn.read_game(pgn_io)
        if not game:
            return bot.reply_to(message, "❌ PGN غير صالح.")

        white = clean_txt(game.headers.get("White", "White"))
        black = clean_txt(game.headers.get("Black", "Black"))
        msg_wait = bot.reply_to(message, f"♟️ جاري مراجعة مباراة: {white} 🆚 {black}...")

        board = game.board()
        w_losses, b_losses, moments = [], [], []

        with chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH) as engine:
            # رسم بياني
            graph_buf = generate_eval_graph(game, engine)

            for move in game.mainline_moves():
                info = engine.analyse(board, chess.engine.Limit(depth=12))
                best_score = info["score"].relative.score(mate_score=1000)
                best_move = info.get("pv", [None])[0]
                best_move_san = board.san(best_move) if best_move else "غير متاح"

                is_white = board.turn == chess.WHITE
                player_name = white if is_white else black
                color_icon = "⚪" if is_white else "⚫"
                move_san = board.san(move)

                # اكتشاف التضحية الرائعة (Brilliant)
                is_brilliant = False
                if best_move and move == best_move:
                    val_map = {1: 1, 2: 3, 3: 3, 4: 5, 5: 9}
                    mat_before = sum(len(board.pieces(pt, board.turn)) * v for pt, v in val_map.items())
                    board.push(move)
                    mat_after = sum(len(board.pieces(pt, not board.turn)) * v for pt, v in val_map.items())
                    if mat_after < mat_before:
                        is_brilliant = True
                else:
                    board.push(move)

                # تحليل ما بعد النقلة
                post = engine.analyse(board, chess.engine.Limit(depth=10))
                played_score = -post["score"].relative.score(mate_score=1000)
                loss = max(0, best_score - played_score)

                if is_white:
                    w_losses.append(loss)
                else:
                    b_losses.append(loss)

                if is_brilliant:
                    moments.append(f"{color_icon} **{player_name}** | ✨ **Brilliant !!**\n└ لعب: `{move_san}`\n💡 تضحية تكتيكية رائعة!")
                elif loss > 400:
                    moments.append(f"{color_icon} **{player_name}** | ❌ **Blunder ??**\n└ لعب: `{move_san}`\n✅ البديل: `{best_move_san}`")
                elif loss > 200:
                    moments.append(f"{color_icon} **{player_name}** | ⚠️ **Mistake ?**\n└ لعب: `{move_san}`\n✅ البديل: `{best_move_san}`")

        w_acc = calculate_accuracy(w_losses)
        b_acc = calculate_accuracy(b_losses)
        res = (
            f"📊 **التقرير النهائي**\n\n"
            f"⚪ **{white}**: `{w_acc}%` (ELO {get_estimated_elo(w_acc)})\n"
            f"⚫ **{black}**: `{b_acc}%` (ELO {get_estimated_elo(b_acc)})\n"
            f"━━━━━━━━━━━━━━\n"
        )
        if moments:
            res += "🔍 **أبرز اللحظات التحليلية:**\n\n" + "\n\n".join(moments[:7])

        # حذف رسالة "جاري المراجعة"
        try:
            bot.delete_message(message.chat.id, msg_wait.message_id)
        except:
            pass

        # إرسال الرسم البياني إن وجد
        if graph_buf:
            bot.send_photo(message.chat.id, graph_buf, caption="📈 رسم تقييم المباراة")
        # إرسال التقرير النصي (مقسم)
        markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu"))
        send_long_message(message.chat.id, res, parse_mode="Markdown", reply_markup=markup)

    except Exception as e:
        log_error(message.chat.id, e)
        markup = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🏠 الرئيسية", callback_data="main_menu"))
        try:
            bot.edit_message_text(f"⚠️ خطأ:\n`{clean_txt(e)}`", message.chat.id, msg_wait.message_id, parse_mode="Markdown", reply_markup=markup)
        except:
            bot.send_message(message.chat.id, f"⚠️ خطأ:\n`{clean_txt(e)}`", parse_mode="Markdown", reply_markup=markup)

# --- 7. أمر /logs ---
@bot.message_handler(commands=['logs'])
def show_logs(message):
    if not error_logs:
        bot.reply_to(message, "✅ لا توجد أخطاء مسجلة.")
        return
    logs_text = "📋 **آخر 10 أخطاء:**\n\n" + "\n".join(f"• {log}" for log in reversed(error_logs))
    send_long_message(message.chat.id, logs_text, parse_mode="Markdown")

# --- 8. خادم Flask للتشغيل المستمر ---
@app.route('/')
def home():
    return "Bot is Alive on Render!"

def run_flask():
    app.run(host='0.0.0.0', port=10000)

# --- 9. بدء التشغيل ---
if __name__ == "__main__":
    # التحقق من وجود Stockfish
    try:
        with chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH) as test_engine:
            test_engine.analyse(chess.Board(), chess.engine.Limit(depth=2))
        logger.info("✅ Stockfish يعمل بنجاح.")
    except Exception as e:
        logger.critical(f"❌ فشل تشغيل Stockfish: {e}. تأكد من تثبيته أو تعديل STOCKFISH_PATH.")
        exit(1)

    setup_commands()
    Thread(target=run_flask).start()
    logger.info("🚀 البوت يعمل الآن...")
    bot.infinity_polling(timeout=90, long_polling_timeout=90)