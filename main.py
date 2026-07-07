import sqlite3
import aiohttp
import re
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, BufferedInputFile, Update
from aiogram.filters import CommandStart
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
import asyncio
import logging
import sys
import traceback
import ssl
import json
import os
from dotenv import load_dotenv

# Загружаем переменные окружения из .env файла
load_dotenv()

# --- Настройка из переменных окружения ---
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise ValueError("BOT_TOKEN не найден в переменных окружения!")

# URL вашего приложения на Vercel
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "https://pinterest-save-bot-git-main-nikita1601frolov-2874s-projects.vercel.app")
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "your-secret-key-here")  # Опционально, но рекомендуется
BASE_WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

# --- Настройка логгера ---
data_dir = "data"
os.makedirs(data_dir, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(data_dir, "bot.log")),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

DB_NAME = os.path.join(data_dir, "users.db")

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# --- SSL контекст ---
ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE

# --- База данных ---
def init_db():
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                user_id INTEGER UNIQUE,
                username TEXT
            )
        """)
        conn.commit()
        conn.close()
        logger.info("База данных инициализирована успешно")
    except Exception as e:
        logger.error(f"Ошибка при инициализации базы данных: {e}")
        raise

def add_user(user_id: int, username: str):
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user_id, username))
        conn.commit()
        conn.close()
        logger.info(f"Пользователь {username} ({user_id}) добавлен в базу данных")
    except Exception as e:
        logger.error(f"Ошибка при добавлении пользователя в базу данных: {e}")
        raise

def extract_json_objects(text):
    """Извлекает все JSON объекты из текста"""
    objects = []
    stack = []
    start = -1
    
    for i, char in enumerate(text):
        if char == '{':
            if not stack:
                start = i
            stack.append(char)
        elif char == '}':
            if stack:
                stack.pop()
                if not stack and start != -1:
                    try:
                        obj = json.loads(text[start:i+1])
                        objects.append(obj)
                    except json.JSONDecodeError:
                        pass
                    start = -1
    
    return objects

def find_media_in_json_objects(json_objects):
    """Находит медиа ссылки в JSON объектах"""
    video_urls = []
    image_urls = []
    
    for obj in json_objects:
        def search_dict(d):
            if isinstance(d, dict):
                for key, value in d.items():
                    if key == 'videoUrl' and isinstance(value, str):
                        video_urls.append(value)
                    elif key == 'url' in d and isinstance(value, str) and ('video' in key.lower() or 'mp4' in value.lower()):
                        video_urls.append(value)
                    elif key == 'url' and isinstance(value, str) and ('.jpg' in value or '.png' in value):
                        image_urls.append(value)
                    elif isinstance(value, dict):
                        search_dict(value)
                    elif isinstance(value, list):
                        for item in value:
                            if isinstance(item, dict):
                                search_dict(item)
            elif isinstance(d, list):
                for item in d:
                    if isinstance(item, dict):
                        search_dict(item)
        
        search_dict(obj)
    
    return video_urls, image_urls

# --- Обработчики ---
@dp.message(CommandStart())
async def start_cmd(message: Message):
    try:
        add_user(message.from_user.id, message.from_user.username or "NoUsername")
        await message.answer(
            "👋 Привет!\n\n"
            "📥 Я могу скачать фото/видео с Pinterest без водяных знаков.\n"
            "🚀 Просто отправь ссылку на пост!\n\n"
            "Поддерживаются ссылки формата:\n"
            "• https://pin.it/...\n"
            "• https://pinterest.com/pin/...\n"
            "• https://ru.pinterest.com/pin/..."
        )
        logger.info(f"Пользователь {message.from_user.username} ({message.from_user.id}) запустил бота")
    except Exception as e:
        logger.error(f"Ошибка в команде /start: {e}")
        await message.answer("⚠️ Произошла ошибка при обработке команды. Мы работаем над решением проблемы.")

@dp.message(F.text)
async def handle_pinterest(message: Message):
    pinterest_urls = re.findall(r"https?://(?:[a-z]+\.)?pinterest\.com/pin/[\w\-]+|https?://pin\.it/[\w\-]+", message.text)
    
    if not pinterest_urls:
        return
    
    url = pinterest_urls[0]
    
    if not url:
        return
    
    downloading_message = await message.answer("⏳ Скачиваю...")
    logger.info(f"Получена ссылка от пользователя {message.from_user.username} ({message.from_user.id}): {url}")

    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        }
        
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=ssl_context),
            headers=headers
        ) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    content = await resp.text()
                    logger.info(f"Получен HTML контент размером {len(content)} символов")

                    json_objects = extract_json_objects(content)
                    logger.info(f"Найдено {len(json_objects)} JSON объектов в контенте")
                    
                    json_video_urls, json_image_urls = find_media_in_json_objects(json_objects)
                    logger.info(f"Найдено {len(json_video_urls)} видео и {len(json_image_urls)} изображений в JSON объектах")
                    
                    video_urls = []
                    video_urls.extend([url.replace('\\u002F', '/').replace('\\', '') for url in json_video_urls])
                    
                    regex_video_matches = re.findall(r'"videoUrl":"([^"]*)"', content)
                    video_urls.extend([url.replace('\\u002F', '/').replace('\\', '') for url in regex_video_matches])
                    
                    video_tag_matches = re.findall(r'<video[^>]*src="([^"]*)"', content)
                    video_urls.extend(video_tag_matches)
                    
                    data_video_matches = re.findall(r'data-video-url="([^"]*)"', content)
                    video_urls.extend(data_video_matches)
                    
                    if video_urls:
                        logger.info(f"Всего найдено {len(video_urls)} потенциальных видео ссылок")
                        
                        filtered_video_urls = [url for url in video_urls if not url.endswith('.m3u8')]
                        logger.info(f"После фильтрации осталось {len(filtered_video_urls)} видео ссылок")
                        
                        if filtered_video_urls:
                            video_url_str = filtered_video_urls[0]
                            logger.info(f"Используем первую ссылку: {video_url_str}")
                            
                            logger.info("Скачиваем видео...")
                            
                            async with session.get(video_url_str, ssl=ssl_context, headers=headers) as vresp:
                                logger.info(f"Статус ответа видео: {vresp.status}")
                                if vresp.status == 200:
                                    content_length = vresp.headers.get('Content-Length')
                                    if content_length:
                                        logger.info(f"Размер видео: {content_length} байт")
                                    
                                    content_type = vresp.headers.get('Content-Type')
                                    logger.info(f"Тип контента: {content_type}")
                                    
                                    video_data = bytearray()
                                    chunk_count = 0
                                    async for chunk in vresp.content.iter_chunked(8192):
                                        video_data.extend(chunk)
                                        chunk_count += 1
                                        if chunk_count % 100 == 0:
                                            logger.info(f"Загружено {chunk_count} чанков, {len(video_data)} байт")
                                    
                                    if len(video_data) > 0:
                                        logger.info(f"Видео скачано, размер: {len(video_data)} байт")
                                        
                                        file_extension = "mp4"
                                        if content_type:
                                            if "video/mp4" in content_type:
                                                file_extension = "mp4"
                                            elif "video/webm" in content_type:
                                                file_extension = "webm"
                                            elif "video/quicktime" in content_type:
                                                file_extension = "mov"
                                            elif "video/avi" in content_type:
                                                file_extension = "avi"
                                        
                                        video = BufferedInputFile(video_data, filename=f"pinterest.{file_extension}")
                                        await bot.send_video(message.chat.id, video)
                                        await bot.delete_message(message.chat.id, downloading_message.message_id)
                                        logger.info("Видео успешно отправлено и сообщение удалено")
                                        return
                                    else:
                                        logger.error("Получены пустые данные видео")
                                        await message.answer("❌ Получены пустые данные видео. Попробуйте другую ссылку.")
                                else:
                                    logger.error(f"Ошибка при скачивании видео: статус {vresp.status}")
                                    await message.answer(f"❌ Ошибка при скачивании видео: статус {vresp.status}")
                        else:
                            logger.info("Подходящие видео ссылки не найдены")
                    else:
                        logger.info("Видео ссылки не найдены")

                    img_urls = []
                    img_urls.extend([url.replace('\\u002F', '/').replace('\\', '') for url in json_image_urls])
                    
                    regex_img_matches = re.findall(r'"url":"([^"]*\.jpg[^"]*)"', content)
                    img_urls.extend([url.replace('\\u002F', '/').replace('\\', '') for url in regex_img_matches])
                    
                    image_urls_matches = re.findall(r'"imageUrls":\["([^"]*)"\]', content)
                    img_urls.extend([url.replace('\\u002F', '/').replace('\\', '') for url in image_urls_matches])
                    
                    img_tag_matches = re.findall(r'<img[^>]*src="([^"]*)"', content)
                    img_urls.extend(img_tag_matches)
                    
                    if img_urls:
                        logger.info(f"Всего найдено {len(img_urls)} потенциальных изображений")
                        img_url_str = img_urls[0]
                        logger.info(f"Используем первую ссылку: {img_url_str}")
                        logger.info("Скачиваем изображение...")
                        
                        async with session.get(img_url_str, ssl=ssl_context, headers=headers) as iresp:
                            logger.info(f"Статус ответа изображения: {iresp.status}")
                            if iresp.status == 200:
                                content_type = iresp.headers.get('Content-Type')
                                logger.info(f"Тип контента изображения: {content_type}")
                                
                                img_data = await iresp.read()
                                if len(img_data) > 0:
                                    logger.info(f"Изображение скачано, размер: {len(img_data)} байт")
                                    
                                    file_extension = "jpg"
                                    if content_type:
                                        if "image/jpeg" in content_type:
                                            file_extension = "jpg"
                                        elif "image/png" in content_type:
                                            file_extension = "png"
                                        elif "image/gif" in content_type:
                                            file_extension = "gif"
                                        elif "image/webp" in content_type:
                                            file_extension = "webp"
                                    
                                    photo = BufferedInputFile(img_data, filename=f"pinterest.{file_extension}")
                                    await bot.send_photo(message.chat.id, photo)
                                    await bot.delete_message(message.chat.id, downloading_message.message_id)
                                    logger.info("Изображение успешно отправлено и сообщение удалено")
                                    return
                                else:
                                    logger.error("Получены пустые данные изображения")
                                    await message.answer("❌ Получены пустые данные изображения. Попробуйте другую ссылку.")
                            else:
                                logger.error(f"Ошибка при скачивании изображения: статус {iresp.status}")
                                await message.answer(f"❌ Ошибка при скачивании изображения: статус {iresp.status}")
                    else:
                        logger.info("Изображения не найдены")

                    await message.answer("❌ Не удалось найти медиа в этой ссылке.")
                    await bot.delete_message(message.chat.id, downloading_message.message_id)
                    logger.warning("Не удалось найти медиа в ссылке, сообщение удалено")
                else:
                    await message.answer("❌ Ошибка при получении данных с Pinterest.")
                    await bot.delete_message(message.chat.id, downloading_message.message_id)
                    logger.error(f"Ошибка при получении данных с Pinterest: статус {resp.status}")
    except aiohttp.ClientError as e:
        error_msg = "⚠️ Ошибка сети при подключении к Pinterest. Попробуйте позже."
        await message.answer(error_msg)
        await bot.delete_message(message.chat.id, downloading_message.message_id)
        logger.error(f"Ошибка сети при подключении к Pinterest: {e}")
    except Exception as e:
        error_msg = f"⚠️ Ошибка: {e}"
        await message.answer(error_msg)
        await bot.delete_message(message.chat.id, downloading_message.message_id)
        logger.error(f"Ошибка при обработке ссылки Pinterest: {e}")
        logger.error(traceback.format_exc())

# --- Настройка веб-хука ---
async def on_startup():
    """Устанавливает веб-хук при запуске приложения"""
    try:
        await bot.set_webhook(
            url=BASE_WEBHOOK_URL,
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=True  # Очищает очередь обновлений
        )
        logger.info(f"Веб-хук установлен на {BASE_WEBHOOK_URL}")
    except Exception as e:
        logger.error(f"Ошибка при установке веб-хука: {e}")
        raise

async def on_shutdown():
    """Закрывает сессию бота при остановке приложения"""
    await bot.session.close()
    logger.info("Сессия бота закрыта")

# --- Создание приложения aiohttp ---
def create_app() -> web.Application:
    """Создает и настраивает веб-приложение"""
    app = web.Application()
    
    # Создаем обработчик веб-хука
    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
    )
    
    # Настраиваем маршруты
    webhook_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    
    # Добавляем обработчики событий старта и остановки
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    
    # Добавляем корневой маршрут для проверки работоспособности
    async def health_check(request):
        return web.Response(text="Bot is running!")
    
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    
    return app

# --- Экспортируем приложение для хостинга ---
app = create_app()
application = app  # Некоторые хостинги ожидают именно application

# --- Точка входа для локального запуска ---
if __name__ == "__main__":
    # Инициализируем базу данных
    init_db()
    
    # Для локального запуска используем polling
    async def main():
        logger.info("Запуск бота в режиме polling...")
        await dp.start_polling(bot)
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"Фатальная ошибка: {e}")
        logger.error(traceback.format_exc())
