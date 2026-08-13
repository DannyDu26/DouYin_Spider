'''
Author: duyulin@kingsoft.com
Date: 2026-07-29 10:48:13
LastEditors: duyulin@kingsoft.com
LastEditTime: 2026-07-29 11:36:11
FilePath: \DouYin_Spider\test_spider_user_some_work.py
Description: 

'''
# coding=utf-8
import os

from loguru import logger

from dy_apis.douyin_api import DouyinAPI
from utils.common_util import init
from utils.data_util import download_work, handle_work_info, save_to_xlsx


def spider_user_some_work(auth, user_url: str, page_num: int, base_path: dict,
                          save_choice: str = 'excel', excel_name: str = '') -> list:
    """按页数爬取用户的部分作品并保存。"""
    douyin_api = DouyinAPI()
    user_info = douyin_api.get_user_info(auth, user_url)
    work_list = douyin_api.get_user_some_work_info(auth, user_url, page_num)
    work_info_list = []
    logger.info(f'用户 {user_url} 爬取页数: {page_num}，获取作品数量: {len(work_list)}')

    if (save_choice == 'all' or save_choice == 'excel') and not excel_name:
        excel_name = user_url.split('/')[-1].split('?')[0]

    for work_info in work_list:
        # 合并主页信息，使导出的用户字段更加完整
        work_info['author'].update(user_info['user'])
        handled_work = handle_work_info(work_info)
        work_info_list.append(handled_work)
        logger.info(f'爬取作品信息 {handled_work["work_url"]}')
        if save_choice == 'all' or 'media' in save_choice:
            download_work(handled_work, base_path['media'], save_choice)

    if save_choice == 'all' or save_choice == 'excel':
        file_path = os.path.abspath(os.path.join(base_path['excel'], f'{excel_name}.xlsx'))
        save_to_xlsx(work_info_list, file_path)

    return work_info_list


if __name__ == '__main__':
    auth, base_path = init()
    user_url = 'https://www.douyin.com/user/MS4wLjABAAAAHDPNA7vKh585sLPZ7Wpn5X40NKPHvBzHAhavS0g2hKM?from_tab_name=main'
    page_num = 2  # 需要爬取的作品页数
    spider_user_some_work(auth, user_url, page_num, base_path, save_choice='excel')
