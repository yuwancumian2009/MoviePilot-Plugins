from abc import ABC, abstractmethod
from typing import Dict, List, Optional
from app.log import logger
from app.utils.http import RequestUtils
from app.core.config import settings

class BaseMedalSiteHandler(ABC):
    """勋章站点处理器基类"""
    
    def __init__(self):
        self._timeout = 30
        self._retry_times = 3
        self._retry_interval = 5
        self._use_proxy = False  # 默认禁用代理

    @abstractmethod
    def match(self, site) -> bool:
        """判断是否适配该站点"""
        pass

    @abstractmethod
    def fetch_medals(self, site) -> List[Dict]:
        """获取并格式化勋章数据"""
        pass

    def _request_with_retry(self, url: str, cookies: str = None, **kwargs) -> Optional[Dict]:
        """带重试机制的请求方法"""
        req_kwargs = {
            'timeout': self._timeout,
            'headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
        }
        
        # 只在明确需要代理时使用
        if self._use_proxy and hasattr(settings, 'PROXY'):
            req_kwargs['proxies'] = settings.PROXY
            
        # 设置 cookies
        if cookies:
            req_kwargs['cookies'] = cookies

        for i in range(self._retry_times):
            try:
                res = RequestUtils(**req_kwargs).get_res(
                    url=url,
                    **kwargs
                )
                if res and res.status_code == 200:
                    return res
                if i < self._retry_times - 1:
                    logger.warning(f"第{i+1}次请求失败，{self._retry_interval}秒后重试...")
                    import time
                    time.sleep(self._retry_interval)
            except Exception as e:
                if i < self._retry_times - 1:
                    logger.warning(f"第{i+1}次请求异常：{str(e)}，{self._retry_interval}秒后重试...")
                    time.sleep(self._retry_interval)
                else:
                    raise e
        return None

    def _format_medal_data(self, medal: Dict) -> Dict:
        """统一格式化勋章数据"""
        return {
            'name': medal.get('name', ''),           # 勋章名称
            'description': medal.get('description', ''),  # 勋章描述
            'imageSmall': medal.get('imageSmall', ''),   # 勋章图片
            'saleBeginTime': medal.get('saleBeginTime', ''), # 销售开始时间
            'saleEndTime': medal.get('saleEndTime', ''),  # 销售结束时间
            'price': medal.get('price', 0),          # 勋章价格
            'site': medal.get('site', ''),           # 所属站点
            'validity': medal.get('validity', ''),    # 有效期
            'bonus_rate': medal.get('bonus_rate', ''), # 加成比例
            'purchase_status': medal.get('purchase_status', ''), # 购买状态
            'gift_status': medal.get('gift_status', ''), # 赠送状态
            'stock': medal.get('stock', ''),         # 库存数量
        } 