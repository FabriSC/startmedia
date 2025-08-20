import os
import re
import asyncio
import logging
import psutil
import uuid
import time
import math
import configparser

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from google.auth.transport.requests import Request

# --- Cargar Configuración ---
config = configparser.ConfigParser()
config.read('config.ini')

try:
    TOKEN = config['BOT']['TelegramToken']
    DRIVE_FOLDER_ID = config['GOOGLE']['DriveFolderID']
except KeyError as e:
    print(f"❌ Error: La clave {e} no se encuentra en config.ini. Asegúrate de que el archivo esté completo.")
    exit()

SCOPES = ['https://www.googleapis.com/auth/drive.file']
TOKEN_FILE = 'token.json'
CREDENTIALS_FILE = 'credentials.json'

# --- Configuración de Logging ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Almacenamiento en memoria (igual que antes) ---
active_processes = {}
task_data = {}
progress_messages = {}

# --- Funciones Auxiliares (sin cambios) ---
def create_progress_bar(percentage: int) -> str:
    return "".join(["█" if i < percentage // 5 else "░" for i in range(20)])

def human_readable_size(size_bytes: int) -> str:
    if size_bytes == 0: return "0B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"

def transform_mediaset_url(url: str) -> str:
    pattern = re.compile(r'(/mpd-cenc\.ism)/(web|ctv)?(\.mpd)')
    if pattern.search(url):
        logger.info(f"URL de Mediaset detectada, transformando a HLS: {url}")
        return pattern.sub(r'/main.ism/picky.m3u8', url)
    return url

# --- Autenticación y Subida a Drive (MEJORADO) ---
def authenticate_drive():
    """
    Gestiona la autenticación con la API de Google Drive.
    Crea o refresca 'token.json' según sea necesario.
    """
    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        except Exception as e:
            logger.warning(f"El archivo {TOKEN_FILE} está corrupto o es inválido: {e}. Se solicitará nueva autenticación.")
            os.remove(TOKEN_FILE) # Eliminar token corrupto

    # Si no hay credenciales o no son válidas
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                logger.info("Refrescando token de acceso...")
                creds.refresh(Request())
            except Exception as e:
                logger.error(f"Fallo al refrescar el token: {e}. Se requiere nueva autenticación.")
                creds = None # Forzar re-autenticación
        
        # Si aún no hay credenciales, iniciar flujo de autenticación
        if not creds:
            try:
                print("\n--- ACCIÓN REQUERIDA ---")
                print(f"Se necesita autenticación. Asegúrate de que '{CREDENTIALS_FILE}' está en esta carpeta.")
                print("Se abrirá una ventana en tu navegador para que autorices el acceso a Google Drive.")
                print("Una vez autorizado, se creará el archivo 'token.json' y el bot continuará.")
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
                creds = flow.run_local_server(port=0)
            except FileNotFoundError:
                logger.critical(f"Error fatal: El archivo '{CREDENTIALS_FILE}' no se encontró. Descárgalo de Google Cloud Console.")
                return None
            except Exception as e:
                logger.critical(f"No se pudo completar el flujo de autenticación: {e}")
                return None

        # Guardar las credenciales para la próxima ejecución
        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())
        logger.info(f"Credenciales guardadas exitosamente en '{TOKEN_FILE}'.")

    try:
        service = build('drive', 'v3', credentials=creds)
        logger.info("Autenticación con Google Drive exitosa.")
        return service
    except HttpError as error:
        logger.error(f"Ocurrió un error al construir el servicio de Drive: {error}")
        return None

# El resto del código (upload_with_progress, download_and_upload_task, handlers, etc.)
# permanece prácticamente igual que en tu script original. He añadido algunos logs
# para mejor depuración en caso de errores al editar mensajes.
# A continuación, el resto del código sin cambios funcionales importantes...

async def upload_with_progress(msg, file_path: str, drive_file_name: str, task_id: str):
    try:
        service = await asyncio.to_thread(authenticate_drive)
        if not service:
            await msg.edit_text("❌ No se pudo autenticar con Google Drive. Revisa la consola del bot.")
            return None

        file_metadata = {'name': drive_file_name, 'parents': [DRIVE_FOLDER_ID]}
        media = MediaFileUpload(file_path, mimetype='video/mp4', resumable=True)
        
        logger.info(f"Iniciando subida de '{drive_file_name}' (Task ID: {task_id}) a Drive.")
        request = service.files().create(
            body=file_metadata, media_body=media, fields='id, webViewLink', supportsAllDrives=True
        )

        response = None
        last_uploaded_bytes = 0
        last_update_time = time.time()
        cancel_button = [[InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel_{task_id}")]]

        while response is None:
            if task_id not in active_processes and msg.message_id not in [m.message_id for m in progress_messages.values()]:
                logger.info(f"Subida para la tarea {task_id} cancelada externamente.")
                return None
            
            status, response = await asyncio.to_thread(request.next_chunk)
            
            if status:
                current_time = time.time()
                elapsed_time = current_time - last_update_time
                bytes_since_last = status.resumable_progress - last_uploaded_bytes
                
                speed = bytes_since_last / elapsed_time if elapsed_time > 0 else 0
                percentage = int(status.resumable_progress / media.size() * 100)
                
                progress_text = (
                    f"📤 Subiendo: *{drive_file_name}*\n\n"
                    f"{create_progress_bar(percentage)} {percentage}%\n\n" 
                    f"Subido: {human_readable_size(status.resumable_progress)} / {human_readable_size(media.size())}\n"
                    f"Velocidad: {human_readable_size(speed)}/s" 
                )
                
                try:
                    await msg.edit_text(progress_text, reply_markup=InlineKeyboardMarkup(cancel_button), parse_mode='Markdown')
                except Exception as e:
                    logger.warning(f"No se pudo actualizar progreso de subida para {task_id} (puede que el mensaje fuera borrado): {e}")

                last_uploaded_bytes = status.resumable_progress
                last_update_time = current_time
        
        return response

    except Exception as e:
        logger.error(f"Error durante la subida a Drive para tarea {task_id}: {e}", exc_info=True)
        if task_id in progress_messages:
            try:
                await msg.edit_text(f"❌ Error al subir a Google Drive: *{drive_file_name}*.\n`{e}`", parse_mode='Markdown')
            except Exception: pass
        return None

async def download_and_upload_task(chat_id: int, url: str, quality_param: list, file_name: str, task_id: str, initial_msg: 'Message'):
    output_filename = f"{file_name}.mp4"
    cancel_button = [[InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel_{task_id}")]]

    try:
        await initial_msg.edit_text(
            f"⏳ Iniciando descarga de: *{file_name}*", reply_markup=InlineKeyboardMarkup(cancel_button), parse_mode='Markdown'
        )
        
        url_to_download = transform_mediaset_url(url) 
        
        cmd_list = [
            'yt-dlp', *quality_param, '--remux-video', 'mp4',
            '--add-header', 'Origin: https://www.mediasetinfinity.es', 
            '--add-header', 'Referer: https://www.mediasetinfinity.es',
            '-o', output_filename, url_to_download
        ]
        
        logger.info(f"Iniciando yt-dlp para Task ID {task_id}: {' '.join(cmd_list)}")
        process = await asyncio.create_subprocess_exec(
            *cmd_list, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        active_processes[task_id] = (process, output_filename)

        buffer = ''
        last_update_pct = -1
        while True:
            if task_id not in active_processes:
                logger.info(f"Tarea {task_id} cancelada durante la descarga.")
                break 

            chunk = await process.stdout.read(1024)
            if not chunk: break
            buffer += chunk.decode('utf-8', errors='ignore')
            
            if '\r' in buffer:
                last_line = buffer.split('\r')[-1]
                match = re.search(
                    r'\[download\]\s+([0-9.]+)\%\s+of\s+~?\s*([\d.]+\w+)\s+at\s+([\d.]+\w+/s)\s+ETA\s+([\d:]+)', last_line
                )
                if match:
                    pct = int(float(match.group(1)))
                    if pct > last_update_pct:
                        last_update_pct = pct
                        progress_text = (
                            f"📥 Descargando: *{file_name}*\n\n"
                            f"{create_progress_bar(pct)} {pct}%\n\n"
                            f"Tamaño: {match.group(2)} | Velocidad: {match.group(3)}\n"
                            f"ETA: {match.group(4)}"
                        )
                        try:
                            await initial_msg.edit_text(
                                progress_text, reply_markup=InlineKeyboardMarkup(cancel_button), parse_mode='Markdown'
                            )
                        except Exception as e:
                            logger.warning(f"No se pudo actualizar progreso de descarga para {task_id}: {e}")
                buffer = buffer.split('\r')[-1]
        
        await process.wait() 

        if task_id not in active_processes:
            logger.info(f"Tarea {task_id} cancelada tras la descarga.")
            return

        active_processes.pop(task_id, None)

        if process.returncode == 0 and os.path.exists(output_filename):
            await initial_msg.edit_text(f"✅ Descarga completa: *{file_name}*\n\n📤 Preparando subida...", parse_mode='Markdown')
            uploaded_file = await upload_with_progress(initial_msg, output_filename, output_filename, task_id)

            if uploaded_file:
                file_id = uploaded_file.get('id')
                view_link = uploaded_file.get('webViewLink')
                buttons = [
                    [InlineKeyboardButton("🔗 Google Drive", url=view_link)],
                    [InlineKeyboardButton("🔗 Mirror Link", url=f"https://worker-withered-breeze-c480.jostynv.workers.dev/0:findpath?id={file_id}")]
                ]
                await initial_msg.edit_text(
                    f"✅ ¡Completado!\n\n🎬 **Título:** `{file_name}`",
                    reply_markup=InlineKeyboardMarkup(buttons), parse_mode='Markdown', disable_web_page_preview=True
                )
        else:
            stderr_text = (await process.stderr.read()).decode('utf-8', 'ignore').strip()
            if task_id in progress_messages:
                await initial_msg.edit_text(f"❌ Error en la descarga de *{file_name}*.\n`{stderr_text[:1000]}`", parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Error general en la tarea (Task ID {task_id}): {e}", exc_info=True)
        if task_id in progress_messages:
            await initial_msg.edit_text(f"❌ Error inesperado para *{file_name}*.\n`{e}`", parse_mode='Markdown')
    finally:
        progress_messages.pop(task_id, None)
        if os.path.exists(output_filename):
            os.remove(output_filename)
            logger.info(f"Archivo temporal '{output_filename}' eliminado para Task ID {task_id}.")

async def startmedia_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # (Esta función y las siguientes no necesitan cambios)
    if not context.args:
        await update.message.reply_text("⚠️ Uso: `/startmedia <URL> [nombre_opcional]`", parse_mode='Markdown')
        return
    url = context.args[0]
    if not re.match(r'https?://', url):
        await update.message.reply_text(f"❌ URL inválida: '{url}'")
        return
    if len(context.args) > 1:
        custom_name = " ".join(context.args[1:]).strip()
        safe_filename = re.sub(r'[\\/*?:"<>|]', "", custom_name)
    else:
        status_msg = await update.message.reply_text("🔎 Obteniendo título del video...")
        try:
            url_for_title = transform_mediaset_url(url)
            get_title_cmd = [
                'yt-dlp', '--get-title', '--no-warnings',
                '--add-header', 'Origin: https://www.mediasetinfinity.es',
                '--add-header', 'Referer: https://www.mediasetinfinity.es',
                url_for_title
            ]
            proc_title = await asyncio.create_subprocess_exec(
                *get_title_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout_title, stderr_title = await proc_title.communicate()
            if proc_title.returncode != 0:
                error = stderr_title.decode('utf-8', errors='ignore')
                await status_msg.edit_text(f"❌ No se pudo obtener el título.\n`{error[:1000]}`", parse_mode='Markdown')
                return
            filename_base = stdout_title.decode('utf-8', errors='ignore').strip()
            safe_filename = re.sub(r'[\\/*?:"<>|]', "", filename_base)
            await status_msg.delete()
        except Exception as e:
            await status_msg.edit_text(f"❌ Error al obtener título:\n`{e}`", parse_mode='Markdown')
            return

    task_id = str(uuid.uuid4())
    buttons = [
        [InlineKeyboardButton("🏆 Máxima Calidad", callback_data=f"quality_best_{task_id}")],
        [InlineKeyboardButton("1080p", callback_data=f"quality_1080_{task_id}")],
        [InlineKeyboardButton("720p", callback_data=f"quality_720_{task_id}")],
        [InlineKeyboardButton("480p", callback_data=f"quality_480_{task_id}")],
    ]
    sent_msg = await update.message.reply_text(
        f"🎬 *{safe_filename}*\n\n📐 Elige la calidad para la descarga:",
        reply_markup=InlineKeyboardMarkup(buttons), parse_mode='Markdown'
    )
    task_data[sent_msg.message_id] = {
        'url': url, 'filename': safe_filename, 'task_id': task_id, 'initial_msg': sent_msg
    }

async def quality_selection_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    message_id = query.message.message_id
    task_info = task_data.pop(message_id, None)
    if not task_info:
        await query.edit_message_text("⌛️ Este botón ha expirado. Inicia una nueva descarga.")
        return
    url, file_name, task_id, initial_msg = task_info.values()
    quality = query.data.split('_')[1]
    quality_param = ['-f', 'bestvideo+bestaudio/best'] if quality == "best" else ['-f', f'bestvideo[height<={quality}]+bestaudio/best[height<={quality}]']
    progress_messages[task_id] = initial_msg
    asyncio.create_task(
        download_and_upload_task(query.message.chat.id, url, quality_param, file_name, task_id, initial_msg)
    )

async def cancel_any_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Cancelando...")
    task_id = query.data.split('_', 1)[1]
    process_info = active_processes.pop(task_id, None)
    msg_to_edit = progress_messages.pop(task_id, None)
    if process_info:
        process, filename = process_info
        try:
            parent = psutil.Process(process.pid)
            for child in parent.children(recursive=True): child.terminate()
            parent.terminate()
            await process.wait()
            logger.info(f"Proceso para tarea {task_id} cancelado.")
        except psutil.NoSuchProcess:
            logger.warning(f"Proceso para tarea {task_id} ya no existía.")
        if os.path.exists(filename):
            os.remove(filename)
        if msg_to_edit:
            await msg_to_edit.edit_text(f"❌ Descarga cancelada: *{os.path.basename(filename).replace('.mp4', '')}*", parse_mode='Markdown')
    elif msg_to_edit:
        await msg_to_edit.edit_text("❓ Descarga ya finalizada o cancelada.")
    else:
        await query.edit_message_text("❓ No se encontró una descarga activa.")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Excepción al manejar una actualización:", exc_info=context.error)

def main():
    if not os.path.exists('config.ini'):
        print("❌ Error: No se encuentra 'config.ini'. Por favor, crea el archivo con la configuración necesaria.")
        return
    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(CommandHandler('startmedia', startmedia_command))
    application.add_handler(CallbackQueryHandler(quality_selection_handler, pattern=r'^quality_(best|\d+)'))
    application.add_handler(CallbackQueryHandler(cancel_any_download, pattern=r'^cancel_'))
    application.add_error_handler(error_handler)
    print("🚀 El bot está en línea y esperando comandos...")
    # Añade 'drop_pending_updates=True' aquí
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
    main()