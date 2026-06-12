-- 选股系统独立建库 + 专用账号脚本（与已有 Node 项目 MySQL 共存，互不影响）
-- 用法（在服务器上以有权限的账号执行）：
--   mysql -uroot -p < sql/init_database.sql
-- 执行前请把下面的 'CHANGE_ME_strong_password' 改成你的强密码。

-- 1. 建独立库（已存在则跳过，不影响其它库）
CREATE DATABASE IF NOT EXISTS trade_analyze
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- 2. 建专用账号（仅能访问 trade_analyze 库，最小权限）
CREATE USER IF NOT EXISTS 'ta_app'@'localhost'
  IDENTIFIED BY 'CHANGE_ME_strong_password';
GRANT ALL PRIVILEGES ON trade_analyze.* TO 'ta_app'@'localhost';
FLUSH PRIVILEGES;

-- 3. 建表（schema.sql 里的表都带 IF NOT EXISTS，安全幂等）
USE trade_analyze;
SOURCE sql/schema.sql;
