# coding=utf-8
import asyncio
import os

from loguru import logger

from dy_apis.login_api import DYLoginApi


async def qrcode_login(timeout: int = 180,
                       qrcode_path: str = 'datas/douyin_login_qrcode.png',
                       headless: bool = False):
    """打开抖音登录页，扫码成功后将凭证保存到 .env。"""
    login_api = DYLoginApi()
    auth = await login_api.login_grab_ticket(
        headless=headless,
        timeout=timeout,
        qrcode_path=os.path.abspath(qrcode_path),
    )
    credential_path = login_api.save_credential(auth)
    logger.info(f'扫码登录成功，凭证已保存至 {credential_path}')
    return auth


if __name__ == '__main__':
    # 超时时间可按需要调整，单位为秒
    asyncio.run(qrcode_login(
        timeout=180,
        qrcode_path='datas/douyin_login_qrcode.png',
        headless=False,  # Linux 服务器请通过 xvfb-run 提供虚拟显示
    ))
