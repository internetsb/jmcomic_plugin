import asyncio
import random
import datetime

import pyzipper
import httpx
from pathlib import Path

from jmcomic import *
from src.common.logger import get_logger
from src.plugin_system import (
    BasePlugin,
    register_plugin,
    BaseCommand,
    ComponentInfo,
    ConfigField,
)

logger = get_logger("jmcomic.plugin")
plugin_path = Path(__file__).parent.resolve()
album_path = plugin_path / "albums"
album_path.mkdir(parents=True, exist_ok=True)


# ================== 工具函数 ==================
async def send_text_to_target(host: str, port: int, napcat_token: str, message: str, user_id: str = None,
                              group_id: str = None) -> bool:
    """
    发送文本消息到指定用户或QQ群
    Args:
        host: Napcat服务地址
        port: Napcat服务端口
        napcat_token: Napcat认证Token
        message: 要发送的文本消息
        user_id: 目标用户QQ号（私聊）（与group_id二选一）
        group_id: 目标QQ群号（群聊）（与user_id二选一）
    Returns:
        发送成功返回True，失败返回False
    """
    payload = {
        "message": [
            {
                "type": "text",
                "data": {
                    "text": message
                }
            }
        ]
    }
    if group_id:
        payload["group_id"] = group_id
        url = f"http://{host}:{port}/send_group_msg"
    else:
        payload["user_id"] = user_id
        url = f"http://{host}:{port}/send_private_msg"
    headers = {'Content-Type': 'application/json'}
    if napcat_token:
        headers["Authorization"] = f"Bearer {napcat_token}"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, timeout=60)
        if response.status_code == 200:
            if response.json().get("retcode") == 0:
                logger.info(f"发送文本消息成功")
                return True
            else:
                logger.error(f"发送文本消息失败: {response.json().get('message')}")
                return False
        else:
            logger.error(f"发送文本消息异常: {response.text}")
            return False
    except Exception as e:
        logger.error(f"发送文本消息异常: {str(e)}")
        return False


async def upload_file(command: BaseCommand, file_name: str) -> bool:
    """上传图片目录下名为{file_name}文件到接收命令的QQ群或私聊"""
    file_path = str(album_path / file_name)
    group_info = command.message.chat_stream.group_info
    user_info = command.message.chat_stream.user_info
    if not group_info and not user_info:
        logger.error("无法获取上传目标，既不是群聊也不是私聊")
        return False
    group_id = group_info.group_id if group_info else None
    user_id = user_info.user_id if user_info else None

    host = command.get_config("plugin.host", "127.0.0.1")
    port = command.get_config("plugin.port", 9999)
    napcat_token = command.get_config("plugin.napcat_token", "")
    if group_id is not None:
        url = f"http://{host}:{port}/upload_group_file"
        payload = {
            "group_id": group_id,
            "file": file_path,
            "name": file_name,
        }
    else:
        url = f"http://{host}:{port}/upload_private_file"
        payload = {
            "user_id": user_id,
            "file": file_path,
            "name": file_name,
        }
    headers = {'Content-Type': 'application/json'}
    if napcat_token:
        headers["Authorization"] = f"Bearer {napcat_token}"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, timeout=60)
        if response.status_code == 200:
            if response.json().get("retcode") == 0:
                logger.info(f"上传文件 {file_name} 成功")
                return True
            else:
                logger.error(f"上传文件 {file_name} 失败: {response.json().get('message')}")
                return False
        else:
            logger.error(f"上传文件 {file_name} 异常: {response.text}")
            return False
    except Exception as e:
        logger.error(f"上传文件 {file_name} 异常: {str(e)}")
        return False


def get_option() -> JmOption:
    """获取下载选项配置"""
    option = create_option_by_file(str(plugin_path / "option.yml"))
    option.dir_rule.base_dir = str(plugin_path / "albums")  # 设置下载目录
    return option


def create_encrypted_zip(comic_id: int, encrypt: str) -> tuple[bool, None] | tuple[bool, Path]:
    """创建加密压缩包"""
    comic_dir_path = album_path / str(comic_id)
    zip_file_path = album_path / f"{comic_id}.zip"

    if not comic_dir_path.exists() or not comic_dir_path.is_dir():
        logger.error(f"漫画目录 {comic_dir_path} 不存在或不是文件夹")
        return False, None

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
        return False, None


async def cleanup_files(comic_id: int, clear_dir: bool, clear_zip: bool):
    """清理文件"""
    comic_dir_path = album_path / str(comic_id)
    zip_file_path = album_path / f"{comic_id}.zip"

    try:
        if clear_dir and comic_dir_path.exists() and comic_dir_path.is_dir():
            for root, dirs, files in os.walk(comic_dir_path, topdown=False):
                for file in files:
                    (Path(root) / file).unlink()
                for dire in dirs:
                    (Path(root) / dire).rmdir()
            comic_dir_path.rmdir()
            logger.debug(f"已删除漫画目录 {comic_dir_path}")

        if clear_zip and zip_file_path.exists() and zip_file_path.is_file():
            zip_file_path.unlink()
            logger.debug(f"已删除压缩包 {zip_file_path}")

    except Exception as e:
        logger.error(f"清理文件失败: {str(e)}")


def generate_album_message(album_details) -> str:
    """生成专辑信息消息"""
    return f"==========\n{album_details.name}\n==========\n简介：{album_details.description}\n标签: [{'、'.join(album_details.tags)}]"


async def send_tease_message(command: BaseCommand, tease_type: str):
    """发送调侃消息"""
    if tease_list := command.get_config(f"tease.{tease_type}", []):
        await command.send_text(random.choice(tease_list))


async def download_and_process_comic(comic_input, tag: str = None) -> Tuple[bool, any, any]:
    """统一的漫画下载和处理逻辑"""
    try:
        option = get_option()
        client = option.new_jm_client()
        comic_id = comic_input

        # 获取漫画ID的逻辑
        if comic_input == "random":
            if tag:
                page: JmCategoryPage = client.search_tag(tag, page=1)
                aid_list = list(page.iter_id())
            else:
                page: JmCategoryPage = client.categories_filter(
                    page=random.randint(1, 3),
                    time=JmMagicConstants.TIME_WEEK,
                    category=JmMagicConstants.CATEGORY_ALL,
                    order_by=JmMagicConstants.ORDER_BY_VIEW,
                )
                aid_list = list(page.iter_id())

            if not aid_list:
                return False, f"未找到包含标签 '{tag}' 的漫画" if tag else "未找到漫画", None
            comic_id = random.choice(aid_list)
        # 下载漫画
        logger.info(f"开始下载漫画ID {comic_id}")
        album_details, _ = download_album(comic_id, option)
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

                    # 下载漫画
                    success, comic_id, album_details = await download_and_process_comic("random", tag)
                    if not success:
                        logger.error(f"定时任务下载漫画失败: {comic_id}")
                        continue

                    message_text = generate_album_message(album_details)

                    # 压缩文件
                    encrypt = self.plugin.get_config("plugin.encrypt", "jmcomic")
                    zip_success, zip_file_path = create_encrypted_zip(comic_id, encrypt)
                    if not zip_success:
                        logger.error("定时任务创建压缩包失败")
                        continue

                    # 上传文件
                    host = self.plugin.get_config("plugin.host", "127.0.0.1")
                    port = self.plugin.get_config("plugin.port", 9999)
                    napcat_token = self.plugin.get_config("plugin.napcat_token", "")

                    # 发送至目标群
                    for group_id in target_groups:
                        # 上传文件
                        url = f"http://{host}:{port}/upload_group_file"
                        headers = {'Content-Type': 'application/json'}
                        if napcat_token:
                            headers["Authorization"] = f"Bearer {napcat_token}"

                        payload = {
                            "group_id": group_id,
                            "file": str(zip_file_path),
                            "name": zip_file_path.name,
                        }
                        try:
                            async with httpx.AsyncClient() as client:
                                response = await client.post(url, json=payload, headers=headers, timeout=60)
                            if response.status_code == 200 and response.json().get("retcode") == 0:
                                logger.info(f"上传文件 {zip_file_path.name} 至群 {group_id} 成功")
                            else:
                                logger.error(f"上传文件 {zip_file_path.name} 至群 {group_id} 失败")
                        except Exception as e:
                            logger.error(f"上传文件 {zip_file_path.name} 至群 {group_id} 异常: {str(e)}")

                        # 发送文本消息
                        await send_text_to_target(host, port, napcat_token, message_text, group_id=group_id)
                        if tease_list := self.plugin.get_config("tease.schedule", []):
                            await send_text_to_target(host, port, napcat_token, random.choice(tease_list),
                                                      group_id=group_id)

                    # 发送至目标用户
                    for user_id in target_users:
                        # 上传文件
                        url = f"http://{host}:{port}/upload_private_file"
                        headers = {'Content-Type': 'application/json'}
                        if napcat_token:
                            headers["Authorization"] = f"Bearer {napcat_token}"

                        payload = {
                            "user_id": user_id,
                            "file": str(zip_file_path),
                            "name": zip_file_path.name,
                        }
                        try:
                            async with httpx.AsyncClient() as client:
                                response = await client.post(url, json=payload, headers=headers, timeout=60)
                            if response.status_code == 200 and response.json().get("retcode") == 0:
                                logger.info(f"上传文件 {zip_file_path.name} 至用户 {user_id} 成功")
                            else:
                                logger.error(f"上传文件 {zip_file_path.name} 至用户 {user_id} 失败")
                        except Exception as e:
                            logger.error(f"上传文件 {zip_file_path.name} 至用户 {user_id} 异常: {str(e)}")

                        # 发送文本消息
                        await send_text_to_target(host, port, napcat_token, message_text, user_id=user_id)
                        if tease_list := self.plugin.get_config("tease.schedule", []):
                            await send_text_to_target(host, port, napcat_token, random.choice(tease_list),
                                                      user_id=user_id)

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
            success, result, album_details = await download_and_process_comic(comic_id, tag)

        elif arg1.lower() == "random":
            # 匹配/jm random [tag=<分类>]
            logger.info(f"用户{user_id}请求下载随机漫画 tag={tag}")
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
        zip_success, zip_path = create_encrypted_zip(comic_id, encrypt)

        if not zip_success:
            await send_tease_message(self, "fail")
            return False, "创建压缩包失败", True

        # ---上传压缩包---
        upload_success = await upload_file(self, zip_path.name)
        if upload_success:
            await send_tease_message(self, "success")
        else:
            await send_tease_message(self, "fail")

        # ---清理文件---
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
            "host": ConfigField(type=str, default="127.0.0.1", description="Napcat服务地址"),
            "port": ConfigField(type=int, default=9999, description="Napcat服务端口"),
            "napcat_token": ConfigField(type=str, default="", description="Napcat认证Token"),
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
