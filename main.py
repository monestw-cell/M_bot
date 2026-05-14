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

# --- 1. الإعدادات الأساسية ---
TOKEN = os.environ.get('TELEGRAM_TOKEN')
apihelper.READ_TIMEOUT = 90
apihelper.CONNECT_TIMEOUT = 90

CONFIG_FILE = "config.json"
STOCKFISH_PATH = "/usr/games/stockfish" 
bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

user_steps = {}
start_time = datetime.now() 

# --- إعداد قائمة الأوامر ---
def setup_commands():
    commands = [
        types.BotCommand("start", "🚀 القائمة الرئيسية والمشاريع"),
        types.BotCommand("check", "♟️ تحليل مباراة شطرنج (PGN)"),
        types.BotCommand("setup", "⚙️ إعداد حساب GitHub")
    ]
    try: bot.set_my_commands(commands)
    except Exception: pass

# --- 2. وظائف النظام والرفع ---
def save_config(token, username):
    with open(CONFIG_FILE, 'w') as f: json.dump({"token": token, "username": username}, f)

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f: return json.load(f)
    return None

def clean_txt(text):
    return str(text).replace('_', '\\_').replace('*', '\\*').replace('`', '\\`').replace('[', '\\[')

def extract_and_upload(repo, zip_bytes, chat_id):
    bot.send_message(chat_id, "📦 جاري فك الضغط ورفع الملفات فرادى...")
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            for file_info in z.infolist():
                if file_info.is_dir(): continue
                file_path = file_info.filename
                file_content = z.read(file_path)
                try: repo.create_file(file_path, f"Upload {file_path}", file_content)
                except Exception:
                    try:
                        contents = repo.get_contents(file_path)
                        repo.update_file(contents.path, f"Update {file_path}", file_content, contents.sha)
                    except Exception: pass
        bot.send_message(chat_id, f"✅ تم فك الضغط ورفع الملفات بنجاح!\n🔗 الرابط: {repo.html_url}")
    except Exception as e:
        bot.send_message(chat_id, f"❌ حدث خطأ أثناء الرفع:\n`{clean_txt(e)}`", parse_mode="Markdown")

# --- 3. منطق الشطرنج ---
def get_estimated_elo(acc):
    if acc >= 98: return 2800 + (acc - 98) * 40
    if acc >= 90: return 2000 + (acc - 90) * 80
    if acc >= 75: return 1400 + (acc - 75) * 40
    return max(100, round(acc * 12))

def calculate_accuracy(loss_list):
    if not loss_list: return 100.0
    avg_loss = sum(loss_list) / len(loss_list)
    return round(100 * math.exp(-0.0035 * avg_loss), 1)

# --- 4. واجهة المستخدم ---
@bot.message_handler(commands=['start'])
def send_welcome(message): show_main_menu(message.chat.id)

def show_main_menu(chat_id):
    config = load_config()
    user_status = f"👤 المستخدم: `{config['username']}`" if config else "⚠️ لم يتم الإعداد بعد."
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("📁 مشاريعي (المستودعات)", callback_data="my_projects"),
        types.InlineKeyboardButton("🆕 إنشاء مشروع بملف ZIP", callback_data="create_new_repo"),
        types.InlineKeyboardButton("♟️ تحليل مباراة", callback_data="start_check"),
        types.InlineKeyboardButton("⚙️ الإعدادات", callback_data="setup_now")
    )
    bot.send_message(chat_id, f"🤖 **الوكيل السحابي جاهز!**\n\n{user_status}\n\nاختر من القائمة:", parse_mode="Markdown", reply_markup=markup)

# --- 5. إدارة المشاريع ---
@bot.callback_query_handler(func=lambda call: call.data == "main_menu")
def back_to_main(call):
    bot.answer_callback_query(call.id)
    bot.delete_message(call.message.chat.id, call.message.message_id)
    show_main_menu(call.message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data == "my_projects")
def list_projects(call):
    bot.answer_callback_query(call.id)
    config = load_config()
    if not config: return bot.send_message(call.message.chat.id, "⚠️ يرجى ضبط الإعدادات أولاً.")
    bot.edit_message_text("⏳ جاري جلب مشاريعك...", call.message.chat.id, call.message.message_id)
    try:
        repos = Github(config['token']).get_user().get_repos()
        markup = types.InlineKeyboardMarkup(row_width=1)
        for repo in repos: markup.add(types.InlineKeyboardButton(f"📁 {repo.name}", callback_data=f"repo_{repo.name}"))
        markup.add(types.InlineKeyboardButton("🔙 رجوع", callback_data="main_menu"))
        bot.edit_message_text("🗂️ **مشاريعك:**", call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=markup)
    except Exception as e: bot.edit_message_text(f"❌ خطأ: {clean_txt(e)}", call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("repo_"))
def repo_menu(call):
    bot.answer_callback_query(call.id)
    repo_name = call.data.replace("repo_", "")
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🔄 تحديث", callback_data=f"update_{repo_name}"),
        types.InlineKeyboardButton("🗑️ حذف", callback_data=f"delete_{repo_name}"),
        types.InlineKeyboardButton("🔙 عودة", callback_data="my_projects")
    )
    bot.edit_message_text(f"⚙️ **إدارة:** `{repo_name}`", call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("delete_"))
def confirm_delete_repo(call):
    bot.answer_callback_query(call.id)
    repo_name = call.data.replace("delete_", "")
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("⚠️ نعم، احذف", callback_data=f"confirmdel_{repo_name}"),
        types.InlineKeyboardButton("❌ إلغاء", callback_data=f"repo_{repo_name}")
    )
    bot.edit_message_text(f"⚠️ هل أنت متأكد من حذف `{repo_name}` نهائياً؟", call.message.chat.id, call.message.message_id, parse_mode="Markdown", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirmdel_"))
def execute_delete(call):
    bot.answer_callback_query(call.id)
    repo_name = call.data.replace("confirmdel_", "")
    config = load_config()
    try:
        Github(config['token']).get_repo(f"{config['username']}/{repo_name}").delete()
        bot.edit_message_text(f"✅ تم الحذف.", call.message.chat.id, call.message.message_id)
    except Exception as e: bot.edit_message_text(f"❌ فشل: {clean_txt(e)}", call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("update_") or call.data == "create_new_repo")
def ask_for_zip(call):
    bot.answer_callback_query(call.id)
    if call.data == "create_new_repo":
        user_steps[call.message.chat.id] = {'mode': 'create'}
        bot.send_message(call.message.chat.id, "📦 لإنشاء مشروع، أرسل ملف ZIP:")
    else:
        repo_name = call.data.replace("update_", "")
        user_steps[call.message.chat.id] = {'mode': 'update', 'repo': repo_name}
        bot.send_message(call.message.chat.id, f"📦 لتحديث `{repo_name}`، أرسل ملف ZIP:")

@bot.message_handler(content_types=['document'])
def handle_zip(message):
    chat_id = message.chat.id
    config = load_config()
    if not config or chat_id not in user_steps or not message.document.file_name.endswith('.zip'): return
    zip_bytes = bot.download_file(bot.get_file(message.document.file_id).file_path)
    mode = user_steps[chat_id].get('mode')
    
    if mode == 'update':
        try: extract_and_upload(Github(config['token']).get_repo(f"{config['username']}/{user_steps[chat_id]['repo']}"), zip_bytes, chat_id)
        except Exception as e: bot.send_message(chat_id, f"❌ خطأ: {clean_txt(e)}")
        finally: del user_steps[chat_id]
    elif mode == 'create':
        user_steps[chat_id]['file'] = zip_bytes
        msg = bot.send_message(chat_id, "✨ أرسل **اسماً للمشروع الجديد**:")
        bot.register_next_step_handler(msg, finalize_create_repo)

def finalize_create_repo(message):
    chat_id = message.chat.id
    repo_name = message.text.strip().replace(" ", "-")
    try:
        repo = Github(load_config()['token']).get_user().create_repo(repo_name)
        extract_and_upload(repo, user_steps[chat_id]['file'], chat_id)
    except Exception as e: bot.send_message(chat_id, f"❌ فشل: {clean_txt(e)}")
    finally:
        if chat_id in user_steps: del user_steps[chat_id]

@bot.callback_query_handler(func=lambda call: call.data in ["start_check", "setup_now"])
def handle_basic_buttons(call):
    bot.answer_callback_query(call.id) 
    if call.data == "start_check": bot.register_next_step_handler(bot.send_message(call.message.chat.id, "📝 الصق الـ PGN هنا:"), lambda m: process_chess(m, m.text))
    elif call.data == "setup_now": start_setup(call.message)

@bot.message_handler(commands=['setup'])
def start_setup(message): bot.register_next_step_handler(bot.send_message(message.chat.id, "🔑 أرسل **GitHub Token**:"), get_token_step)
def get_token_step(message): bot.register_next_step_handler(bot.reply_to(message, "👤 أرسل **اسم المستخدم**:"), lambda m: finish_setup(m, message.text.strip()))
def finish_setup(message, token):
    save_config(token, message.text.strip())
    bot.reply_to(message, "✅ تم الحفظ!")

# --- 6. نظام تحليل الشطرنج الاحترافي المطور ---
@bot.message_handler(commands=['check'])
def handle_check(message):
    data = message.text.replace('/check', '').strip()
    if data: process_chess(message, data)
    else: bot.register_next_step_handler(bot.reply_to(message, "📝 أرسل PGN:"), lambda m: process_chess(m, m.text))

def process_chess(message, pgn_data):
    try:
        pgn_io = io.StringIO(pgn_data)
        game = chess.pgn.read_game(pgn_io)
        if not game: return bot.reply_to(message, "❌ PGN غير صالح.")

        white = clean_txt(game.headers.get("White", "White"))
        black = clean_txt(game.headers.get("Black", "Black"))
        msg_wait = bot.reply_to(message, f"♟️ جاري مراجعة مباراة: {white} 🆚 {black}...")

        board = game.board()
        w_losses, b_losses, moments = [], [], []
        
        with chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH) as engine:
            for move in game.mainline_moves():
                info = engine.analyse(board, chess.engine.Limit(depth=12))
                best_score = info["score"].relative.score(mate_score=1000)
                
                # استخراج البديل الأقوى قبل أن يلعب اللاعب نقلته
                best_move = info.get("pv", [None])[0]
                best_move_san = board.san(best_move) if best_move else "غير متاح"
                
                is_white = board.turn == chess.WHITE
                player_name = white if is_white else black
                color_icon = "⚪" if is_white else "⚫"
                
                move_san = board.san(move)
                
                # فحص النقلة العبقرية (تضحية ناجحة بالقطع)
                is_brilliant = False
                if best_move and move == best_move:
                    val_map = {1: 1, 2: 3, 3: 3, 4: 5, 5: 9}
                    mat_b = sum(len(board.pieces(pt, board.turn)) * v for pt, v in val_map.items())
                    board.push(move)
                    mat_a = sum(len(board.pieces(pt, not board.turn)) * v for pt, v in val_map.items())
                    if mat_a < mat_b: is_brilliant = True
                else:
                    board.push(move)
                
                post = engine.analyse(board, chess.engine.Limit(depth=10))
                played_score = -post["score"].relative.score(mate_score=1000)
                loss = max(0, best_score - played_score)
                
                if is_white: w_losses.append(loss)
                else: b_losses.append(loss)

                # تنسيق اللحظات بالشكل الاحترافي المطلوب
                if is_brilliant: 
                    moments.append(f"{color_icon} **{player_name}** | ✨ **Brilliant !!**\n└ لعب: `{move_san}`\n💡 **الشرح:** تضحية تكتيكية رائعة لاختراق دفاع الخصم أو كسب أفضلية حاسمة!")
                elif loss > 400: 
                    moments.append(f"{color_icon} **{player_name}** | ❌ **Blunder ??**\n└ لعب: `{move_san}`\n✅ **البديل الأقوى:** `{best_move_san}`")
                elif loss > 200: 
                    moments.append(f"{color_icon} **{player_name}** | ⚠️ **Mistake ?**\n└ لعب: `{move_san}`\n✅ **البديل الأقوى:** `{best_move_san}`")

        w_acc, b_acc = calculate_accuracy(w_losses), calculate_accuracy(b_losses)
        res = f"📊 **التقرير النهائي**\n\n⚪ **{white}**: `{w_acc}%` (ELO {get_estimated_elo(w_acc)})\n⚫ **{black}**: `{b_acc}%` (ELO {get_estimated_elo(b_acc)})\n━━━━━━━━━━━━━━\n"
        if moments: 
            # إظهار أهم 7 لحظات فقط لعدم تشويه الرسالة
            res += "🔍 **أبرز اللحظات التحليلية:**\n\n" + "\n\n".join(moments[:7])
            
        bot.edit_message_text(chat_id=message.chat.id, message_id=msg_wait.message_id, text=res, parse_mode="Markdown")
    except Exception as e:
        bot.edit_message_text(chat_id=message.chat.id, message_id=msg_wait.message_id, text=f"⚠️ خطأ:\n`{clean_txt(e)}`", parse_mode="Markdown")

# --- 7. التشغيل ---
@app.route('/')
def home(): return "Bot is Alive on Hugging Face!"

def run_flask(): app.run(host='0.0.0.0', port=7860)

if __name__ == "__main__":
    setup_commands() 
    Thread(target=run_flask).start()
    print("🚀 البوت يعمل الآن...")
    bot.infinity_polling(timeout=90, long_polling_timeout=90)

