import sqlite3
import aiohttp
import re
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, BufferedInputFile
from aiogram.filters import CommandStart
import asyncio
import logging
import sys
import traceback
import ssl
import json
import os
API_TOKEN = "7715952986:AAEgHLn4HJMXNuQvtf8NusrsYAz28IJilZ8"

# --- Настройка логгера ---
# Создаем директорию для данных, если она не существует
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

# Имя файла базы данных в директории data
DB_NAME = os.path.join(data_dir, "users.db")

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# --- Настройка SSL контекста для обхода проблем с сертификатами ---
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
        # Рекурсивно ищем в объекте
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

# Обработчик для всех поддерживаемых ссылок Pinterest
# Обновленное регулярное выражение для поиска ссылок в любом месте текста
@dp.message(F.text)
async def handle_pinterest(message: Message):
    # Ищем ссылки Pinterest в тексте сообщения
    # Исправленное регулярное выражение для корректного извлечения ссылок
    pinterest_urls = re.findall(r"https?://(?:[a-z]+\.)?pinterest\.com/pin/[\w\-]+|https?://pin\.it/[\w\-]+", message.text)
    
    if not pinterest_urls:
        # Если ссылки не найдены, не обрабатываем сообщение
        return
    
    # Берем первую найденную ссылку
    url = pinterest_urls[0]
    
    # Проверяем, что URL не пустой
    if not url:
        return
    
    # Сохраняем сообщение "Скачиваю" чтобы удалить его позже
    downloading_message = await message.answer("⏳ Скачиваю...")
    logger.info(f"Получена ссылка от пользователя {message.from_user.username} ({message.from_user.id}): {url}")

    try:
        # Создаем заголовки для имитации браузера
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        }
        
        # Создаем клиентскую сессию с отключенной проверкой SSL и заголовками
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=ssl_context),
            headers=headers
        ) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    content = await resp.text()
                    logger.info(f"Получен HTML контент размером {len(content)} символов")

                    # Извлекаем все JSON объекты из контента
                    json_objects = extract_json_objects(content)
                    logger.info(f"Найдено {len(json_objects)} JSON объектов в контенте")
                    
                    # Ищем медиа ссылки в JSON объектах
                    json_video_urls, json_image_urls = find_media_in_json_objects(json_objects)
                    logger.info(f"Найдено {len(json_video_urls)} видео и {len(json_image_urls)} изображений в JSON объектах")
                    
                    # Также ищем медиа ссылки с помощью регулярных выражений
                    # Проверяем наличие видео
                    video_urls = []
                    
                    # Добавляем видео ссылки из JSON
                    video_urls.extend([url.replace('\\u002F', '/').replace('\\', '') for url in json_video_urls])
                    
                    # Метод 1: Ищем прямые ссылки на видео
                    regex_video_matches = re.findall(r'"videoUrl":"([^"]*)"', content)
                    video_urls.extend([url.replace('\\u002F', '/').replace('\\', '') for url in regex_video_matches])
                    logger.info(f"Найдено {len(regex_video_matches)} видео ссылок методом регулярных выражений")
                    
                    # Метод 2: Ищем видео в тегах video
                    video_tag_matches = re.findall(r'<video[^>]*src="([^"]*)"', content)
                    video_urls.extend(video_tag_matches)
                    logger.info(f"Найдено {len(video_tag_matches)} видео ссылок в тегах video")
                    
                    # Метод 3: Ищем видео в атрибутах data-video-url
                    data_video_matches = re.findall(r'data-video-url="([^"]*)"', content)
                    video_urls.extend(data_video_matches)
                    logger.info(f"Найдено {len(data_video_matches)} видео ссылок в атрибутах data-video-url")
                    
                    if video_urls:
                        logger.info(f"Всего найдено {len(video_urls)} потенциальных видео ссылок")
                        
                        # Фильтруем ссылки, исключаем m3u8
                        filtered_video_urls = [url for url in video_urls if not url.endswith('.m3u8')]
                        logger.info(f"После фильтрации осталось {len(filtered_video_urls)} видео ссылок")
                        
                        if filtered_video_urls:
                            # Берем первую найденную ссылку на видео
                            video_url_str = filtered_video_urls[0]
                            logger.info(f"Используем первую ссылку: {video_url_str}")
                            
                            logger.info("Скачиваем видео...")
                            
                            # Для видео используем потоковую загрузку
                            async with session.get(video_url_str, ssl=ssl_context, headers=headers) as vresp:
                                logger.info(f"Статус ответа видео: {vresp.status}")
                                if vresp.status == 200:
                                    # Получаем размер контента если доступен
                                    content_length = vresp.headers.get('Content-Length')
                                    if content_length:
                                        logger.info(f"Размер видео: {content_length} байт")
                                    
                                    # Получаем тип контента
                                    content_type = vresp.headers.get('Content-Type')
                                    logger.info(f"Тип контента: {content_type}")
                                    
                                    # Читаем данные по частям для больших файлов
                                    video_data = bytearray()
                                    chunk_count = 0
                                    async for chunk in vresp.content.iter_chunked(8192):
                                        video_data.extend(chunk)
                                        chunk_count += 1
                                        if chunk_count % 100 == 0:  # Логируем каждые 100 чанков
                                            logger.info(f"Загружено {chunk_count} чанков, {len(video_data)} байт")
                                    
                                    # Проверяем, что данные получены
                                    if len(video_data) > 0:
                                        logger.info(f"Видео скачано, размер: {len(video_data)} байт")
                                        
                                        # Определяем расширение файла по типу контента
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
                                        # Удаляем сообщение "Скачиваю"
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

                    # Проверяем наличие картинки
                    img_urls = []
                    
                    # Добавляем изображения из JSON
                    img_urls.extend([url.replace('\\u002F', '/').replace('\\', '') for url in json_image_urls])
                    
                    # Метод 1: Ищем прямые ссылки на изображения
                    regex_img_matches = re.findall(r'"url":"([^"]*\.jpg[^"]*)"', content)
                    img_urls.extend([url.replace('\\u002F', '/').replace('\\', '') for url in regex_img_matches])
                    logger.info(f"Найдено {len(regex_img_matches)} изображений методом регулярных выражений")
                    
                    # Метод 2: Ищем изображения в imageUrls
                    image_urls_matches = re.findall(r'"imageUrls":\["([^"]*)"\]', content)
                    img_urls.extend([url.replace('\\u002F', '/').replace('\\', '') for url in image_urls_matches])
                    logger.info(f"Найдено {len(image_urls_matches)} изображений в imageUrls")
                    
                    # Метод 3: Ищем изображения в тегах img
                    img_tag_matches = re.findall(r'<img[^>]*src="([^"]*)"', content)
                    img_urls.extend(img_tag_matches)
                    logger.info(f"Найдено {len(img_tag_matches)} изображений в тегах img")
                    
                    if img_urls:
                        logger.info(f"Всего найдено {len(img_urls)} потенциальных изображений")
                        img_url_str = img_urls[0]
                        logger.info(f"Используем первую ссылку: {img_url_str}")
                        logger.info("Скачиваем изображение...")
                        
                        async with session.get(img_url_str, ssl=ssl_context, headers=headers) as iresp:
                            logger.info(f"Статус ответа изображения: {iresp.status}")
                            if iresp.status == 200:
                                # Получаем тип контента
                                content_type = iresp.headers.get('Content-Type')
                                logger.info(f"Тип контента изображения: {content_type}")
                                
                                img_data = await iresp.read()
                                if len(img_data) > 0:
                                    logger.info(f"Изображение скачано, размер: {len(img_data)} байт")
                                    
                                    # Определяем расширение файла по типу контента
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
                                    # Удаляем сообщение "Скачиваю"
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
                    # Удаляем сообщение "Скачиваю" даже если медиа не найдено
                    await bot.delete_message(message.chat.id, downloading_message.message_id)
                    logger.warning("Не удалось найти медиа в ссылке, сообщение удалено")
                else:
                    await message.answer("❌ Ошибка при получении данных с Pinterest.")
                    # Удаляем сообщение "Скачиваю" при ошибке
                    await bot.delete_message(message.chat.id, downloading_message.message_id)
                    logger.error(f"Ошибка при получении данных с Pinterest: статус {resp.status}")
    except aiohttp.ClientError as e:
        error_msg = "⚠️ Ошибка сети при подключении к Pinterest. Попробуйте позже."
        await message.answer(error_msg)
        # Удаляем сообщение "Скачиваю" при ошибке
        await bot.delete_message(message.chat.id, downloading_message.message_id)
        logger.error(f"Ошибка сети при подключении к Pinterest: {e}")
    except Exception as e:
        error_msg = f"⚠️ Ошибка: {e}"
        await message.answer(error_msg)
        # Удаляем сообщение "Скачиваю" при ошибке
        await bot.delete_message(message.chat.id, downloading_message.message_id)
        logger.error(f"Ошибка при обработке ссылки Pinterest: {e}")
        logger.error(traceback.format_exc())

# --- Основная функция с постоянным сканированием ---
async def main():
    try:
        init_db()
        logger.info("Бот запущен и готов к работе")
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"Критическая ошибка в работе бота: {e}")
        logger.error(traceback.format_exc())
        # Попытка перезапуска через 5 секунд
        logger.info("Попытка перезапуска через 5 секунд...")
        await asyncio.sleep(5)
        await main()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logger.error(f"Фатальная ошибка при запуске бота: {e}")
        logger.error(traceback.format_exc())
