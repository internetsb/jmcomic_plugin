import asyncio
import random
import datetime
import shutil
import os
from pathlib import Path
from typing import Tuple, Optional, Any, List, Type

import pyzipper

from jmcomic import *
from src.common.logger import get_logger
from src.plugin_system import (
    BasePlugin,
    register_plugin,
    BaseCommand,
    ComponentInfo,
    ConfigField,
    chat_api,
    send_api
)

logger = get_logger("jmcomic.plugin")
plugin_path = Path(__file__).parent.resolve()
album_path = plugin_path / "albums"
album_path.mkdir(parents=True, exist_ok=True)

# ================== 全局下载锁 ==================
DOWNLOAD_LOCK = asyncio.Lock()
CURRENT_DOWNLOADING = None


# ================== 配置常量 ==================
class PluginConfig:
    DOWNLOAD_TIMEOUT = 120
    UPLOAD_MIN_WAIT = 3
    UPLOAD_SPEED_ESTIMATE = 1 * 1024 * 1024
    SCHEDULE_CHECK_INTERVAL = 60
    SCHEDULE_COOLDOWN = 61

    DEFAULT_ENCRYPT = "jmcomic"
    DEFAULT_CLEAR_DIR = True
    DEFAULT_CLEAR_ZIP = True
    DEFAULT_PERMISSION_TYPE = "whitelist"


# ================== 核心功能类 ==================
class DownloadManager:
    """下载管理器，确保同一时刻只能下载一本漫画"""

    @staticmethod
    async def download_comic(comic_input: str, tag: Optional[str] = None) -> Tuple[bool, Any, Optional[JmAlbumDetail]]:
        """
        下载漫画主逻辑，使用全局锁确保同一时刻只能有一个下载任务

        Returns:
            Tuple[成功标志, 漫画ID或错误信息, 漫画详情]
        """
        global CURRENT_DOWNLOADING

        # 检查是否已有下载任务
        if DOWNLOAD_LOCK.locked():
            return False, "当前有漫画正在下载，请稍后再试", None

        async with DOWNLOAD_LOCK:
            try:
                CURRENT_DOWNLOADING = comic_input
                return await DownloadManager._execute_download(comic_input, tag)
            finally:
                CURRENT_DOWNLOADING = None

    @staticmethod
    async def _execute_download(comic_input: str, tag: Optional[str]) -> Tuple[bool, Any, Optional[JmAlbumDetail]]:
        """执行下载逻辑"""
        try:
            option = get_option()
            client = option.new_jm_client()
            comic_id = comic_input

            # 解析漫画ID
            if comic_input == "random":
                comic_id = await DownloadManager._get_random_comic_id(client, tag)
                if not comic_id:
                    error_msg = f"未找到包含标签 '{tag}' 的漫画" if tag else "未找到随机漫画"
                    return False, error_msg, None

            # 检查是否已存在
            if await DownloadManager._check_comic_exists(comic_id):
                logger.info(f"漫画 {comic_id} 已存在，跳过下载")
                album_details = await DownloadManager._get_album_details(client, comic_id)
                return True, comic_id, album_details

            # 执行下载
            return await DownloadManager._download_album(comic_id, option)

        except asyncio.TimeoutError:
            error_msg = f"下载漫画超时"
            logger.error(error_msg)
            return False, error_msg, None
        except Exception as e:
            error_msg = f"下载漫画失败: {e}"
            logger.error(error_msg)
            return False, error_msg, None

    @staticmethod
    async def _get_random_comic_id(client, tag: Optional[str]) -> Optional[int]:
        """获取随机漫画ID"""
        try:
            if tag:
                aid_list = []
                for page_num in range(1, 5):
                    page: JmSearchPage = client.search_site(search_query=f'+{tag}', page=page_num)
                    aid_list.extend(list(page.iter_id()))
            else:
                page = client.categories_filter(
                    page=random.randint(1, 3),
                    time=JmMagicConstants.TIME_MONTH,
                    category=JmMagicConstants.CATEGORY_ALL,
                    order_by=JmMagicConstants.ORDER_BY_VIEW,
                )
                aid_list = list(page.iter_id())

            return random.choice(aid_list) if aid_list else None
        except Exception as e:
            logger.error(f"获取随机漫画ID失败: {e}")
            return None

    @staticmethod
    async def _check_comic_exists(comic_id: int) -> bool:
        """检查漫画是否已存在"""
        comic_dir = album_path / str(comic_id)
        zip_file = album_path / f"{comic_id}.zip"
        return comic_dir.exists() or zip_file.exists()

    @staticmethod
    async def _get_album_details(client, comic_id: int) -> Optional[JmAlbumDetail]:
        """获取漫画详情"""
        try:
            page = client.search_site(search_query=comic_id)
            return page.single_album
        except Exception as e:
            logger.error(f"获取漫画详情失败: {e}")
            return None

    @staticmethod
    async def _download_album(comic_id: int, option: JmOption) -> Tuple[bool, Any, Optional[JmAlbumDetail]]:
        """执行下载操作"""
        try:
            download_task = asyncio.to_thread(download_album, comic_id, option)
            album_details, _ = await asyncio.wait_for(
                download_task,
                timeout=PluginConfig.DOWNLOAD_TIMEOUT
            )
            return True, comic_id, album_details
        except asyncio.TimeoutError:
            await ResourceManager.cleanup_files(comic_id, True, True)
            raise
        except Exception as e:
            await ResourceManager.cleanup_files(comic_id, True, True)
            raise


class ResourceManager:
    """资源管理器"""

    @staticmethod
    async def cleanup_files(comic_id: int, clear_dir: bool = True, clear_zip: bool = True) -> bool:
        """清理漫画相关文件"""
        try:
            comic_dir = album_path / str(comic_id)
            zip_file = album_path / f"{comic_id}.zip"

            def _cleanup():
                success = True
                if clear_dir and comic_dir.exists() and comic_dir.is_dir():
                    try:
                        shutil.rmtree(comic_dir, ignore_errors=True)
                        logger.debug(f"已清理漫画目录: {comic_dir}")
                    except Exception as e:
                        logger.error(f"清理目录失败: {e}")
                        success = False

                if clear_zip and zip_file.exists() and zip_file.is_file():
                    try:
                        zip_file.unlink()
                        logger.debug(f"已清理压缩包: {zip_file}")
                    except Exception as e:
                        logger.error(f"清理压缩包失败: {e}")
                        success = False

                return success

            return await asyncio.to_thread(_cleanup)
        except Exception as e:
            logger.error(f"资源清理异常: {e}")
            return False


class ZipManager:
    """压缩文件管理器"""

    @staticmethod
    async def create_encrypted_zip(comic_id: int, encrypt: str) -> Tuple[bool, Optional[Path]]:
        """创建加密压缩包"""
        try:
            return await asyncio.to_thread(
                ZipManager._sync_create_encrypted_zip,
                comic_id,
                encrypt
            )
        except Exception as e:
            logger.error(f"创建压缩包异常: {e}")
            return False, None

    @staticmethod
    def _sync_create_encrypted_zip(comic_id: int, encrypt: str) -> Tuple[bool, Optional[Path]]:
        """同步创建加密压缩包"""
        comic_dir = album_path / str(comic_id)
        zip_file = album_path / f"{comic_id}.zip"

        if not comic_dir.exists() or not comic_dir.is_dir():
            logger.error(f"漫画目录不存在: {comic_dir}")
            return False, None

        if zip_file.exists():
            logger.info(f"压缩包已存在: {zip_file}")
            return True, zip_file

        try:
            with pyzipper.AESZipFile(
                    zip_file,
                    'w',
                    encryption=pyzipper.WZ_AES
            ) as zipf:
                zipf.setpassword(encrypt.encode('utf-8'))

                for root, dirs, files in os.walk(comic_dir):
                    for file in files:
                        file_path = Path(root) / file
                        arcname = file_path.relative_to(comic_dir)
                        zipf.write(file_path, arcname)

            logger.info(f"压缩包创建成功: {zip_file}")
            return True, zip_file

        except Exception as e:
            logger.error(f"压缩包创建失败: {e}")
            if zip_file.exists():
                try:
                    zip_file.unlink()
                except Exception:
                    pass
            return False, None


class MessageManager:
    """消息管理器"""

    @staticmethod
    async def send_text_to_stream(message: str, stream) -> bool:
        """发送文本消息到指定流"""
        try:
            await send_api.text_to_stream(message, stream.stream_id)
            return True
        except Exception as e:
            logger.error(f"发送文本消息失败: {e}")
            return False

    @staticmethod
    async def upload_file_to_stream(file_name: str, stream) -> bool:
        """上传文件到指定流"""
        try:
            file_path = album_path / file_name
            if not file_path.exists():
                logger.error(f"文件不存在: {file_path}")
                return False

            result = await send_api.custom_to_stream(
                message_type="file",
                content=str(file_path.resolve()),
                stream_id=stream.stream_id,
                display_message=f"发送文件 {file_name} ..."
            )
            return result
        except Exception as e:
            logger.error(f"上传文件失败: {e}")
            return False

    @staticmethod
    def generate_album_message(album_details: JmAlbumDetail) -> str:
        """生成本子简介消息"""
        try:
            tags = getattr(album_details, "tags", [])
            name = getattr(album_details, "name", "未知")
            description = getattr(album_details, "description", "")

            return f"""
========== {name} ==========
简介：{description}
标签: [{'、'.join(tags)}]
"""
        except Exception as e:
            logger.error(f"生成简介消息失败: {e}")
            return "漫画信息获取失败"

    @staticmethod
    async def send_tease_message(command: BaseCommand, tease_type: str):
        """发送提示消息"""
        if tease_list := command.get_config(f"tease.{tease_type}", []):
            await command.send_text(random.choice(tease_list))


# ================== 工具函数 ==================
def get_option() -> JmOption:
    """获取下载选项配置"""
    option = create_option_by_file(str(plugin_path / "option.yml"))
    option.dir_rule.base_dir = str(album_path)
    return option


async def estimate_upload_time(file_path: Path) -> float:
    """估算上传时间"""
    try:
        file_size = file_path.stat().st_size
        upload_time = file_size / PluginConfig.UPLOAD_SPEED_ESTIMATE
        return max(upload_time, PluginConfig.UPLOAD_MIN_WAIT)
    except Exception as e:
        logger.error(f"估算上传时间失败: {e}")
        return PluginConfig.UPLOAD_MIN_WAIT


# ================== 定时任务组件 ==================
class ScheduleSender:
    """定时任务发送器"""

    def __init__(self, plugin):
        self.plugin = plugin
        self.is_running = False
        self.task = None

    async def start(self):
        """启动定时发送任务"""
        if self.is_running:
            return

        self.is_running = True
        self.task = asyncio.create_task(self._schedule_loop())
        logger.info("定时随机漫画任务已启动")

    async def stop(self):
        """停止定时发送任务"""
        if not self.is_running:
            return

        self.is_running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logger.info("定时随机漫画任务已停止")

    async def _schedule_loop(self):
        """定时发送循环"""
        time_table = self.plugin.get_config("schedule.time", ["23:00"])
        tag = self.plugin.get_config("schedule.tag", "")
        target_groups = self.plugin.get_config("schedule.target_group", [])
        target_users = self.plugin.get_config("schedule.target_user", [])

        if not time_table or (not target_groups and not target_users):
            logger.error("定时发送配置不完整")
            self.is_running = False
            return

        while self.is_running:
            try:
                current_time = datetime.datetime.now().strftime("%H:%M")

                if current_time in time_table:
                    await self._execute_schedule_task(tag, target_groups, target_users)
                    await asyncio.sleep(PluginConfig.SCHEDULE_COOLDOWN)

                await asyncio.sleep(PluginConfig.SCHEDULE_CHECK_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"定时任务循环异常: {e}")
                await asyncio.sleep(PluginConfig.SCHEDULE_CHECK_INTERVAL)

    async def _execute_schedule_task(self, tag: str, target_groups: list, target_users: list):
        """执行定时任务"""
        logger.info("开始执行定时漫画推荐任务")

        # 下载漫画（使用下载管理器确保互斥）
        success, comic_id, album_details = await DownloadManager.download_comic("random", tag)
        if not success:
            logger.error(f"定时任务下载失败: {comic_id}")
            return

        # 生成消息
        message_text = MessageManager.generate_album_message(album_details)

        # 创建压缩包
        encrypt = self.plugin.get_config("plugin.encrypt", PluginConfig.DEFAULT_ENCRYPT)
        zip_success, zip_path = await ZipManager.create_encrypted_zip(comic_id, encrypt)
        if not zip_success:
            logger.error("定时任务创建压缩包失败")
            await ResourceManager.cleanup_files(comic_id, True, False)
            return

        # 发送消息
        await self._send_schedule_messages(message_text, zip_path.name, target_groups, target_users)

        # 清理文件
        upload_time = await estimate_upload_time(zip_path)
        await asyncio.sleep(upload_time)

        clear_dir = self.plugin.get_config("plugin.clear_dir", PluginConfig.DEFAULT_CLEAR_DIR)
        clear_zip = self.plugin.get_config("plugin.clear_zip", PluginConfig.DEFAULT_CLEAR_ZIP)
        await ResourceManager.cleanup_files(comic_id, clear_dir, clear_zip)

    async def _send_schedule_messages(self, message: str, zip_filename: str, target_groups: list, target_users: list):
        """发送定时任务消息"""
        # 群组发送
        for group_id in target_groups:
            stream = chat_api.get_stream_by_group_id(group_id, "qq")
            if stream:
                await MessageManager.upload_file_to_stream(zip_filename, stream)
                await MessageManager.send_text_to_stream(message, stream)
                if tease_list := self.plugin.get_config("tease.schedule", []):
                    await MessageManager.send_text_to_stream(random.choice(tease_list), stream)

        # 用户发送
        for user_id in target_users:
            stream = chat_api.get_stream_by_user_id(user_id, "qq")
            if stream:
                await MessageManager.upload_file_to_stream(zip_filename, stream)
                await MessageManager.send_text_to_stream(message, stream)
                if tease_list := self.plugin.get_config("tease.schedule", []):
                    await MessageManager.send_text_to_stream(random.choice(tease_list), stream)


# ================== Command组件 ==================
class JMComicCommand(BaseCommand):
    """JM下载Command - 响应/jm命令"""

    command_name = "jm"
    command_description = "根据指令下载漫画"
    command_pattern = r"^/jm\s+(?P<arg1>\S+)(?:\s+tag=(?P<tag>\S+))?$"
    command_help = """
====================
用法: /jm <漫画ID|random|help> [tag=<分类>]
====================
示例:
/jm 350234 # 下载指定ID的漫画
/jm random # 下载随机漫画
/jm random tag=全彩 # 下载全彩的随机漫画
"""
    command_examples = [
        "/jm help",
        "/jm 350234",
        "/jm random",
        "/jm random tag=全彩"
    ]
    intercept_message = True

    def check_permission(self, qq_account: str) -> bool:
        """检查用户权限"""
        permission_list = self.get_config("plugin.permission", [])
        permission_type = self.get_config("plugin.permission_type", PluginConfig.DEFAULT_PERMISSION_TYPE)

        if permission_type == 'whitelist':
            return qq_account in permission_list
        elif permission_type == 'blacklist':
            return qq_account not in permission_list
        else:
            logger.error(f'无效的权限类型: {permission_type}')
            return False

    async def execute(self) -> tuple[bool, Optional[str], bool]:
        """命令执行入口"""
        user_id = self.message.message_info.user_info.user_id

        # 权限检查
        if not self.check_permission(user_id):
            logger.info(f"用户 {user_id} 无权限使用命令")
            await MessageManager.send_tease_message(self, "no_permission")
            return False, "权限不足", True

        # 参数解析
        arg1 = self.matched_groups.get("arg1")
        tag = self.matched_groups.get("tag")

        # 帮助命令
        if arg1 is None or arg1.lower() == "help":
            await self.send_text(self.command_help)
            return False, "显示帮助信息", True

        # 下载命令
        comic_id = None
        if arg1.isdigit():
            comic_id = int(arg1)
            logger.info(f"用户 {user_id} 请求下载漫画 {comic_id}")
            await self.send_text(f"开始下载漫画ID {comic_id} ...")
            success, result, album_details = await DownloadManager.download_comic(comic_id, tag)

        elif arg1.lower() == "random":
            logger.info(f"用户 {user_id} 请求下载随机漫画 tag={tag}")
            await self.send_text(f"开始下载'{tag if tag else '随机'}'漫画 ...")
            success, result, album_details = await DownloadManager.download_comic("random", tag)
            if success:
                comic_id = result
        else:
            await self.send_text(self.command_help)
            return False, "参数错误", True

        # 处理下载结果
        if not success or comic_id is None:
            await MessageManager.send_tease_message(self, "fail")
            return False, result, True

        return await self._handle_successful_download(comic_id, album_details)

    async def _handle_successful_download(self, comic_id: int, album_details: JmAlbumDetail) -> tuple[
        bool, Optional[str], bool]:
        """处理下载成功后的流程"""
        # 发送漫画信息
        await self.send_text(MessageManager.generate_album_message(album_details))

        # 创建压缩包
        encrypt = self.get_config("plugin.encrypt", PluginConfig.DEFAULT_ENCRYPT)
        zip_success, zip_path = await ZipManager.create_encrypted_zip(comic_id, encrypt)

        if not zip_success:
            await MessageManager.send_tease_message(self, "fail")
            return False, "创建压缩包失败", True

        # 上传文件
        upload_success = await MessageManager.upload_file_to_stream(zip_path.name, self.message.chat_stream)
        if upload_success:
            await MessageManager.send_tease_message(self, "success")
        else:
            await MessageManager.send_tease_message(self, "fail")

        # 清理文件
        upload_time = await estimate_upload_time(zip_path)
        await asyncio.sleep(upload_time)

        clear_dir = self.get_config("plugin.clear_dir", PluginConfig.DEFAULT_CLEAR_DIR)
        clear_zip = self.get_config("plugin.clear_zip", PluginConfig.DEFAULT_CLEAR_ZIP)
        await ResourceManager.cleanup_files(comic_id, clear_dir, clear_zip)

        return True, 'success', True


# ===== 插件注册 =====
@register_plugin
class JMComicPlugin(BasePlugin):
    """JMComic插件 - 提供下载漫画功能"""

    plugin_name: str = "jmcomic"
    enable_plugin: bool = True
    dependencies: List[str] = []
    python_dependencies: List[str] = ["jmcomic", "pyzipper", "httpx", "asyncio"]
    config_file_name: str = "config.toml"

    config_section_descriptions = {
        "plugin": "插件基本信息",
        "tease": "响应命令的文本列表，从中随机调用，禁用请清空",
        "schedule": "每日漫画推荐"
    }

    config_schema: dict = {
        "plugin": {
            "enable": ConfigField(type=bool, default=True, description="是否启用插件"),
            "encrypt": ConfigField(type=str, default=PluginConfig.DEFAULT_ENCRYPT, description="解压密码"),
            "clear_dir": ConfigField(type=bool, default=PluginConfig.DEFAULT_CLEAR_DIR,
                                     description="是否清理下载的漫画文件夹"),
            "clear_zip": ConfigField(type=bool, default=PluginConfig.DEFAULT_CLEAR_ZIP,
                                     description="是否清理上传后压缩包"),
            "permission_type": ConfigField(type=str, default=PluginConfig.DEFAULT_PERMISSION_TYPE,
                                           description="权限类型，whitelist或blacklist"),
            "permission": ConfigField(type=list, default=["114514", "1919810"], description="权限QQ号列表"),
        },
        "tease": {
            "success": ConfigField(type=list, default=["漫画到手喵~"], description="下载成功后发送的提醒"),
            "schedule": ConfigField(type=list, default=["luguanluguanlulushijiandaole", "每日推荐送达喵~"],
                                    description="每日推荐发送成功后发送的提醒"),
            "fail": ConfigField(type=list, default=["下载失败了喵~"], description="下载失败后发送的提醒"),
            "no_permission": ConfigField(type=list, default=["不给你下哦~杂鱼杂鱼~你能拿我怎么办ww",
                                                             "满脑子黄色废料的杂鱼干脆自己幻想着导几管吧ww"],
                                         description="无权限使用命令时发送的提醒")
        },
        "schedule": {
            "enable": ConfigField(type=bool, default=False, description="是否启用每日推荐"),
            "time": ConfigField(type=list, default=["23:00"], description="每日推荐时间列表，格式HH:MM"),
            "tag": ConfigField(type=str, default="", description="推荐漫画的关键词，留空则随机"),
            "target_group": ConfigField(type=list, default=[], description="发送QQ群列表，用引号包括群号，逗号分隔"),
            "target_user": ConfigField(type=list, default=[], description="发送目标QQ列表，用引号包括QQ号，逗号分隔"),
        }
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.scheduler = None
        self._initialize_plugin()

    def _initialize_plugin(self):
        """初始化插件"""
        self.enable_plugin = self.get_config("plugin.enable", True)

        if self.enable_plugin and self.get_config("schedule.enable", False):
            self.scheduler = ScheduleSender(self)
            asyncio.create_task(self._start_scheduler_after_delay())

    async def _start_scheduler_after_delay(self):
        """延迟启动定时任务"""
        await asyncio.sleep(5)
        if self.scheduler:
            await self.scheduler.start()

    async def on_unload(self):
        """插件卸载时的清理工作"""
        if self.scheduler:
            await self.scheduler.stop()

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        return [
            (JMComicCommand.get_command_info(), JMComicCommand),
        ]