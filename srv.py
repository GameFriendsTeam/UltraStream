from pathlib import Path
import tempfile
from flask import Flask, abort, render_template, request, redirect, url_for, flash, Response, send_file
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from loader import get_videos, upload_video
from dotenv import load_dotenv
from anyascii import anyascii
import mimetypes
import uuid
import thumbnail
import sqlite3
import os
import traceback
import hashlib

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "MUST-BE-FILLED")

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

VERSION = "1.0.2"


def get_version():
    return VERSION

# ==================== БАЗА ДАННЫХ ====================


def get_db():
    conn = sqlite3.connect('users.db')
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()


init_db()


class User(UserMixin):
    def __init__(self, id, username):
        self.id = id
        self.username = username


@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id = ?',
                        (user_id,)).fetchone()
    conn.close()
    if user:
        return User(user['id'], user['username'])
    return None


# ==================== ВИДЕО ====================
VIDEOS_DIR = 'videos'
VIDEOS = get_videos(VIDEOS_DIR)


def get_video_path(video_id, quality):
    if video_id not in VIDEOS:
        return None
    if quality not in VIDEOS[video_id]["files"]:
        return None
    filename = VIDEOS[video_id]["files"][quality]
    return filename


def generate_chunks(path, start_byte=0, end_byte=None):
    with open(path, 'rb') as f:
        f.seek(start_byte)
        chunk_size = 1024 * 1024
        remaining = end_byte - start_byte + 1 if end_byte is not None else None
        while True:
            if remaining is not None and remaining <= 0:
                break
            read_size = min(
                chunk_size,
                remaining) if remaining is not None else chunk_size
            chunk = f.read(read_size)
            if not chunk:
                break
            if remaining is not None:
                remaining -= len(chunk)
            yield chunk

# ==================== МАРШРУТЫ ====================


@app.route('/')
def index():
    return render_template('index.html', videos=VIDEOS, user=current_user)


@app.route('/favicon.ico')
def icon():
    b = bytes()
    with open("64x64.ico", 'rb') as f:
        b = f.read()

    # Создаём ETag на основе содержимого файла
    etag = f'"{hashlib.md5(b).hexdigest()}"'

    # Проверяем If-None-Match заголовок для кэша
    if request.headers.get('If-None-Match') == etag:
        return Response(status=304)  # Not Modified

    response = Response(b, status=200, mimetype="image/x-icon")
    response.headers['Cache-Control'] = 'public, max-age=2592000'  # 30 дней
    response.headers['ETag'] = etag
    return response


@app.route('/watch/<video_id>')
def watch(video_id):
    if video_id not in VIDEOS:
        return "Видео не найдено", 404
    return render_template(
        'watch.html',
        video=VIDEOS[video_id],
        video_id=video_id,
        user=current_user)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        conn = get_db()
        user = conn.execute(
            'SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        conn.close()

        if user and check_password_hash(user['password'], password):
            user_obj = User(user['id'], user['username'])
            login_user(user_obj)
            return redirect(url_for('index'))
        else:
            flash('Неверный логин или пароль', 'error')

    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        if not username or not password:
            flash('Заполните все поля', 'error')
            return redirect(url_for('register'))

        hashed_password = generate_password_hash(password)

        try:
            conn = get_db()
            user = conn.execute(
                'SELECT * FROM users WHERE username = ?', (username,)).fetchone()
            
            if user:
                flash('Такой пользователь уже существует', 'error')
                return redirect(url_for('register'))

            conn.execute(
                'INSERT INTO users (username, password) VALUES (?, ?)',
                (username,
                 hashed_password))
            conn.commit()
            conn.close()
            flash('Регистрация успешна! Теперь войдите.', 'success')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Такой пользователь уже существует', 'error')

    return render_template('register.html')


@app.route('/upload', methods=['GET', 'POST'])
@login_required
def upload_page():
    global VIDEOS
    if request.method == 'POST':
        print(request.files)
        if 'video' not in request.files:
            flash('Видео не найдено', 'error')
            return Response(status=400)

        video_file = request.files['video']
        cover_file = request.files.get('cover', None)
        if video_file.filename == '':
            flash('Видео не выбрано', 'error')
            return Response(status=400)

        if video_file.content_length > 10 * 1024 * 1024 * 1024:
            flash('Видео слишком большое (максимум 10 ГБ)', 'error')
            return Response(status=400)
        if cover_file and cover_file.content_length > 5 * 1024 * 1024:
            flash('Обложка слишком большая (максимум 5 МБ)', 'error')
            return Response(status=400)

        title = request.form.get('title', '')
        description = request.form.get('description', '')
        if len(description) >= 400:
            flash('Описание слишком длинное (максимум 400 символов)', 'error')
            return Response(status=400)
        if not title.strip():
            flash('название обязательно', 'error')
            return Response(status=400)

        video_id = str(uuid.uuid4())[:16]
        target_dir = str(Path(VIDEOS_DIR) / video_id)
        os.makedirs(target_dir, exist_ok=False)

        cover_path = ''
        if not cover_file.filename == '':
            print('Cover has been loaded')
            cover_filename = video_id + \
                str(Path(secure_filename(anyascii(cover_file.filename))).suffix)
            thumbnail.load_thumbnail(cover_filename, cover_file.read())
            cover_path = "thumbnail/" + cover_filename

        file_path = ""
        success = True
        with tempfile.TemporaryDirectory() as tmp_dir:
            filename = video_id + ".mp4"
            file_path = Path(tmp_dir) / filename
            with open(file_path, 'wb') as f:
                f.write(video_file.read())

            print(file_path)

            try:
                upload_video(
                    target_dir,
                    str(file_path),
                    title,
                    description,
                    cover_path)
            except Exception as e:
                traceback.print_exc()
                print(e)
                success = False

        if not success:
            return Response(status=500)

        VIDEOS = get_videos(VIDEOS_DIR)
        return redirect(url_for('index'))
    return render_template('upload.html', user=current_user)


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# ==================== СТРИМИНГ ВИДЕО ====================


@app.route('/video/<video_id>/<quality>')
def serve_video(video_id, quality):
    path = get_video_path(video_id, quality)
    if not path:
        abort(404)
    if not os.path.exists(path):
        abort(404)

    file_size = os.path.getsize(path)
    range_header = request.headers.get('Range')

    if range_header:
        parts = range_header.replace('bytes=', '').split('-')
        start = int(parts[0]) if parts[0] else 0
        end = int(parts[1]) if len(parts) > 1 and parts[1] else file_size - 1
        if end >= file_size:
            end = file_size - 1
        content_length = end - start + 1

        response = Response(
            generate_chunks(
                path,
                start,
                end),
            status=206,
            mimetype='video/mp4',
            direct_passthrough=True)
        response.headers['Content-Range'] = f'bytes {start}-{end}/{file_size}'
        response.headers['Accept-Ranges'] = 'bytes'
        response.headers['Content-Length'] = str(content_length)
        return response
    else:
        response = Response(
            generate_chunks(path),
            mimetype='video/mp4',
            direct_passthrough=True)
        response.headers['Content-Length'] = str(file_size)
        response.headers['Accept-Ranges'] = 'bytes'
        return response

# ==================== thumbnail ====================


@app.route('/thumbnail/<thumbnail_name>')
def thumbnail_serve(thumbnail_name):
    path = thumbnail.get_thumbnail_path(thumbnail_name)
    if not os.path.exists(path):
        return Response(status=404)

    mt = mimetypes.guess_type(path)[0]

    # Создаём ETag на основе размера и времени модификации файла
    stat_info = os.stat(path)
    etag = f'"{stat_info.st_mtime}-{stat_info.st_size}"'

    # Проверяем If-None-Match заголовок для кэша
    if request.headers.get('If-None-Match') == etag:
        return Response(status=304)  # Not Modified

    response = send_file(path, mimetype=mt)
    response.headers['Cache-Control'] = 'public, max-age=2592000'  # 30 дней
    response.headers['ETag'] = etag
    return response


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(
        description='Запуск Flask-сервера для видео-платформы')

    parser.add_argument(
        '--host',
        '-H',
        default='0.0.0.0',
        help='IP-адрес для прослушивания (по умолчанию: все интерфейсы)')
    parser.add_argument(
        '--port',
        '-p',
        type=int,
        default=5000,
        help='Порт для прослушивания (по умолчанию: 5000)')
    parser.add_argument(
        '--debug',
        '-d',
        action='store_true',
        help='Запуск в режиме отладки')
    parser.add_argument(
        '--video_dir',
        '-v',
        default='videos',
        help='Путь к директории для видео (по умолчанию: videos)')
    parser.add_argument(
        '--thumbnail_dir',
        '-t',
        default='thumbnail',
        help='thumbnail dir')
    parser.add_argument(
        '--version',
        '-V',
        action='store_true',
        help='Show program version')
    parser.add_argument(
        '--no-check-update',
        '-N',
        action='store_true',
        help='Cancel check update')

    args = parser.parse_args()

    if args.version:
        print(f"UltraStream {get_version()}")
        exit()

    check_update = not args.no_check_update
    if check_update:
        from updater import check_update, get_latest_release, update
        if check_update():
            print("Update available!")
            i = True if input(
                "Do you want to update now? (y/n): ").lower() == 'y' else False
            if i:
                update(get_latest_release())
        else:
            print("No update available.")

    VIDEOS_DIR = args.video_dir
    VIDEOS = get_videos(args.video_dir)

    thumbnail.standard_dir = args.thumbnail_dir

    app.run(host=args.host, port=args.port, debug=args.debug)
