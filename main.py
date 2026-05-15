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
import base64
import requests
from github import Github
from flask import Flask
from threading import Thread
from datetime import datetime
import logging

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TOKEN = os.environ.get('TELEGRAM_TOKEN')
apihelper.READ_TIMEOUT = 90
apihelper.CONNECT_TIMEOUT = 90
CONFIG_FILE = "config.json"
STOCKFISH_PATH = "/usr/games/stockfish"
bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

user_steps = {}
awaiting_pgn = {}
chess_wait_msg = {}
error_logs = []
start_time = datetime.now()

def setup_commands():
    commands = [
        types.BotCommand("start", "القائمة الرئيسية"),
        types.BotCommand("help", "مساعدة"),
        types.BotCommand("check", "تحليل شطرنج"),
        types.BotCommand("setup", "ضبط GitHub"),
        types.BotCommand("logs", "سجل الاخطاء"),
    ]
    try:
        bot.set_my_commands(commands)
    except Exception as e:
        logger.error(f"فشل تعيين الاوامر: {e}")

def save_config(token, username):
    with open(CONFIG_FILE, 'w') as f:
        json.dump({"token": token, "username": username}, f)

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    return None

def clean_txt(text):
    return str(text).replace('_','\_').replace('*','\*').replace('`','\`').replace('[','\[')

def clear_user_state(chat_id):
    repo_map = user_steps.get(chat_id, {}).get('repo_map')
    wf_map   = user_steps.get(chat_id, {}).get('wf_map')
    current  = user_steps.get(chat_id, {}).get('current_repo')
    user_steps.pop(chat_id, None)
    awaiting_pgn.pop(chat_id, None)
    chess_wait_msg.pop(chat_id, None)
    preserved = {}
    if repo_map:   preserved['repo_map']    = repo_map
    if wf_map:     preserved['wf_map']      = wf_map
    if current:    preserved['current_repo']= current
    if preserved:  user_steps[chat_id]      = preserved

def send_long_message(chat_id, text, parse_mode="Markdown", reply_markup=None):
    max_len = 4096
    if len(text) <= max_len:
        return bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
    parts = []
    while len(text) > max_len:
        idx = text.rfind('\n', 0, max_len)
        if idx == -1: idx = max_len
        parts.append(text[:idx])
        text = text[idx:].lstrip('\n')
    parts.append(text)
    for i, p in enumerate(parts):
        bot.send_message(chat_id, p, parse_mode=parse_mode,
                         reply_markup=reply_markup if i==len(parts)-1 else None)

def log_error(chat_id, error_msg):
    entry = f"{datetime.now().strftime('%H:%M:%S')} | Chat {chat_id} | {clean_txt(str(error_msg))}"
    error_logs.append(entry)
    if len(error_logs) > 10: error_logs.pop(0)
    logger.error(entry)

def get_estimated_elo(acc):
    """تقدير ELO بناءً على الدقة - مدرّج ليتناسب مع Chess.com"""
    if acc >= 99:  return 2800
    if acc >= 95:  return 2200 + int((acc - 95) * 120)
    if acc >= 90:  return 1800 + int((acc - 90) * 80)
    if acc >= 80:  return 1300 + int((acc - 80) * 50)
    if acc >= 70:  return 900  + int((acc - 70) * 40)
    if acc >= 55:  return 500  + int((acc - 55) * 26)
    return max(100, int(acc * 7))

def calculate_accuracy(loss_list):
    """معادلة Chess.com المعكوسة: 103.1668 * exp(-0.04354 * sqrt(avg_loss)) - 3.1668"""
    if not loss_list: return 100.0
    avg_loss = sum(loss_list) / len(loss_list)
    acc = 103.1668 * math.exp(-0.04354 * math.sqrt(avg_loss)) - 3.1668
    return round(max(0.0, min(100.0, acc)), 1)

def generate_eval_graph(game, engine):
    if not MATPLOTLIB_AVAILABLE: return None
    try:
        board = game.board()
        scores = []
        for move in game.mainline_moves():
            info = engine.analyse(board, chess.engine.Limit(depth=10))
            s = info["score"].relative.score(mate_score=10000)
            scores.append(max(-1000, min(1000, s / 100.0)))
            board.push(move)
        plt.figure(figsize=(8, 4))
        plt.plot(range(1, len(scores)+1), scores, marker='.', linestyle='-', color='blue')
        plt.axhline(y=0, color='black', linewidth=0.8)
        plt.title('تقييم المباراة')
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
        logger.warning(f"فشل الرسم: {e}")
        return None

# --- القائمة الرئيسية ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    clear_user_state(message.chat.id)
    show_main_menu(message.chat.id)

def show_main_menu(chat_id):
    clear_user_state(chat_id)
    config = load_config()
    status = f"متصل: `{config['username']}`" if config else "غير متصل بـ GitHub"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("مشاريعي", callback_data="my_projects"),
        types.InlineKeyboardButton("مشروع جديد", callback_data="create_new_repo")
    )
    markup.add(
        types.InlineKeyboardButton("احصائيات", callback_data="account_info"),
        types.InlineKeyboardButton("تحليل شطرنج", callback_data="start_check")
    )
    markup.add(
        types.InlineKeyboardButton("الاعدادات", callback_data="setup_now"),
        types.InlineKeyboardButton("مساعدة", callback_data="help_menu")
    )
    bot.send_message(chat_id,
        f"**مدير المهام السحابي**\n\n{status}\n\nاختر من القائمة:",
        parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data == "main_menu")
def back_to_main(call):
    bot.answer_callback_query(call.id)
    clear_user_state(call.message.chat.id)
    try: bot.delete_message(call.message.chat.id, call.message.message_id)
    except: pass
    show_main_menu(call.message.chat.id)

@bot.message_handler(commands=['help'])
def cmd_help(message): show_help_menu(message.chat.id, None)

@bot.callback_query_handler(func=lambda c: c.data == "help_menu")
def callback_help(call):
    bot.answer_callback_query(call.id)
    show_help_menu(call.message.chat.id, call.message.message_id)

def show_help_menu(chat_id, message_id):
    text = (
        "**دليل الاستخدام:**\n\n"
        "**GitHub:**\n"
        "- انشاء مستودع برفع ZIP او مستودع فارغ\n"
        "- تحديث وحذف المستودعات\n"
        "- تشغيل Workflows\n\n"
        "**الشطرنج:**\n"
        "- اضغط (تحليل شطرنج) وارسل PGN\n\n"
        "**اوامر:** /start /help /check /setup /logs"
    )
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("الرئيسية", callback_data="main_menu"))
    if message_id:
        bot.edit_message_text(text, chat_id, message_id, parse_mode="Markdown", reply_markup=markup)
    else:
        bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=markup)

# --- GitHub ---
@bot.callback_query_handler(func=lambda c: c.data == "account_info")
def show_account_info(call):
    bot.answer_callback_query(call.id)
    config = load_config()
    if not config:
        bot.edit_message_text("يرجى ضبط الاعدادات اولا.", call.message.chat.id, call.message.message_id)
        return
    bot.edit_message_text("جاري جلب بيانات حسابك...", call.message.chat.id, call.message.message_id)
    try:
        user = Github(config['token']).get_user()
        text = (
            f"**معلومات GitHub:**\n\n"
            f"الاسم: {user.name or user.login}\n"
            f"المتابعون: {user.followers} | يتابع: {user.following}\n"
            f"المستودعات العامة: {user.public_repos}\n"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("الرئيسية", callback_data="main_menu"))
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              parse_mode="Markdown", reply_markup=markup)
    except Exception as e:
        log_error(call.message.chat.id, e)
        bot.edit_message_text(f"خطا: {clean_txt(e)}", call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda c: c.data == "my_projects")
def list_projects(call):
    bot.answer_callback_query(call.id)
    config = load_config()
    if not config:
        bot.edit_message_text("يرجى ضبط الاعدادات اولا.", call.message.chat.id, call.message.message_id)
        return
    bot.edit_message_text("جاري جلب مشاريعك...", call.message.chat.id, call.message.message_id)
    try:
        repos = Github(config['token']).get_user().get_repos()
        markup = types.InlineKeyboardMarkup(row_width=1)
        repo_list = {}
        for repo in repos:
            cb = f"select_repo_{hash(repo.name) % 10000}"
            repo_list[cb] = repo.name
            markup.add(types.InlineKeyboardButton(f"  {repo.name}", callback_data=cb))
        markup.add(types.InlineKeyboardButton("الرئيسية", callback_data="main_menu"))
        user_steps[call.message.chat.id] = user_steps.get(call.message.chat.id, {})
        user_steps[call.message.chat.id]['repo_map'] = repo_list
        bot.edit_message_text("**مشاريعك:**", call.message.chat.id, call.message.message_id,
                              parse_mode="Markdown", reply_markup=markup)
    except Exception as e:
        log_error(call.message.chat.id, e)
        bot.edit_message_text(f"خطا: {clean_txt(e)}", call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("select_repo_"))
def repo_selected(call):
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    repo_map = user_steps.get(chat_id, {}).get('repo_map', {})
    repo_name = repo_map.get(call.data)
    if not repo_name:
        bot.edit_message_text("حدث خطا، حاول مجددا.", chat_id, call.message.message_id)
        return
    user_steps[chat_id]['current_repo'] = repo_name
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("تحديث ZIP", callback_data="cmd_update_repo"),
        types.InlineKeyboardButton("Workflows", callback_data="cmd_workflows")
    )
    markup.add(
        types.InlineKeyboardButton("حذف نهائي", callback_data="cmd_delete_repo"),
        types.InlineKeyboardButton("رجوع", callback_data="my_projects")
    )
    markup.add(types.InlineKeyboardButton("الرئيسية", callback_data="main_menu"))
    bot.edit_message_text(f"**ادارة:** `{repo_name}`\nاختر العملية:",
                          chat_id, call.message.message_id,
                          parse_mode="Markdown", reply_markup=markup)

# --- Workflows ---
@bot.callback_query_handler(func=lambda c: c.data == "cmd_workflows")
def list_workflows(call):
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    repo_name = user_steps.get(chat_id, {}).get('current_repo')
    config = load_config()
    if not config or not repo_name:
        bot.edit_message_text("خطا في البيانات.", chat_id, call.message.message_id)
        return
    bot.edit_message_text("جاري فحص الـ Workflows...", chat_id, call.message.message_id)
    try:
        repo = Github(config['token']).get_repo(f"{config['username']}/{repo_name}")
        workflows = list(repo.get_workflows())
        markup = types.InlineKeyboardMarkup(row_width=1)
        wf_map = {}
        for wf in workflows:
            wf_map[str(wf.id)] = wf.name
            icon = "🟢" if wf.state == "active" else "🔴"
            markup.add(types.InlineKeyboardButton(
                f"{icon} {wf.name} - تشغيل",
                callback_data=f"run_wf_{wf.id}"
            ))
        user_steps[chat_id]['wf_map'] = wf_map
        back_cb = f"select_repo_{hash(repo_name) % 10000}"
        markup.add(types.InlineKeyboardButton("رجوع للمشروع", callback_data=back_cb))
        markup.add(types.InlineKeyboardButton("الرئيسية", callback_data="main_menu"))
        text = (f"**Workflows في `{repo_name}`:**\nاضغط لتشغيل:"
                if workflows else f"لا يوجد Workflows في `{repo_name}`.")
        bot.edit_message_text(text, chat_id, call.message.message_id,
                              parse_mode="Markdown", reply_markup=markup)
    except Exception as e:
        log_error(chat_id, e)
        bot.edit_message_text(f"خطا: {clean_txt(e)}", chat_id, call.message.message_id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("run_wf_"))
def run_workflow(call):
    bot.answer_callback_query(call.id, "جاري التشغيل...")
    chat_id = call.message.chat.id
    wf_id = call.data.replace("run_wf_", "")
    repo_name = user_steps.get(chat_id, {}).get('current_repo')
    config = load_config()
    if not config or not repo_name:
        bot.answer_callback_query(call.id, "خطا في البيانات.")
        return
    try:
        repo = Github(config['token']).get_repo(f"{config['username']}/{repo_name}")
        branch = repo.default_branch
        url = f"https://api.github.com/repos/{config['username']}/{repo_name}/actions/workflows/{wf_id}/dispatches"
        headers = {"Authorization": f"token {config['token']}",
                   "Accept": "application/vnd.github.v3+json"}
        r = requests.post(url, headers=headers, json={"ref": branch})
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("عرض Workflows", callback_data="cmd_workflows"))
        markup.add(types.InlineKeyboardButton("الرئيسية", callback_data="main_menu"))
        wf_name = user_steps.get(chat_id, {}).get('wf_map', {}).get(wf_id, wf_id)
        if r.status_code == 204:
            bot.edit_message_text(f"تم تشغيل `{wf_name}` على فرع `{branch}`!",
                                  chat_id, call.message.message_id,
                                  parse_mode="Markdown", reply_markup=markup)
        else:
            err = r.json().get('message', r.text)
            bot.edit_message_text(f"فشل التشغيل:\n`{clean_txt(err)}`",
                                  chat_id, call.message.message_id,
                                  parse_mode="Markdown", reply_markup=markup)
    except Exception as e:
        log_error(chat_id, e)
        bot.edit_message_text(f"خطا: {clean_txt(e)}", chat_id, call.message.message_id)

# --- حذف مستودع ---
@bot.callback_query_handler(func=lambda c: c.data == "cmd_delete_repo")
def confirm_delete_repo(call):
    bot.answer_callback_query(call.id)
    repo_name = user_steps.get(call.message.chat.id, {}).get('current_repo')
    if not repo_name: return
    back_cb = f"select_repo_{hash(repo_name) % 10000}"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("نعم، احذف", callback_data="execute_delete_repo"),
        types.InlineKeyboardButton("الغاء", callback_data=back_cb)
    )
    markup.add(types.InlineKeyboardButton("الرئيسية", callback_data="main_menu"))
    bot.edit_message_text(f"هل انت متاكد من حذف `{repo_name}` نهائيا؟",
                          call.message.chat.id, call.message.message_id,
                          parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data == "execute_delete_repo")
def execute_delete(call):
    bot.answer_callback_query(call.id)
    repo_name = user_steps.get(call.message.chat.id, {}).get('current_repo')
    config = load_config()
    try:
        Github(config['token']).get_repo(f"{config['username']}/{repo_name}").delete()
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("الرئيسية", callback_data="main_menu"))
        bot.edit_message_text("تم الحذف بنجاح.", call.message.chat.id, call.message.message_id,
                              reply_markup=markup)
    except Exception as e:
        log_error(call.message.chat.id, e)
        bot.edit_message_text(f"فشل: {clean_txt(e)}", call.message.chat.id, call.message.message_id)

# --- رفع ZIP (النسخة المطورة: Commit واحد) ---
@bot.callback_query_handler(func=lambda c: c.data in ["cmd_update_repo", "create_new_repo"])
def ask_for_zip(call):
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    user_steps[chat_id] = user_steps.get(chat_id, {})
    if call.data == "create_new_repo":
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("رفع ZIP (مشروع جاهز)", callback_data="zip_mode_create"),
            types.InlineKeyboardButton("مستودع فارغ", callback_data="zip_mode_empty"),
            types.InlineKeyboardButton("الغاء", callback_data="main_menu")
        )
        bot.edit_message_text("**انشاء مشروع جديد**\nاختر طريقة الانشاء:",
                              chat_id, call.message.message_id,
                              parse_mode="Markdown", reply_markup=markup)
    else:
        repo_name = user_steps[chat_id].get('current_repo', 'المشروع')
        user_steps[chat_id]['mode'] = 'update'
        markup = types.InlineKeyboardMarkup().add(
            types.InlineKeyboardButton("الغاء", callback_data="main_menu"))
        msg = bot.edit_message_text(
            f"**تحديث `{repo_name}`**\nارسل ملف ZIP الان:",
            chat_id, call.message.message_id,
            parse_mode="Markdown", reply_markup=markup)
        user_steps[chat_id]['waiting_zip_msg'] = msg.message_id

@bot.callback_query_handler(func=lambda c: c.data == "zip_mode_create")
def zip_mode_create(call):
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    user_steps[chat_id] = user_steps.get(chat_id, {})
    user_steps[chat_id]['mode'] = 'create'
    markup = types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton("الغاء", callback_data="main_menu"))
    msg = bot.edit_message_text("**انشاء من ZIP**\nارسل ملف ZIP الان:",
                                chat_id, call.message.message_id,
                                parse_mode="Markdown", reply_markup=markup)
    user_steps[chat_id]['waiting_zip_msg'] = msg.message_id

@bot.callback_query_handler(func=lambda c: c.data == "zip_mode_empty")
def zip_mode_empty(call):
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    user_steps[chat_id] = user_steps.get(chat_id, {})
    user_steps[chat_id]['mode'] = 'create_empty'
    markup = types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton("الغاء", callback_data="main_menu"))
    msg = bot.send_message(chat_id, "ارسل **اسم المستودع** الجديد:",
                           parse_mode="Markdown", reply_markup=markup)
    bot.register_next_step_handler(msg, create_empty_repo_step)
    try: bot.delete_message(chat_id, call.message.message_id)
    except: pass

def create_empty_repo_step(message):
    chat_id = message.chat.id
    repo_name = message.text.strip().replace(" ", "-")
    config = load_config()
    if not config:
        bot.send_message(chat_id, "يرجى ضبط الاعدادات اولا.")
        return
    try:
        repo = Github(config['token']).get_user().create_repo(repo_name, auto_init=True)
        user_steps[chat_id] = user_steps.get(chat_id, {})
        user_steps[chat_id]['current_repo'] = repo_name
        user_steps[chat_id]['mode'] = None
        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(
            types.InlineKeyboardButton("رفع ZIP لهذا المستودع", callback_data="cmd_update_repo"),
            types.InlineKeyboardButton("الرئيسية", callback_data="main_menu")
        )
        bot.send_message(chat_id, f"تم انشاء المستودع!\n{repo.html_url}",
                         parse_mode="Markdown", reply_markup=markup)
    except Exception as e:
        log_error(chat_id, e)
        bot.send_message(chat_id, f"فشل الانشاء: `{clean_txt(e)}`", parse_mode="Markdown")

@bot.message_handler(content_types=['document'])
def handle_zip(message):
    chat_id = message.chat.id
    config = load_config()
    if not config:
        bot.reply_to(message, "يرجى ضبط اعدادات GitHub اولا عبر /setup")
        return
    mode = user_steps.get(chat_id, {}).get('mode')
    if mode not in ('update', 'create'):
        return
    fname = message.document.file_name or ""
    if not fname.lower().endswith('.zip'):
        bot.reply_to(message, "يرجى ارسال ملف ZIP فقط.")
        return
    wait_id = user_steps[chat_id].pop('waiting_zip_msg', None)
    if wait_id:
        try: bot.delete_message(chat_id, wait_id)
        except: pass
    pmsg = bot.reply_to(message, "جاري تحميل الملف...")
    try:
        zip_bytes = bot.download_file(bot.get_file(message.document.file_id).file_path)
    except Exception as e:
        log_error(chat_id, e)
        bot.edit_message_text(f"فشل التحميل: `{clean_txt(e)}`",
                              chat_id, pmsg.message_id, parse_mode="Markdown")
        return
    if mode == 'update':
        repo_name = user_steps[chat_id].get('current_repo')
        if not repo_name:
            bot.edit_message_text("لم يتم تحديد المستودع.", chat_id, pmsg.message_id)
            return
        try:
            repo = Github(config['token']).get_repo(f"{config['username']}/{repo_name}")
            bot.edit_message_text("جاري الرفع...", chat_id, pmsg.message_id)
            extract_and_upload(repo, zip_bytes, chat_id, pmsg.message_id)
        except Exception as e:
            log_error(chat_id, e)
            bot.edit_message_text(f"خطا: `{clean_txt(e)}`", chat_id, pmsg.message_id, parse_mode="Markdown")
        finally:
            user_steps[chat_id]['mode'] = None
    elif mode == 'create':
        user_steps[chat_id]['file'] = zip_bytes
        user_steps[chat_id]['progress_msg_id'] = pmsg.message_id
        markup = types.InlineKeyboardMarkup().add(
            types.InlineKeyboardButton("الغاء", callback_data="main_menu"))
        bot.edit_message_text("ارسل **اسم المستودع الجديد**:",
                              chat_id, pmsg.message_id,
                              parse_mode="Markdown", reply_markup=markup)
        bot.register_next_step_handler_by_chat_id(chat_id, finalize_create_repo)

def finalize_create_repo(message):
    chat_id = message.chat.id
    repo_name = message.text.strip().replace(" ", "-")
    config = load_config()
    pmid = user_steps.get(chat_id, {}).get('progress_msg_id')
    zb   = user_steps.get(chat_id, {}).get('file')
    if not zb or not config:
        bot.reply_to(message, "حدث خطا، ابدا من جديد.")
        return
    try:
        bot.edit_message_text("جاري انشاء المستودع...", chat_id, pmid)
    except:
        pmid = bot.send_message(chat_id, "جاري الانشاء...").message_id
    try:
        repo = Github(config['token']).get_user().create_repo(repo_name)
        user_steps[chat_id]['current_repo'] = repo_name
        extract_and_upload(repo, zb, chat_id, pmid)
    except Exception as e:
        log_error(chat_id, e)
        try: bot.edit_message_text(f"فشل: `{clean_txt(e)}`", chat_id, pmid, parse_mode="Markdown")
        except: bot.send_message(chat_id, f"فشل: `{clean_txt(e)}`", parse_mode="Markdown")
    finally:
        user_steps[chat_id]['mode'] = None
        user_steps[chat_id].pop('file', None)

def extract_and_upload(repo, zip_bytes, chat_id, progress_msg_id=None):
    def upd(text):
        if progress_msg_id:
            try: bot.edit_message_text(text, chat_id, progress_msg_id)
            except: pass

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            files = [f for f in z.infolist()
                     if not f.is_dir()
                     and not f.filename.startswith('__MACOSX')
                     and '/.DS_Store' not in f.filename]
            total = len(files)
            if total == 0:
                upd("الملف المضغوط فارغ من الملفات الصالحة!")
                return
            upd(f"تم اكتشاف {total} ملف، جاري الرفع في Commit واحد...")

            # الحصول على الشجرة الحالية والمرجع الأساسي
            try:
                branch = repo.get_branch(repo.default_branch)
                base_commit = repo.get_git_commit(branch.commit.sha)
                base_tree = base_commit.tree.sha
            except Exception:
                base_tree = None
                base_commit = None

            # كشف المجلد الجذري المشترك
            top_dirs = set()
            for fi in files:
                parts = fi.filename.split('/')
                if len(parts) > 1:
                    top_dirs.add(parts[0])
            strip_prefix = (list(top_dirs)[0] + '/') if len(top_dirs) == 1 else ''

            # إعداد عناصر الشجرة الجديدة
            tree_elements = []
            skipped = 0
            for fi in files:
                raw_path = fi.filename
                clean_path = raw_path[len(strip_prefix):] if strip_prefix and raw_path.startswith(strip_prefix) else raw_path
                if not clean_path:
                    skipped += 1
                    continue
                content = z.read(fi.filename)
                encoded_content = base64.b64encode(content).decode('ascii')
                tree_elements.append(types.InputGitTreeElement(
                    path=clean_path,
                    mode='100644',
                    type='blob',
                    content=encoded_content
                ))

            if not tree_elements:
                upd("لم يتم العثور على ملفات صالحة بعد المعالجة!")
                return

            # إنشاء شجرة Git جديدة (مع الدمج مع الشجرة السابقة إن وجدت)
            new_tree = repo.create_git_tree(tree_elements, base_tree=base_tree)

            # إنشاء commit واحد
            parents = [base_commit] if base_commit else []
            commit_message = f"رفع {total} ملف دفعة واحدة (ZIP)"
            new_commit = repo.create_git_commit(
                message=commit_message,
                tree=new_tree,
                parents=parents
            )

            # تحديث المرجع الرئيسي ليشير إلى الـ commit الجديد
            ref_name = f"heads/{repo.default_branch}"
            git_ref = repo.get_git_ref(ref_name)
            git_ref.edit(sha=new_commit.sha, force=False)

            # النجاح
            markup = types.InlineKeyboardMarkup(row_width=1)
            markup.add(
                types.InlineKeyboardButton("فتح المستودع", url=repo.html_url),
                types.InlineKeyboardButton("الرئيسية", callback_data="main_menu")
            )
            result = (
                f"✅ تم الرفع في Commit واحد!\n\n"
                f"عدد الملفات: {total} | تم رفعها: {len(tree_elements)} | تخطي: {skipped}\n"
                f"الرابط: {repo.html_url}"
            )
            upd(result)
            # إرسال زرين مع النتيجة إذا تم التعديل بنجاح
            try:
                bot.edit_message_reply_markup(chat_id, progress_msg_id, reply_markup=markup)
            except:
                pass

    except zipfile.BadZipFile:
        upd("الملف المرسل ليس ZIP صالحًا!")
    except Exception as e:
        log_error(chat_id, e)
        upd(f"خطأ أثناء الرفع: `{clean_txt(str(e))}`")

# --- الاعداد ---
@bot.callback_query_handler(func=lambda c: c.data == "setup_now")
def callback_setup(call):
    bot.answer_callback_query(call.id)
    start_setup(call.message)

@bot.message_handler(commands=['setup'])
def start_setup(message):
    clear_user_state(message.chat.id)
    markup = types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton("الغاء", callback_data="main_menu"))
    bot.register_next_step_handler(
        bot.send_message(message.chat.id,
            "ارسل **GitHub Token** الخاص بك:",
            parse_mode="Markdown", reply_markup=markup),
        get_token_step)

def get_token_step(message):
    markup = types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton("الغاء", callback_data="main_menu"))
    bot.register_next_step_handler(
        bot.reply_to(message, "ارسل **اسم المستخدم**:",
                     parse_mode="Markdown", reply_markup=markup),
        lambda m: finish_setup(m, message.text.strip()))

def finish_setup(message, token):
    save_config(token, message.text.strip())
    markup = types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton("الرئيسية", callback_data="main_menu"))
    bot.reply_to(message, "تم حفظ التوكن واسم المستخدم بنجاح!", reply_markup=markup)

# --- الشطرنج ---
@bot.callback_query_handler(func=lambda c: c.data == "start_check")
def start_chess_check(call):
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    clear_user_state(chat_id)
    markup = types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton("الغاء", callback_data="cancel_chess"))
    msg = bot.edit_message_text("الصق نص الـ PGN هنا للتحليل:",
                                chat_id, call.message.message_id, reply_markup=markup)
    awaiting_pgn[chat_id] = True
    chess_wait_msg[chat_id] = msg.message_id

@bot.callback_query_handler(func=lambda c: c.data == "cancel_chess")
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
        markup = types.InlineKeyboardMarkup().add(
            types.InlineKeyboardButton("الغاء", callback_data="cancel_chess"))
        msg = bot.reply_to(message, "ارسل نص PGN للتحليل:", reply_markup=markup)
        awaiting_pgn[message.chat.id] = True
        chess_wait_msg[message.chat.id] = msg.message_id

@bot.message_handler(func=lambda m: awaiting_pgn.get(m.chat.id, False) and m.text)
def receive_pgn(message):
    chat_id = message.chat.id
    awaiting_pgn.pop(chat_id, None)
    wid = chess_wait_msg.pop(chat_id, None)
    if wid:
        try: bot.delete_message(chat_id, wid)
        except: pass
    process_chess(message, message.text)

def process_chess(message, pgn_data):
    try:
        game = chess.pgn.read_game(io.StringIO(pgn_data))
        if not game:
            return bot.reply_to(message, "PGN غير صالح.")
        white = clean_txt(game.headers.get("White", "White"))
        black = clean_txt(game.headers.get("Black", "Black"))
        msg_wait = bot.reply_to(message, f"جاري مراجعة: {white} vs {black}...")
        board = game.board()
        w_losses, b_losses, moments = [], [], []
        ply = 0
        with chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH) as engine:
            graph_buf = generate_eval_graph(game, engine)
            for move in game.mainline_moves():
                ply += 1
                move_number = (ply + 1) // 2
                is_white = (board.turn == chess.WHITE)
                player   = white if is_white else black
                icon     = "⚪" if is_white else "⚫"
                info = engine.analyse(board, chess.engine.Limit(depth=12))
                best_score = info["score"].relative.score(mate_score=1000)
                best_move  = info.get("pv", [None])[0]
                best_san   = board.san(best_move) if best_move else "غير متاح"
                move_san   = board.san(move)
                is_brilliant = False
                if best_move and move == best_move:
                    val = {chess.PAWN:1, chess.KNIGHT:3, chess.BISHOP:3,
                           chess.ROOK:5, chess.QUEEN:9}
                    mat_before = sum(len(board.pieces(pt, board.turn))*v for pt,v in val.items())
                    board.push(move)
                    mat_after  = sum(len(board.pieces(pt, not board.turn))*v for pt,v in val.items())
                    if mat_after < mat_before:
                        is_brilliant = True
                else:
                    board.push(move)
                post = engine.analyse(board, chess.engine.Limit(depth=10))
                played_score = -post["score"].relative.score(mate_score=1000)
                loss = max(0, best_score - played_score)
                if is_white: w_losses.append(loss)
                else:        b_losses.append(loss)
                if is_brilliant:
                    moments.append(f"{icon} نقلة {move_number} | **{player}** | ✨ Brilliant!!\n└ لعب: `{move_san}`")
                elif loss > 400:
                    moments.append(f"{icon} نقلة {move_number} | **{player}** | ❌ Blunder ??\n└ لعب: `{move_san}`\n└ الأفضل: `{best_san}`")
                elif loss > 200:
                    moments.append(f"{icon} نقلة {move_number} | **{player}** | ⚠️ Mistake ?\n└ لعب: `{move_san}`\n└ الأفضل: `{best_san}`")
        w_acc = calculate_accuracy(w_losses)
        b_acc = calculate_accuracy(b_losses)
        res = (
            f"**التقرير النهائي**\n\n"
            f"⚪ **{white}**: `{w_acc}%` (ELO {get_estimated_elo(w_acc)})\n"
            f"⚫ **{black}**: `{b_acc}%` (ELO {get_estimated_elo(b_acc)})\n"
            f"━━━━━━━━━━━━━━\n"
        )
        if moments:
            res += "**ابرز اللحظات:**\n\n" + "\n\n".join(moments[:7])
        try: bot.delete_message(message.chat.id, msg_wait.message_id)
        except: pass
        if graph_buf:
            bot.send_photo(message.chat.id, graph_buf, caption="رسم تقييم المباراة")
        markup = types.InlineKeyboardMarkup().add(
            types.InlineKeyboardButton("الرئيسية", callback_data="main_menu"))
        send_long_message(message.chat.id, res, parse_mode="Markdown", reply_markup=markup)
    except Exception as e:
        log_error(message.chat.id, e)
        markup = types.InlineKeyboardMarkup().add(
            types.InlineKeyboardButton("الرئيسية", callback_data="main_menu"))
        try:
            bot.edit_message_text(f"خطا:\n`{clean_txt(e)}`",
                                  message.chat.id, msg_wait.message_id,
                                  parse_mode="Markdown", reply_markup=markup)
        except:
            bot.send_message(message.chat.id, f"خطا:\n`{clean_txt(e)}`",
                             parse_mode="Markdown", reply_markup=markup)

@bot.message_handler(commands=['logs'])
def show_logs(message):
    if not error_logs:
        bot.reply_to(message, "لا توجد اخطاء مسجلة.")
        return
    send_long_message(message.chat.id,
        "**اخر 10 اخطاء:**\n\n" + "\n".join(f"- {l}" for l in reversed(error_logs)),
        parse_mode="Markdown")

# --- Flask ---
@app.route('/')
def home(): return "Bot is Alive!"

def run_flask():
    app.run(host='0.0.0.0', port=10000)

# --- بدء التشغيل ---
if __name__ == "__main__":
    try:
        with chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH) as e:
            e.analyse(chess.Board(), chess.engine.Limit(depth=2))
        logger.info("Stockfish يعمل.")
    except Exception as e:
        logger.critical(f"فشل Stockfish: {e}")
        exit(1)
    setup_commands()
    Thread(target=run_flask).start()
    logger.info("البوت يعمل...")
    bot.infinity_polling(timeout=90, long_polling_timeout=90)
