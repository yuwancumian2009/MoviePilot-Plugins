from typing import Dict, Optional, Tuple
from urllib.parse import urljoin

from app.log import logger
from app.db.site_oper import SiteOper
from . import ISiteHandler

class VicomoHandler(ISiteHandler):
    """
    Vicomo站点处理类
    """
    
    def __init__(self, site_info: dict):
        super().__init__(site_info)
        self.shoutbox_url = urljoin(self.site_url, "/shoutbox.php")
        self.messages_url = urljoin(self.site_url, "/messages.php")
        self.siteoper = SiteOper()
        
    def match(self) -> bool:
        """
        判断是否为Vicomo站点
        """
        site_name = self.site_name.lower()
        return "象站" in site_name
        
    def send_messagebox(self, message: str) -> Tuple[bool, str]:
        """
        发送消息到喊话区并获取反馈
        :param message: 消息内容
        :return: (是否成功, 结果信息)
        """
        try:
            # 发送消息
            result = super().send_messagebox(message, lambda response: "")
            if not result[0]:
                return False, "发送消息失败"
                
            # 获取消息列表
            message_list = self.get_message_list()
            if not message_list:
                return False, "获取消息列表失败"
                
            # 获取反馈消息
            feedback_message = message_list[1].get("topic", "") if len(message_list) > 1 else ""
            
            # 将消息标记为已读
            if len(message_list) > 1:
                self.set_message_read(message_list[1].get("id", ""))
                
            # 保存结果，使用站内信格式
            self._last_message_result = feedback_message
            return True, feedback_message
            
        except Exception as e:
            logger.error(f"发送消息失败: {str(e)}")
            return False, str(e)
            
    def get_feedback(self, message: str = None) -> Optional[Dict]:
        """
        获取站点反馈
        :param message: 发送的消息内容(可选)
        :return: 反馈信息字典
        """
        # 如果有最后一次消息发送结果,使用它
        if self._last_message_result:
            return {
                "site": self.site_name,
                "message": message,
                "rewards": [{
                    "type": "象草",
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
                "type": "象草",
                "description": "消息发送成功",
                "amount": "",
                "unit": "",
                "is_negative": False
            }]
        }
