from typing import Dict, List
from datetime import datetime, timedelta
import pytz
from app.log import logger
from app.core.config import settings
from .base import BaseMedalSiteHandler

class ZmMedalHandler(BaseMedalSiteHandler):
    """织梦站点勋章处理器"""
    
    def match(self, site) -> bool:
        """判断是否为织梦站点"""
        site_name = site.name.lower()
        site_url = site.url.lower()
        return "zm" in site_name or "织梦" in site_name or "zm" in site_url

    def fetch_medals(self, site) -> List[Dict]:
        """获取织梦站点勋章数据"""
        try:
            site_name = site.name
            site_url = site.url
            site_cookie = site.cookie
            
            # 发送请求获取勋章数据
            res = self._request_with_retry(
                url=f"{site_url}/javaapi/user/queryAllMedals",
                cookies=site_cookie
            )
            
            if not res:
                logger.error(f"请求勋章接口失败！站点：{site_name}")
                return []
                
            # 处理勋章数据
            data = res.json().get('result', {})
            medal_groups = data.get('medalGroups', [])
            medals = data.get('medals', [])
            
            # 用于去重的集合
            processed_medals = set()
            all_medals = []
            
            # 处理独立勋章
            for medal in medals:
                medal_data = self._process_medal(medal, site_name)
                medal_key = f"{medal_data['name']}_{site_name}"
                if medal_key not in processed_medals:
                    processed_medals.add(medal_key)
                    all_medals.append(medal_data)
            
            # 处理分组勋章
            for group in medal_groups:
                for medal in group.get('medalList', []):
                    medal_data = self._process_medal(medal, site_name)
                    medal_key = f"{medal_data['name']}_{site_name}"
                    if medal_key not in processed_medals:
                        processed_medals.add(medal_key)
                        all_medals.append(medal_data)
            
            return all_medals
            
        except Exception as e:
            logger.error(f"处理织梦站点勋章数据时发生错误: {str(e)}")
            return []

    def _process_medal(self, medal: Dict, site_name: str) -> Dict:
        """处理单个勋章数据"""
        try:
            has_medal = medal.get('hasMedal', False)
            image_small = medal.get('imageSmall', '')
            price = medal.get('price', 0)
            name = medal.get('name', '')
            sale_begin_time = medal.get('saleBeginTime', '')
            sale_end_time = medal.get('saleEndTime', '')
            
            # 确定购买状态
            if has_medal:
                purchase_status = '已经购买'
            elif self._is_current_time_in_range(sale_begin_time, sale_end_time):
                purchase_status = '购买'
            else:
                purchase_status = '未到可购买时间'
            
            # 格式化勋章数据
            return self._format_medal_data({
                'name': name,
                'imageSmall': image_small,
                'saleBeginTime': sale_begin_time,
                'saleEndTime': sale_end_time,
                'price': price,
                'site': site_name,
                'purchase_status': purchase_status,
            })
            
        except Exception as e:
            logger.error(f"处理勋章数据时发生错误: {str(e)}")
            return self._format_medal_data({
                'name': medal.get('name', '未知勋章'),
                'imageSmall': medal.get('imageSmall', ''),
                'site': site_name,
                'purchase_status': '未知状态',
            })

    def _is_current_time_in_range(self, start_time: str, end_time: str) -> bool:
        """判断当前时间是否在给定的时间范围内"""
        try:
            # 处理空值
            if not start_time or not end_time:
                logger.debug(f"时间值为空: start_time={start_time}, end_time={end_time}")
                return True
                
            # 处理"~"分隔符
            if "~" in start_time:
                start_time = start_time.split("~")[0].strip()
            if "~" in end_time:
                end_time = end_time.split("~")[1].strip()
                
            # 处理"不限"的情况
            if "不限" in start_time or "不限" in end_time:
                logger.debug(f"时间包含'不限': start_time={start_time}, end_time={end_time}")
                return True
                
            # 清理时间字符串
            start_time = start_time.strip()
            end_time = end_time.strip()
            
            # 处理空字符串
            if not start_time or not end_time:
                logger.debug(f"清理后时间值为空: start_time={start_time}, end_time={end_time}")
                return True
                
            # 尝试解析时间
            try:
                # 使用系统时区
                current_time = datetime.now(pytz.timezone(settings.TZ))
                start_datetime = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.timezone(settings.TZ))
                end_datetime = datetime.strptime(end_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.timezone(settings.TZ))
                
                # 添加时间容差(5分钟)
                time_tolerance = timedelta(minutes=5)
                return (start_datetime - time_tolerance) <= current_time <= (end_datetime + time_tolerance)
                
            except ValueError as e:
                logger.warning(f"时间格式解析失败: {e}, start_time={start_time}, end_time={end_time}")
                return True
                
        except Exception as e:
            logger.error(f"解析时间范围时发生错误: {e}, start_time={start_time}, end_time={end_time}")
            return True 