# coding=utf-8
"""旧版落盘命令行入口。

此模块保留 Excel、媒体、info.json 和 detail.txt 输出能力，FastAPI 服务不会导入它。
"""
import json
import os

from loguru import logger

from dy_apis.douyin_api import DouyinAPI
from utils.common_util import init
from utils.data_util import download_work, handle_work_info, save_to_xlsx


class DataSpider:
    def __init__(self):
        self.douyin_apis = DouyinAPI()

    def spider_work(self, auth, work_url: str, proxies=None):
        """爬取单个作品并返回标准化信息。"""
        response = self.douyin_apis.get_work_info(auth, work_url)
        work_info = handle_work_info(response['aweme_detail'])
        logger.info(f'爬取作品信息 {work_url}')
        return work_info

    def spider_some_work(self, auth, works: list, base_path: dict, save_choice: str,
                         excel_name: str = '', proxies=None):
        """爬取作品列表并按旧版配置落盘。"""
        if save_choice in ('all', 'excel') and not excel_name:
            raise ValueError('excel_name 不能为空')
        work_list = [self.spider_work(auth, work_url) for work_url in works]
        for work_info in work_list:
            if save_choice == 'all' or 'media' in save_choice:
                download_work(work_info, base_path['media'], save_choice)
        if save_choice in ('all', 'excel'):
            file_path = os.path.abspath(os.path.join(base_path['excel'], f'{excel_name}.xlsx'))
            save_to_xlsx(work_list, file_path)
        return work_list

    def _spider_user_works(self, auth, user_url: str, work_list: list, base_path: dict,
                           save_choice: str, excel_name: str = '') -> list:
        user_info = self.douyin_apis.get_user_info(auth, user_url)
        work_info_list = []
        logger.info(f'用户 {user_url} 作品数量: {len(work_list)}')
        if save_choice in ('all', 'excel') and not excel_name:
            excel_name = user_url.split('/')[-1].split('?')[0]

        for work in work_list:
            # 合并主页信息，使导出字段更完整
            work['author'].update(user_info['user'])
            work_info = handle_work_info(work)
            work_info_list.append(work_info)
            logger.info(f'爬取作品信息 {work_info["work_url"]}')
            if save_choice == 'all' or 'media' in save_choice:
                download_work(work_info, base_path['media'], save_choice)
        if save_choice in ('all', 'excel'):
            file_path = os.path.abspath(os.path.join(base_path['excel'], f'{excel_name}.xlsx'))
            save_to_xlsx(work_info_list, file_path)
        return work_info_list

    def spider_user_all_work(self, auth, user_url: str, base_path: dict, save_choice: str,
                             excel_name: str = '', proxies=None) -> list:
        """爬取用户全部作品并按旧版配置落盘。"""
        works = self.douyin_apis.get_user_all_work_info(auth, user_url)
        return self._spider_user_works(
            auth, user_url, works, base_path, save_choice, excel_name,
        )

    def spider_user_some_work(self, auth, user_url: str, page_num: int, base_path: dict,
                              save_choice: str = 'excel', excel_name: str = '') -> list:
        """按指定页数爬取用户作品并按旧版配置落盘。"""
        works = self.douyin_apis.get_user_some_work_info(auth, user_url, page_num)
        return self._spider_user_works(
            auth, user_url, works, base_path, save_choice, excel_name,
        )

    def spider_some_search_work(self, auth, query: str, require_num: int, base_path: dict,
                                save_choice: str, sort_type: str, publish_time: str,
                                filter_duration='', search_range='', content_type='',
                                excel_name: str = '', proxies=None) -> list:
        """搜索指定数量作品并按旧版配置落盘。"""
        works = self.douyin_apis.search_some_general_work(
            auth, query, require_num, sort_type, publish_time,
            filter_duration, search_range, content_type,
        )
        logger.info(f'搜索关键词 {query} 作品数量: {len(works)}')
        work_info_list = []
        for work in works:
            logger.info(json.dumps(work, ensure_ascii=False))
            work_info = handle_work_info(work['aweme_info'])
            work_info_list.append(work_info)
            if save_choice == 'all' or 'media' in save_choice:
                download_work(work_info, base_path['media'], save_choice)
        if save_choice in ('all', 'excel'):
            file_path = os.path.abspath(os.path.join(base_path['excel'], f'{excel_name or query}.xlsx'))
            save_to_xlsx(work_info_list, file_path)
        return work_info_list


# 保留旧入口使用过的类名，方便已有脚本迁移
Data_Spider = DataSpider


if __name__ == '__main__':
    # 保留原工作区中的用户作品抓取示例；只有显式运行本文件才会落盘
    auth, paths = init()
    spider = DataSpider()
    target_user_url = (
        'https://www.douyin.com/user/'
        'MS4wLjABAAAAHDPNA7vKh585sLPZ7Wpn5X40NKPHvBzHAhavS0g2hKM?from_tab_name=main'
    )
    spider.spider_user_all_work(auth, target_user_url, paths, 'all')
