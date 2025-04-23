from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin
import re
import time
import requests
from lxml import etree

from app.log import logger
from app.utils.string import StringUtils
from app.db.site_oper import SiteOper
from . import ISiteHandler

class ZmHandler(ISiteHandler):
    """
    Zm站点处理类
    """
    
    def __init__(self, site_info: dict):
        super().__init__(site_info)
        self.shoutbox_url = urljoin(self.site_url, "/shoutbox.php")
        self.messages_url = urljoin(self.site_url, "/messages.php")
        self.medal_url = urljoin(self.site_url, "/javaapi/user/drawMedalGroupReward?medalGroupId=3")
        self.siteoper = SiteOper()
        self._feedback_timeout = site_info.get("feedback_timeout", 5)  # 从配置中获取反馈超时时间，默认5秒
        self._last_message_result = None  # 初始化最后一次消息发送结果
        
    def match(self) -> bool:
        """
        判断是否为Zm站点
        :return: 是否匹配
        """
        return "织梦" in self.site_name.lower()

    def send_messagebox(self, messages: List[str] = None, callback=None, zm_stats: Dict = None) -> Tuple[bool, str]:
        """
        发送消息到喊话区并获取反馈
        :param messages: 消息内容列表
        :param callback: 回调函数
        :param zm_stats: 上传量历史记录
        :return: (是否成功, 结果信息)
        """
        try:
            if not messages:
                messages = []
            elif isinstance(messages, str):
                messages = [messages]
                
            result_list = []
            for message in messages:
                # 发送消息
                result = super().send_messagebox(message, lambda response: "")
                if not result[0]:
                    return False, "发送消息失败"
                    
                # 等待消息发送完成
                time.sleep(self._feedback_timeout)
                
                # 强制刷新页面
                refresh_response = self._send_get_request(self.site_url + "/index.php")
                if not refresh_response:
                    logger.error("刷新页面失败！")
                    continue
                    
                # 从刷新后的页面获取最新数据
                current_stats = self.get_user_stats()
                if not current_stats:
                    continue
                    
                # 处理不同类型的请求
                request_types = {
                    "求上传": "upload",
                    "求下载": "download",
                    "求电力": "bonus"
                }
                
                for request_text, request_type in request_types.items():
                    if request_text in message:
                        feedback = self._process_request(message, current_stats, zm_stats, request_type)
                        if feedback:
                            result_list.append(feedback)
                    
            # 保存结果
            self._last_message_result = "\n".join(result_list) if result_list else "消息发送成功"
            return True, self._last_message_result
            
        except Exception as e:
            logger.error(f"发送消息失败: {str(e)}")
            return False, str(e)
            
    def get_feedback(self, message: str = None) -> Optional[Dict]:
        """
        获取消息反馈
        :param message: 消息内容
        :return: 反馈信息字典
        """
        # 如果有最后一次消息发送结果,使用它
        if self._last_message_result:
            return {
                "site": self.site_name,
                "message": message,
                "rewards": [{
                    "type": "电力",
                    "description": self._last_message_result,
                    "amount": "",
                    "unit": "",
                    "is_negative": False
                }]
            }
            
        # 如果都没有,返回默认反馈
        return {
            "site": self.site_name,
            "message": message,
            "rewards": [{
                "type": "电力",
                "description": "消息发送成功",
                "amount": "",
                "unit": "",
                "is_negative": False
            }]
        }
    
    def _process_request(self, message: str, current_stats: Dict, zm_stats: Dict, request_type: str) -> Optional[str]:
        """
        处理单个请求并生成反馈消息
        :param message: 消息内容
        :param current_stats: 当前统计数据
        :param zm_stats: 历史统计数据
        :param request_type: 请求类型(upload/download/bonus)
        :return: 反馈消息或None
        """
        if not current_stats or not current_stats.get(request_type):
            return None
            
        if not zm_stats or not zm_stats.get(request_type):
            return None
            
        # 转换当前值和历史值
        def convert_value(value_str: str) -> float:
            match = re.match(r'([\d,.]+)\s*([TGM]B)?', value_str.strip())
            if not match:
                return 0.0
            value = float(match.group(1).replace(',', ''))
            unit = match.group(2)
            if unit == 'TB':
                return value * 1024
            elif unit == 'MB':
                return value / 1024
            return value
            
        current_value = convert_value(current_stats[request_type])
        history_value = convert_value(zm_stats[request_type])
        
        # 计算差值
        diff = current_value - history_value
        
        # 生成反馈消息
        if diff > 0:
            return f"皮总响应了你的请求，赠送你【{diff:.2f}{'GB' if request_type != 'bonus' else ''}{'上传量' if request_type == 'upload' else '下载量' if request_type == 'download' else '电力'}】"
        elif diff < 0:
            return f"皮总响应了你的请求，扣减你【{abs(diff):.2f}{'GB' if request_type != 'bonus' else ''}{'上传量' if request_type == 'upload' else '下载量' if request_type == 'download' else '电力'}】"
        else:
            return "皮总没有理你，明天再来吧"

    def get_latest_message_time(self) -> Optional[str]:
        """
        获取最新电力赠送邮件的完整时间值,优先获取未读邮件,如果没有则获取已读邮件
        :return: 最新邮件的时间字符串，格式如"2025-04-20 20:55:49"，如果获取失败则返回None
        """
        try:
            logger.info(f"开始获取站点 {self.site_name} 的最新电力赠送邮件时间...")
            
            # 自定义回调函数，提取邮件时间的title属性
            def extract_message_time(response):
                try:
                    logger.debug(f"开始解析响应内容...")
                    
                    # 解析HTML
                    html = etree.HTML(response.text)
                    
                    # 查找所有邮件行
                    message_rows = html.xpath("//tr[td[@class='rowfollow']]")
                    logger.debug(f"找到 {len(message_rows)} 个邮件")
                    
                    # 遍历邮件行,查找符合条件的邮件
                    for row in message_rows:
                        # 检查是否为电力赠送邮件
                        content = row.xpath(".//a[contains(text(), '收到来自 zmpt 赠送的')]")
                        if not content:
                            continue
                            
                        # 提取时间值
                        time_span = row.xpath(".//span[@title]")
                        if time_span:
                            time_value = time_span[0].get("title")
                            if time_value:
                                # 检查是否为未读邮件
                                unread = row.xpath(".//img[@class='unreadpm']")
                                if unread:
                                    logger.debug(f"找到未读电力赠送邮件时间: {time_value}")
                                    return time_value
                                else:
                                    # 如果是已读邮件,保存时间值
                                    return time_value
                    
                    logger.debug("未找到符合条件的邮件")
                    return None
                    
                except Exception as e:
                    logger.error(f"提取邮件时间失败: {str(e)}")
                    return None
            
            # 调用基类方法获取邮件列表
            latest_time = super().get_message_list(rt_method=extract_message_time)
            
            if latest_time:
                return latest_time
                
            logger.warning("未获取到符合条件的邮件时间")
            return None
            
        except Exception as e:
            logger.error(f"获取最新电力赠送邮件时间失败: {str(e)}")
            return None
    
    def medal_bonus(self) -> Tuple[bool, str]:
        """
        领取勋章奖励
        :return: (是否成功, 结果信息)
        """
        try:
            # 发送POST请求获取勋章奖励
            response = requests.post(
                self.medal_url,
                headers=self.headers
            )
            
            if response.status_code != 200:
                logger.error(f"获取勋章奖励失败: HTTP {response.status_code}")
                return False, f"HTTP错误: {response.status_code}"
                
            # 解析返回数据
            data = response.json()
            if data.get("errorCode") != 0:
                error_msg = data.get("errorMsg", "未知错误")
                logger.error(f"获取勋章奖励失败: {error_msg}")
                return False, error_msg
                
            # 获取奖励信息
            result = data.get("result", {})
            if not result:
                logger.error("获取勋章奖励失败: 返回数据格式错误")
                return False, "返回数据格式错误"
                
            # 提取奖励金额和电力值
            reward = result.get("rewardAmount", 0)
            seed_bonus = result.get("seedBonus", "0")
            
            # 记录成功日志
            success_msg = f"梅兰竹菊成套勋章奖励: {reward}, 总电力: {seed_bonus}"
            logger.info(success_msg)
            return True, success_msg
            
        except requests.exceptions.RequestException as e:
            logger.error(f"请求勋章奖励失败: {str(e)}")
            return False, f"请求失败: {str(e)}"
        except Exception as e:
            logger.error(f"处理勋章奖励失败: {str(e)}")
            return False, f"处理失败: {str(e)}"

    def get_username(self) -> Optional[str]:
        """
        获取用户名
        :return: 用户名或None
        """
        site_name = self.site_name
        site_domain = StringUtils.get_url_domain(self.site_url)
        
        try:
            user_data_list = self.siteoper.get_userdata_latest()
            for user_data in user_data_list:
                if user_data.domain == site_domain:
                    logger.info(f"站点: {user_data.name}, 用户名: {user_data.username}")
                    return user_data.username
            
            logger.warning(f"未找到站点 {site_name} 的用户信息")
            return None
        except Exception as e:
            logger.error(f"获取站点 {site_name} 的用户信息失败: {str(e)}")
            return None

    def get_user_stats(self) -> Dict[str, Optional[str]]:
        """
        获取用户数据统计信息(上传量、下载量、电力值)
        :return: 包含用户统计数据的字典,格式如:
        {
            "upload": "2.608 TB",
            "download": "801.65 GB", 
            "bonus": "261,865.1"
        }
        """
        try:
            # 获取首页内容
            response = self._send_get_request(self.site_url + "/index.php")
            if not response:
                logger.error("获取首页内容失败")
                return {}
            
            # 解析HTML
            html = etree.HTML(response.text)
            
            # 初始化结果字典
            stats = {
                "upload": None,
                "download": None,
                "bonus": None
            }
            
            try:
                # 提取上传量
                # 使用更精确的xpath表达式
                upload_info = html.xpath("//font[contains(text(), '上传量')]/following-sibling::text()[1]")
                if upload_info:
                    # 清理数据
                    upload_value = upload_info[0].strip()
                    if upload_value:
                        stats["upload"] = upload_value
                    else:
                        logger.warning("上传量数据为空")
                
                # 提取下载量
                download_info = html.xpath("//font[contains(text(), '下载量')]/following-sibling::text()[1]")
                if download_info:
                    # 清理数据
                    download_value = download_info[0].strip()
                    if download_value:
                        stats["download"] = download_value
                    else:
                        logger.warning("下载量数据为空")
                
                # 提取电力值
                bonus_info = html.xpath("//a[@id='self_bonus']/text()[last()]")
                if bonus_info:
                    # 清理数据
                    bonus = bonus_info[0].strip().replace(",", "")
                    if bonus:
                        stats["bonus"] = bonus
                    else:
                        logger.warning("电力值数据为空")
                
            except Exception as parse_error:
                logger.error(f"解析数据时出错: {str(parse_error)}")
                return {}
            
            # 验证数据完整性
            if all(stats.values()):
                logger.info(f"成功获取用户统计数据: 上传={stats['upload']}, "
                           f"下载={stats['download']}, 电力值={stats['bonus']}")
            else:
                missing = [k for k, v in stats.items() if not v]
                if missing:
                    logger.warning(f"部分数据未获取到: {', '.join(missing)}")
            
            return stats
            
        except Exception as e:
            logger.error(f"获取用户统计数据失败: {str(e)}")
            return {}