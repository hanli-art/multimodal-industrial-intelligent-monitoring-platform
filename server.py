from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Query
from pydantic import BaseModel, Field
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

import base64
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
            model='qwen3.7-plus',
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


def save_alarm(violations, summary):
    """把违规结果写入 alarms 表，同一类型 30 秒内防抖"""
    now = time.time()
    for v in violations:
        if now - _last_alarm_ts.get(v, 0) < ALARM_DEBOUNCE:
            continue
        _last_alarm_ts[v] = now
        level = VIOLATION_LEVEL.get(v, 2)
        db.execute(
            'INSERT INTO alarms (alarm_time, location, violation_type, level, confidence, summary, status) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s)',
            (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), '', v, level, 0, summary, '待处理')
        )


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

            # 检出违规时写入告警表（带防抖）
            if violations:
                save_alarm(violations, result_text)

            # 回推给前端
            await websocket.send_json({
                'status': 'ok',
                'model': 'qwen-vl-max',
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
        where.append('alarm_time >= %s')
        params.append(start_time)
    if end_time:
        where.append('alarm_time <= %s')
        params.append(end_time)
    if violation_type:
        where.append('violation_type = %s')
        params.append(violation_type)
    if level:
        where.append('level = %s')
        params.append(level)
    if status:
        where.append('status = %s')
        params.append(status)

    where_sql = (' WHERE ' + ' AND '.join(where)) if where else ''
    total = db.query(f'SELECT COUNT(*) AS c FROM alarms{where_sql}', params)[0]['c']
    offset = (page - 1) * page_size
    items = db.query(
        f'SELECT * FROM alarms{where_sql} ORDER BY alarm_time DESC LIMIT %s OFFSET %s',
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


# ==================== 静态资源（必须放最后） ====================
app.mount('/', StaticFiles(directory='.', html=True), name='static')


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=8200)