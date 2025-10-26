import asyncio
import random
import datetime
import shutil
from pathlib import Path

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
# ================== 常量配置 ==================
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(2)  # 限制并发下载数，防止内存被耗尽
DOWNLOAD_TIMEOUT_SECONDS = 120  # 单次下载超时（秒）
MAX_PAGES_DEFAULT = 200  # 默认页面数上限（无效）

# ================== 工具函数 ==================
async def send_text_by_id(message: str, user_id: str = None, group_id: str = None) -> bool:
    """
    发送文本消息到指定用户或QQ群
    """
    if group_id:
        stream = chat_api.get_stream_by_group_id(group_id, "qq")
    else:
        stream = chat_api.get_stream_by_user_id(user_id, "qq")
    try:
        await send_api.text_to_stream(message, stream.stream_id)
        return True
    except Exception as e:
        logger.error(f"发送文本消息异常: {str(e)}")
        return False


async def upload_file_by_id(file_name: str, user_id: str = None, group_id: str = None) -> bool:
    """
    上传图片目录下名为{file_name}文件到指定用户或QQ群
    """
    file_path = str(album_path / file_name)
    normpath = os.path.normpath(file_path)
    if group_id:
        stream = chat_api.get_stream_by_group_id(group_id, "qq")
    else:
        stream = chat_api.get_stream_by_user_id(user_id, "qq")
    try:
        result = await send_api.custom_to_stream(
            message_type="file",
            content=normpath,
            stream_id=stream.stream_id,
            display_message=f"发送文件 {file_name} ..."
        )
        return result
    except Exception as e:
        logger.error(f"上传文件异常: {str(e)}")
        return False


async def upload_file_by_stream(file_name: str, stream) -> bool:
    """
    上传图片目录下名为{file_name}文件到指定流
    """
    file_path = str(album_path / file_name)
    normpath = os.path.normpath(file_path)
    try:
        result = await send_api.custom_to_stream(
            message_type="file",
            content=normpath,
            stream_id=stream.stream_id,
            display_message=f"发送文件 {file_name} ..."
        )
        return result
    except Exception as e:
        logger.error(f"上传文件异常: {str(e)}")
        return False


def get_option() -> JmOption:
    """获取下载选项配置"""
    option = create_option_by_file(str(plugin_path / "option.yml"))
    option.dir_rule.base_dir = str(plugin_path / "albums")  # 设置下载目录
    return option


def _sync_create_encrypted_zip(comic_id: int, encrypt: str) -> Tuple[bool, Optional[Path]]:
    """同步压缩实现"""
    comic_dir_path = album_path / str(comic_id)
    zip_file_path = album_path / f"{comic_id}.zip"

    if not comic_dir_path.exists() or not comic_dir_path.is_dir():
        logger.error(f"漫画目录 {comic_dir_path} 不存在或不是文件夹")
        return False, None
    if zip_file_path.exists() and zip_file_path.is_file():
        logger.info(f"压缩包 {zip_file_path} 已存在，跳过创建")
        return True, zip_file_path
    try:
        with pyzipper.AESZipFile(zip_file_path, 'w', encryption=pyzipper.WZ_AES) as zipf:
            zipf.setpassword(encrypt.encode('utf-8'))
            for root, dirs, files in os.walk(comic_dir_path):
                for file in files:
                    file_path = Path(root) / file
                    arcname = file_path.relative_to(comic_dir_path)
                    zipf.write(file_path, arcname)

        logger.debug(f"成功创建AES加密压缩包: {zip_file_path}")
        return True, zip_file_path

    except Exception as e:
        logger.error(f"创建压缩包失败: {str(e)}")
        try:
            if zip_file_path.exists():
                zip_file_path.unlink()
        except Exception:
            pass
        return False, None


async def create_encrypted_zip(comic_id: int, encrypt: str) -> Tuple[bool, Optional[Path]]:
    """异步创建加密压缩包（内部在线程池中运行）"""
    return await asyncio.to_thread(_sync_create_encrypted_zip, comic_id, encrypt)

async def cleanup_files(comic_id: int, clear_dir: bool, clear_zip: bool):
    """清理文件夹和压缩包（将阻塞操作提交到线程池执行）"""
    comic_dir_path = album_path / str(comic_id)
    zip_file_path = album_path / f"{comic_id}.zip"

    def _cleanup():
        try:
            if clear_dir and comic_dir_path.exists() and comic_dir_path.is_dir():
                shutil.rmtree(comic_dir_path, ignore_errors=True)
                logger.debug(f"已删除漫画目录 {comic_dir_path}")

            if clear_zip and zip_file_path.exists() and zip_file_path.is_file():
                try:
                    zip_file_path.unlink()
                except Exception:
                    pass
                logger.debug(f"已删除压缩包 {zip_file_path}")

        except Exception as e:
            logger.error(f"清理文件失败: {str(e)}")

    await asyncio.to_thread(_cleanup)


def generate_album_message(album_details) -> str:
    """生成本子简介消息"""
    try:
        tags = album_details.tags if getattr(album_details, "tags", None) else []
        name = getattr(album_details, "name", "未知")
        description = getattr(album_details, "description", "")
        return f"""
=========={name}==========
简介：{description}
标签: [{'、'.join(tags)}]
"""
    except Exception as e:
        logger.error(f"生成简介消息失败: {str(e)}")
        return "漫画信息获取失败"


async def send_tease_message(command: BaseCommand, tease_type: str):
    """发送提示消息"""
    if tease_list := command.get_config(f"tease.{tease_type}", []):
        await command.send_text(random.choice(tease_list))


async def download_and_process_comic(comic_input, tag: str = None) -> Tuple[bool, Any, JmAlbumDetail|None]:
    """
    统一的漫画下载和处理逻辑
    Args:
        comic_input: 漫画ID或"random"
        tag: 可选的标签，用于随机漫画筛选
    Returns:
        Tuple[bool, Any, JmAlbumDetail]: (成功标志, 实际漫画ID或错误信息, 漫画详情对象)
    """
    async with DOWNLOAD_SEMAPHORE:
        try:
            option = get_option()
            client = option.new_jm_client()
            comic_id = comic_input

            # 获取漫画ID的逻辑
            if comic_input == "random":
                if tag:
                    aid_list = []
                    for p in range(1, 5):
                        page: JmCategoryPage = client.search_tag(tag, page=p)
                        aid_list += list(page.iter_id())
                    logger.debug(f"根据标签 '{tag}' 搜索到的最新漫画ID列表: {aid_list}")
                else:
                    page: JmCategoryPage = client.categories_filter(
                        page=random.randint(1, 3),
                        time=JmMagicConstants.TIME_WEEK,
                        category=JmMagicConstants.CATEGORY_ALL,
                        order_by=JmMagicConstants.ORDER_BY_VIEW,
                    )
                    aid_list = list(page.iter_id())

                if not aid_list:
                    return False, (f"未找到包含标签 '{tag}' 的漫画" if tag else "未找到漫画"), None
                comic_id = random.choice(aid_list)

            page = client.search_site(search_query=comic_id)
            album: JmAlbumDetail = page.single_album
            # 下载漫画（在后台线程执行，带超时）
            logger.info(f"开始下载漫画 {album}")
            # 检查目录或压缩包是否已存在，避免重复下载
            comic_dir = album_path / str(comic_id)
            zip_file_path = album_path / f"{comic_id}.zip"
            if (comic_dir.exists() and comic_dir.is_dir()) or (zip_file_path.exists() and zip_file_path.is_file()):
                logger.info(f"漫画ID {comic_id} 已存在，跳过下载")
                return True, comic_id, album
            try:
                download_task = asyncio.to_thread(download_album, comic_id, option)
                album_details, _ = await asyncio.wait_for(download_task, timeout=DOWNLOAD_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                logger.error(f"下载漫画ID {comic_id} 超时")
                # 尝试清理可能残留的目录
                await cleanup_files(comic_id, True, True)
                return False, "下载超时", None
            except Exception as e:
                logger.error(f"下载漫画失败（内部）: {str(e)}")
                await cleanup_files(comic_id, True, True)
                return False, f"下载漫画失败: {str(e)}", None

            return True, comic_id, album_details

        except Exception as e:
            logger.error(f"下载漫画失败: {str(e)}")
            return False, f"下载漫画失败: {str(e)}", None


# ================== 定时任务组件 ==================
class ScheduleSender:
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
        logger.info("定时随即漫画任务已停止")

    async def _schedule_loop(self):
        """定时发送循环"""
        time_table = self.plugin.get_config("schedule.time", ["23:00"])
        tag = self.plugin.get_config("schedule.tag", "")
        target_groups = self.plugin.get_config("schedule.target_group", [])
        target_users = self.plugin.get_config("schedule.target_user", [])
        # 配置检查
        if not time_table or (not target_groups and not target_users):
            logger.error("定时发送配置不完整，任务无法启动")
            self.is_running = False
            return
        while self.is_running:
            try:
                # 获取当前时间
                current_time = datetime.datetime.now().strftime("%H:%M")
                if current_time in time_table:
                    logger.info(f"当前时间 {current_time}，开始发送推荐漫画")

                    # 下载漫画（使用非阻塞实现）
                    success, comic_id, album_details = await download_and_process_comic("random", tag)
                    if not success:
                        logger.error(f"定时任务下载漫画失败: {comic_id}")
                        # 等待一会再重试，防止快速循环失败
                        await asyncio.sleep(10)
                        continue

                    message_text = generate_album_message(album_details)

                    # 压缩文件
                    encrypt = self.plugin.get_config("plugin.encrypt", "jmcomic")
                    zip_success, zip_file_path = await create_encrypted_zip(comic_id, encrypt)
                    if not zip_success:
                        logger.error("定时任务创建压缩包失败")
                        await cleanup_files(comic_id, True, False)
                        continue

                    # 上传并发送消息
                    for group_id in target_groups:
                        if await upload_file_by_id(zip_file_path.name, group_id=group_id):
                            logger.info(f"上传文件 {zip_file_path.name} 至群 {group_id} 成功")
                        else:
                            logger.error(f"上传文件 {zip_file_path.name} 至群 {group_id} 失败")
                        if await send_text_by_id(message_text, group_id=group_id):
                            logger.info(f"发送漫画信息至群 {group_id} 成功")
                        else:
                            logger.error(f"发送漫画信息至群 {group_id} 失败")
                        if tease_list := self.plugin.get_config("tease.schedule", []):
                            if await send_text_by_id(random.choice(tease_list), group_id=group_id):
                                logger.info(f"发送提示消息至群 {group_id} 成功")
                            else:
                                logger.error(f"发送提示消息至群 {group_id} 失败")

                    for user_id in target_users:
                        if await upload_file_by_id(zip_file_path.name, user_id=user_id):
                            logger.info(f"上传文件 {zip_file_path.name} 至用户 {user_id} 成功")
                        else:
                            logger.error(f"上传文件 {zip_file_path.name} 至用户 {user_id} 失败")
                        if await send_text_by_id(message_text, user_id=user_id):
                            logger.info(f"发送漫画信息至用户 {user_id} 成功")
                        else:
                            logger.error(f"发送漫画信息至用户 {user_id} 失败")
                        if tease_list := self.plugin.get_config("tease.schedule", []):
                            if await send_text_by_id(random.choice(tease_list), user_id=user_id):
                                logger.info(f"发送提示消息至用户 {user_id} 成功")
                            else:
                                logger.error(f"发送提示消息至用户 {user_id} 失败")
                    # 估算上传时间并等待
                    upload_time = max(os.path.getsize(zip_file_path) / (1 * 1024 * 1024), 3)  # 估算上传时间，最低3秒，假设1MB/s
                    await asyncio.sleep(upload_time)
                    # 清理文件
                    clear_dir = self.plugin.get_config("plugin.clear_dir", True)
                    clear_zip = self.plugin.get_config("plugin.clear_zip", True)
                    await cleanup_files(comic_id, clear_dir, clear_zip)

                    # 为避免多次发送，等待61秒
                    await asyncio.sleep(61)
                # 每分钟检查一次
                await asyncio.sleep(60)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"定时发送任务出错: {str(e)}")
                await self.stop()


# ================== Command组件 ==================
class JMComicCommand(BaseCommand):
    """JM下载Command - 响应/jm命令"""

    command_name = "jm"
    command_description = "根据指令下载漫画"

    command_pattern = r"^/jm\s+(?P<arg1>\S+)(?:\s+tag=(?P<tag>\S+))?$"
    command_help = r"""
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
        """检查qq号为qq_account的用户是否拥有权限"""
        permission_list = self.get_config("plugin.permission")
        permission_type = self.get_config("plugin.permission_type")
        logger.info(f'[{self.command_name}]{permission_type}:{str(permission_list)}')
        if permission_type == 'whitelist':
            return qq_account in permission_list
        elif permission_type == 'blacklist':
            return qq_account not in permission_list
        else:
            logger.error('permission_type错误，可能为拼写错误')
            return False

    async def execute(self) -> tuple[bool, Optional[str], bool]:
        # ---权限检查---
        user_id = self.message.message_info.user_info.user_id
        if not self.check_permission(user_id):
            logger.info(f"{user_id}无{self.command_name}权限")
            await send_tease_message(self, "no_permission")
            return False, f"{user_id}权限不足，无权使用命令", True
        else:
            logger.info(f"{user_id}拥有{self.command_name}权限")

        # ---参数解析---
        arg1 = self.matched_groups.get("arg1")
        tag = self.matched_groups.get("tag")
        comic_id = None

        # ---命令处理---
        if arg1 is None or arg1.lower() == "help":
            # 匹配/jm 和 /jm help
            await self.send_text(self.command_help)
            return False, "显示帮助信息", True

        elif arg1.isdigit():
            # 匹配/jm <漫画ID>
            comic_id = int(arg1)
            logger.info(f"用户{user_id}请求下载漫画ID {comic_id}")
            await self.send_text(f"开始下载漫画ID {comic_id} ...")
            success, result, album_details = await download_and_process_comic(comic_id, tag)

        elif arg1.lower() == "random":
            # 匹配/jm random [tag=<分类>]
            logger.info(f"用户{user_id}请求下载随机漫画 tag={tag}")
            await self.send_text(f"开始下载'{tag if tag else '随机'}'漫画 ...")
            success, result, album_details = await download_and_process_comic("random", tag)
            if success:
                comic_id = result  # 这里result是实际的comic_id

        else:
            await self.send_text(self.command_help)
            return False, "参数错误", True

        if not success or comic_id is None:
            await send_tease_message(self, "fail")
            return False, result, True

        # 发送漫画信息
        await self.send_text(generate_album_message(album_details))

        # ---压缩文件---
        encrypt = self.get_config("plugin.encrypt", "jmcomic")
        zip_success, zip_path = await create_encrypted_zip(comic_id, encrypt)

        if not zip_success:
            await send_tease_message(self, "fail")
            return False, "创建压缩包失败", True

        # ---上传压缩包---
        upload_success = await upload_file_by_stream(zip_path.name, self.message.chat_stream)
        if upload_success:
            await send_tease_message(self, "success")
        else:
            await send_tease_message(self, "fail")

        # ---清理文件---
        file_size = os.path.getsize(zip_path)
        upload_time = max(file_size / (1 * 1024 * 1024), 3)  # 估算上传时间，最低3秒，假设1MB/s
        await asyncio.sleep(upload_time)
        clear_dir = self.get_config("plugin.clear_dir", True)
        clear_zip = self.get_config("plugin.clear_zip", True)
        await cleanup_files(comic_id, clear_dir, clear_zip)

        return True, 'success', True


# ===== 插件注册 =====
@register_plugin
class JMComicPlugin(BasePlugin):
    """JMComic插件 - 提供下载漫画功能"""

    # 插件基本信息
    plugin_name: str = "jmcomic"  # 内部标识符
    enable_plugin: bool = True
    dependencies: List[str] = []  # 插件依赖列表
    python_dependencies: List[str] = ["jmcomic", "pyzipper", "httpx", "asyncio"]  # Python包依赖列表
    config_file_name: str = "config.toml"  # 配置文件名

    # 配置节描述
    config_section_descriptions = {"plugin": "插件基本信息", "tease": "响应命令的文本列表，从中随机调用，禁用请清空",
                                   "schedule": "每日漫画推荐"}

    # 配置Schema定义
    config_schema: dict = {
        "plugin": {
            "enable": ConfigField(type=bool, default=True, description="是否启用插件"),
            "encrypt": ConfigField(type=str, default="jmcomic", description="解压密码"),
            "clear_dir": ConfigField(type=bool, default=True, description="是否清理下载的漫画文件夹"),
            "clear_zip": ConfigField(type=bool, default=True, description="是否清理上传后压缩包"),
            "permission_type": ConfigField(type=str, default="whitelist", description="权限类型，whitelist或blacklist"),
            "permission": ConfigField(type=list, default=["114514", "1919810"], description="权限QQ号列表"),
        },
        "tease": {
            "success": ConfigField(type=list, default=["漫画到手喵~"],
                                   description="下载成功后发送的提醒"),
            "schedule": ConfigField(type=list, default=["luguanluguanlulushijiandaole", "每日推荐送达喵~"],
                                    description="每日推荐发送成功后发送的提醒"),
            "fail": ConfigField(type=list, default=["下载失败了喵~"],
                                description="下载失败后发送的提醒"),
            "no_permission": ConfigField(type=list,
                                         default=["不给你下哦~杂鱼杂鱼~你能拿我怎么办ww",
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

        if self.get_config("plugin.enable", True):
            self.enable_plugin = True

            if self.get_config("schedule.enable", False):
                self.scheduler = ScheduleSender(self)
                asyncio.create_task(self._start_scheduler_after_delay())
        else:
            self.enable_plugin = False

    async def _start_scheduler_after_delay(self):
        """延迟启动日程任务"""
        await asyncio.sleep(5)
        if self.scheduler:
            await self.scheduler.start()

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        return [
            (JMComicCommand.get_command_info(), JMComicCommand),
        ]
