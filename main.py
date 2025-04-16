import base64
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
import fitz  # PyMuPDF
import logging
from fastapi.responses import JSONResponse
from io import BytesIO
from PIL import Image
import aiohttp
import asyncio
from fastapi import FastAPI, Request, UploadFile, Query, HTTPException, WebSocket, WebSocketDisconnect
import os
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, Response
import uuid
import platform
import json

# 配置日志记录
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 定义自定义日志适配器，用于向日志记录添加用户ID
class UserLoggerAdapter(logging.LoggerAdapter):
    """
    自定义日志适配器，用于向日志记录添加用户ID
    """
    def process(self, msg, kwargs):
        # 将用户ID添加到日志记录的额外属性中
        if 'extra' not in kwargs:
            kwargs['extra'] = {}
        
        # 从适配器的额外信息中获取用户ID
        if 'user_id' in self.extra:
            kwargs['extra']['user_id'] = self.extra['user_id']
            
        return msg, kwargs

# WebSocket连接管理
class ConnectionManager:
    def __init__(self):
        self.active_connections: dict = {}  # 改为字典，存储用户ID和WebSocket连接
        self.user_tasks: dict = {}  # 存储用户ID和关联的任务
        # 设置自动清理定时器
        self.cleanup_threshold = 1000  # 当任务数达到此阈值时触发清理
        self.last_cleanup = 0  # 上次清理的时间戳
        # 添加定期清理的间隔时间（秒）
        self.cleanup_interval = 300  # 每5分钟执行一次清理
        # 启动定期清理任务
        self.start_periodic_cleanup()

    def start_periodic_cleanup(self):
        """启动定期清理任务"""
        asyncio.create_task(self.periodic_cleanup())
        
    async def periodic_cleanup(self):
        """定期清理断开连接的用户资源"""
        while True:
            try:
                await asyncio.sleep(self.cleanup_interval)
                logger.info("执行定期清理检查...")
                await self.force_cleanup_inactive_users()
            except Exception as e:
                logger.error(f"定期清理任务异常: {str(e)}")

    async def force_cleanup_inactive_users(self):
        """强制清理不活跃的用户资源"""
        # 获取所有任务用户
        task_users = list(self.user_tasks.keys())
        cleanup_count = 0
        
        for user_id in task_users:
            # 如果用户不在活动连接中，清理其资源
            if user_id not in self.active_connections:
                self.cancel_user_tasks(user_id)
                cleanup_count += 1
        
        if cleanup_count > 0:
            logger.info(f"定期清理完成: 清理了 {cleanup_count} 个断开连接用户的资源")

    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        self.active_connections[user_id] = websocket
        logger.info(f"用户 {user_id} 已连接")

    def disconnect(self, user_id: str):
        if user_id in self.active_connections:
            del self.active_connections[user_id]
            logger.info(f"用户 {user_id} 已断开连接")
            self.cancel_user_tasks(user_id)

    def cancel_user_tasks(self, user_id: str):
        """取消指定用户的所有OCR任务"""
        if user_id in self.user_tasks:
            for task in self.user_tasks[user_id]:
                if not task.done():
                    logger.info(f"正在取消用户 {user_id} 的OCR任务")
                    task.cancel()
            del self.user_tasks[user_id]
            logger.info(f"用户 {user_id} 的所有OCR任务已取消")

    def register_task(self, user_id: str, task):
        """注册用户的OCR任务"""
        # 只为活跃连接的用户注册任务
        if user_id not in self.active_connections:
            logger.warning(f"尝试为不活跃用户 {user_id} 注册任务，已拒绝")
            if not task.done():
                task.cancel()
            return False
            
        if user_id not in self.user_tasks:
            self.user_tasks[user_id] = []
        self.user_tasks[user_id].append(task)
        logger.info(f"用户 {user_id} 注册了新的OCR任务")
        
        # 检查是否需要清理已完成任务
        total_tasks = sum(len(tasks) for tasks in self.user_tasks.values())
        if total_tasks > self.cleanup_threshold:
            self.cleanup_completed_tasks()
            
        return True

    def cleanup_completed_tasks(self):
        """清理所有已完成的任务，释放资源"""
        current_time = asyncio.get_event_loop().time()
        # 避免过于频繁的清理
        if current_time - self.last_cleanup < 60:  # 至少间隔60秒
            return
            
        logger.info("开始清理已完成的任务...")
        self.last_cleanup = current_time
        users_to_check = list(self.user_tasks.keys())
        
        for user_id in users_to_check:
            if user_id in self.user_tasks:
                # 保留未完成的任务
                active_tasks = [t for t in self.user_tasks[user_id] if not t.done()]
                completed_count = len(self.user_tasks[user_id]) - len(active_tasks)
                
                if completed_count > 0:
                    logger.info(f"用户 {user_id} 清理了 {completed_count} 个已完成任务")
                    
                # 更新任务列表
                self.user_tasks[user_id] = active_tasks
                
                # 如果用户没有活动任务且不在活动连接中，完全移除
                if not active_tasks and user_id not in self.active_connections:
                    del self.user_tasks[user_id]
                    logger.info(f"用户 {user_id} 的任务记录已完全清理")

    async def send_log(self, message: str, user_id: str = None):
        """
        发送日志给特定用户或所有用户
        
        :param message: 日志消息
        :param user_id: 用户ID，如果指定，则只发送给该用户；否则发送给所有用户
        """
        if user_id and user_id in self.active_connections:
            # 只发送给指定用户
            try:
                await self.active_connections[user_id].send_text(message)
            except Exception as e:
                logger.error(f"发送日志给用户 {user_id} 失败: {str(e)}")
                # 连接可能已断开，清理资源
                self.disconnect(user_id)
        elif not user_id:
            # 没有指定用户ID，发送给所有连接的用户
            disconnected_users = []
            for uid, connection in self.active_connections.items():
                try:
                    await connection.send_text(message)
                except Exception as e:
                    logger.error(f"发送日志给用户 {uid} 失败: {str(e)}")
                    disconnected_users.append(uid)
            
            # 清理断开的连接
            for uid in disconnected_users:
                self.disconnect(uid)

manager = ConnectionManager()

# 自定义日志处理器，将日志信息发送到WebSocket
class WebSocketLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.pending_logs = asyncio.Queue(maxsize=1000)  # 限制队列大小，防止内存溢出
        self.is_running = False
        # 设置日志记录格式
        self.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        
    async def emit_async(self, record):
        try:
            log_entry = self.format(record)
            
            # 提取用户ID（如果存在）
            user_id = None
            if hasattr(record, 'user_id'):
                user_id = record.user_id
                
            # 将日志和用户ID一起放入队列
            await self.pending_logs.put((log_entry, user_id))
            
            # 如果消费者任务还没有运行，启动它
            if not self.is_running:
                self.is_running = True
                asyncio.create_task(self.consume_logs())
        except Exception as e:
            # 忽略错误，防止日志处理阻塞主程序
            print(f"Error in WebSocketLogHandler.emit_async: {e}")
    
    async def consume_logs(self):
        """消费日志队列中的消息"""
        try:
            while True:
                # 从队列中获取日志条目和用户ID
                log_entry, user_id = await self.pending_logs.get()
                
                try:
                    # 将日志发送给指定用户或所有用户
                    await manager.send_log(log_entry, user_id)
                except Exception as e:
                    print(f"Error sending log to WebSocket: {e}")
                
                # 标记任务完成
                self.pending_logs.task_done()
                
                # 如果队列为空，等待一段时间再检查
                if self.pending_logs.empty():
                    await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            self.is_running = False
        except Exception as e:
            print(f"Error in WebSocketLogHandler.consume_logs: {e}")
            self.is_running = False
        
    def emit(self, record):
        try:
            # 创建任务来异步发送日志
            asyncio.create_task(self.emit_async(record))
        except Exception:
            self.handleError(record)

# 添加WebSocket日志处理器
websocket_handler = WebSocketLogHandler()
logger.addHandler(websocket_handler)

# 加载 .env 文件（如果存在）
load_dotenv()

# 从环境变量中读取配置
API_BASE_URL = os.getenv("API_BASE_URL", "https://api.openai.com")
API_KEY = os.getenv("OPENAI_API_KEY", "sk-111111111")
MODEL = os.getenv("MODEL", "gpt-4o")

# 并发限制和重试机制
CONCURRENCY = int(os.getenv("CONCURRENCY", 5))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", 5))
RETRY_DELAY = 0.5  # 重试延迟时间（秒）

# 初始化 FastAPI 应用
app = FastAPI(
    title="OCR图像转文本",
    description="使用 OpenAI API 进行图像文字识别",
    version="1.0.0"
)

# 跨域支持
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载静态文件目录
app.mount("/static", StaticFiles(directory="static"), name="static")

# 环境变量配置
FAVICON_URL = os.getenv("FAVICON_URL", "/static/favicon.ico")
TITLE = os.getenv("TITLE", "OCR图像转文本")
BACK_URL = os.getenv("BACK_URL", "")
INVITE_CODE = os.getenv("INVITE_CODE", "")  # 移除默认值，如果环境变量中不存在则为空字符串

# 配置 Jinja2 模板目录
templates = Jinja2Templates(directory="templates")

@app.websocket("/ws/logs/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    # 用户特定日志记录器
    user_log = UserLoggerAdapter(logger, {"user_id": user_id})
    
    await manager.connect(websocket, user_id)
    user_log.info(f"用户 {user_id} WebSocket连接已建立")
    
    try:
        while True:
            # 监听来自客户端的消息
            message = await websocket.receive_text()
            # 处理客户端消息，例如用户显式离开的通知
            if message == "user_leaving":
                user_log.info(f"用户 {user_id} 主动通知离开")
                # 立即清理用户资源
                manager.cancel_user_tasks(user_id)
    except WebSocketDisconnect:
        user_log.info(f"用户 {user_id} WebSocket连接已断开")
        manager.disconnect(user_id)

async def process_image(session, image_data, semaphore, max_retries=MAX_RETRIES, user_id=None):
    """使用 OCR 识别图像并进行 Markdown 格式化"""
    system_prompt = """
    You are an expert OCR assistant. Your job is to extract all text from the provided image and convert it into a well-structured, easy-to-read Markdown document that mirrors the intended structure of the original. Follow these precise guidelines:

- Use Markdown headings, paragraphs, lists, and tables to match the document's hierarchy and flow.  
- For tables, use standard Markdown table syntax and merge cells if needed. If a table has a title, include it as plain text above the table.  
- Render mathematical formulas with LaTeX syntax: use $...$ for inline and $$...$$ for display equations.  
- For images, use the syntax ![descriptive alt text](link) with a clear, descriptive alt text.  
- Remove unnecessary line breaks so that the text flows naturally without awkward breaks.
- Your final Markdown output must be direct text (do not wrap it in code blocks).

Ensure your output is clear, accurate, and faithfully reflects the original image's content and structure.
    """
    for attempt in range(max_retries):
        try:
            async with semaphore:
                log_message = f"正在处理图像... (尝试 {attempt+1}/{max_retries})"
                if user_id:
                    # 创建一个带有用户ID的日志记录
                    logger_with_user = UserLoggerAdapter(logger, {"user_id": user_id})
                    logger_with_user.info(log_message)
                else:
                    logger.info(log_message)
                
                encoded_image = base64.b64encode(image_data).decode('utf-8')
                
                # 添加超时设置
                timeout = aiohttp.ClientTimeout(total=60)  # 60秒超时
                
                response = await session.post(
                    f"{API_BASE_URL}/v1/chat/completions",
                    headers={"Authorization": f"Bearer {API_KEY}"},
                    json={
                        "messages": [
                            {
                                "role": "system",
                                "content": system_prompt
                            },
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "Analyze the image and provide the content in the specified format, you only need to return the content, before returning the content you need to say: 'This is the content:', add 'this is the end of the content' at the end of the returned content, don't reply to me before I upload the image!"
                                    },
                                    {
                                        "type": "image_url",
                                        "image_url": {
                                            "url": f"data:image/png;base64,{encoded_image}"
                                        }
                                    }
                                ]
                            }
                        ],
                        "stream": False,
                        "model": MODEL,
                        "temperature": 0.5,
                        "presence_penalty": 0,
                        "frequency_penalty": 0,
                        "top_p": 1,
                    },
                    timeout=timeout
                )
                if response.status == 200:
                    log_message = "图像处理API请求成功"
                    if user_id:
                        logger_with_user.info(log_message)
                    else:
                        logger.info(log_message)
                    result = await response.json()
                    return result['choices'][0]['message']['content']
                else:
                    error_msg = f"请求失败, 状态码: {response.status}"
                    if user_id:
                        logger_with_user.error(error_msg)
                    else:
                        logger.error(error_msg)
                    raise Exception(error_msg)
        except asyncio.TimeoutError:
            error_msg = f"请求超时 (尝试 {attempt+1}/{max_retries})"
            if user_id:
                logger_with_user = UserLoggerAdapter(logger, {"user_id": user_id})
                logger_with_user.error(error_msg)
            else:
                logger.error(error_msg)
            if attempt >= max_retries - 1:
                return f"识别失败: 请求超时，达到最大重试次数 {max_retries}"
            await asyncio.sleep(2 * attempt)  # 指数退避
        except Exception as e:
            if attempt >= max_retries - 1:
                error_msg = f"识别失败: {str(e)}"
                if user_id:
                    logger_with_user = UserLoggerAdapter(logger, {"user_id": user_id})
                    logger_with_user.error(error_msg)
                else:
                    logger.error(error_msg)
                return error_msg
            log_message = f"尝试 {attempt+1} 失败，将在 {2 * attempt} 秒后重试"
            if user_id:
                logger_with_user = UserLoggerAdapter(logger, {"user_id": user_id})
                logger_with_user.warning(log_message)
            else:
                logger.warning(log_message)
            await asyncio.sleep(2 * attempt)  # 指数退避

def pdf_to_images(pdf_bytes: bytes, dpi: int = 300, user_id=None) -> list:
    """
    使用 PyMuPDF 将 PDF 转换为图片。
    :param pdf_bytes: PDF 文件的字节数据。
    :param dpi: 图像分辨率 (300 DPI)。
    :param user_id: 用户ID，用于记录特定用户的日志。
    :return: PIL 图像列表。
    """
    images = []
    pdf_document = None
    try:
        # 创建带有用户ID的日志记录器（如果提供了用户ID）
        log = logger
        if user_id:
            log = UserLoggerAdapter(logger, {"user_id": user_id})
            
        pdf_document = fitz.open(stream=pdf_bytes, filetype="pdf")
        log.info(f"PDF 文件包含 {len(pdf_document)} 页")
        for page_number in range(len(pdf_document)):
            log.info(f"正在转换 PDF 第 {page_number+1}/{len(pdf_document)} 页为图像")
            page = pdf_document.load_page(page_number)
            pix = page.get_pixmap(dpi=dpi)
            img_bytes = pix.tobytes("png")
            pix = None  # 显式释放pixmap资源
            image = Image.open(BytesIO(img_bytes))
            images.append(image)
            log.info(f"PDF 第 {page_number+1} 页转换完成")
        log.info(f"PDF 所有 {len(pdf_document)} 页面转换完成")
        return images
    except Exception as e:
        if user_id:
            log = UserLoggerAdapter(logger, {"user_id": user_id})
            log.error(f"PDF 转图片失败: {e}")
        else:
            logger.error(f"PDF 转图片失败: {e}")
        raise e
    finally:
        # 确保PDF文档被关闭，释放资源
        if pdf_document:
            pdf_document.close()
            pdf_document = None


async def upload_image_to_endpoint(image_data: bytes or Image.Image, page_number: int, semaphore: asyncio.Semaphore, user_id=None):
    """处理图像并获取OCR结果"""
    try:
        async with semaphore:
            # 创建带有用户ID的日志记录器（如果提供了用户ID）
            log = logger
            if user_id:
                log = UserLoggerAdapter(logger, {"user_id": user_id})
                
            log.info(f"开始处理第 {page_number} 页")
            
            # 将PIL Image对象转换为字节数据
            if isinstance(image_data, Image.Image):
                img_byte_arr = BytesIO()
                image_data.save(img_byte_arr, format='PNG')
                image_bytes = img_byte_arr.getvalue()
            else:
                image_bytes = image_data
            
            # 直接处理图像，不通过HTTP调用
            async with aiohttp.ClientSession() as session:
                processed_text = await process_image(session, image_bytes, asyncio.Semaphore(1), MAX_RETRIES, user_id)
                
                # 处理响应格式
                if "This is the content:" in processed_text:
                    start_marker = "This is the content:"
                    end_marker = "this is the end of the content"
                    
                    start_pos = processed_text.find(start_marker) + len(start_marker)
                    end_pos = processed_text.find(end_marker)
                    
                    if end_pos > start_pos:
                        processed_text = processed_text[start_pos:end_pos].strip()
                
                log.info(f"第 {page_number} 页处理成功")
                
                return processed_text
    except Exception as e:
        # 创建带有用户ID的日志记录器（如果提供了用户ID）
        if user_id:
            log = UserLoggerAdapter(logger, {"user_id": user_id})
            log.error(f"第 {page_number} 页处理异常: {e}")
        else:
            logger.error(f"第 {page_number} 页处理异常: {e}")
        return f"第 {page_number} 页处理异常: {e}"

@app.post("/process/image")
async def process_image_endpoint(file: UploadFile, request: Request):
    """处理上传的图像文件"""
    # 初始化user_id变量，确保在任何情况下都已定义
    user_id = None
    user_log = logger
    
    try:
        # 获取用户ID
        try:
            cookie_data = request.cookies.get("ocr_user_data", "")
            if not cookie_data or cookie_data.isspace():
                cookie_data = "{}"
            cookie_data = json.loads(cookie_data)
            user_id = cookie_data.get("userId", None)
        except json.JSONDecodeError as e:
            logger.warning(f"解析cookie数据失败: {e}，将使用匿名用户处理")
            user_id = None
        
        # 创建用户特定的日志处理器
        if user_id:
            user_log = UserLoggerAdapter(logger, {"user_id": user_id})
        
        # 读取图像数据
        image_data = await file.read()
        user_log.info(f"收到图像文件：{file.filename}，大小：{len(image_data)/1024:.2f} KB")
        
        # 创建信号量
        semaphore = asyncio.Semaphore(1)
        
        # 创建处理任务
        task = asyncio.create_task(upload_image_to_endpoint(image_data, 1, semaphore, user_id))
        
        # 注册任务，如果用户不活跃则取消
        if user_id:
            if not manager.register_task(user_id, task):
                task.cancel()
                user_log.warning(f"用户 {user_id} 会话已过期，请求被拒绝")
                return JSONResponse({
                    "status": "error", 
                    "message": "用户会话已过期，请刷新页面"
                }, status_code=400)
                
        # 等待任务完成
        result = await task
        
        # 构造响应
        response_data = {
            "status": "success" if "识别失败" not in result else "error",
            "message": "处理完成" if "识别失败" not in result else "处理失败",
            "text": result
        }
        
        user_log.info("图像处理完成")
        return JSONResponse(response_data)
        
    except Exception as e:
        error_msg = f"图像处理异常: {str(e)}"
        if user_id:
            user_log = UserLoggerAdapter(logger, {"user_id": user_id})
            user_log.error(error_msg)
        else:
            logger.error(error_msg)
        return JSONResponse({"status": "error", "message": error_msg}, status_code=500)


@app.post("/process/pdf")
async def process_pdf_endpoint(file: UploadFile, request: Request):
    """处理上传的PDF文件"""
    # 初始化user_id变量，确保在任何情况下都已定义
    user_id = None
    user_log = logger
    
    try:
        # 获取用户ID
        try:
            cookie_data = request.cookies.get("ocr_user_data", "")
            if not cookie_data or cookie_data.isspace():
                cookie_data = "{}"
            cookie_data = json.loads(cookie_data)
            user_id = cookie_data.get("userId", None)
        except json.JSONDecodeError as e:
            logger.warning(f"解析cookie数据失败: {e}，将使用匿名用户处理")
            user_id = None
        
        # 创建用户特定的日志处理器
        if user_id:
            user_log = UserLoggerAdapter(logger, {"user_id": user_id})
        
        pdf_bytes = await file.read()
        user_log.info(f"收到PDF文件：{file.filename}，大小：{len(pdf_bytes)/1024:.2f} KB")
        
        # 将PDF转换为图像
        images_data = pdf_to_images(pdf_bytes, 300, user_id)
        user_log.info(f"PDF共转换为 {len(images_data)} 页图像")
        
        # 创建并发限制的信号量
        semaphore = asyncio.Semaphore(CONCURRENCY)
        
        # 并发处理所有图像
        tasks = []
        for i, img_data in enumerate(images_data):
            task = asyncio.create_task(upload_image_to_endpoint(img_data, i+1, semaphore, user_id))
            # 注册任务，如果注册失败（用户不活跃），取消任务
            if user_id:
                if not manager.register_task(user_id, task):
                    task.cancel()
                    user_log.warning(f"用户 {user_id} 会话已过期，任务 {i+1} 被取消")
                    continue
            tasks.append(task)
            
        # 等待所有任务完成
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 处理结果
        successes = []
        failures = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                failures.append(f"第 {i+1} 页处理失败: {str(result)}")
            elif "识别失败" in result:
                failures.append(f"第 {i+1} 页: {result}")
            else:
                successes.append(result)
                
        combined_text = "\n\n".join(successes)
        
        # 构建响应
        response_data = {
            "status": "success" if len(successes) > 0 else "error",
            "message": f"成功处理了 {len(successes)}/{len(images_data)} 页" if len(successes) > 0 else "所有页面处理失败",
            "text": combined_text
        }
        
        if failures:
            response_data["failures"] = failures
            
        user_log.info(f"PDF处理完成，成功页数: {len(successes)}/{len(images_data)}")
        return JSONResponse(response_data)
        
    except Exception as e:
        error_msg = f"PDF处理异常: {str(e)}"
        if user_id:
            user_log = UserLoggerAdapter(logger, {"user_id": user_id})
            user_log.error(error_msg)
        else:
            logger.error(error_msg)
        return JSONResponse({"status": "error", "message": error_msg}, status_code=500)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """重定向到需要验证的入口页面或直接显示主页面"""
    # 如果未设置邀请码，直接显示主页面
    if not INVITE_CODE:
        return templates.TemplateResponse("web.html", {
            "request": request,
            "title": TITLE,
            "favicon_url": FAVICON_URL,
            "back_url": BACK_URL or (request.url.scheme + "://" + request.url.netloc)
        })
    # 否则显示邀请码验证页面
    return templates.TemplateResponse("invite.html", {"request": request, "title": TITLE, "favicon_url": FAVICON_URL})

@app.get("/{code}", response_class=HTMLResponse)
async def main_page(request: Request, code: str):
    """渲染主页面，但需要验证邀请码是否正确"""
    # 如果未设置邀请码，直接显示主页面
    if not INVITE_CODE:
        return templates.TemplateResponse("web.html", {
            "request": request,
            "title": TITLE,
            "favicon_url": FAVICON_URL,
            "back_url": BACK_URL or (request.url.scheme + "://" + request.url.netloc)
        })
    # 否则验证邀请码
    if code == INVITE_CODE:
        return templates.TemplateResponse("web.html", {
            "request": request,
            "title": TITLE,
            "favicon_url": FAVICON_URL,
            "back_url": BACK_URL or (request.url.scheme + "://" + request.url.netloc)
        })
    else:
        return templates.TemplateResponse("invite.html", {
            "request": request,
            "title": TITLE,
            "favicon_url": FAVICON_URL,
            "error": "邀请码不正确，请重试！"
        })

@app.post("/verify_code")
async def verify_invite_code(request: Request):
    """验证邀请码API"""
    # 如果未设置邀请码，直接返回成功
    if not INVITE_CODE:
        return JSONResponse({"success": True, "redirect": "/"})
        
    form_data = await request.form()
    input_code = form_data.get("invite_code", "")
    
    if input_code == INVITE_CODE:
        return JSONResponse({"success": True, "redirect": f"/{INVITE_CODE}"})
    else:
        return JSONResponse({"success": False, "message": "邀请码不正确，请重试！"})

# 应用程序启动和关闭事件处理
@app.on_event("startup")
async def startup_event():
    """应用程序启动时的事件处理"""
    logger.info("应用程序启动")
    
    # 配置并发和资源限制
    if platform.system() != "Windows":
        import resource
        # 增加文件描述符的软限制和硬限制
        try:
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            new_soft = min(hard, 4096)  # 4096或硬限制中较小的值
            resource.setrlimit(resource.RLIMIT_NOFILE, (new_soft, hard))
            logger.info(f"增加文件描述符限制: {soft} -> {new_soft} (硬限制: {hard})")
        except Exception as e:
            logger.warning(f"设置资源限制失败: {e}")
    else:
        logger.info("Windows系统下无法直接增加文件描述符限制，确保合理的并发设置")
    
    # 打印当前配置
    logger.info(f"服务配置: API 基础URL: {API_BASE_URL}")
    logger.info(f"服务配置: 模型: {MODEL}")
    logger.info(f"服务配置: 并发数: {CONCURRENCY}")
    logger.info(f"服务配置: 最大重试次数: {MAX_RETRIES}")

@app.on_event("shutdown")
async def shutdown_event():
    """应用程序关闭时的事件处理"""
    logger.info("应用程序关闭，清理资源")
    
    # 取消所有用户的任务
    for user_id in list(manager.user_tasks.keys()):
        manager.cancel_user_tasks(user_id)
    
    # 清空连接
    manager.active_connections.clear()
    
    # 确保所有日志都被消费
    if isinstance(websocket_handler, WebSocketLogHandler):
        try:
            if not websocket_handler.pending_logs.empty():
                logger.info(f"等待 {websocket_handler.pending_logs.qsize()} 个待处理日志消息")
                await websocket_handler.pending_logs.join()
        except Exception as e:
            logger.error(f"清理日志队列时出错: {e}")
    
    logger.info("资源清理完成，应用程序已停止")

if __name__ == "__main__":
    import uvicorn
    # 直接启动服务器，资源限制和日志将由startup_event处理
    uvicorn.run(app, host="0.0.0.0", port=54188, reload=True)