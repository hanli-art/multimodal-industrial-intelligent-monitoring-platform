from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query, Request
from pydantic import BaseModel, Field
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

import base64
import hashlib
import hmac
import random
import secrets
from io import BytesIO
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
from datetime import datetime

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
def run_yolo(frame):
    """同步函数，跑 YOLO，返回检测结果列表"""
    results = model.predict(frame, conf=0.5, classes=[0, 2, 7], verbose=False)

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

    car_cnt    = sum(1 for d in detections if d['class_name'] == 'car')
    truck_cnt  = sum(1 for d in detections if d['class_name'] == 'truck')
    person_cnt = sum(1 for d in detections if d['class_name'] == 'person')
    vehicle_cnt = car_cnt + truck_cnt
    ts = time.strftime('%H:%M:%S')
    print(f'[{ts}] 车辆 = {vehicle_cnt} (car={car_cnt}, truck={truck_cnt}) | 行人 = {person_cnt} | 总目标 = {len(detections)}')

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

# 防抖：同一违规类型 30 秒内不重复入库
ALARM_DEBOUNCE = 30
_last_alarm_ts = {}


def save_alarm(violations, summary, pure_b64):
    """把违规结果写入 alarms 表并抓拍存证，同一类型 30 秒内防抖；返回本轮新建的告警列表"""
    now = time.time()
    created = []
    for v in violations:
        if now - _last_alarm_ts.get(v, 0) < ALARM_DEBOUNCE:
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

            # 丢到线程池跑 YOLO
            loop = asyncio.get_running_loop()
            detections = await loop.run_in_executor(executor, run_yolo, frame)

            # 回推检测结果（前端拿这个画框 + 更新计数）
            await manager.send_personal_message(
                {'status': 'ok', 'detections': detections}, websocket
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

            # 调用千问大模型做安全分析
            try:
                result_text = await call_qianwen_model_async(pure_b64)
            except Exception as e:
                logging.error(f'千问调用失败: {e}')
                await websocket.send_json({'status': 'error', 'message': f'千问调用失败: {e}'})
                continue

            # 简单解析：根据关键词判断风险等级
            risk_level = '低'
            if any(k in result_text for k in ['火灾', '打架', '斗殴']):
                risk_level = '高'
            elif any(k in result_text for k in ['抽烟', '未戴安全帽', '爬墙', '攀爬']):
                risk_level = '中'

            # 提取违规项（用于前端展示）
            violations = []
            if '抽烟' in result_text:
                violations.append('抽烟')
            if '未戴安全帽' in result_text:
                violations.append('未戴安全帽')
            if '打架' in result_text or '斗殴' in result_text:
                violations.append('打架斗殴')
            if '火灾' in result_text or '火苗' in result_text:
                violations.append('火灾')
            if '爬墙' in result_text or '攀爬' in result_text:
                violations.append('攀爬围墙')

            # 检出违规时写入告警表并抓拍存证（带防抖），随后广播给所有在线前端
            if violations:
                new_alarms = save_alarm(violations, result_text, pure_b64)
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
                'detections': detections            # 顺便回传 YOLO 上下文
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

    loop = asyncio.get_running_loop()
    detections = await loop.run_in_executor(executor, run_yolo, frame)

    return {'status': 'ok', 'detections': detections}


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


# ==================== HTTP：告警处理 /api/alarms/{id} ====================
class AlarmUpdateRequest(BaseModel):
    status: str = Field(..., description='处理状态：已处理 / 已驳回')
    remark: str = ''


@app.patch('/api/alarms/{alarm_id}')
def update_alarm(alarm_id: int, req: AlarmUpdateRequest):
    if req.status not in ('已处理', '已驳回'):
        raise HTTPException(status_code=400, detail='无效的处理状态')
    if not db.query('SELECT id FROM alarms WHERE id = %s', (alarm_id,)):
        raise HTTPException(status_code=404, detail='告警不存在')
    db.execute(
        'UPDATE alarms SET status = %s, remark = %s WHERE id = %s',
        (req.status, req.remark, alarm_id)
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
def get_evidence_image(evidence_id: int):
    from urllib.parse import quote
    rows = db.query('SELECT * FROM evidences WHERE id = %s', (evidence_id,))
    if not rows:
        raise HTTPException(status_code=404, detail='取证记录不存在')
    ev = rows[0]
    if not os.path.exists(ev['image_path']):
        raise HTTPException(status_code=404, detail='图片文件不存在')
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
def create_device(req: DeviceRequest):
    if db.query('SELECT id FROM devices WHERE code = %s', (req.code,)):
        raise HTTPException(status_code=400, detail='设备编号已存在')
    new_id = db.execute(
        'INSERT INTO devices (code, name, type, location, workshop, ip, online_status, ai_enabled) '
        'VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
        (req.code, req.name, req.type, req.location, req.workshop, req.ip,
         req.online_status, req.ai_enabled)
    )
    return {'status': 'ok', 'id': new_id}


# ==================== HTTP：编辑设备 ====================
@app.put('/api/devices/{device_id}')
def update_device(device_id: int, req: DeviceRequest):
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
    return {'status': 'ok'}


# ==================== HTTP：删除设备 ====================
@app.delete('/api/devices/{device_id}')
def delete_device(device_id: int):
    if not db.query('SELECT id FROM devices WHERE id = %s', (device_id,)):
        raise HTTPException(status_code=404, detail='设备不存在')
    db.execute('DELETE FROM devices WHERE id = %s', (device_id,))
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


def allowed_roles(method: str, path: str):
    """接口允许的角色；返回 None 表示所有已登录角色可用"""
    if path in ('/api/logout', '/api/me'):
        return None
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
    return {'status': 'ok', 'token': token, 'username': user['username'], 'role': user['role']}


@app.post('/api/logout')
def logout(request: Request):
    auth = request.headers.get('authorization', '')
    if auth.lower().startswith('bearer '):
        SESSIONS.pop(auth[7:].strip(), None)
    return {'status': 'ok'}


@app.get('/api/me')
def get_me(request: Request):
    sess = read_session(request)
    if not sess:
        raise HTTPException(status_code=401, detail='未登录或登录已过期')
    return {'status': 'ok', 'username': sess['username'], 'role': sess['role']}


# ==================== 静态资源（必须放最后） ====================
app.mount('/', StaticFiles(directory='.', html=True), name='static')


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=8200)