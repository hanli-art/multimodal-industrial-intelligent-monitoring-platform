import hashlib
import secrets

import db

# ==================== 建表 SQL ====================
SCHEMA_SQL = [
    """CREATE TABLE IF NOT EXISTS devices (
        id INT AUTO_INCREMENT PRIMARY KEY,
        code VARCHAR(50) NOT NULL UNIQUE COMMENT '设备编号',
        name VARCHAR(100) NOT NULL COMMENT '设备名称',
        type VARCHAR(50) DEFAULT '' COMMENT '设备类型',
        location VARCHAR(200) DEFAULT '' COMMENT '安装位置',
        workshop VARCHAR(100) DEFAULT '' COMMENT '所属车间',
        ip VARCHAR(50) DEFAULT '' COMMENT 'IP地址',
        online_status TINYINT DEFAULT 0 COMMENT '在线状态 0离线 1在线',
        ai_enabled TINYINT DEFAULT 1 COMMENT 'AI开关 0关 1开',
        last_heartbeat DATETIME DEFAULT NULL COMMENT '最后心跳时间（R6 在线状态模拟）',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间'
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='设备表'""",
    """CREATE TABLE IF NOT EXISTS alarms (
        id INT AUTO_INCREMENT PRIMARY KEY,
        alarm_time DATETIME NOT NULL COMMENT '告警时间',
        location VARCHAR(200) DEFAULT '' COMMENT '设备/点位',
        violation_type VARCHAR(50) NOT NULL COMMENT '违规类型',
        level TINYINT NOT NULL COMMENT '告警等级 1一级 2二级 3三级',
        confidence FLOAT DEFAULT 0 COMMENT '置信度',
        summary TEXT COMMENT '告警摘要',
        image_path VARCHAR(300) DEFAULT '' COMMENT '抓拍图路径',
        status VARCHAR(20) DEFAULT '待处理' COMMENT '处理状态',
        handler VARCHAR(50) DEFAULT '' COMMENT '处理人',
        remark VARCHAR(500) DEFAULT '' COMMENT '备注',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间'
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='告警表'""",
    """CREATE TABLE IF NOT EXISTS evidences (
        id INT AUTO_INCREMENT PRIMARY KEY,
        alarm_id INT DEFAULT NULL COMMENT '关联告警ID',
        image_path VARCHAR(300) NOT NULL COMMENT '图片路径',
        ev_time DATETIME NOT NULL COMMENT '取证时间',
        location VARCHAR(200) DEFAULT '' COMMENT '点位',
        violation_type VARCHAR(50) DEFAULT '' COMMENT '违规类型',
        confidence FLOAT DEFAULT 0 COMMENT '置信度',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间'
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='取证表'""",
    """CREATE TABLE IF NOT EXISTS users (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(50) NOT NULL UNIQUE COMMENT '账号',
        password_hash VARCHAR(128) NOT NULL COMMENT '密码哈希',
        role VARCHAR(20) NOT NULL COMMENT '角色',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间'
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户表'""",
    """CREATE TABLE IF NOT EXISTS op_logs (
        id INT AUTO_INCREMENT PRIMARY KEY,
        op_time DATETIME NOT NULL COMMENT '操作时间',
        username VARCHAR(50) DEFAULT '' COMMENT '操作用户',
        op_type VARCHAR(50) DEFAULT '' COMMENT '操作类型',
        detail TEXT COMMENT '详情',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间'
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='操作日志表'""",
    """CREATE TABLE IF NOT EXISTS config (
        id INT AUTO_INCREMENT PRIMARY KEY,
        cfg_key VARCHAR(100) NOT NULL UNIQUE COMMENT '配置键',
        cfg_value VARCHAR(500) DEFAULT '' COMMENT '配置值',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间'
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='系统配置表'""",
]


def migrate():
    """对已存在的旧库补齐后加字段（幂等，可重复执行）"""
    cols = {r['Field'] for r in db.query('SHOW COLUMNS FROM devices')}
    if 'last_heartbeat' not in cols:
        db.execute(
            "ALTER TABLE devices ADD COLUMN last_heartbeat DATETIME DEFAULT NULL "
            "COMMENT '最后心跳时间（R6 在线状态模拟）'"
        )
        print('[migrate] devices 表已补充 last_heartbeat 列')


def migrate_users():
    """R8：把 R8 之前写入的裸 sha256 密码升级为加盐哈希格式"""
    legacy = hashlib.sha256('admin123'.encode('utf-8')).hexdigest()
    rows = db.query("SELECT id, username, password_hash FROM users WHERE password_hash NOT LIKE '%$%'")
    for row in rows:
        # 只认种子账号 admin 的已知默认密码，用户自改过的密码无从推断明文，保持原样
        if row['username'] == 'admin' and row['password_hash'] == legacy:
            db.execute('UPDATE users SET password_hash = %s WHERE id = %s',
                       (hash_password('admin123', secrets.token_hex(8)), row['id']))
            print('[migrate] admin 密码已升级为加盐哈希')


def hash_password(password, salt):
    """加盐哈希，存储格式 salt$sha256(salt+password)，与 server.py 保持一致"""
    return f'{salt}${hashlib.sha256((salt + password).encode("utf-8")).hexdigest()}'


def seed_users():
    """三种角色各一个演示账号（幂等，已存在则跳过）"""
    accounts = [
        ('admin', 'admin123', '超级管理员'),
        ('safety', 'safety123', '安全管理员'),
        ('viewer', 'viewer123', '查看员'),
    ]
    for username, password, role in accounts:
        if db.query('SELECT id FROM users WHERE username = %s', (username,)):
            continue
        db.execute(
            'INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s)',
            (username, hash_password(password, secrets.token_hex(8)), role)
        )
        print(f'[seed] 已创建演示账号 {username}（{role}）')


def seed_devices():
    if db.query('SELECT id FROM devices LIMIT 1'):
        return
    rows = [
        ('DEV-001', '车间大门摄像头', '摄像头', '1号车间东门', '1号车间', '192.168.1.101', 1, 1),
        ('DEV-002', '仓库通道摄像头', '摄像头', '原料仓库A通道', '仓库区', '192.168.1.102', 1, 1),
        ('DEV-003', '装卸区摄像头', '摄像头', '装卸平台', '装卸区', '192.168.1.103', 0, 0),
    ]
    for r in rows:
        db.execute(
            'INSERT INTO devices (code, name, type, location, workshop, ip, online_status, ai_enabled) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s, %s)', r
        )


def init():
    db.ensure_database()
    for sql in SCHEMA_SQL:
        db.execute(sql)
    migrate()
    migrate_users()
    seed_devices()
    seed_users()


if __name__ == '__main__':
    init()
    print('数据库初始化完成：6 张表 + 种子数据已就绪')
