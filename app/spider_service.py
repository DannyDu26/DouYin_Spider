# coding=utf-8
import math
import os
import threading
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from loguru import logger

from app.account_pool import NoAvailableAccountError
from dy_apis.douyin_api import DouyinAPI, DouyinAuthenticationError, DouyinRiskControlError
from utils.data_util import handle_work_info


class UpstreamServiceError(RuntimeError):
    """抖音上游请求整体失败。"""

    def __init__(self, message: str, details=None):
        super().__init__(message)
        self.message = message
        self.details = details


class AccountPinningDisabledError(RuntimeError):
    """服务未启用测试账号定向能力。"""


@dataclass(frozen=True)
class _StaticLease:
    account_id: str
    auth: object
    row_id: int = 0

    @property
    def credential_id(self):
        return self.row_id


class _StaticAccountPool:
    """兼容旧调用与单元测试的单账号池。"""

    def __init__(self, auth):
        self.auth = auth

    @contextmanager
    def acquire(self, exclude=None, timeout=None, account_id=None):
        if (
                self.auth is None
                or 'default' in (exclude or set())
                or account_id not in (None, 'default')
        ):
            raise NoAvailableAccountError()
        yield _StaticLease('default', self.auth)

    def mark_auth_failure(self, account_id, credential_id=None, credential_auth=None):
        return None

    def mark_risk_control(self, account_id, credential_id=None, credential_auth=None):
        return None

    def retry_after_seconds(self):
        return None

    def stats(self):
        count = 1 if self.auth is not None else 0
        return {'total': count, 'available': count, 'cooling': 0, 'invalid': 0}

    def list_accounts(self):
        if self.auth is None:
            return []
        return [{
            'account_id': 'default',
            'credential_id': 0,
            'updated_at': None,
            'status': 'available',
            'cooldown_until': None,
        }]


class SpiderService:
    """使用账号池抓取并只返回内存数据。"""

    def __init__(
            self,
            auth=None,
            max_concurrent: int = 2,
            douyin_api=None,
            account_pool=None,
            account_acquire_timeout_seconds: float | None = None,
            test_account_pinning_enabled: bool | None = None,
    ):
        if max_concurrent <= 0:
            raise ValueError('max_concurrent 必须是正整数')
        if account_acquire_timeout_seconds is None:
            account_acquire_timeout_seconds = self._positive_timeout_from_env(
                'ACCOUNT_ACQUIRE_TIMEOUT_SECONDS', 30.0
            )
        if not math.isfinite(account_acquire_timeout_seconds) or account_acquire_timeout_seconds <= 0:
            raise ValueError('account_acquire_timeout_seconds 必须是有限正数')
        self.max_concurrent = max_concurrent
        self.account_acquire_timeout_seconds = float(account_acquire_timeout_seconds)
        self.douyin_api = douyin_api or DouyinAPI()
        self.account_pool = account_pool or _StaticAccountPool(auth)
        if test_account_pinning_enabled is None:
            test_account_pinning_enabled = self._boolean_from_env(
                'ENABLE_TEST_ACCOUNT_PINNING', False
            )
        self.test_account_pinning_enabled = bool(test_account_pinning_enabled)
        # 全局闸门限制服务总并发，账号池另行限制单账号并发
        self._semaphore = threading.BoundedSemaphore(max_concurrent)

    @staticmethod
    def _positive_timeout_from_env(name: str, default: float) -> float:
        """读取队列等待超时，防止请求无限占用工作线程。"""
        try:
            value = float(os.getenv(name, str(default)))
        except ValueError as error:
            raise RuntimeError(f'{name} 必须是有限正数') from error
        if not math.isfinite(value) or value <= 0:
            raise RuntimeError(f'{name} 必须是有限正数')
        return value

    @staticmethod
    def _boolean_from_env(name: str, default: bool) -> bool:
        """读取显式布尔开关，避免非空字符串被误判为开启。"""
        raw_value = os.getenv(name)
        if raw_value is None:
            return default
        normalized = raw_value.strip().lower()
        if normalized in {'1', 'true', 'yes', 'on'}:
            return True
        if normalized in {'0', 'false', 'no', 'off'}:
            return False
        raise RuntimeError(f'{name} 必须是布尔值')

    @staticmethod
    def _safe_error(error: Exception) -> str:
        """只暴露异常类型，避免上游请求 URL 中的令牌进入日志或响应。"""
        return error.__class__.__name__

    @staticmethod
    def _safe_url_for_log(value: str) -> str:
        """日志只保留主机、路径及数字作品 ID，丢弃查询凭证。"""
        try:
            parsed = urlsplit(value)
            safe_url = f'{parsed.scheme}://{parsed.hostname or ""}{parsed.path}'
            modal_ids = parse_qs(parsed.query).get('modal_id', [])
            if len(modal_ids) == 1 and modal_ids[0].isdigit():
                safe_url = f'{safe_url}?modal_id={modal_ids[0]}'
            return safe_url
        except (TypeError, ValueError):
            return '<invalid-url>'

    def _pool_retry_after(self) -> int | None:
        """兼容注入账号池，并返回安全的最早恢复时间。"""
        getter = getattr(self.account_pool, 'retry_after_seconds', None)
        return getter() if callable(getter) else None

    def _execute_with_failover(
            self,
            operation,
            request_id: str,
            target_account_id: str | None = None,
    ) -> dict:
        if target_account_id is not None and not self.test_account_pinning_enabled:
            raise AccountPinningDisabledError()
        excluded = set()
        acquired = self._semaphore.acquire(timeout=self.account_acquire_timeout_seconds)
        if not acquired:
            raise NoAvailableAccountError()
        try:
            max_attempts = 1 if target_account_id is not None else 2
            for failover_count in range(max_attempts):
                try:
                    acquire_kwargs = {
                        'exclude': excluded,
                        'timeout': self.account_acquire_timeout_seconds,
                    }
                    if target_account_id is not None:
                        acquire_kwargs['account_id'] = target_account_id
                    with self.account_pool.acquire(**acquire_kwargs) as lease:
                        try:
                            result = operation(lease.auth)
                        except DouyinAuthenticationError:
                            # 在释放账号槽位前完成版本校验和冷却，关闭并发竞态窗口
                            cooldown_until = self.account_pool.mark_auth_failure(
                                lease.account_id,
                                lease.credential_id,
                                lease.auth,
                            )
                            if cooldown_until is not None:
                                excluded.add(lease.account_id)
                            logger.warning(
                                '[{}] 账号认证失效 account_id={} credential_id={} cooled={} failover_count={}',
                                request_id,
                                lease.account_id,
                                lease.credential_id,
                                cooldown_until is not None,
                                failover_count,
                            )
                            if target_account_id is not None or failover_count == max_attempts - 1:
                                raise NoAvailableAccountError(self._pool_retry_after())
                            continue
                        except DouyinRiskControlError:
                            # 风控只标记实际租用账号，避免账号池继续分配该账号。
                            marker = getattr(self.account_pool, 'mark_risk_control', None)
                            if callable(marker):
                                marker(lease.account_id, lease.credential_id, lease.auth)
                            raise
                except NoAvailableAccountError as error:
                    if error.retry_after_seconds is not None:
                        raise
                    raise NoAvailableAccountError(self._pool_retry_after()) from error

                result['account_id'] = lease.account_id
                result['failover_count'] = failover_count
                return result
        finally:
            self._semaphore.release()

        raise NoAvailableAccountError(self._pool_retry_after())

    def get_works(self, urls: list[str], request_id: str) -> dict:
        def operation(auth):
            items = []
            errors = []
            for work_url in urls:
                safe_work_url = self._safe_url_for_log(work_url)
                try:
                    response = self.douyin_api.get_work_info(auth, work_url)
                    detail = response.get('aweme_detail') if isinstance(response, dict) else None
                    if not isinstance(detail, dict):
                        raise ValueError('上游响应缺少 aweme_detail')
                    item = handle_work_info(detail)
                    items.append(item)
                    logger.info('[{}] 抓取作品成功 url={}', request_id, safe_work_url)
                except (DouyinAuthenticationError, DouyinRiskControlError):
                    raise
                except Exception as error:
                    error_type = self._safe_error(error)
                    errors.append({'url': safe_work_url, 'error': error_type})
                    logger.error('[{}] 抓取作品失败 url={} error={}', request_id, safe_work_url, error_type)

            if not items:
                raise UpstreamServiceError('所有作品均抓取失败', errors)
            return {
                'items': items,
                'errors': errors,
                'total': len(urls),
                'success_count': len(items),
                'failed_count': len(errors),
            }

        return self._execute_with_failover(operation, request_id)

    def get_work_comments(
            self,
            url: str,
            cursor: int,
            count: int,
            request_id: str,
    ) -> dict:
        """分页抓取视频一级评论，不执行任何本地落盘。"""
        safe_work_url = self._safe_url_for_log(url)

        def operation(auth):
            try:
                response = self.douyin_api.get_work_out_comment(
                    auth,
                    url,
                    str(cursor),
                    str(count),
                )
                if not isinstance(response, dict) or 'comments' not in response:
                    raise ValueError('上游响应缺少 comments')
                comments = response.get('comments')
                if comments is None:
                    comments = []
                if not isinstance(comments, list):
                    raise ValueError('上游评论列表格式错误')

                raw_next_cursor = response.get('cursor', cursor)
                if isinstance(raw_next_cursor, bool):
                    raise ValueError('上游评论游标格式错误')
                if isinstance(raw_next_cursor, int):
                    next_cursor = raw_next_cursor
                elif isinstance(raw_next_cursor, str) and raw_next_cursor.isdigit():
                    next_cursor = int(raw_next_cursor)
                else:
                    raise ValueError('上游评论游标格式错误')
                if next_cursor < 0:
                    raise ValueError('上游评论游标格式错误')

                raw_has_more = response.get('has_more', 0)
                if isinstance(raw_has_more, bool):
                    has_more = raw_has_more
                elif isinstance(raw_has_more, int) and raw_has_more in (0, 1):
                    has_more = raw_has_more == 1
                elif isinstance(raw_has_more, str) and raw_has_more in ('0', '1'):
                    has_more = raw_has_more == '1'
                else:
                    raise ValueError('上游评论分页标记格式错误')
                items = [comment for comment in comments if isinstance(comment, dict)]
            except (DouyinAuthenticationError, DouyinRiskControlError):
                raise
            except Exception as error:
                error_type = self._safe_error(error)
                logger.error(
                    '[{}] 抓取视频评论失败 url={} cursor={} count={} error={}',
                    request_id,
                    safe_work_url,
                    cursor,
                    count,
                    error_type,
                )
                raise UpstreamServiceError('视频评论抓取失败') from error

            logger.info(
                '[{}] 视频评论抓取完成 url={} cursor={} next_cursor={} total={} has_more={}',
                request_id,
                safe_work_url,
                cursor,
                next_cursor,
                len(items),
                has_more,
            )
            return {
                'items': items,
                'total': len(items),
                'work_url': safe_work_url,
                'cursor': cursor,
                'next_cursor': next_cursor,
                'has_more': has_more,
            }

        return self._execute_with_failover(operation, request_id)

    def get_video_sub_comments(
            self,
            video_id: str,
            comment_id: str,
            cursor: int,
            count: int,
            request_id: str,
    ) -> dict:
        """分页抓取一级评论下的二级评论。"""
        comment = {'aweme_id': video_id, 'cid': comment_id}

        def operation(auth):
            try:
                response = self.douyin_api.get_work_inner_comment(
                    auth,
                    comment,
                    str(cursor),
                    str(count),
                )
                if not isinstance(response, dict) or 'comments' not in response:
                    raise ValueError('上游响应缺少 comments')
                comments = response.get('comments')
                if comments is None:
                    comments = []
                if not isinstance(comments, list):
                    raise ValueError('上游二级评论列表格式错误')

                raw_next_cursor = response.get('cursor', cursor)
                if isinstance(raw_next_cursor, bool):
                    raise ValueError('上游二级评论游标格式错误')
                if isinstance(raw_next_cursor, int):
                    next_cursor = raw_next_cursor
                elif isinstance(raw_next_cursor, str) and raw_next_cursor.isdigit():
                    next_cursor = int(raw_next_cursor)
                else:
                    raise ValueError('上游二级评论游标格式错误')
                if next_cursor < 0:
                    raise ValueError('上游二级评论游标格式错误')

                raw_has_more = response.get('has_more', 0)
                if isinstance(raw_has_more, bool):
                    has_more = raw_has_more
                elif isinstance(raw_has_more, int) and raw_has_more in (0, 1):
                    has_more = raw_has_more == 1
                elif isinstance(raw_has_more, str) and raw_has_more in ('0', '1'):
                    has_more = raw_has_more == '1'
                else:
                    raise ValueError('上游二级评论分页标记格式错误')
                items = [reply for reply in comments if isinstance(reply, dict)]
            except (DouyinAuthenticationError, DouyinRiskControlError):
                raise
            except Exception as error:
                error_type = self._safe_error(error)
                logger.error(
                    '[{}] 抓取二级评论失败 video_id={} comment_id={} cursor={} count={} error={}',
                    request_id,
                    video_id,
                    comment_id,
                    cursor,
                    count,
                    error_type,
                )
                raise UpstreamServiceError('二级评论抓取失败') from error

            logger.info(
                '[{}] 二级评论抓取完成 video_id={} comment_id={} cursor={} next_cursor={} total={} has_more={}',
                request_id,
                video_id,
                comment_id,
                cursor,
                next_cursor,
                len(items),
                has_more,
            )
            return {
                'items': items,
                'total': len(items),
                'video_id': video_id,
                'comment_id': comment_id,
                'cursor': cursor,
                'next_cursor': next_cursor,
                'has_more': has_more,
            }

        return self._execute_with_failover(operation, request_id)

    def get_user_works(
            self,
            user_url: str | None,
            page_num: int,
            request_id: str,
            user_id: str | None = None,
    ) -> dict:
        def operation(auth):
            try:
                resolved_user_url = user_url
                if resolved_user_url is None:
                    # 用户作品接口直接使用主页路径中的 sec_user_id。
                    resolved_user_url = f'https://www.douyin.com/user/{user_id}'

                user_response = self.douyin_api.get_user_info(auth, resolved_user_url)
                user = user_response.get('user') if isinstance(user_response, dict) else None
                if not isinstance(user, dict):
                    raise ValueError('上游响应缺少 user')
                works = self.douyin_api.get_user_some_work_info(auth, resolved_user_url, page_num)
                if not isinstance(works, list):
                    raise ValueError('上游作品列表格式错误')

                items = []
                for work in works:
                    if not isinstance(work, dict):
                        continue
                    # 复制原始数据，避免修改上游返回对象
                    merged_work = deepcopy(work)
                    author = merged_work.get('author')
                    if not isinstance(author, dict):
                        author = {}
                    author.update(user)
                    merged_work['author'] = author
                    item = handle_work_info(merged_work)
                    items.append(item)
                    logger.info('[{}] 抓取用户作品成功 url={}', request_id, item['work_url'])
            except (DouyinAuthenticationError, DouyinRiskControlError):
                raise
            except Exception as error:
                error_type = self._safe_error(error)
                logger.error(
                    '[{}] 抓取用户作品失败 url={} error={}',
                    request_id,
                    self._safe_url_for_log(user_url or f'https://www.douyin.com/user/{user_id}'),
                    error_type,
                )
                raise UpstreamServiceError('用户作品抓取失败') from error

            logger.info('[{}] 用户作品抓取完成 pages={} total={}', request_id, page_num, len(items))
            return {
                'items': items,
                'total': len(items),
                'user_url': resolved_user_url,
                'page_num': page_num,
            }

        return self._execute_with_failover(operation, request_id)

    def search_works(self, request_data, request_id: str) -> dict:
        def operation(auth):
            try:
                search_result = self.douyin_api.search_some_general_work(
                    auth,
                    request_data.query,
                    request_data.limit,
                    request_data.sort_type,
                    request_data.publish_time,
                    request_data.filter_duration,
                    request_data.search_range,
                    request_data.content_type,
                    True,
                )
                if isinstance(search_result, dict):
                    works = search_result.get('items')
                    has_more = search_result.get('has_more')
                    raw_page_counts = search_result.get('raw_page_counts')
                    if not isinstance(has_more, bool):
                        raise ValueError('上游搜索 has_more 格式错误')
                    if (not isinstance(raw_page_counts, list)
                            or any(not isinstance(count, int) or isinstance(count, bool) or count < 0
                                   for count in raw_page_counts)):
                        raise ValueError('上游搜索每页原始数量格式错误')
                else:
                    # 兼容仍返回列表的旧版底层实现。
                    works = search_result
                    has_more = False
                    raw_page_counts = []
                if not isinstance(works, list):
                    raise ValueError('上游搜索列表格式错误')

                items = []
                for work in works:
                    aweme_info = work.get('aweme_info') if isinstance(work, dict) else None
                    if not isinstance(aweme_info, dict):
                        continue
                    item = handle_work_info(aweme_info)
                    items.append(item)
                    logger.info('[{}] 搜索作品成功 url={}', request_id, item['work_url'])
            except (DouyinAuthenticationError, DouyinRiskControlError):
                raise
            except Exception as error:
                error_type = self._safe_error(error)
                logger.error(
                    '[{}] 搜索作品失败 query_length={} error={}',
                    request_id,
                    len(request_data.query),
                    error_type,
                )
                raise UpstreamServiceError('作品搜索失败') from error

            logger.info(
                '[{}] 搜索作品完成 query_length={} total={}',
                request_id,
                len(request_data.query),
                len(items),
            )
            return {
                'items': items,
                'total': len(items),
                'query': request_data.query,
                'has_more': has_more,
                'raw_page_counts': raw_page_counts,
            }

        return self._execute_with_failover(
            operation,
            request_id,
            target_account_id=getattr(request_data, 'target_account_id', None),
        )

    def account_stats(self) -> dict:
        return self.account_pool.stats()

    @property
    def max_concurrent_per_account(self) -> int:
        """返回账号池实际采用的单账号并发上限。"""
        return int(getattr(self.account_pool, 'max_concurrent_per_account', self.max_concurrent))

    def list_accounts(self) -> list[dict]:
        return self.account_pool.list_accounts()
