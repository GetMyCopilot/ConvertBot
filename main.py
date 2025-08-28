import asyncio
import io
import logging
import os
import tempfile
from typing import List, Tuple, Optional
from dataclasses import dataclass
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, ReplyKeyboardMarkup, KeyboardButton
try:
    from aiogram import BaseMiddleware
except ImportError:
    # Для более старых версий aiogram 3.x
    try:
        from aiogram.dispatcher.middlewares.base import BaseMiddleware
    except ImportError:
        # Альтернативный импорт для разных версий
        from aiogram.dispatcher.middlewares import BaseMiddleware
from PIL import Image

# =============================================================================
# КОНФИГУРАЦИЯ И КОНСТАНТЫ
# =============================================================================

@dataclass
class Config:
    """Конфигурация приложения"""
    MAX_FILE_SIZE: int = 20 * 1024 * 1024  # 20MB
    MAX_IMAGE_DIMENSION: int = 4096  # Максимальный размер изображения
    SUPPORTED_FORMATS: List[str] = None
    KEYBOARD_ROW_SIZE: int = 3
    ICO_SIZES: List[int] = None
    
    def __post_init__(self):
        if self.SUPPORTED_FORMATS is None:
            self.SUPPORTED_FORMATS = [
                'PNG', 'JPEG', 'WEBP', 'BMP', 'TIF', 'TIFF',
                'GIF', 'ICO', 'PDF', 'JP2', 'APNG'
            ]
        if self.ICO_SIZES is None:
            self.ICO_SIZES = [256]

config = Config()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Получение токена из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required. Set it with your bot token.")

# =============================================================================
# СОСТОЯНИЯ FSM
# =============================================================================

class ConversionStates(StatesGroup):
    waiting_for_formats = State()

# =============================================================================
# MIDDLEWARE
# =============================================================================

class LoggingMiddleware(BaseMiddleware):
    """Middleware для логирования запросов"""
    
    async def __call__(self, handler, event, data):
        if hasattr(event, 'from_user') and event.from_user:
            user_id = event.from_user.id
            username = event.from_user.username or "unknown"
            logger.info(f"Request from user {user_id} (@{username})")
        
        try:
            return await handler(event, data)
        except Exception as e:
            logger.error(f"Error in handler: {e}", exc_info=True)
            if hasattr(event, 'reply'):
                try:
                    await event.reply("⛔ Произошла ошибка при обработке запроса. Попробуйте позже.")
                except Exception:
                    pass
            raise

class FileSizeMiddleware(BaseMiddleware):
    """Middleware для проверки размера файлов"""
    
    async def __call__(self, handler, event, data):
        if hasattr(event, 'photo') and event.photo:
            photo = event.photo[-1]  # Берем самое большое фото
            if photo.file_size and photo.file_size > config.MAX_FILE_SIZE:
                await event.reply(
                    f"⛔ Файл слишком большой. Максимальный размер: {config.MAX_FILE_SIZE // (1024*1024)} МБ"
                )
                return
        elif hasattr(event, 'document') and event.document:
            if event.document.file_size and event.document.file_size > config.MAX_FILE_SIZE:
                await event.reply(
                    f"⛔ Файл слишком большой. Максимальный размер: {config.MAX_FILE_SIZE // (1024*1024)} МБ"
                )
                return
        
        return await handler(event, data)

# =============================================================================
# УТИЛИТЫ И СЕРВИСЫ
# =============================================================================

class ImageValidator:
    """Валидатор изображений"""
    
    @staticmethod
    def validate_image(img: Image.Image) -> Tuple[bool, Optional[str]]:
        """Валидация изображения"""
        try:
            # Проверка размеров
            width, height = img.size
            if width > config.MAX_IMAGE_DIMENSION or height > config.MAX_IMAGE_DIMENSION:
                return False, f"Изображение слишком большое. Максимальный размер: {config.MAX_IMAGE_DIMENSION}x{config.MAX_IMAGE_DIMENSION}"
            
            if width < 1 or height < 1:
                return False, "Некорректные размеры изображения"
            
            # Проверка формата
            if not img.format:
                return False, "Не удалось определить формат изображения"
            
            return True, None
        except Exception as e:
            return False, f"Ошибка валидации: {str(e)}"

class ImageConverter:
    """Сервис для конвертации изображений"""
    
    FORMAT_MAP = {
        'PNG': ('PNG', 'png'),
        'JPEG': ('JPEG', 'jpg'),
        'WEBP': ('WEBP', 'webp'),
        'BMP': ('BMP', 'bmp'),
        'TIF': ('TIFF', 'tif'),
        'TIFF': ('TIFF', 'tiff'),
        'GIF': ('GIF', 'gif'),
        'ICO': ('ICO', 'ico'),
        'PDF': ('PDF', 'pdf'),
        'JP2': ('JPEG2000', 'jp2'),
        'APNG': ('PNG', 'apng'),
    }
    
    @classmethod
    async def convert_to_format(cls, img: Image.Image, format_name: str) -> Tuple[bool, io.BytesIO, str]:
        """
        Конвертация изображения в указанный формат
        
        Returns:
            Tuple[success, buffer, error_message]
        """
        try:
            if format_name not in cls.FORMAT_MAP:
                return False, io.BytesIO(), f"Неподдерживаемый формат: {format_name}"
            
            pil_format, extension = cls.FORMAT_MAP[format_name]
            output = io.BytesIO()
            
            # Специальная обработка для разных форматов
            if format_name == 'ICO':
                converted_img = await cls._convert_to_ico(img, output)
            elif format_name == 'JPEG':
                converted_img = await cls._convert_to_jpeg(img, output, pil_format)
            elif format_name == 'PDF':
                converted_img = await cls._convert_to_pdf(img, output)
            else:
                img.save(output, format=pil_format, optimize=True)
                converted_img = True
            
            if not converted_img:
                return False, io.BytesIO(), f"Не удалось конвертировать в {format_name}"
            
            output.seek(0)
            return True, output, ""
            
        except Exception as e:
            logger.error(f"Ошибка конвертации в {format_name}: {e}")
            return False, io.BytesIO(), f"Ошибка конвертации: {str(e)}"
    
    @staticmethod
    async def _convert_to_ico(img: Image.Image, output: io.BytesIO) -> bool:
        """Конвертация в ICO формат"""
        try:
            # Получаем оригинальный размер
            original_size = min(img.width, img.height)
            
            # Определяем подходящие размеры для ICO
            # Используем только стандартные размеры, которые хорошо поддерживаются
            if original_size >= 256:
                sizes = [256]
            elif original_size >= 128:
                sizes = [128]
            elif original_size >= 64:
                sizes = [64]
            elif original_size >= 32:
                sizes = [32]
            else:
                # Для очень маленьких изображений используем оригинальный размер
                sizes = [original_size] if original_size >= 16 else [16]
            
            # Создаем изображения разных размеров
            images = []
            
            for size in sizes:
                # Изменяем размер с высоким качеством
                if img.width != size or img.height != size:
                    # Создаем квадратное изображение
                    if img.width != img.height:
                        # Если изображение не квадратное, обрезаем до квадрата
                        min_dimension = min(img.width, img.height)
                        left = (img.width - min_dimension) // 2
                        top = (img.height - min_dimension) // 2
                        right = left + min_dimension
                        bottom = top + min_dimension
                        square_img = img.crop((left, top, right, bottom))
                    else:
                        square_img = img
                    
                    resized = square_img.resize((size, size), Image.Resampling.LANCZOS)
                else:
                    resized = img.copy()
                
                # Правильная обработка цветовых режимов для ICO
                if resized.mode == 'RGBA':
                    # RGBA оставляем как есть
                    pass
                elif resized.mode == 'RGB':
                    # RGB тоже оставляем, ICO поддерживает
                    pass
                elif resized.mode == 'P':
                    # Палитровые изображения конвертируем в RGBA
                    resized = resized.convert('RGBA')
                elif resized.mode in ['L', 'LA']:
                    # Градации серого конвертируем в RGBA
                    resized = resized.convert('RGBA')
                else:
                    # Остальные режимы конвертируем в RGBA
                    resized = resized.convert('RGBA')
                
                images.append(resized)
            
            # Сохраняем ICO файл
            if images:
                # Создаем кортеж размеров
                sizes_tuple = [(img.width, img.height) for img in images]
                
                # Сохраняем первое изображение как основное
                first_image = images[0]
                
                # Используем более простые параметры для совместимости
                save_kwargs = {
                    'format': 'ICO',
                    'sizes': sizes_tuple,
                    'bitmap_format': 'bmp'
                }
                
                # Добавляем дополнительные изображения только если они есть
                if len(images) > 1:
                    save_kwargs['append_images'] = images[1:]
                
                first_image.save(output, **save_kwargs)
                
                output.seek(0)
                logger.info(f"ICO файл успешно создан, размеры: {sizes_tuple}")
                return True
            else:
                return False
                
        except Exception as e:
            logger.error(f"Ошибка конвертации в ICO: {e}")
            
            # Попробуем упрощенный метод как fallback
            try:
                # Простой метод - один размер, без сложных преобразований
                simple_size = min(256, min(img.width, img.height))
                
                # Обрезаем до квадрата если нужно
                if img.width != img.height:
                    min_dimension = min(img.width, img.height)
                    left = (img.width - min_dimension) // 2
                    top = (img.height - min_dimension) // 2
                    right = left + min_dimension
                    bottom = top + min_dimension
                    square_img = img.crop((left, top, right, bottom))
                else:
                    square_img = img
                
                # Изменяем размер
                if square_img.size != (simple_size, simple_size):
                    resized = square_img.resize((simple_size, simple_size), Image.Resampling.LANCZOS)
                else:
                    resized = square_img
                
                # Конвертируем в подходящий режим
                if resized.mode not in ['RGB', 'RGBA']:
                    if 'transparency' in resized.info or resized.mode in ['LA', 'P']:
                        resized = resized.convert('RGBA')
                    else:
                        resized = resized.convert('RGB')
                
                # Сохраняем с минимальными параметрами
                resized.save(output, format='ICO', bitmap_format='bmp')
                output.seek(0)
                return True
                
            except Exception as fallback_error:
                logger.error(f"Fallback ICO конвертация тоже не удалась: {fallback_error}")
                return False
    
    @staticmethod
    async def _convert_to_jpeg(img: Image.Image, output: io.BytesIO, pil_format: str) -> bool:
        """Конвертация в JPEG формат"""
        try:
            # JPEG не поддерживает прозрачность
            if img.mode in ("RGBA", "LA", "P"):
                # Создаем белый фон
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                background.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
                img = background
            
            img.save(output, format=pil_format, optimize=True, quality=90)
            return True
        except Exception as e:
            logger.error(f"Ошибка конвертации в JPEG: {e}")
            return False
    
    @staticmethod
    async def _convert_to_pdf(img: Image.Image, output: io.BytesIO) -> bool:
        """Конвертация в PDF формат"""
        try:
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.save(output, format='PDF', optimize=True)
            return True
        except Exception as e:
            logger.error(f"Ошибка конвертации в PDF: {e}")
            return False

class KeyboardBuilder:
    """Строитель клавиатур"""
    
    @staticmethod
    def create_formats_keyboard(formats: List[str]) -> ReplyKeyboardMarkup:
        """Создание клавиатуры с форматами"""
        all_formats = formats + ['✅ ГОТОВО']
        keyboard = []
        row = []
        
        for fmt in all_formats:
            row.append(KeyboardButton(text=fmt))
            if len(row) == config.KEYBOARD_ROW_SIZE:
                keyboard.append(row)
                row = []
        
        if row:
            keyboard.append(row)
        
        return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, one_time_keyboard=False)

# =============================================================================
# ОБРАБОТЧИКИ
# =============================================================================

async def start_handler(message: types.Message):
    """Обработчик команды /start"""
    welcome_text = (
        "👋 Привет! Я бот для конвертации изображений.\n\n"
        "📝 Что я умею:\n"
        "• Конвертировать изображения в различные форматы\n"
        "• Поддерживаю форматы: PNG, JPEG, WEBP, BMP, TIFF, GIF, ICO, PDF, JPEG2000, APNG\n"
        "• Работаю с файлами до 20 МБ\n\n"
        "🖼 Просто отправь мне изображение, и я помогу его конвертировать!"
    )
    await message.reply(welcome_text)

async def help_handler(message: types.Message):
    """Обработчик команды /help"""
    help_text = (
        "🔧 <b>Как пользоваться ботом:</b>\n\n"
        "1️⃣ Отправь изображение (фото или документ)\n"
        "2️⃣ Выбери нужные форматы из списка\n"
        "3️⃣ Нажми '✅ ГОТОВО' для конвертации\n\n"
        "📋 <b>Поддерживаемые форматы:</b>\n"
        "• PNG - с прозрачностью\n"
        "• JPEG - сжатие с потерями\n"
        "• WEBP - современный формат\n"
        "• BMP - без сжатия\n"
        "• TIFF - высокое качество\n"
        "• GIF - анимация\n"
        "• ICO - иконки (несколько размеров)\n"
        "• PDF - документ\n"
        "• JPEG2000 - улучшенное сжатие\n"
        "• APNG - анимированный PNG\n\n"
        "⚠️ <b>Ограничения:</b>\n"
        f"• Максимальный размер файла: {config.MAX_FILE_SIZE // (1024*1024)} МБ\n"
        f"• Максимальное разрешение: {config.MAX_IMAGE_DIMENSION}x{config.MAX_IMAGE_DIMENSION}"
    )
    await message.reply(help_text, parse_mode='HTML')

@asynccontextmanager
async def safe_file_download(bot: Bot, file_path: str):
    """Безопасное скачивание файла во временную директорию"""
    with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
        try:
            await bot.download_file(file_path, tmp_file.name)
            yield tmp_file.name
        finally:
            try:
                os.unlink(tmp_file.name)
            except OSError:
                pass

async def photo_handler(message: types.Message, state: FSMContext, bot: Bot):
    """Обработчик изображений"""
    try:
        # Отправляем сообщение о начале обработки
        processing_msg = await message.reply("🔄 Обрабатываю изображение...")
        
        # Получаем файл (фото или документ)
        file_obj = None
        if message.photo:
            file_obj = message.photo[-1]  # Берем самое большое фото
        elif message.document and message.document.mime_type and message.document.mime_type.startswith('image/'):
            file_obj = message.document
        else:
            await processing_msg.edit_text("⛔ Пожалуйста, отправь изображение (фото или файл).")
            return
        
        # Получаем информацию о файле
        file_info = await bot.get_file(file_obj.file_id)
        
        # Безопасно скачиваем и обрабатываем файл
        async with safe_file_download(bot, file_info.file_path) as temp_file_path:
            try:
                # Открываем изображение
                img = Image.open(temp_file_path)
                original_format = img.format or 'UNKNOWN'
                
                # Валидация изображения
                is_valid, error_msg = ImageValidator.validate_image(img)
                if not is_valid:
                    await processing_msg.edit_text(f"⛔ {error_msg}")
                    return
                
                # Читаем файл в байты для сохранения в состоянии
                with open(temp_file_path, 'rb') as f:
                    file_bytes = f.read()
                
                # Сохраняем данные в состоянии
                await state.update_data(
                    photo_bytes=file_bytes,
                    original_format=original_format,
                    selected_formats=[],
                    image_info={
                        'width': img.width,
                        'height': img.height,
                        'mode': img.mode,
                        'size_kb': len(file_bytes) // 1024
                    }
                )
                
            except Exception as e:
                logger.error(f"Ошибка при обработке изображения: {e}")
                await processing_msg.edit_text("⛔ Не удалось обработать изображение. Убедитесь, что это корректный файл изображения.")
                return
        
        # Создаем клавиатуру с форматами
        keyboard = KeyboardBuilder.create_formats_keyboard(config.SUPPORTED_FORMATS)
        
        # Устанавливаем состояние ожидания выбора форматов
        await state.set_state(ConversionStates.waiting_for_formats)
        
        # Информация об изображении
        image_info = (await state.get_data())['image_info']
        info_text = (
            f"📊 <b>Информация об изображении:</b>\n"
            f"• Формат: {original_format}\n"
            f"• Размер: {image_info['width']}x{image_info['height']}\n"
            f"• Цветовой режим: {image_info['mode']}\n"
            f"• Размер файла: {image_info['size_kb']} КБ\n\n"
            f"🎯 Выбери один или несколько форматов для конвертации, затем нажми '✅ ГОТОВО':"
        )
        
        await processing_msg.edit_text(info_text, parse_mode='HTML')
        await message.reply("Выбери формат из меню ниже:", reply_markup=keyboard)
        
    except Exception as e:
        logger.error(f"Ошибка в photo_handler: {e}")
        await message.reply("⛔ Произошла ошибка при обработке изображения.")

async def formats_handler(message: types.Message, state: FSMContext, bot: Bot):
    """Обработчик выбора форматов"""
    try:
        text = message.text.strip().upper()
        data = await state.get_data()
        
        if text == "✅ ГОТОВО":
            await handle_conversion_completion(message, state, data)
            return
        
        # Проверяем, поддерживается ли формат
        if text not in config.SUPPORTED_FORMATS:
            supported_list = ', '.join(config.SUPPORTED_FORMATS)
            await message.reply(f"⛔ Неподдерживаемый формат.\n\n✅ Поддерживаемые форматы:\n{supported_list}")
            return
        
        # Управляем списком выбранных форматов
        selected = data.get("selected_formats", [])
        if text not in selected:
            selected.append(text)
            await state.update_data(selected_formats=selected)
            await message.reply(
                f"✅ Формат {text} добавлен.\n\n"
                f"📋 Выбрано форматов: {len(selected)}\n"
                f"🎯 Форматы: {', '.join(selected)}\n\n"
                f"Можешь добавить еще или нажать '✅ ГОТОВО' для конвертации."
            )
        else:
            await message.reply(
                f"⚠️ Формат {text} уже выбран.\n\n"
                f"📋 Текущий список: {', '.join(selected)}"
            )
    
    except Exception as e:
        logger.error(f"Ошибка в formats_handler: {e}")
        await message.reply("⛔ Произошла ошибка при обработке выбора формата.")

async def handle_conversion_completion(message: types.Message, state: FSMContext, data: dict):
    """Обработка завершения выбора форматов и конвертации"""
    selected_formats = data.get("selected_formats", [])
    
    if not selected_formats:
        await message.reply("⛔ Ты не выбрал ни одного формата для конвертации.")
        return
    
    photo_bytes = data.get("photo_bytes")
    if not photo_bytes:
        await message.reply("⛔ Данные изображения потеряны. Отправь изображение заново.")
        await state.clear()
        return
    
    # Сообщение о начале конвертации
    conversion_msg = await message.reply(
        f"🔄 Начинаю конвертацию в {len(selected_formats)} формат(ов)...",
        reply_markup=types.ReplyKeyboardRemove()
    )
    
    try:
        img = Image.open(io.BytesIO(photo_bytes))
        successful_conversions = 0
        failed_conversions = []
        
        for i, fmt in enumerate(selected_formats, 1):
            # Обновляем прогресс (безопасно)
            try:
                await conversion_msg.edit_text(
                    f"🔄 Конвертирую в {fmt}... ({i}/{len(selected_formats)})"
                )
            except Exception:
                # Если не можем редактировать, отправляем новое сообщение
                conversion_msg = await message.reply(
                    f"🔄 Конвертирую в {fmt}... ({i}/{len(selected_formats)})"
                )
            
            # Конвертируем изображение
            success, output_buffer, error_msg = await ImageConverter.convert_to_format(img, fmt)
            
            if success:
                try:
                    extension = ImageConverter.FORMAT_MAP[fmt][1]
                    filename = f"converted.{extension}"
                    
                    await message.reply_document(
                        BufferedInputFile(output_buffer.getvalue(), filename=filename),
                        caption=f"✅ Конвертация в {fmt} завершена"
                    )
                    successful_conversions += 1
                    
                except Exception as e:
                    logger.error(f"Ошибка при отправке файла {fmt}: {e}")
                    failed_conversions.append(f"{fmt}: ошибка отправки")
            else:
                failed_conversions.append(f"{fmt}: {error_msg}")
                logger.error(f"Ошибка конвертации в {fmt}: {error_msg}")
        
        # Финальное сообщение
        result_text = f"🎉 Конвертация завершена!\n\n"
        result_text += f"✅ Успешно: {successful_conversions}\n"
        
        if failed_conversions:
            result_text += f"⛔ Неудачно: {len(failed_conversions)}\n"
            result_text += f"\n🔍 Ошибки:\n" + "\n".join(f"• {error}" for error in failed_conversions)
        
        result_text += "\n\n📤 Отправь новое изображение для следующей конвертации."
        
        try:
            await conversion_msg.edit_text(result_text)
        except Exception:
            # Если не можем редактировать, отправляем новое сообщение
            await message.reply(result_text)
        
    except Exception as e:
        logger.error(f"Критическая ошибка при конвертации: {e}")
        try:
            await conversion_msg.edit_text(
                "⛔ Произошла критическая ошибка при конвертации. "
                "Проверьте изображение и попробуйте снова."
            )
        except Exception:
            await message.reply(
                "⛔ Произошла критическая ошибка при конвертации. "
                "Проверьте изображение и попробуйте снова."
            )
    finally:
        await state.clear()

async def cancel_handler(message: types.Message, state: FSMContext):
    """Обработчик отмены операции"""
    current_state = await state.get_state()
    if current_state is not None:
        await state.clear()
        await message.reply(
            "⛔ Операция отменена. Отправь новое изображение для конвертации.",
            reply_markup=types.ReplyKeyboardRemove()
        )
    else:
        await message.reply("🤔 Нет активных операций для отмены.")

# =============================================================================
# ОСНОВНАЯ ФУНКЦИЯ
# =============================================================================

async def main():
    """Основная функция запуска бота"""
    try:
        # Создаем бота и диспетчер
        bot = Bot(token=BOT_TOKEN)
        dp = Dispatcher()
        
        # Добавляем middleware
        dp.message.middleware(LoggingMiddleware())
        dp.message.middleware(FileSizeMiddleware())
        
        # Регистрируем обработчики команд
        dp.message.register(start_handler, Command(commands=["start"]))
        dp.message.register(help_handler, Command(commands=["help"]))
        dp.message.register(cancel_handler, Command(commands=["cancel"]))
        
        # Регистрируем обработчики изображений
        dp.message.register(
            photo_handler, 
            lambda m: (m.photo is not None) or 
                     (m.document and m.document.mime_type and m.document.mime_type.startswith('image/'))
        )
        
        # Регистрируем обработчик выбора форматов
        dp.message.register(formats_handler, ConversionStates.waiting_for_formats)
        
        logger.info("🚀 Запускаю бота...")
        await dp.start_polling(bot)
        
    except Exception as e:
        logger.error(f"Критическая ошибка при запуске бота: {e}")
        raise

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"Неожиданная ошибка: {e}")
        exit(1)