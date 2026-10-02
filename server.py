from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query, Request
from pydantic import BaseModel, Field
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response

import base64
import csv
import hashlib
import hmac
import random
import secrets
from io import BytesIO, StringIO
from PIL import Image
import numpy as np
import cv2
from ultralytics import YOLO

import dashscope
from dashscope import MultiModalConversation

from concurrent.futures import ThreadPoolExecutor
import asyncio
import time
import logging
import os
from datetime import datetime, timedelta

import db  # import 时加载 .env，供下方 os.getenv 读取

# ========== 日志配置 ==========
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)

# ========== 阿里云千问 API KEY（仅从环境变量读取）==========
dashscope.api_key = os.getenv('DASHSCOPE_API_KEY')
if not dashscope.api_key:
    logging.warning('未检测到环境变量 DASHSCOPE_API_KEY，/ws/qwen 大模型分析将不可用；'
                    '请在 .env 或系统环境变量中配置后重启。')

# ========== 通义千问模型名（可在 .env 中通过 QWEN_MODEL 配置）==========
QWEN_MODEL = os.getenv('QWEN_MODEL', 'qwen3.8-omni-flash')

# ========== 加载 YOLO 模型 ==========
print('[init] 正在加载 YOLO 模型...')
model = YOLO('./yolov8m.pt')
print('[init] 模型加载完成')

# 线程池：YOLO 推理 + 千问调用都放这里，避免阻塞事件循环
executor = ThreadPoolExecutor(max_workers=4)


# ==================== WebSocket 连接管理 ====================
class ConnectionManager:
    def __init__(self):
        self.activate_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.activate_connections.append(websocket)
        logging.info(f'websocket已连接：当前链接数:{len(self.activate_connections)}')

    def disconnect(self, websocket: WebSocket):
        if websocket in self.activate_connections:
            self.activate_connections.remove(websocket)
        logging.info(f'websocket已断开：当前链接数:{len(self.activate_connections)}')

    async def send_personal_message(self, message: dict, websocket: WebSocket):
        await websocket.send_json(message)


manager = ConnectionManager()


# ==================== 告警广播连接管理（R5） ====================
class NotifyManager:
    """维护 /ws/notify 的在线前端连接，把新告警实时广播给所有订阅端"""

    def __init__(self):
        self.connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.connections.append(websocket)
        logging.info(f'告警推送通道已连接：当前订阅数:{len(self.connections)}')

    def disconnect(self, websocket: WebSocket):
        if websocket in self.connections:
            self.connections.remove(websocket)
        logging.info(f'告警推送通道已断开：当前订阅数:{len(self.connections)}')

    async def broadcast(self, message: dict):
        """逐个推送，推失败的连接直接剔除"""
        dead = []
        for ws in list(self.connections):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


notify_manager = NotifyManager()


# ==================== 请求体 ====================
class DetectRequest(BaseModel):
    base64_image: str = Field(..., min_length=100, description='Base64格式图片')


# ==================== 工具 1：base64 → OpenCV 帧 ====================
def base64_to_frame(base64_image: str):
    """返回 (frame, error_msg)，成功时 error_msg=None"""
    if not base64_image.startswith('data:image/'):
        return None, '图片格式错误，并非Base64图片格式'

    try:
        base64_part, base64_data = base64_image.split(',', 1)
    except ValueError:
        return None, 'base64图片数据格式提取出错'

    if len(base64_part) < 2:
        return None, 'base64图片数据格式提取出错'
    if not base64_data:
        return None, 'base64图片数据为空'

    try:
        image_data = base64.b64decode(base64_data)
    except base64.binascii.Error as e:
        return None, f'base64数据解码错误: {str(e)}'

    try:
        image = Image.open(BytesIO(image_data))
        if image.mode not in ['RGB', 'RGBA', 'L']:
            image = image.convert('RGB')
        frame = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
        return frame, None
    except Exception as e:
        return None, f'图片解析失败: {str(e)}'


# ==================== 工具 2：YOLO 检测 ====================
def run_yolo(frame, conf=0.5):
    """同步函数，跑 YOLO，返回检测结果列表；conf 由系统配置传入，调整后即时生效"""
    results = model.predict(frame, conf=conf, classes=[0, 2, 7], verbose=False)

    detections = []
    if hasattr(results[0], 'boxes') and results[0].boxes is not None:
        for box in results[0].boxes:
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            conf = round(float(box.conf[0]), 2)
            cls_id = int(box.cls[0])
            class_name = model.names[cls_id]
            detections.append({
                'x1': x1, 'y1': y1, 'x2': x2, 'y2': y2,
                'conf': conf, 'class_name': class_name
            })

    return detections


# ==================== 工具 3：千问大模型调用（同步，放线程池） ====================
def call_qianwen_model(base64_data: str) -> str:
    """
    入参 base64_data 必须是纯 base64 数据（不带 data:image/...;base64, 前缀）
    返回: 模型分析的文本结果
    """
    try:
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "image": f"data:image/jpeg;base64,{base64_data}",
                    },
                    {
                        "text": """请分析这张图片，检测是否存在以下违规行为：
1. 抽烟（有人手持香烟/吸烟）
2. 未戴安全帽（工地场景中人员未佩戴安全帽）
3. 打架斗殴（工地上人员聚众闹事，打架）
4. 发生火灾（在生产车间或者工地上出现火苗）
5. 攀爬围墙（有人爬墙发生安全隐患）

请用简洁的中文描述检测结果，格式示例：
- 无违规行为
- 检测到抽烟行为
- 检测到未戴安全帽行为
- 检测到有人打架斗殴行为
- 检测到发生火灾
- 检测到有人爬墙
- 检测到抽烟行为和未戴安全帽行为"""
                    }
                ]
            }
        ]

        response = MultiModalConversation.call(
            model=QWEN_MODEL,
            messages=messages,
            result_format='messages',
            stream=False,
            temperature=0.01
        )

        if response.status_code == 200:
            content = response.output.choices[0].message.content
            if isinstance(content, list):
                text = ''
                for item in content:
                    if isinstance(item, dict) and 'text' in item:
                        text += item['text']
                return text.strip()
            return str(content).strip()
        else:
            logging.error(f'千问大模型调用失败: {response.code} - {response.message}')
            return f'大模型分析失败: {response.message}'

    except Exception as e:
        logging.error(f'千问大模型调用异常: {str(e)}')
        return f'大模型分析失败: {str(e)}'


async def call_qianwen_model_async(base64_data: str) -> str:
    """把同步的千问调用丢到线程池"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, call_qianwen_model, base64_data)


# ==================== 工具 4：从 DataURL 提取纯 base64 ====================
def extract_pure_base64(base64_image: str) -> str:
    """从 data:image/xxx;base64,xxxx 中提取 xxxx；如果没有前缀，原样返回"""
    if base64_image.startswith('data:image/'):
        try:
            return base64_image.split(',', 1)[1]
        except (ValueError, IndexError):
            return ''
    return base64_image


# ==================== 告警入库（R2） ====================
# 违规类型 → 告警等级（1 一级 / 2 二级 / 3 三级）
VIOLATION_LEVEL = {
    '火灾': 1,
    '打架斗殴': 1,
    '攀爬围墙': 1,
    '抽烟': 1,
    '未戴安全帽': 2,
}

# ==================== 系统配置（R9） ====================
# 默认值仅作兜底，实际以 config 表为准；键名与 init_db.py 的 DEFAULT_CONFIG 保持一致
CONFIG_DEFAULTS = {
    'yolo_enabled': '1',            # YOLO 目标检测总开关
    'qwen_enabled': '1',            # 千问大模型分析总开关
    'violation_抽烟': '1',
    'violation_未戴安全帽': '1',
    'violation_打架斗殴': '1',
    'violation_火灾': '1',
    'violation_攀爬围墙': '1',
    'yolo_conf': '0.5',             # YOLO 置信度阈值 0.3~0.9
    'alarm_debounce': '30',         # 同类型告警防抖秒数
}
# 违规类型 → 识别开关的配置键
VIOLATION_SWITCH_KEY = {v: f'violation_{v}' for v in VIOLATION_LEVEL}

# 防抖时间戳：同一违规类型在该时长内不重复入库
_last_alarm_ts = {}


def get_config():
    """每次调用都重新读 config 表，页面改完配置后检测链路即时生效，无需重启"""
    cfg = dict(CONFIG_DEFAULTS)
    for row in db.query('SELECT cfg_key, cfg_value FROM config'):
        if row['cfg_key'] in CONFIG_DEFAULTS:
            cfg[row['cfg_key']] = row['cfg_value']
    return cfg


def enabled_violations(cfg):
    """当前开启识别的违规类型集合"""
    return {v for v in VIOLATION_LEVEL if cfg.get(VIOLATION_SWITCH_KEY[v], '1') == '1'}


def save_alarm(violations, summary, pure_b64, debounce_seconds):
    """把违规结果写入 alarms 表并抓拍存证，同一类型 debounce_seconds 秒内防抖；返回本轮新建的告警列表"""
    now = time.time()
    created = []
    for v in violations:
        if now - _last_alarm_ts.get(v, 0) < debounce_seconds:
            continue
        _last_alarm_ts[v] = now
        level = VIOLATION_LEVEL.get(v, 2)
        alarm_time = datetime.now()
        alarm_time_str = alarm_time.strftime('%Y-%m-%d %H:%M:%S')
        alarm_id = db.execute(
            'INSERT INTO alarms (alarm_time, location, violation_type, level, confidence, summary, status) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s)',
            (alarm_time_str, '', v, level, 0, summary, '待处理')
        )
        image_path = save_evidence(alarm_id, v, alarm_time, pure_b64)
        db.execute('UPDATE alarms SET image_path = %s WHERE id = %s', (image_path, alarm_id))
        created.append({
            'id': alarm_id,
            'alarm_time': alarm_time_str,
            'location': '',
            'violation_type': v,
            'level': level,
            'confidence': 0,
            'summary': summary,
            'status': '待处理',
            'image_path': image_path,
        })
    return created


def save_evidence(alarm_id, violation_type, alarm_time, pure_b64):
    """把抓拍帧落盘并写 evidences 表，返回图片相对路径"""
    date_dir = alarm_time.strftime('%Y%m%d')
    filename = f"{alarm_time.strftime('%Y%m%d_%H%M%S')}_{violation_type}.jpg"
    rel_dir = f'evidence/{date_dir}'
    os.makedirs(rel_dir, exist_ok=True)
    filepath = f'{rel_dir}/{filename}'
    with open(filepath, 'wb') as f:
        f.write(base64.b64decode(pure_b64))
    db.execute(
        'INSERT INTO evidences (alarm_id, image_path, ev_time, location, violation_type, confidence) '
        'VALUES (%s, %s, %s, %s, %s, %s)',
        (alarm_id, filepath, alarm_time.strftime('%Y-%m-%d %H:%M:%S'), '', violation_type, 0)
    )
    return filepath


# ==================== FastAPI 实例 ====================
app = FastAPI(title='智能工业监控平台')


# ==================== 首页 ====================
@app.get('/')
def get_index_page():
    return FileResponse('index.html')


# ==================== WebSocket 1：/ws/detect —— YOLO 实时目标检测 ====================
@app.websocket('/ws/detect')
async def websocket_detect(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            msg = await websocket.receive_json()
            base64_image = msg.get('base64_image', '')

            if not base64_image:
                await manager.send_personal_message(
                    {'status': 'error', 'message': '缺少 base64_image 字段'}, websocket
                )
                continue

            # 解码成 OpenCV 帧
            frame, err = base64_to_frame(base64_image)
            if err:
                await manager.send_personal_message(
                    {'status': 'error', 'message': err}, websocket
                )
                continue

            # 实时读配置：YOLO 总开关关闭时直接返回空结果，不再推理
            cfg = get_config()
            if cfg['yolo_enabled'] != '1':
                await manager.send_personal_message(
                    {'status': 'ok', 'detections': [], 'yolo_enabled': False,
                     'message': 'YOLO 检测已在系统配置中关闭'}, websocket
                )
                continue

            # 丢到线程池跑 YOLO（阈值取当前配置）
            loop = asyncio.get_running_loop()
            detections = await loop.run_in_executor(
                executor, run_yolo, frame, float(cfg['yolo_conf'])
            )

            # 回推检测结果（前端拿这个画框 + 更新计数）
            await manager.send_personal_message(
                {'status': 'ok', 'detections': detections, 'yolo_enabled': True}, websocket
            )

    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logging.error(f'/ws/detect 异常: {e}')
        manager.disconnect(websocket)


# ==================== WebSocket 2：/ws/qwen —— 通义千问场景安全分析 ====================
@app.websocket('/ws/qwen')
async def websocket_qwen(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            msg = await websocket.receive_json()
            base64_image = msg.get('base64_image', '')
            detections = msg.get('detections', [])   # 可选：前端可把 YOLO 结果一并传过来

            if not base64_image:
                await websocket.send_json({'status': 'error', 'message': '缺少图片数据'})
                continue

            # 提取纯 base64
            pure_b64 = extract_pure_base64(base64_image)
            if not pure_b64:
                await websocket.send_json({'status': 'error', 'message': '图片数据解析失败'})
                continue

            # 实时读配置：千问总开关关闭时不调用大模型，也不产生告警
            cfg = get_config()
            if cfg['qwen_enabled'] != '1':
                await websocket.send_json({
                    'status': 'ok',
                    'model': QWEN_MODEL,
                    'timestamp': time.strftime('%H:%M:%S'),
                    'summary': '千问分析已在系统配置中关闭',
                    'violations_cn': [],
                    'risk_level': '低',
                    'suggestions': [],
                    'detections': detections,
                    'qwen_enabled': False,
                })
                continue

            # 调用千问大模型做安全分析
            try:
                result_text = await call_qianwen_model_async(pure_b64)
            except Exception as e:
                logging.error(f'千问调用失败: {e}')
                await websocket.send_json({'status': 'error', 'message': f'千问调用失败: {e}'})
                continue

            # 千问把结论写在首行，"说明："之后是解释性文字，常含"无打架斗殴""未戴安全帽"
            # 这类否定描述；只拿结论行做匹配，否则会把否定句误判成违规
            head = result_text.split('说明', 1)[0]
            verdict = ''
            for line in head.splitlines():
                line = line.strip().strip('-').strip()
                if line:
                    verdict = line
                    break
            negated = any(k in verdict for k in ('无违规', '未检测到', '未发现', '无异常'))

            # 提取违规项（用于前端展示）
            violations = []
            if verdict and not negated:
                if '抽烟' in verdict:
                    violations.append('抽烟')
                if '未戴安全帽' in verdict:
                    violations.append('未戴安全帽')
                if '打架' in verdict or '斗殴' in verdict:
                    violations.append('打架斗殴')
                if '火灾' in verdict or '火苗' in verdict:
                    violations.append('火灾')
                if '爬墙' in verdict or '攀爬' in verdict:
                    violations.append('攀爬围墙')

            # 风险等级同样只依据结论行
            risk_level = '低'
            if '火灾' in verdict or '打架' in verdict or '斗殴' in verdict:
                risk_level = '高'
            elif '抽烟' in verdict or '未戴安全帽' in verdict or '爬墙' in verdict or '攀爬' in verdict:
                risk_level = '中'

            # 按系统配置剔除已关闭的违规类型：关掉后既不告警也不前端提示
            enabled = enabled_violations(cfg)
            violations = [v for v in violations if v in enabled]

            # 检出违规时写入告警表并抓拍存证（防抖秒数取当前配置），随后广播给所有在线前端
            if violations:
                new_alarms = save_alarm(
                    violations, result_text, pure_b64, int(cfg['alarm_debounce'])
                )
                if new_alarms:
                    await notify_manager.broadcast({
                        'status': 'ok',
                        'type': 'alarm',
                        'alarms': new_alarms,
                    })

            # 回推给前端
            await websocket.send_json({
                'status': 'ok',
                'model': QWEN_MODEL,
                'timestamp': time.strftime('%H:%M:%S'),
                'summary': result_text,             # 千问原始文本
                'violations_cn': violations,        # 中文违规项列表
                'risk_level': risk_level,           # 风险等级 高/中/低
                'suggestions': [],                  # 可扩展：让千问返回建议
                'detections': detections,           # 顺便回传 YOLO 上下文
                'qwen_enabled': True
            })

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logging.error(f'/ws/qwen 异常: {e}')


# ==================== WebSocket 3：/ws/notify —— 实时告警广播（R5） ====================
@app.websocket('/ws/notify')
async def websocket_notify(websocket: WebSocket):
    await notify_manager.connect(websocket)
    try:
        while True:
            # 该通道只做单向推送，前端心跳内容直接忽略，仅用于保活
            await websocket.receive_text()
    except WebSocketDisconnect:
        notify_manager.disconnect(websocket)
    except Exception as e:
        logging.error(f'/ws/notify 异常: {e}')
        notify_manager.disconnect(websocket)


# ==================== HTTP：/detect（保留，做兼容） ====================
@app.post('/detect')
async def detect_object(request: DetectRequest):
    base64_image = request.base64_image

    frame, err = base64_to_frame(base64_image)
    if err:
        raise HTTPException(status_code=400, detail=err)

    cfg = get_config()
    if cfg['yolo_enabled'] != '1':
        return {'status': 'ok', 'detections': [], 'yolo_enabled': False,
                'message': 'YOLO 检测已在系统配置中关闭'}

    loop = asyncio.get_running_loop()
    detections = await loop.run_in_executor(
        executor, run_yolo, frame, float(cfg['yolo_conf'])
    )

    return {'status': 'ok', 'detections': detections, 'yolo_enabled': True}


# ==================== HTTP：告警查询 /api/alarms ====================
@app.get('/api/alarms')
def get_alarms(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    start_time: str = '',
    end_time: str = '',
    violation_type: str = '',
    level: int = Query(None),
    status: str = '',
):
    where = []
    params = []
    if start_time:
        where.append('a.alarm_time >= %s')
        params.append(start_time)
    if end_time:
        where.append('a.alarm_time <= %s')
        params.append(end_time)
    if violation_type:
        where.append('a.violation_type = %s')
        params.append(violation_type)
    if level:
        where.append('a.level = %s')
        params.append(level)
    if status:
        where.append('a.status = %s')
        params.append(status)

    where_sql = (' WHERE ' + ' AND '.join(where)) if where else ''
    total = db.query(f'SELECT COUNT(*) AS c FROM alarms a{where_sql}', params)[0]['c']
    offset = (page - 1) * page_size
    items = db.query(
        f'SELECT a.*, e.id AS evidence_id FROM alarms a '
        f'LEFT JOIN evidences e ON e.alarm_id = a.id{where_sql} '
        f'ORDER BY a.alarm_time DESC, a.id DESC LIMIT %s OFFSET %s',
        params + [page_size, offset]
    )
    return {'status': 'ok', 'total': total, 'page': page, 'page_size': page_size, 'items': items}


# ==================== HTTP：告警统计 /api/alarms/summary ====================
@app.get('/api/alarms/summary')
def get_alarm_summary():
    today = datetime.now().strftime('%Y-%m-%d')
    today_count = db.query(
        'SELECT COUNT(*) AS c FROM alarms WHERE alarm_time >= %s', (today + ' 00:00:00',)
    )[0]['c']
    pending_count = db.query(
        "SELECT COUNT(*) AS c FROM alarms WHERE status = '待处理'"
    )[0]['c']
    level_rows = db.query('SELECT level, COUNT(*) AS c FROM alarms GROUP BY level')
    level_counts = {1: 0, 2: 0, 3: 0}
    for r in level_rows:
        level_counts[r['level']] = r['c']
    return {
        'status': 'ok',
        'today_count': today_count,
        'pending_count': pending_count,
        'level_counts': level_counts,
    }


# ==================== HTTP：最近未处理告警 /api/alarms/recent（R5 顶部告警条） ====================
@app.get('/api/alarms/recent')
def get_recent_alarms(limit: int = Query(10, ge=1, le=50)):
    """最近 N 条未处理告警，一级置顶"""
    items = db.query(
        'SELECT id, alarm_time, location, violation_type, level, status, image_path '
        "FROM alarms WHERE status = '待处理' "
        'ORDER BY level ASC, alarm_time DESC, id DESC LIMIT %s',
        (limit,)
    )
    return {'status': 'ok', 'total': len(items), 'items': items}


# ==================== R7 数据总览：/api/stats ====================
@app.get('/api/stats')
def get_stats(days: int = Query(7, ge=1, le=30, description='统计天数，默认近 7 日')):
    """数据总览所需的全部统计：卡片数据 + 违规类型占比 + 近 N 日趋势 + 车间违规排名 + 最新告警"""
    now = datetime.now()
    today = now.strftime('%Y-%m-%d')
    since = (now - timedelta(days=days - 1)).strftime('%Y-%m-%d 00:00:00')

    cards = {
        'today_count': db.query(
            'SELECT COUNT(*) AS c FROM alarms WHERE alarm_time >= %s', (today + ' 00:00:00',)
        )[0]['c'],
        'today_level1': db.query(
            'SELECT COUNT(*) AS c FROM alarms WHERE level = 1 AND alarm_time >= %s',
            (today + ' 00:00:00',)
        )[0]['c'],
        'pending_count': db.query(
            "SELECT COUNT(*) AS c FROM alarms WHERE status = '待处理'"
        )[0]['c'],
    }
    dev = db.query(f'SELECT COUNT(*) AS total, SUM({_ONLINE_CASE}) AS online FROM devices d')[0]
    cards['device_total'] = int(dev['total'])
    cards['device_online'] = int(dev['online'] or 0)

    # 违规类型占比（近 N 日）
    type_ratio = [
        {'name': r['violation_type'], 'value': int(r['c'])}
        for r in db.query(
            'SELECT violation_type, COUNT(*) AS c FROM alarms WHERE alarm_time >= %s '
            'GROUP BY violation_type ORDER BY c DESC', (since,)
        )
    ]

    # 近 N 日趋势，缺失的日期补 0，保证折线 X 轴连续
    daily = {r['d']: int(r['c']) for r in db.query(
        "SELECT DATE_FORMAT(alarm_time, '%%Y-%%m-%%d') AS d, COUNT(*) AS c "
        'FROM alarms WHERE alarm_time >= %s GROUP BY d', (since,)
    )}
    trend = []
    for i in range(days - 1, -1, -1):
        d = (now - timedelta(days=i)).strftime('%Y-%m-%d')
        trend.append({'date': d[5:], 'count': daily.get(d, 0)})

    # 车间违规排名：告警点位与设备安装位置一致才算归属车间，其余归入“未分配点位”
    matched = db.query(
        "SELECT COALESCE(NULLIF(d.workshop, ''), '未分组设备') AS ws_name, COUNT(*) AS c "
        'FROM alarms a JOIN devices d ON d.location = a.location '
        "WHERE a.alarm_time >= %s AND a.location <> '' "
        "GROUP BY COALESCE(NULLIF(d.workshop, ''), '未分组设备') ORDER BY c DESC", (since,)
    )
    workshop_rank = [{'name': r['ws_name'], 'value': int(r['c'])} for r in matched]
    rest = sum(t['value'] for t in type_ratio) - sum(w['value'] for w in workshop_rank)
    if rest > 0:
        workshop_rank.append({'name': '未分配点位', 'value': rest})

    recent = db.query(
        'SELECT id, alarm_time, location, violation_type, level, status FROM alarms '
        'ORDER BY alarm_time DESC, id DESC LIMIT 10'
    )
    return {
        'status': 'ok',
        'days': days,
        'cards': cards,
        'type_ratio': type_ratio,
        'trend': trend,
        'workshop_rank': workshop_rank,
        'recent': recent,
    }


# ==================== R7 台账报表：/api/alarms/export（CSV 下载） ====================
@app.get('/api/alarms/export')
def export_alarms_csv(
    start_time: str = '',
    end_time: str = '',
    violation_type: str = '',
    status: str = '',
):
    """按筛选条件导出全部告警台账为 CSV；加 UTF-8 BOM，Excel 双击打开不乱码"""
    where = []
    params = []
    if start_time:
        where.append('alarm_time >= %s')
        params.append(start_time)
    if end_time:
        where.append('alarm_time <= %s')
        params.append(end_time)
    if violation_type:
        where.append('violation_type = %s')
        params.append(violation_type)
    if status:
        where.append('status = %s')
        params.append(status)

    where_sql = (' WHERE ' + ' AND '.join(where)) if where else ''
    rows = db.query(
        f'SELECT id, alarm_time, location, violation_type, status FROM alarms{where_sql} '
        'ORDER BY alarm_time DESC, id DESC', params
    )

    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(['隐患编号', '时间', '地点', '违规行为', '状态'])
    for r in rows:
        writer.writerow([
            f"HZ{r['id']:05d}",
            r['alarm_time'].strftime('%Y-%m-%d %H:%M:%S'),
            r['location'] or '未分配点位',
            r['violation_type'],
            r['status'],
        ])

    filename = f"alarm_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        content=('\ufeff' + buf.getvalue()).encode('utf-8'),
        media_type='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


# ==================== HTTP：告警处理 /api/alarms/{id} ====================
class AlarmUpdateRequest(BaseModel):
    status: str = Field(..., description='处理状态：已处理 / 已驳回')
    remark: str = ''


@app.patch('/api/alarms/{alarm_id}')
def update_alarm(alarm_id: int, req: AlarmUpdateRequest, request: Request):
    if req.status not in ('已处理', '已驳回'):
        raise HTTPException(status_code=400, detail='无效的处理状态')
    rows = db.query('SELECT id, status, violation_type, location FROM alarms WHERE id = %s', (alarm_id,))
    if not rows:
        raise HTTPException(status_code=404, detail='告警不存在')
    old = rows[0]
    db.execute(
        'UPDATE alarms SET status = %s, remark = %s WHERE id = %s',
        (req.status, req.remark, alarm_id)
    )
    # R10：告警处理 / 误报驳回留痕
    write_op_log(
        request.state.user.get('username', ''),
        '告警处理' if req.status == '已处理' else '误报驳回',
        f'告警 HZ{alarm_id:05d}（{old["violation_type"]} / {old["location"] or "未分配点位"}）'
        f'：{old["status"]} → {req.status}；备注：{req.remark or "无"}'
    )
    return {'status': 'ok'}


# ==================== HTTP：取证检索 /api/evidences ====================
@app.get('/api/evidences')
def get_evidences(
    start_time: str = '',
    end_time: str = '',
    location: str = '',
    violation_type: str = '',
):
    where = []
    params = []
    if start_time:
        where.append('ev_time >= %s')
        params.append(start_time)
    if end_time:
        where.append('ev_time <= %s')
        params.append(end_time)
    if location:
        where.append('location = %s')
        params.append(location)
    if violation_type:
        where.append('violation_type = %s')
        params.append(violation_type)

    where_sql = (' WHERE ' + ' AND '.join(where)) if where else ''
    items = db.query(f'SELECT * FROM evidences{where_sql} ORDER BY ev_time DESC', params)
    return {'status': 'ok', 'total': len(items), 'items': items}


# ==================== HTTP：取证图片下载 /api/evidences/{id}/image ====================
@app.get('/api/evidences/{evidence_id}/image')
def get_evidence_image(evidence_id: int, request: Request):
    from urllib.parse import quote
    rows = db.query('SELECT * FROM evidences WHERE id = %s', (evidence_id,))
    if not rows:
        raise HTTPException(status_code=404, detail='取证记录不存在')
    ev = rows[0]
    if not os.path.exists(ev['image_path']):
        raise HTTPException(status_code=404, detail='图片文件不存在')
    # R10：证据下载属敏感操作，记录操作人
    write_op_log(
        request.state.user.get('username', ''),
        '证据下载',
        f'取证记录 #{evidence_id}（{ev["ev_time"]} / {ev["violation_type"]}）图片下载'
    )
    watermark = quote(f"{ev['ev_time']}|{ev['violation_type']}")
    return FileResponse(
        ev['image_path'],
        media_type='image/jpeg',
        filename=os.path.basename(ev['image_path']),
        headers={'X-Evidence-Watermark': watermark},
    )


# ==================== R6 设备管理：台账 CRUD + 在线状态模拟 ====================
# 在线判定：online_status=1 且 60 秒内有过心跳；设备页会定时调心跳接口模拟上报
OFFLINE_SECONDS = 60

_ONLINE_CASE = (
    'CASE WHEN d.online_status = 1 AND d.last_heartbeat IS NOT NULL '
    f'AND TIMESTAMPDIFF(SECOND, d.last_heartbeat, NOW()) <= {OFFLINE_SECONDS} '
    'THEN 1 ELSE 0 END'
)


class DeviceRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=50, description='设备编号')
    name: str = Field(..., min_length=1, max_length=100, description='设备名称')
    type: str = Field('', max_length=50)
    location: str = Field('', max_length=200)
    workshop: str = Field('', max_length=100)
    ip: str = Field('', max_length=50)
    online_status: int = Field(0, ge=0, le=1, description='基准在线状态 0离线 1在线')
    ai_enabled: int = Field(1, ge=0, le=1, description='AI开关 0关 1开')


# ==================== HTTP：车间分组（树） /api/devices/workshops ====================
@app.get('/api/devices/workshops')
def get_device_workshops():
    """按车间分组返回设备总数与在线数，供左侧分组树使用"""
    rows = db.query(
        f'SELECT d.workshop, COUNT(*) AS total, SUM({_ONLINE_CASE}) AS online_count '
        'FROM devices d GROUP BY d.workshop ORDER BY d.workshop'
    )
    items = [{
        'workshop': r['workshop'] or '未分组',
        'total': int(r['total']),
        'online_count': int(r['online_count'] or 0),
    } for r in rows]
    return {'status': 'ok', 'total': sum(i['total'] for i in items), 'items': items}


# ==================== HTTP：设备列表 /api/devices ====================
@app.get('/api/devices')
def get_devices(
    workshop: str = '',
    keyword: str = '',
    online: int = Query(None, description='按在线状态过滤 0离线 1在线'),
):
    where = []
    params = []
    if workshop:
        if workshop == '未分组':
            where.append("d.workshop = ''")
        else:
            where.append('d.workshop = %s')
            params.append(workshop)
    if keyword:
        where.append('(d.name LIKE %s OR d.code LIKE %s OR d.ip LIKE %s)')
        kw = f'%{keyword}%'
        params.extend([kw, kw, kw])

    where_sql = (' WHERE ' + ' AND '.join(where)) if where else ''
    items = db.query(
        f'SELECT d.*, {_ONLINE_CASE} AS online FROM devices d{where_sql} '
        'ORDER BY d.workshop, d.code',
        params
    )
    if online in (0, 1):
        items = [d for d in items if d['online'] == online]
    return {'status': 'ok', 'total': len(items), 'items': items}


# ==================== HTTP：新增设备 ====================
@app.post('/api/devices')
def create_device(req: DeviceRequest, request: Request):
    if db.query('SELECT id FROM devices WHERE code = %s', (req.code,)):
        raise HTTPException(status_code=400, detail='设备编号已存在')
    new_id = db.execute(
        'INSERT INTO devices (code, name, type, location, workshop, ip, online_status, ai_enabled) '
        'VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
        (req.code, req.name, req.type, req.location, req.workshop, req.ip,
         req.online_status, req.ai_enabled)
    )
    write_op_log(request.state.user.get('username', ''), '设备新增',
                 f'新增设备 {req.code}（{req.name}，{req.workshop or "未分组"} / {req.location or "未填点位"}）')
    return {'status': 'ok', 'id': new_id}


# ==================== HTTP：编辑设备 ====================
@app.put('/api/devices/{device_id}')
def update_device(device_id: int, req: DeviceRequest, request: Request):
    if not db.query('SELECT id FROM devices WHERE id = %s', (device_id,)):
        raise HTTPException(status_code=404, detail='设备不存在')
    if db.query('SELECT id FROM devices WHERE code = %s AND id <> %s', (req.code, device_id)):
        raise HTTPException(status_code=400, detail='设备编号已被其他设备占用')
    db.execute(
        'UPDATE devices SET code = %s, name = %s, type = %s, location = %s, workshop = %s, '
        'ip = %s, online_status = %s, ai_enabled = %s WHERE id = %s',
        (req.code, req.name, req.type, req.location, req.workshop, req.ip,
         req.online_status, req.ai_enabled, device_id)
    )
    write_op_log(request.state.user.get('username', ''), '设备修改',
                 f'修改设备 {req.code}（{req.name}，{req.workshop or "未分组"} / {req.location or "未填点位"}）')
    return {'status': 'ok'}


# ==================== HTTP：删除设备 ====================
@app.delete('/api/devices/{device_id}')
def delete_device(device_id: int, request: Request):
    rows = db.query('SELECT id, code, name FROM devices WHERE id = %s', (device_id,))
    if not rows:
        raise HTTPException(status_code=404, detail='设备不存在')
    old = rows[0]
    db.execute('DELETE FROM devices WHERE id = %s', (device_id,))
    write_op_log(request.state.user.get('username', ''), '设备删除',
                 f'删除设备 {old["code"]}（{old["name"]}）')
    return {'status': 'ok'}


# ==================== HTTP：设备心跳上报（在线状态模拟） ====================
@app.post('/api/devices/{device_id}/heartbeat')
def device_heartbeat(device_id: int):
    if not db.query('SELECT id FROM devices WHERE id = %s', (device_id,)):
        raise HTTPException(status_code=404, detail='设备不存在')
    db.execute(
        'UPDATE devices SET online_status = 1, last_heartbeat = NOW() WHERE id = %s',
        (device_id,)
    )
    return {'status': 'ok', 'ts': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}


# ==================== R8 登录鉴权与三级角色权限 ====================
# 角色定义（与前端 js/auth.js 的判断保持一致，改动需同步）
ROLE_ADMIN = '超级管理员'
ROLE_SAFETY = '安全管理员'
ROLE_VIEWER = '查看员'

SESSION_TTL = 8 * 3600      # 会话有效期 8 小时
CAPTCHA_TTL = 120           # 验证码有效期 2 分钟
CAPTCHA_CHARS = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'   # 去掉易混淆的 0/1/I/O

# token → {'username', 'role', 'expire'}；进程内存存储，重启即失效
SESSIONS = {}
# captcha_id → (code, expire_ts)
_CAPTCHA_STORE = {}

# 免鉴权接口：登录与验证码
PUBLIC_API = {'/api/login', '/api/captcha'}


def hash_password(password: str, salt: str) -> str:
    """加盐哈希，存储格式 salt$sha256(salt+password)，与 init_db.py 保持一致"""
    return f'{salt}${hashlib.sha256((salt + password).encode("utf-8")).hexdigest()}'


def verify_password(password: str, stored: str) -> bool:
    if '$' in stored:
        salt, _, digest = stored.partition('$')
        return hmac.compare_digest(hash_password(password, salt).split('$', 1)[1], digest)
    # 兼容 R8 之前写入的裸 sha256 记录
    return hmac.compare_digest(hashlib.sha256(password.encode('utf-8')).hexdigest(), stored)


def create_session(username: str, role: str) -> str:
    token = secrets.token_hex(16)
    SESSIONS[token] = {'username': username, 'role': role, 'expire': time.time() + SESSION_TTL}
    return token


def read_session(request: Request):
    """从 Authorization: Bearer <token> 取出会话，过期即作废"""
    auth = request.headers.get('authorization', '')
    token = auth[7:].strip() if auth.lower().startswith('bearer ') else ''
    sess = SESSIONS.get(token)
    if not sess:
        return None
    if sess['expire'] < time.time():
        SESSIONS.pop(token, None)
        return None
    return sess


def write_op_log(username: str, op_type: str, detail: str):
    """R10：关键操作留痕，写入 op_logs（失败不影响主流程）"""
    try:
        db.execute(
            'INSERT INTO op_logs (op_time, username, op_type, detail) VALUES (%s, %s, %s, %s)',
            (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), username or '', op_type, detail or '')
        )
    except Exception as e:
        logging.warning(f'[op_log] 写入失败：{e}')


def allowed_roles(method: str, path: str):
    """接口允许的角色；返回 None 表示所有已登录角色可用"""
    if path in ('/api/logout', '/api/me'):
        return None
    if path.startswith('/api/config'):
        return [ROLE_ADMIN]                        # 系统配置仅超级管理员可见可改
    if path.startswith('/api/logs'):
        return [ROLE_ADMIN]                        # 操作日志仅超级管理员可查
    if path.startswith('/api/alarms/export'):
        return [ROLE_ADMIN, ROLE_SAFETY]           # 台账导出属于报表能力，查看员不可用
    if method == 'GET':
        if path.startswith('/api/devices'):
            return [ROLE_ADMIN]                    # 设备台账仅超管可见
        if path.startswith('/api/evidences'):
            return [ROLE_ADMIN, ROLE_SAFETY]       # 取证查看
        return None                                # 监控与告警列表：三角色只读可见
    if path.startswith('/api/alarms'):
        return [ROLE_ADMIN, ROLE_SAFETY]           # 告警处理/驳回：查看员不可写
    if path.startswith('/api/devices'):
        return [ROLE_ADMIN]
    return [ROLE_ADMIN]


def gen_captcha():
    """生成 4 位图形验证码，返回 (captcha_id, data_url)"""
    from PIL import ImageDraw, ImageFont
    code = ''.join(random.choice(CAPTCHA_CHARS) for _ in range(4))
    width, height = 120, 40
    img = Image.new('RGB', (width, height), (245, 247, 250))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype('arial.ttf', 26)
    except Exception:
        font = ImageFont.load_default()
    # 干扰线，降低机器识别成功率即可，不追求强度
    for _ in range(4):
        draw.line(
            [(random.randint(0, width), random.randint(0, height)),
             (random.randint(0, width), random.randint(0, height))],
            fill=(205, 210, 220), width=1
        )
    for i, ch in enumerate(code):
        draw.text((12 + i * 26, random.randint(2, 8)), ch, font=font, fill=(37, 99, 235))

    buf = BytesIO()
    img.save(buf, format='PNG')
    captcha_id = secrets.token_hex(8)
    _CAPTCHA_STORE[captcha_id] = (code, time.time() + CAPTCHA_TTL)
    return captcha_id, 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()


def check_captcha(captcha_id: str, captcha: str):
    """验证码一次性使用，校验后立即失效"""
    item = _CAPTCHA_STORE.pop(captcha_id, None)
    if not item or item[1] < time.time() or item[0] != (captcha or '').strip().upper():
        raise HTTPException(status_code=400, detail='验证码错误或已过期')


@app.middleware('http')
async def auth_middleware(request: Request, call_next):
    """静态资源与登录/验证码接口放行，其余 /api/* 需登录；写接口再按角色二次校验"""
    path = request.url.path
    if not path.startswith('/api/') or path in PUBLIC_API:
        return await call_next(request)

    sess = read_session(request)
    if not sess:
        return JSONResponse({'status': 'error', 'message': '未登录或登录已过期'}, status_code=401)

    roles = allowed_roles(request.method, path)
    if roles and sess['role'] not in roles:
        return JSONResponse({'status': 'error', 'message': '当前角色无权执行该操作'}, status_code=403)

    request.state.user = sess
    return await call_next(request)


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    password: str = Field(..., min_length=1, max_length=64)
    captcha_id: str = ''
    captcha: str = ''


@app.get('/api/captcha')
def get_captcha():
    captcha_id, image = gen_captcha()
    return {'status': 'ok', 'captcha_id': captcha_id, 'image': image}


@app.post('/api/login')
def login(req: LoginRequest):
    check_captcha(req.captcha_id, req.captcha)
    rows = db.query('SELECT * FROM users WHERE username = %s', (req.username,))
    if not rows or not verify_password(req.password, rows[0]['password_hash']):
        raise HTTPException(status_code=401, detail='账号或密码错误')
    user = rows[0]
    token = create_session(user['username'], user['role'])
    write_op_log(user['username'], '登录', f'登录成功（角色：{user["role"]}）')
    return {'status': 'ok', 'token': token, 'username': user['username'], 'role': user['role']}


@app.post('/api/logout')
def logout(request: Request):
    auth = request.headers.get('authorization', '')
    if auth.lower().startswith('bearer '):
        sess = SESSIONS.pop(auth[7:].strip(), None)
        if sess:
            write_op_log(sess['username'], '登出', '退出登录')
    return {'status': 'ok'}


@app.get('/api/me')
def get_me(request: Request):
    sess = read_session(request)
    if not sess:
        raise HTTPException(status_code=401, detail='未登录或登录已过期')
    return {'status': 'ok', 'username': sess['username'], 'role': sess['role']}


# ==================== R9 系统配置：读取 / 修改 ====================
class ConfigUpdateRequest(BaseModel):
    items: dict[str, str] = Field(..., description='待更新的配置键值对')


def normalize_config_item(key: str, value: str) -> str:
    """校验并归一化单个配置项；非法值直接拒绝，避免脏配置影响检测链路"""
    if key not in CONFIG_DEFAULTS:
        raise HTTPException(status_code=400, detail=f'不支持的配置项：{key}')
    value = (value or '').strip()
    if key == 'yolo_conf':
        try:
            num = float(value)
        except ValueError:
            raise HTTPException(status_code=400, detail='置信度阈值必须是数字')
        if not 0.3 <= num <= 0.9:
            raise HTTPException(status_code=400, detail='置信度阈值需在 0.3~0.9 之间')
        return f'{num:.2f}'
    if key == 'alarm_debounce':
        try:
            num = int(value)
        except ValueError:
            raise HTTPException(status_code=400, detail='防抖时长必须是整数秒')
        if not 1 <= num <= 3600:
            raise HTTPException(status_code=400, detail='防抖时长需在 1~3600 秒之间')
        return str(num)
    # 其余均为开关项，只接受 0 / 1
    if value not in ('0', '1'):
        raise HTTPException(status_code=400, detail=f'{key} 只接受 0 或 1')
    return value


@app.get('/api/config')
def get_system_config():
    return {'status': 'ok', 'config': get_config()}


@app.put('/api/config')
def update_system_config(req: ConfigUpdateRequest, request: Request):
    if not req.items:
        raise HTTPException(status_code=400, detail='没有需要更新的配置项')

    old = get_config()
    changes = []
    for key, value in req.items.items():
        new_value = normalize_config_item(key, value)
        if old.get(key) == new_value:
            continue
        db.execute(
            'INSERT INTO config (cfg_key, cfg_value) VALUES (%s, %s) '
            'ON DUPLICATE KEY UPDATE cfg_value = VALUES(cfg_value)',
            (key, new_value)
        )
        changes.append(f'{key}: {old.get(key, "—")} → {new_value}')

    # 配置变更留痕（R10 操作日志页会展示这些记录）
    if changes:
        write_op_log(request.state.user.get('username', ''), '配置修改', '；'.join(changes))
    return {'status': 'ok', 'changed': len(changes), 'config': get_config()}


# ==================== R10 操作日志：筛选项 / 分页查询 ====================
@app.get('/api/logs/filters')
def get_log_filters():
    """筛选下拉数据：日志里已出现过的操作类型与操作用户"""
    op_types = [r['op_type'] for r in db.query(
        'SELECT DISTINCT op_type FROM op_logs ORDER BY op_type')]
    usernames = [r['username'] for r in db.query(
        'SELECT DISTINCT username FROM op_logs ORDER BY username')]
    return {'status': 'ok', 'op_types': op_types, 'usernames': usernames}


@app.get('/api/logs')
def get_logs(
    start_time: str = '',
    end_time: str = '',
    username: str = '',
    op_type: str = '',
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """操作日志分页查询（仅超级管理员），支持时间 / 用户 / 操作类型筛选"""
    where = []
    params = []
    if start_time:
        where.append('op_time >= %s')
        params.append(start_time)
    if end_time:
        where.append('op_time <= %s')
        params.append(end_time)
    if username:
        where.append('username = %s')
        params.append(username)
    if op_type:
        where.append('op_type = %s')
        params.append(op_type)

    where_sql = (' WHERE ' + ' AND '.join(where)) if where else ''
    total = db.query(f'SELECT COUNT(*) AS c FROM op_logs{where_sql}', params)[0]['c']
    items = db.query(
        f'SELECT id, op_time, username, op_type, detail FROM op_logs{where_sql} '
        'ORDER BY op_time DESC, id DESC LIMIT %s OFFSET %s',
        params + [page_size, (page - 1) * page_size]
    )
    return {'status': 'ok', 'total': total, 'page': page, 'page_size': page_size, 'items': items}


# ==================== 静态资源（必须放最后） ====================
app.mount('/', StaticFiles(directory='.', html=True), name='static')


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=8200)