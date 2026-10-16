-- 先在目标数据库执行；现有备注内容保持完整。
ALTER TABLE crawler_cookie
    MODIFY COLUMN remark TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
    NULL COMMENT '备注或平台登录状态JSON';
