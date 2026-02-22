# app.py
# 이 스크립트는 Flask 웹 애플리케이션으로, 방사선 및 기상 데이터를 MongoDB에서 가져와 API 및 웹 페이지로 제공합니다.
# 데이터 필터링, 최신 데이터 조회, CSV 내보내기 등의 기능을 제공합니다.


from flask import Flask, render_template, request, redirect, jsonify, session, flash, make_response
from flask_caching import Cache
from flask import Response
import urllib.parse
from flask_login import logout_user
import csv
import io
import pandas as pd
from datetime import datetime, timedelta
import logging
from dateutil import parser
from scipy.signal import find_peaks, savgol_filter
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import os
from pymongo import MongoClient, DESCENDING
from pymongo.errors import PyMongoError
from map_utils import power_plants, compute_top5_for, generate_topsis_map_html
from utils import export_csv, upload_csv
from flask import abort
from flask_login import LoginManager, UserMixin, login_user, user_logged_out,login_required, current_user
from flask_bcrypt import Bcrypt
from bson import ObjectId
from functools import wraps
from dotenv import load_dotenv
from functools import lru_cache
from flask import send_from_directory, url_for
from werkzeug.utils import secure_filename


app = Flask(__name__)

bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

app.config['SECRET_KEY'] = 'supersecretkey'

# Flask-Caching 설정 비활성화
cache = Cache(app, config={'CACHE_TYPE': 'null'})

# Railway에선 환경변수 UPLOAD_DIR=/data/uploads (Volume 마운트) 설정
upload_dir = os.getenv('UPLOAD_DIR') or os.path.join(app.root_path, 'uploads')
app.config['UPLOAD_FOLDER'] = upload_dir

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB

# MongoDB 연결
load_dotenv() # .env 파일에서 환경 변수를 로드합니다. (로컬 개발용)
mongo_uri = os.getenv("MONGO_URI")
if not mongo_uri: # 환경 변수가 설정되지 않았을 경우를 대비한 체크
    raise ValueError("MONGO_URI environment variable not set! Please set MONGO_URI in .env or your deployment environment.")

# --- 이 줄을 다음과 같이 수정해주세요 ---
mongo_uri = mongo_uri.strip().lstrip('=')
# ----------------------------------------

client = MongoClient(mongo_uri)
db = client['Data']
users = db['users'] # 사용자 컬렉션

def get_mongo_connection():
    return client

# 한국시간(KST, UTC+9) 반환 헬퍼
def now_kst():
    # tz 정보 없이, KST 시각을 그대로 쓰고 싶을 때 사용
    return datetime.utcnow() + timedelta(hours=9)

# User 클래스 정의 바로 위나 아래에 추가
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or current_user.email != 'hyoung@dankook.ac.kr':
            abort(403)
        return f(*args, **kwargs)
    return decorated


# 컬렉션 설정
collection = db['NPP_weather']  # NPP_weather 컬렉션 (기존 데이터)
backup_collection = db['NPP_weather_backup']  # NPP_weather_backup 컬렉션 (백업된 데이터)
busan_radiation_collection = db['Busan_radiation']
busan_radiation_backup_collection = db['Busan_radiation_backup']
nuclear_radiation_collection = db['nuclear_radiation']
nuclear_radiation_backup_collection = db['nuclear_radiation_backup']

# 통계 데이터 컬렉션
stats_collection = db['radiation_stats']
avg_db = client['radiation_statistics']  # 평균을 저장할 새로운 데이터베이스
avg_collection = avg_db['daily_average']  # 평균 데이터 저장 컬렉션
regional_avg_collection = avg_db['regional_average']

# 세부 과제 컬렉션
CAU_collection = db['Data_CAU']
FNC_collection = db['Data_FNC']
KAERI_collection = db['Data_KAERI']
RMT_collection = db['Data_RMT']
uploads_collection = db['uploads']  # GIF 업로드 메타 저장
workers_collection = db['Data_RMT_workers']

analysis1_collection = CAU_collection
analysis2_collection = FNC_collection
analysis3_collection = KAERI_collection
analysis4_collection = RMT_collection

# === 인덱스 ===
nuclear_radiation_collection.create_index(
    [('genName', 1), ('time', -1)],
    name='ix_nuclear_gen_time'
)

uploads_collection.create_index([('type', 1), ('created_at', -1)], name='ix_uploads_type_created')

workers_collection.create_index(
    [('code', 1), ('checkTime', -1)],
    name='ix_workers_code_time'
)

# 로깅 설정
class ColoredFormatter(logging.Formatter):
    COLORS = {
        'DEBUG': '\033[94m',  # 파란색
        'INFO': '\033[97m',   # 흰색으로 변경
        'WARNING': '\033[93m',  # 노란색
        'ERROR': '\033[91m',   # 빨간색
        'CRITICAL': '\033[41m',  # 배경 빨간색
    }
    RESET = '\033[0m'

    def format(self, record):
        color = self.COLORS.get(record.levelname, self.RESET)
        return f"{color}{super().format(record)}{self.RESET}"

# 기존의 핸들러 설정을 업데이트합니다.
handler = logging.StreamHandler()
handler.setFormatter(ColoredFormatter('%(asctime)s %(levelname)s:%(message)s'))

# 핸들러를 기존의 로거에 추가
logging.basicConfig(
    level=logging.DEBUG,  # DEBUG로 변경하면 모든 로그가 기본 색으로 나타남
    format='%(asctime)s %(levelname)s:%(message)s',
    handlers=[
        logging.FileHandler("app.log"),
        handler  # 업데이트한 핸들러 추가
    ]
)

# 발전소 이름 매핑 (부산과 전남 제외)
genName_mapping = {
    "KR": "고리 원자력발전소",
    "WS": "월성 원자력발전소",
    "YK": "한빛 원자력발전소",
    "UJ": "한울 원자력발전소",
    "SU": "새울 원자력발전소"
}


# 모든 표준 방향을 리스트로 반환하는 함수
def get_all_directions():
    return ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]

# 각도를 방향으로 변환하는 함수
def get_wind_direction(angle):
    directions = get_all_directions()
    index = int((angle + 11.25) // 22.5) % 16
    return directions[index]

# 방사선 데이터 처리 함수 (여기서는 예시로 데이터를 가져옵니다.)
def get_radiation_data():
    client = get_mongo_connection()
    db = client['power_plant_weather']
    stats_collection = db['radiation_stats']

    # 방사선 데이터 가져오기
    data = list(stats_collection.find({}, {"_id": 0}).sort("date", DESCENDING).limit(10))
    logging.info(f"최근 데이터 {len(data)}개 가져왔습니다.")
    return data



# 방사선 평균값 계산 (예시)
def get_average_radiation():
    client = get_mongo_connection()
    db = client['radiation_statistics']
    avg_collection = db['regional_average']

    # 평균값 가져오기 (예시로 최근 날짜 데이터 가져오기)
    latest_avg_data = avg_collection.find_one(sort=[("date", DESCENDING)])
    logging.info(f"최근 평균 방사선량 데이터 가져왔습니다.")
    return latest_avg_data

def _safe_float(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default

@lru_cache(maxsize=64)
def _latest_regional_avg(gen_name: str):
    gen_name = (gen_name or "").upper()  # ← 대문자 통일
    return regional_avg_collection.find_one(
        {'genName': gen_name},
        sort=[('date', DESCENDING)]
    ) or {}

def get_rain_multiplier(gen_name: str, rainfall_mm) -> float:
    """
    rainfall_mm > 0면 비 가중치 적용, 아니면 1.0
    우선 rain_avg / no_rain_avg, 없으면 1 + percentage_increase/100
    실패 시 1.0
    """
    if _safe_float(rainfall_mm) <= 0:
        return 1.0

    gen_name = (gen_name or "").upper()  # ← 대문자 통일
    doc = _latest_regional_avg(gen_name)
    if not doc:
        return 1.0

    no_rain_avg = _safe_float(doc.get('no_rain_avg'))
    rain_avg    = _safe_float(doc.get('rain_avg'))
    if no_rain_avg > 0 and rain_avg > 0:
        return max(0.1, rain_avg / no_rain_avg)

    pct = _safe_float(doc.get('percentage_increase')) / 100.0
    return max(0.1, 1.0 + max(0.0, pct))

# ===== [ADD] 숫자 변환 유틸 =====
# 숫자 캐스팅 유틸 (파일 상단 헬퍼들 근처에 추가)
def _to_float_or_none(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

class User(UserMixin):
    def __init__(self, user_doc):
        self.id = str(user_doc['_id'])  # Flask-Login에서 user.id 필수
        self.email = user_doc['email']
        self.password = user_doc['password']

    @staticmethod
    def get_by_email(email):
        user_doc = db['users'].find_one({'email': email})
        return User(user_doc) if user_doc else None

    @staticmethod
    def get_by_id(user_id):
        try:
            user_doc = db['users'].find_one({'_id': ObjectId(user_id)})
            return User(user_doc) if user_doc else None
        except Exception:
            return None


def _compute_status_for(gen_name: str, recent_n: int = 500):
    try:
        gen_name = gen_name.upper()

        # ✅ 수정 포인트: _id 대신 실제 '시간' 필드로 먼저 정렬합니다.
        # 같은 시간일 경우에만 _id로 순서를 가립니다.
        cur = (nuclear_radiation_collection
               .find({'genName': gen_name})
               .sort([('time', -1), ('_id', -1)])
               .limit(recent_n))

        docs = list(cur)
        if not docs: return None

        # 1. 모든 데이터 추출
        all_vals = []
        for d in docs:
            v = _to_float_or_none(d.get('value'))
            if v is not None:
                all_vals.append(v)

        if not all_vals: return None

        # 2. 정렬된 것 중 0번이 무조건 최신값(100)이 되어야 함
        latest_val = all_vals[0]

        # 3. 평균 계산 (100을 제외한 나머지로만 평균 산출)
        past_vals = all_vals[1:]
        avg = sum(past_vals) / len(past_vals) if past_vals else latest_val

        # 4. 사고 판정 (100 > threshold 이므로 accident 확정)
        threshold = min(avg + 0.0973, 0.973)
        status = 'accident' if latest_val > threshold else 'normal'

        return {
            "genName": gen_name,
            "current_value": round(latest_val, 4),
            "threshold": round(threshold, 4),
            "average": round(avg, 4),
            "status": status
        }
    except Exception as e:
        logging.error(f"_compute_status_for({gen_name}) error: {e}")
        return None


# app.py에 추가할 내용

@app.route('/api/radiation_status/summary')
def get_radiation_status_summary():
    # 감시할 발전소 코드 리스트
    gen_codes = ["KR", "WS", "YK", "UJ", "SU"]
    summary = []

    for code in gen_codes:
        # 이전에 정의한 _compute_status_for 함수를 호출합니다.
        status_data = _compute_status_for(code)

        if status_data:
            summary.append(status_data)
        else:
            # 데이터가 없는 경우 기본값 처리
            summary.append({
                "genName": code,
                "status": "normal",
                "current_value": 0,
                "threshold": 0.973
            })

    # 프론트엔드 fetch에 응답
    return jsonify(summary)

@login_manager.user_loader
def load_user(user_id):
    return User.get_by_id(user_id)


@app.route('/admin/users/pending')
@login_required
@admin_required
def list_pending_users():
    pendings = users.find({'status': 'pending'}, {'password': 0})
    return render_template('admin_pending.html', users=list(pendings))

@app.route('/admin/users/<user_id>/approve', methods=['POST'])
@login_required
@admin_required
def approve_user(user_id):
    users.update_one({'_id': ObjectId(user_id)}, {'$set': {'status': 'approved'}})
    return redirect(url_for('list_pending_users'))

@app.route('/admin/users/<user_id>/reject', methods=['POST'])
@login_required
@admin_required
def reject_user(user_id):
    users.update_one({'_id': ObjectId(user_id)}, {'$set': {'status': 'rejected'}})
    return redirect(url_for('list_pending_users'))


# ---------------------------------------------------------------------
# 라우터 설정
# ---------------------------------------------------------------------
# 최신 기상 데이터 조회 (genName 기준으로)
@app.route('/api/data/<genName>/latest', methods=['GET'])
def get_latest_weather_data(genName):
    normalized_genName = genName.upper()
    logging.info(f"Received request for latest data for genName: {normalized_genName}")

    try:
        data =collection.find_one({"genName": normalized_genName}, {"_id": 0}, sort=[("time", DESCENDING)])
        if data:
            logging.info(f"Latest data found: {data}")
            return jsonify(data)
        else:
            logging.warning(f"No latest data found for genName: {normalized_genName}")
            return jsonify({"error": "No data found for this genName"}), 404
    except Exception as e:
        logging.error(f"Error fetching latest weather data for {genName}: {e}")
        return jsonify({"error": "An error occurred while fetching the data"}), 500

# 기상 데이터 필터링 조회 (genName 기준으로 날짜 필터링)
@app.route('/api/data/<genName>/filtered', methods=['GET'])
def get_filtered_weather_data(genName):
    start_date = request.args.get('start')
    end_date = request.args.get('end')
    normalized_genName = genName.upper()
    logging.info(f"Received filtered data request for genName: {normalized_genName} from {start_date} to {end_date}")

    try:
        query = {"genName": normalized_genName}
        if start_date and end_date:
            start_time_str = f"{start_date} 00:00"
            # timedelta는 이미 임포트 되었으므로, datetime.timedelta로 접근할 필요 없이 그냥 timedelta로 사용
            end_date_obj = parser.parse(end_date) + timedelta(days=1)
            end_time_str = end_date_obj.strftime("%Y-%m-%d 00:00")
            query["time"] = {"$gte": start_time_str, "$lt": end_time_str}

        data = list(backup_collection.find(query, {"_id": 0}).sort("time", DESCENDING))
        if data:
            logging.info(f"Returning {len(data)} records for genName: {normalized_genName}")
            return jsonify(data)
        else:
            logging.warning(f"No data found for genName: {normalized_genName} with given date range.")
            return jsonify({"error": "No data found for this genName"}), 404
    except Exception as e:
        logging.error(f"Error in get_filtered_weather_data: {e}")
        return jsonify({"error": "An error occurred while fetching the data"}), 500


@app.route('/favicon.ico')
def favicon():
    return app.send_static_file('favicon.ico')


# 기본 기상 데이터 페이지 (genName 기준으로)
@app.route('/<genName>', methods=['GET'])  # POST 메서드 제거
def region_data(genName):
    date_filter = request.args.get('date')
    normalized_genName = genName.upper()
    query = {"genName": normalized_genName}

    # 기본적으로 가져올 데이터의 최대 개수 (초기 페이지 로드 시)
    # 이 값을 조절하여 초기 로딩 시의 메모리 사용량을 제어합니다.
    default_limit = 200  # 예시: 200개 최신 데이터만 가져오기 (필요에 따라 조절)

    if date_filter:
        try:
            date_obj = pd.to_datetime(date_filter)
            start_str = date_obj.strftime("%Y-%m-%d 00:00")
            end_str = (date_obj + pd.Timedelta(days=1)).strftime("%Y-%m-%d 00:00")
            query["time"] = {"$gte": start_str, "$lt": end_str}

            data = list(backup_collection.find(query, {"_id": 0}).sort("time", DESCENDING))
            logging.info(f"Filtered data for {normalized_genName} on {date_filter}: {len(data)} records.")
        except Exception as e:

            logging.error(f"Date parsing error: {e}")
            # 날짜 필터가 잘못된 경우, 쿼리에서 time 필터 제거 후 default_limit 적용
            query.pop("time", None)
            data = list(backup_collection.find(query, {"_id": 0}).sort("time", DESCENDING).limit(default_limit))
            logging.warning(f"Invalid date filter, showing latest {default_limit} records for {normalized_genName}.")
    else:
        # 날짜 필터가 없는 경우 (초기 페이지 로드)
        # 최신 default_limit 개수의 데이터만 가져옵니다.
        data = list(backup_collection.find(query, {"_id": 0}).sort("time", DESCENDING).limit(default_limit))
        logging.info(f"No date filter, showing latest {default_limit} records for {normalized_genName}.")

    # 해당 발전소 이름 가져오기
    plant_name = genName_mapping.get(normalized_genName, "Unknown Plant")

    # 데이터를 템플릿으로 전달
    return render_template('weather.html', region=normalized_genName, data=data, plant_name=plant_name)
# 부산 방사선 데이터 API
@app.route('/api/busan_radiation', methods=['GET'])
@cache.cached(timeout=3600)
def get_busan_radiation_data():
    data = list(busan_radiation_collection.find({}, {"_id": 0}))
    return jsonify(data)

@app.route('/api/busan_radiation/latest', methods=['GET'])
def get_latest_radiation_data():
    try:
        latest_data = list(busan_radiation_collection.find({}, {"_id": 0}).sort("time", DESCENDING))
        data = []
        for item in latest_data:
            data.append({
                "checkTime": item.get("checkTime"),
                "locNm": item.get("locNm"),
                "data": item.get("data"),
                "aveRainData": item.get("aveRainData"),
                "latitude": item.get("lat"),
                "longitude": item.get("lng")
            })

        return jsonify(data)
    except Exception as e:
        logging.error(f"Error fetching latest radiation data: {e}")
        return jsonify({"error": "Failed to fetch latest radiation data"}), 500

@app.route('/busan_radiation_history/<locNm>', methods=['GET'])
def radiation_history_page(locNm):
    return render_template('busan_radiation_history.html', locNm=locNm)

@app.route('/api/busan_radiation/history', methods=['GET'])
def radiation_history():
    locNm = request.args.get('locNm')

    if not locNm:
        return jsonify({"error": "locNm parameter is required"}), 400

    try:
        history_data = list(busan_radiation_backup_collection.find({"locNm": locNm}).sort("time", DESCENDING))

        for item in history_data:
            item['_id'] = str(item['_id'])

        if history_data:
            return jsonify(history_data)
        else:
            return jsonify({"error": f"No data found for location {locNm}"}), 404

    except Exception as e:
        return jsonify({"error": "An error occurred while fetching the data", "details": str(e)}), 500

# 원자력 발전소 주변 방사선 데이터 API
@app.route('/api/nuclear_radiation', methods=['GET'])
def get_nuclear_radiation_data():
    genName = request.args.get('genName')
    date = request.args.get('date')

    query = {}
    if genName:
        query['genName'] = genName
    if date:
        start_time_str = f"{date} 00:00"
        end_date_obj = parser.parse(date) + timedelta(days=1)
        end_time_str = end_date_obj.strftime("%Y-%m-%d 00:00")
        query['time'] = {'$gte': start_time_str, '$lt': end_time_str}

    data = list(nuclear_radiation_collection.find(query, {"_id": 0}).sort("time", DESCENDING))
    return jsonify(data)

# 최신 방사선 데이터를 제공하는 API
@app.route('/api/nuclear_radiation/latest', methods=['GET'])
def get_latest_nuclear_radiation_data():
    try:
        latest_data = list(nuclear_radiation_collection.aggregate([
            {"$sort": {"time": -1}},
            {"$group": {
                "_id": "$genName",
                "genName": {"$first": "$genName"},
                "expl": {"$first": "$expl"},
                "time": {"$first": "$time"},
                "value": {"$first": "$value"},
                "lat": {"$first": "$lat"},
                "lng": {"$first": "$lng"}
            }}
        ]))
        return jsonify(latest_data)
    except Exception as e:
        logging.error(f"Error fetching latest nuclear radiation data: {e}")
        return jsonify({"error": "Failed to fetch latest radiation data"}), 500

@app.route('/api/nuclear_radiation/points', methods=['GET'])
def get_measurement_points():
    genName = request.args.get('genName')
    if not genName:
        logging.warning("No genName provided in the request")
        return jsonify([])

    for key, value in genName_mapping.items():
        if value == genName:
            genName = key
            break

    try:
        points = nuclear_radiation_collection.distinct('expl', {'genName': genName})

        if not points:
            logging.warning(f"No points found for genName: {genName}")
            return jsonify([])

        return jsonify(points)
    except Exception as e:
        logging.error(f"Error fetching measurement points for {genName}: {e}")
        return jsonify({"error": "Failed to fetch measurement points"}), 500

@app.route('/api/nuclear_radiation/highest', methods=['GET'])
def get_highest_radiation():
    genName = request.args.get('genName')

    if not genName:
        return jsonify({"error": "genName parameter is required"}), 400

    try:
        highest_data = nuclear_radiation_collection.find_one(
            {'genName': genName},
            {'_id': 0, 'expl': 1, 'value': 1},
            sort=[('value', DESCENDING)]
        )

        if highest_data:
            return jsonify(highest_data)
        else:
            return jsonify({"error": f"No data found for genName {genName}"}), 404

    except Exception as e:
        logging.error(f"Error fetching highest radiation data for {genName}: {e}")
        return jsonify({"error": "An error occurred while fetching the highest radiation data"}), 500
#==============================================================================
# 로그인 파트
# ---------------------------------------------------------------------

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        if users.find_one({'email': email}):
            flash('이미 등록된 이메일입니다.', 'danger') # 변경
            return render_template('signup.html') # error 파라미터 제거
        hashed_pw = bcrypt.generate_password_hash(password).decode('utf-8')
        users.insert_one({
            'email': email,
            'password': hashed_pw,
            'status': 'pending'
        })
        flash('회원가입 요청이 성공적으로 처리되었습니다. 관리자 승인 후 로그인할 수 있습니다.', 'success') # 추가
        return redirect(url_for('login'))
    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        user_doc = users.find_one({'email': email})
        # 여기에 password.encode('utf-8')이 추가되었습니다.
        if user_doc and bcrypt.check_password_hash(user_doc['password'], password.encode('utf-8')):
            st = user_doc.get('status', 'pending')
            if st == 'pending':
                flash('관리자 승인 대기 중입니다.', 'warning')
                return render_template('login.html')
            if st == 'rejected':
                flash('가입이 거부되었습니다. 문의해주세요.', 'danger')
                return render_template('login.html')
            user = User(user_doc)
            login_user(user)
            next_page = request.form.get('next') or url_for('map_home')
            flash(f'{user.email}님 환영합니다!', 'info')
            return redirect(next_page)
        else:
            flash('이메일 또는 비밀번호가 올바르지 않습니다.', 'danger')
            return render_template('login.html')
    return render_template('login.html')
@app.route('/logout')
@login_required # 로그인된 사용자만 접근 가능하도록
def logout():
    logout_user()
    flash('로그아웃 되었습니다.', 'info')
    return redirect(url_for('login')) # 로그인 페이지로 리디렉션

@app.route('/api/nuclear_radiation/highest_by_plant', methods=['GET'])
def get_highest_radiation_by_plant():
    try:
        # 발전소 리스트
        plants = ["KR", "WS", "YK", "UJ", "SU"]
        highest_radiation_by_plant = []

        # 각 발전소별로 최고 방사선량을 찾음
        for plant in plants:
            highest_data = nuclear_radiation_collection.find_one(
                {'genName': plant},
                {'_id': 0, 'genName': 1, 'expl': 1, 'time': 1, 'value': 1},
                sort=[('value', DESCENDING)]
            )
            if highest_data:
                highest_radiation_by_plant.append(highest_data)

        # 데이터가 있으면 반환
        if highest_radiation_by_plant:
            return jsonify(highest_radiation_by_plant)
        else:
            return jsonify({"error": "No data found"}), 404

    except Exception as e:
        logging.error(f"Error fetching highest radiation data by plant: {e}")
        return jsonify({"error": "An error occurred while fetching the data"}), 500


# 과거 방사선 데이터를 가져오는 API
@app.route('/api/nuclear_radiation/history', methods=['GET'])
def get_radiation_history():
    # 1. 쿼리 파라미터 가져오기
    genName = request.args.get('genName')
    expl = request.args.get('expl')
    minDate = request.args.get('minDate')
    maxDate = request.args.get('maxDate')

    if not genName or not expl:
        return jsonify([])

    # 2. 한글명을 영문 코드로 매핑 (DB 조회용)
    mapped_genName = next((code for code, name in genName_mapping.items() if name == genName), genName)

    try:
        # 3. DB 쿼리 조건 설정
        query = {'genName': mapped_genName, 'expl': expl}

        # 날짜 필터 처리
        if minDate or maxDate:
            query['time'] = {}
            if minDate: query['time']['$gte'] = f"{minDate} 00:00:00"
            if maxDate: query['time']['$lte'] = f"{maxDate} 23:59:59"

        # 4. 데이터 조회 (핵심: limit을 100으로 설정)
        # find()를 사용해야 여러 개를 가져옵니다.
        cursor = nuclear_radiation_backup_collection.find(
            query,
            {'_id': 0, 'time': 1, 'value': 1}
        ).sort('time', -1).limit(100)  # 여기서 숫자를 100으로 확실히 바꿉니다.

        history_data = list(cursor)

        # 서버 터미널에서 데이터 개수 확인용 (실행 시 검은 창에 뜸)
        print(f"--- DB 조회 결과: {len(history_data)}건 가져옴 ---")

        return jsonify(history_data)

    except Exception as e:
        print(f"DB Error: {e}")
        return jsonify([]), 500
@app.route('/nuclear_radiation_history/<genName>', methods=['GET'])
def show_radiation_history(genName):
    logging.info(f"Received request for radiation history of: {genName}")
    return render_template('nuclear_radiation_history.html', genName=genName)

@app.route('/nuclear_radiation_history/<genName>/<expl>', methods=['GET'])
def show_radiation_detail(genName, expl):
    logging.info(f"Received request for radiation history detail for: {genName}, {expl}")
    return render_template('nuclear_radiation_detail.html', genName=genName, expl=expl)

@app.route('/api/radiation_status/<genName>', methods=['GET'])
def radiation_status_api(genName):
    try:
        genName = (genName or "").upper()

        cur = nuclear_radiation_collection.find({'genName': genName}, {'_id': 0, 'value': 1})
        vals = []
        for d in cur:
            try:
                v = float(d.get('value'))
                vals.append(v)
            except (TypeError, ValueError):
                pass

        if not vals:
            return jsonify({"error": "No valid radiation values found"}), 404

        avg = sum(vals) / len(vals)
        threshold = avg + 0.097

        latest = nuclear_radiation_collection.find_one(
            {'genName': genName}, sort=[('time', DESCENDING)], projection={'value': 1}
        )
        try:
            current_value = float(latest.get('value')) if latest else 0.0
        except (TypeError, ValueError):
            current_value = 0.0

        return jsonify({
            "genName": genName,
            "current_value": round(current_value, 4),
            "threshold": round(threshold, 4),
            "status": "accident" if current_value > threshold else "normal"
        })
    except Exception as e:
        logging.error(f"[ERROR] /api/radiation_status/{genName}: {e}")
        return jsonify({"error": "Internal server error"}), 500


@app.route('/api/nuclear_radiation/backup', methods=['GET'])
def get_backup_radiation_data():
    genName = request.args.get('genName')
    expl = request.args.get('expl')

    if not genName or not expl:
        logging.warning("Missing genName or expl in the request for backup data")
        return jsonify([])

    try:
        logging.info(f"Querying backup history for genName: {genName}, expl: {expl}")

        backup_data = list(nuclear_radiation_backup_collection.find(
            {'genName': genName, 'expl': expl},
            {'_id': 0, 'time': 1, 'value': 1}
        ).sort('time', 1))

        logging.info(f"Fetched backup history data: {backup_data}")

        if not backup_data:
            logging.warning(f"No backup data found for genName: {genName}, expl: {expl}")
            return jsonify([])

        return jsonify(backup_data)
    except Exception as e:
        logging.error(f"Error fetching backup history data for {genName}, {expl}: {e}")
        return jsonify({"error": "Failed to fetch backup history data"}), 500


@app.route('/api/nuclear_radiation/highest_per_plant', methods=['GET'])
def get_highest_radiation_per_plant():
    try:
        pipeline = [
            {"$sort": {"genName": 1, "value": -1}},          # genName→value 내림차순
            {"$group": {"_id": "$genName", "doc": {"$first": "$$ROOT"}}},
            {"$replaceRoot": {"newRoot": "$doc"}},           # 최고치 문서 그대로
            {"$project": {"_id": 0, "genName": 1, "expl": 1, "time": 1, "value": 1}},
            {"$sort": {"value": -1}}
        ]
        result = list(nuclear_radiation_collection.aggregate(pipeline))
        return jsonify(result) if result else (jsonify({"error":"데이터가 없습니다."}), 404)
    except Exception as e:
        logging.error(f"발전소별 최고 방사선량 가져오기 오류: {e}")
        return jsonify({"error": "데이터를 가져오지 못했습니다."}), 500

@app.route('/api/get_recent_plant_data', methods=['GET'])
def get_recent_plant_data():
    try:
        plants = ['KR', 'WS', 'YK', 'UJ', 'SU']
        recent_data = []

        for plant in plants:
            data = collection.find_one({"genName": plant}, {"_id": 0}, sort=[("time", DESCENDING)])
            if data:
                recent_data.append({
                    "name": genName_mapping.get(plant, "Unknown Plant"),
                    "time": data.get("time"),
                    "temperature": data.get("temperature", "N/A"),
                    "humidity": data.get("humidity", "N/A"),
                    "windspeed": data.get("windspeed", "N/A"),
                    "radiation": data.get("radiation", "N/A")
                })

        return jsonify(recent_data)
    except Exception as e:
        logging.error(f"Error fetching recent plant data: {e}")
        return jsonify({"error": "Failed to fetch data"}), 500

@app.route('/')
@login_required
def map_home():
    return render_template('map.html')


@app.route('/busan_radiation')
def busan_radiation_page():
    return render_template('busan_radiation.html')

@app.route('/nuclear_radiation')
def nuclear_radiation_page():
    return render_template('nuclear_radiation.html')

#@app.route("/chat", methods=["POST"])
#def chat():
#    user_input = request.json.get("message", "").strip()
#   if not user_input:
#        return jsonify({"answer": "질문을 입력해 주세요."})
#
#    try:
#        result = get_best_match(user_input)
#        return jsonify({
#            "quesion": result["question"],
#            "answer": result["answer"],
#            "similarity": result["score"]
#        })
#
#    except Exception as e:
#        return jsonify({"answer": f"오류가 발생했습니다: {str(e)}"})


# analysis1, analysis2, analysis3, analysis4에 대한 API 엔드포인트 추가
@app.route('/analysis1')
def analysis1():
    try:
        data = list(
            analysis1_collection
            .find({}, {"_id": 0})
            .sort("checkTime", DESCENDING)   # 반드시 checkTime
        )
        return render_template('analysis1.html', data=data)
    except Exception as e:
        logging.error(f"Error in fetching data from MongoDB: {e}")
        return render_template('analysis1.html', data=[], error="Failed to load data")



@app.route('/analysis2')
def analysis2():
    try:
        min_str = request.args.get('minDate')
        max_str = request.args.get('maxDate')

        q = {}
        if min_str or max_str:
            rng = {}
            if min_str:
                rng["$gte"] = pd.to_datetime(min_str)
            if max_str:
                rng["$lt"] = pd.to_datetime(max_str) + pd.Timedelta(days=1)  # 종료일 포함
            q["Start"] = rng  # ✅ Start 로 필터

        data = list(
            analysis2_collection
            .find(q, {"_id": 0})
            .sort("Start", DESCENDING)  # ✅ Start 로 정렬
        )
        return render_template('analysis2.html', data=data)
    except Exception as e:
        logging.error(f"Error in fetching data from MongoDB: {e}")
        return render_template('analysis2.html', data=[], error="Failed to load data")

@app.route('/analysis4')
def analysis4():
    try:
        # 마찬가지로 checkTime 필드 기준으로 정렬
        data = list(
            analysis4_collection
            .find({}, {"_id": 0})
            .sort("checkTime", DESCENDING)
        )
        logging.info(f"Fetched data from analysis4_collection: {data}")
        return render_template('analysis4.html', data=data)
    except Exception as e:
        logging.error(f"Error in fetching data from MongoDB: {e}")
        return render_template('analysis4.html', data=[], error="Failed to load data")

@app.route('/export_csv/<genName>', methods=['GET'])
def export_csv_by_genName(genName):
    normalized = genName.upper()
    query = {"genName": normalized}
    sort  = [("time", DESCENDING)]

    # 한글 헤더
    header = [
        "측정시간",
        "온도 (°C)",
        "습도 (%)",
        "강수량 (mm)",
        "풍속 (m/s)",
        "풍향 (°)",
        "대기 안정도"
    ]
    fields = [
        "time",
        "temperature",
        "humidity",
        "rainfall",
        "windspeed",
        "winddirection",
        "air_stability"
    ]

    # CSV 문자열 생성
    si = io.StringIO()
    si.write('\ufeff')  # UTF-8 BOM
    writer = csv.writer(si)
    writer.writerow(header)

    cursor = backup_collection.find(query, {f: 1 for f in fields}, sort=sort)
    for doc in cursor:
        writer.writerow([
            doc.get("time", ""),
            doc.get("temperature", ""),
            doc.get("humidity", ""),
            doc.get("rainfall", ""),
            doc.get("windspeed", ""),
            doc.get("winddirection", ""),
            doc.get("air_stability", "")
        ])

    # 바디를 바이트로 변환 (utf-8-sig)
    body = si.getvalue().encode('utf-8-sig')

    # 한국어 파일명 URL-encode
    filename = f"{normalized}_기상데이터.csv"
    quoted_name = urllib.parse.quote(filename)

    # Response 생성
    resp = Response(body,
                    mimetype='application/vnd.ms-excel; charset=UTF-8')
    # RFC 5987 형식으로 한글 파일명 설정
    resp.headers.set('Content-Disposition',
                     f"attachment; filename*=UTF-8''{quoted_name}")

    return resp

# ---------------------------------------------------------------------
# 분석1 라우터 그룹
# ---------------------------------------------------------------------
@app.route('/export_analysis1_csv', methods=['GET'])
def export_analysis1_csv():
    """
    CSV 다운로드: checkTime, x, y, Energy range (Mev), radiation
    (minDate/maxDate 필터 지원)
    """
    # 날짜 파라미터 읽기
    min_str = request.args.get('minDate')
    max_str = request.args.get('maxDate')

    q = {}
    if min_str or max_str:
        rng = {}
        if min_str:
            rng["$gte"] = pd.to_datetime(min_str)
        if max_str:
            rng["$lt"] = pd.to_datetime(max_str) + pd.Timedelta(days=1)  # 종료일 포함
        q["checkTime"] = rng

    return export_csv(
        analysis1_collection,
        "analysis1_data",
        ["checkTime", "X", "Y", "Energy range (Mev)", "Radiation (nSv/h)"],
        ["checkTime", "x", "y", "Energy range (Mev)", "radiation"],
        sort=[("checkTime", DESCENDING)],
        query=q
    )

@app.route('/upload_analysis1_csv', methods=['POST'])
@login_required
def upload_analysis1_csv():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    f = request.files['file']
    if not f or f.filename == '':
        return jsonify({"error": "No selected file"}), 400
    if not f.filename.lower().endswith('.csv'):
        return jsonify({"error": "Only CSV files allowed"}), 400

    raw = f.read()
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = raw.decode('cp949')

    df = pd.read_csv(io.StringIO(text))
    df.columns = df.columns.str.replace('\ufeff', '').str.strip()
    df = df.drop(columns=['_id'], errors='ignore')

    # 헤더 매핑
    mapping = {
        "checkTime": "checkTime",
        "X": "x",
        "Y": "y",
        "Energy range (Mev)": "Energy range (Mev)",
        "Radiation (nSv/h)": "radiation"
    }
    if not set(mapping.keys()).issubset(df.columns):
        return jsonify({"error": "Unexpected CSV headers", "headers": df.columns.tolist()}), 400
    df.rename(columns=mapping, inplace=True)

    # 타입 변환
    df['checkTime'] = pd.to_datetime(df['checkTime'], errors='coerce')
    for col in ['x', 'y', 'Energy range (Mev)', 'radiation']:
        df[col] = pd.to_numeric(df[col], errors='coerce')

    # NaN/무효행 정리
    df = df.dropna(subset=['checkTime'])

    # ✅ 여기서 업로드 시각(inputTime) 자동 추가 (KST)
    df['inputTime'] = now_kst()

    uploader = current_user.email if hasattr(current_user, "is_authenticated") and current_user.is_authenticated else None
    df['uploader'] = uploader

    # MongoDB에 직접 insert
    records = df.to_dict(orient='records')
    if not records:
        return jsonify({"error": "No valid rows found in CSV"}), 400

    try:
        result = analysis1_collection.insert_many(records)
        inserted = len(result.inserted_ids)
        app.logger.info(f"/upload_analysis1_csv inserted={inserted}")
        return jsonify({"message": f"업로드 완료: {inserted}건"}), 200
    except Exception as e:
        app.logger.error(f"upload insert error: {e}")
        return jsonify({"error": "DB insert failed"}), 500


# ---------------------------------------------------------------------
# 분석2 라우터 그룹
# ---------------------------------------------------------------------
# -- CSV 내보내기 (영문 헤더) --
@app.route('/export_analysis2_csv', methods=['GET'])
def export_analysis2_csv():
    # ▼ 날짜 파라미터 읽어서 Start 기준으로 필터 생성
    min_str = request.args.get('minDate')
    max_str = request.args.get('maxDate')

    q = {}
    if min_str or max_str:
        rng = {}
        if min_str:
            rng["$gte"] = pd.to_datetime(min_str)
        if max_str:
            rng["$lt"]  = pd.to_datetime(max_str) + pd.Timedelta(days=1)  # 종료일 포함
        q["Start"] = rng

    return export_csv(
        analysis2_collection,
        "analysis2_data",
        ["DroneCode","Start","Stop","MesurementTime","Latitude","Longitude",
         "Altitude","East","West","South","North","Average"],
        ["DroneCode","Start","Stop","MesurementTime","Latitude","Longitude",
         "Altitude","East","West","South","North","Average"],
        sort=[("Start", DESCENDING)],
        query=q  # ▲ 추가
    )

# -- CSV 업로드 (영문 헤더 매핑) --
@app.route('/upload_analysis2_csv', methods=['POST'])
@login_required
def upload_analysis2_csv():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    f = request.files['file']
    if not f or f.filename == '':
        return jsonify({"error": "No selected file"}), 400
    if not f.filename.lower().endswith('.csv'):
        return jsonify({"error": "Only CSV files allowed"}), 400

    raw_bytes = f.read()
    try:
        text = raw_bytes.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = raw_bytes.decode('cp949')

    df = pd.read_csv(io.StringIO(text))
    df.columns = df.columns.str.replace('\ufeff', '').str.strip()
    df = df.drop(columns=['_id'], errors='ignore')

    # ===== 1) 헤더 표준화 =====
    def norm(s: str) -> str:
        s = (s or '').strip().lower()
        s = s.replace('μ', 'u').replace('µ', 'u')   # micro 통일
        return ''.join(ch for ch in s if ch.isalnum())

    alias_map = {
        # 드론 코드
        'dronecode': 'DroneCode',
        'dronecod':  'DroneCode',
        '드론코드':    'DroneCode',

        # 시간
        'start':        'Start',
        '측정시작시간':   'Start',
        '측정시작':      'Start',

        'stop':         'Stop',
        '측정종료시간':    'Stop',
        '측정종료':       'Stop',

        'mesurementtime':  'MesurementTime',
        'measurementtime': 'MesurementTime',
        '측정시간':          'MesurementTime',

        # 좌표/고도
        'latitude':  'Latitude',
        '위도':        'Latitude',
        'longitude': 'Longitude',
        '경도':        'Longitude',
        'altitude':  'Altitude',
        '고도':        'Altitude',
        '고도m':       'Altitude',

        # 동/서/남/북
        'east':       'East',
        '동':          'East',
        'eastusvh':   'East',
        '동usvh':      'East',

        'west':       'West',
        '서':          'West',
        'westusvh':   'West',
        '서usvh':      'West',

        'south':      'South',
        '남':          'South',
        'southusvh':  'South',
        '남usvh':       'South',

        'north':      'North',
        '북':          'North',
        'northusvh':  'North',
        '북usvh':       'North',

        # 평균
        'average':                 'Average',
        '평균':                     'Average',
        '평균방사선량':              'Average',
        '평균방사선량usvh':          'Average',
    }

    rename_map = {}
    for col in list(df.columns):
        key = norm(col)
        if key in alias_map:
            rename_map[col] = alias_map[key]

    df.rename(columns=rename_map, inplace=True)

    # ===== 2) 타입 변환 =====
    for dt_col in ['Start', 'Stop']:
        if dt_col in df.columns:
            df[dt_col] = pd.to_datetime(df[dt_col], errors='coerce')

    # MesurementTime 없으면 Start/Stop으로 계산
    if 'MesurementTime' not in df.columns and {'Start', 'Stop'}.issubset(df.columns):
        def fmt_duration(td):
            if pd.isna(td):
                return None
            mins = int(td.total_seconds() // 60)
            return f"{mins // 60}:{mins % 60:02d}"
        df['MesurementTime'] = (df['Stop'] - df['Start']).apply(fmt_duration)

    for num_col in ['Latitude','Longitude','Altitude','East','West','South','North','Average']:
        if num_col in df.columns:
            df[num_col] = pd.to_numeric(df[num_col], errors='coerce')

    # Average 자동 보정(없으면 동서남북 평균)
    if 'Average' not in df.columns and set(['East','West','South','North']).issubset(df.columns):
        df['Average'] = df[['East','West','South','North']].mean(axis=1)

    # 좌표 없는 행 제거(옵션)
    if {'Latitude','Longitude'}.issubset(df.columns):
        df = df[~(df['Latitude'].isna() | df['Longitude'].isna())]

    # ✅ 여기서 업로드 시각(inputTime) 자동 추가 (KST)
    df['inputTime'] = now_kst()

    # 업로드한 계정 이메일
    uploader = current_user.email if hasattr(current_user, "is_authenticated") and current_user.is_authenticated else None
    df['uploader'] = uploader

    # ===== 3) 업로드 =====
    buf = io.StringIO()
    df.to_csv(buf, index=False, encoding='utf-8-sig')
    buf.seek(0)

    return upload_csv(analysis2_collection, buf, {
        'DroneCode':'DroneCode',
        'Start':'Start',
        'Stop':'Stop',
        'MesurementTime':'MesurementTime',
        'Latitude':'Latitude',
        'Longitude':'Longitude',
        'Altitude':'Altitude',
        'East':'East',
        'West':'West',
        'South':'South',
        'North':'North',
        'Average':'Average',
        'inputTime': 'inputTime',
        'uploader': 'uploader',  # 여기 추가
    })

# ---------------------------------------------------------------------
# 분석4 라우터 그룹
# ---------------------------------------------------------------------
# -- CSV 내보내기 (영문 헤더) --
@app.route('/export_analysis4_csv', methods=['GET'])
def export_analysis4_csv():
    return export_csv(
        analysis4_collection,
        "analysis4_data",
        # CSV 헤더 (영어)
        ["checkTime", "lat", "lng", "radiation"],
        # 필드 이름 (DB 저장 필드)
        ["checkTime", "lat", "lng", "radiation"],
        sort=[("checkTime", DESCENDING)]
    )

# -- CSV 업로드 (영문 헤더 매핑) --
@app.route('/upload_analysis4_csv', methods=['POST'])
@login_required
def upload_analysis4_csv():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    f = request.files['file']
    if not f or f.filename == '':
        return jsonify({"error": "No selected file"}), 400
    if not f.filename.lower().endswith('.csv'):
        return jsonify({"error": "Only CSV files allowed"}), 400

    # 1) 바이너리 읽기
    raw_bytes = f.read()
    # 2) BOM 제거 → CP949 fallback
    try:
        text = raw_bytes.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = raw_bytes.decode('cp949')

    # 3) DataFrame 생성
    df = pd.read_csv(io.StringIO(text))
    df.columns = df.columns.str.replace('\ufeff', '').str.strip()
    df = df.drop(columns=['_id'], errors='ignore')

    # 4) 컬럼 매핑: 업로드된 CSV 의 '영문 헤더' → DB 필드명
    mapping = {
        "checkTime": "checkTime",
        "lat":       "lat",
        "lng":       "lng",
        "radiation": "radiation",
        "inputTime": "inputTime",
        "uploader": "uploader",  # 추가
    }

    # checkTime/lat/lng/radiation 네 개는 최소 있어야 하므로 이쪽만 확인
    if not {"checkTime", "lat", "lng", "radiation"}.issubset(df.columns):
        return jsonify({
            "error":   "Unexpected CSV headers",
            "headers": df.columns.tolist()
        }), 400

    df.rename(columns=mapping, inplace=True)

    # 5) 타입 변환
    df['checkTime'] = pd.to_datetime(df['checkTime'], errors='coerce')
    for col in ['lat', 'lng', 'radiation']:
        df[col] = pd.to_numeric(df[col], errors='coerce')

    # 6) 업로드(입력) 시각 컬럼 추가 – CSV에는 없지만 서버에서 생성 (KST)
    df['inputTime'] = now_kst()


    # 7) 버퍼에 다시 CSV 작성
    buf = io.StringIO()
    df.to_csv(buf, index=False, encoding='utf-8-sig')
    buf.seek(0)

    # 8) MongoDB 업로드
    return upload_csv(analysis4_collection, buf, mapping)

# ---------------------------------------------------------------------
# 구호소 평가
# ---------------------------------------------------------------------
# 발전소 선택 페이지
@app.route('/optimal_shelter_evaluation')
def optimal_shelter_evaluation():
    sites = list(power_plants.keys())  # ['고리','월성','한빛','한울']
    return render_template('optimal_shelter_evaluation.html', sites=sites)

# 선택한 발전소 결과 페이지
@app.route('/optimal_shelter_result/<site>')
def optimal_shelter_result(site):
    # 1) TOP5 정보
    top5     = compute_top5_for(site)
    # 2) folium map HTML
    map_html = generate_topsis_map_html(site)
    return render_template(
        'optimal_shelter_result.html',
        map_html=map_html,
        top5_shelters=top5
    )
# ---------------------------------------------------------------------
# 바람 장미
# ---------------------------------------------------------------------
@app.route('/windRose/<genName>', methods=['GET'])
def wind_rose(genName):  # region -> genName 으로 변경
    normalized_genName = genName.upper()  # region -> genName 으로 변경
    logging.info(f"Generating wind rose for genName: {normalized_genName}")  # region -> genName 으로 변경

    try:
        data = list(backup_collection.find({"genName": normalized_genName}, {"_id": 0, "winddirection": 1, "windspeed": 1}))  # region -> genName 으로 변경

        if not data:
            logging.warning(f"No wind direction data found for genName: {normalized_genName}")  # region -> genName 으로 변경
            return render_template('wind_rose_chart.html', genName=normalized_genName, wind_data={}, error="데이터가 없습니다.")  # region -> genName 으로 변경

        wind_speed_bins = {
            "0.5-1.4 m/s": {"min": 0.5, "max": 1.4},
            "1.5-3.3 m/s": {"min": 1.5, "max": 3.3},
            "3.4-5.4 m/s": {"min": 3.4, "max": 5.4},
            "5.5-7.9 m/s": {"min": 5.5, "max": 7.9},
            "8.0+ m/s": {"min": 8.0, "max": float('inf')}
        }

        direction_bins = {direction: {bin_name: 0 for bin_name in wind_speed_bins} for direction in get_all_directions()}
        total_counts = 0

        for entry in data:
            angle = entry.get("winddirection")
            speed = entry.get("windspeed")
            if isinstance(angle, (int, float)) and isinstance(speed, (int, float)):
                direction = get_wind_direction(angle)
                for bin_name, bin_range in wind_speed_bins.items():
                    if bin_range["min"] <= speed < bin_range["max"]:
                        direction_bins[direction][bin_name] += 1
                        total_counts += 1
                        break

        if total_counts == 0:
            logging.warning(f"No valid wind direction and speed data found for genName: {normalized_genName}")  # region -> genName 으로 변경
            return render_template('wind_rose_chart.html', genName=normalized_genName, wind_data={}, error="유효한 데이터가 없습니다.")  # region -> genName 으로 변경

        wind_data_percent = {}
        for direction, bins in direction_bins.items():
            wind_data_percent[direction] = {}
            for bin_name, count in bins.items():
                wind_data_percent[direction][bin_name] = round((count / total_counts) * 100, 2)

        logging.debug(f"Wind direction and speed percentages for {normalized_genName}: {wind_data_percent}")  # region -> genName 으로 변경

        return render_template('wind_rose_chart.html', genName=normalized_genName, wind_data=wind_data_percent)  # region -> genName 으로 변경

    except Exception as e:
        logging.error(f"Error generating wind rose for {normalized_genName}: {e}")  # region -> genName 으로 변경
        return render_template('wind_rose_chart.html', genName=normalized_genName, wind_data={}, error="데이터를 불러오는 중 오류가 발생했습니다.")  # region -> genName 으로 변경
# ---------------------------------------------------------------------
# Spectrum
# ---------------------------------------------------------------------
@app.route('/upload_spectrum', methods=['POST'])
def upload_spectrum():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    file = request.files['file']

    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    try:
        spect_data = pd.read_csv(file)

        if 'Channel' not in spect_data.columns or 'count' not in spect_data.columns:
            return jsonify({"error": "CSV must contain 'Channel' and 'count' columns."}), 400

        max_energy = 3  # MeV
        num_channels = 1024
        channel_width = max_energy / num_channels
        spect_data['energy'] = spect_data['Channel'] * channel_width

        window_size = 21
        poly_order = 2
        spect_data['smoothed_count'] = savgol_filter(spect_data['count'], window_size, poly_order)

        peaks, _ = find_peaks(spect_data['smoothed_count'], height=30)

        plt.rcParams['font.family'] = 'Times New Roman'
        plt.rcParams['font.size'] = 12

        plt.figure(figsize=(10, 6))
        plt.plot(spect_data['energy'], spect_data['smoothed_count'], label='Smoothed Spectrum')
        plt.scatter(spect_data['energy'].iloc[peaks], spect_data['smoothed_count'].iloc[peaks], color='red', label='Peaks')
        plt.title('Energy Spectrum')
        plt.xlabel('Energy (MeV)')
        plt.ylabel('Counts')
        plt.legend()
        plt.grid()

        plot_path = 'static/spectrum_plot.png'
        plt.savefig(plot_path)
        plt.close()

        identified_nuclides = []
        nuclide_info_dict = {
            "I-131": {
                "physical_half_life": "8.02일",
                "biological_half_life": "5일",
                "effective_half_life": "3.08일",
                "gamma_energy": "364 keV",
                "description": "갑상선에 축적되며, 주로 방사선 치료에 사용된다."
            },
            "Cs-134": {
                "physical_half_life": "2.07년",
                "biological_half_life": "10년",
                "effective_half_life": "1.71년",
                "gamma_energy": "605 keV",
                "description": "환경에 오랜 시간 잔존하며, 식물과 동물에 축적될 수 있다."
            },
            "Cs-137": {
                "physical_half_life": "30.17년",
                "biological_half_life": "110일",
                "effective_half_life": "0.298년 (약 109일)",
                "gamma_energy": "662 keV",
                "description": "생물체에 축적되며, 방사선 오염의 주요 원인 중 하나이다."
            },
            "Co-60": {
                "physical_half_life": "5.27년",
                "biological_half_life": "다양함",
                "effective_half_life": "다양함",
                "gamma_energy": "1.173 및 1.332 MeV",
                "description": "주로 방사선 치료에 사용되며, 방사능 위험이 있다."
            },
            "Ru-106": {
                "physical_half_life": "373.6일",
                "biological_half_life": "다양함",
                "effective_half_life": "다양함",
                "gamma_energy": "500 keV",
                "description": "핵반응에서 생성되며, 다양한 방사선 치료에 사용된다."
            }
        }

        for peak in peaks:
            peak_energy = spect_data.loc[peak, 'energy']
            logging.info(f"Detected peak energy: {peak_energy:.3f} MeV")

            if 0.62 <= peak_energy <= 0.69:
                identified_nuclides.append("Cs-137")
            elif 0.60 <= peak_energy <= 0.61:
                identified_nuclides.append("Cs-134")
            elif 1.173 <= peak_energy <= 1.332:
                identified_nuclides.append("Co-60")
            elif 0.511 <= peak_energy <= 0.515:
                identified_nuclides.append("Ru-106")
            elif 0.36 <= peak_energy <= 0.37:
                identified_nuclides.append("I-131")

        nuclide_info = ', '.join(set(identified_nuclides)) if identified_nuclides else "핵종 없음"

        return jsonify(
            {"message": "File successfully uploaded", "plot_url": f"/{plot_path}", "nuclide": nuclide_info,
             "nuclide_info_table": {key: nuclide_info_dict[key] for key in identified_nuclides if key in nuclide_info_dict}}), 200

    except Exception as e:
        logging.error(f"Error processing uploaded spectrum: {e}")
        return jsonify({"error": f"Failed to process the uploaded file: {str(e)}"}), 500

@app.route('/spectrum')
def spectrum_page():
    return render_template('spectrum.html')


# ---------------------------------------------------------------------
# Dose change
# ---------------------------------------------------------------------
@app.route('/radiation_summary')
def radiation_summary_page():
    try:
        recent = list(stats_collection.find({}, {"_id": 0}).sort("date", DESCENDING).limit(35))

        cleaned = []
        for it in recent:
            it['value'] = 0 if it.get('value') is None else it['value']
            it['rain'] = False if it.get('rain') is None else it['rain']
            if it.get('genName') is None or it.get('value') == 'Undefined':
                continue
            cleaned.append(it)

        avg_results = list(regional_avg_collection.find({}, {"_id": 0}).sort("date", DESCENDING))
        return render_template('radiation_summary.html', recent_data=cleaned, avg_results=avg_results)

    except Exception as e:
        logging.error(f"Error fetching data: {e}")
        return render_template('radiation_summary.html', error="Failed to load data")
@app.route('/accident_select')
def accident_select():
    return render_template('accident_select.html')  # accident_select.html 페이지를 렌더링


# 최신 방사선 데이터를 가져오고 평균값을 계산하는 코드 수정
@app.route('/accident_result/<genName>', methods=['GET'])
def accident_result_page(genName):
    try:
        # genName 대문자 통일 (DB 키 일관성)
        genName = genName.upper()
        logging.info(f"[accident_result] plant={genName}")

        # 1) 최신 방사선값(판정용)
        latest_rad = nuclear_radiation_collection.find_one(
            {'genName': genName},
            sort=[('time', DESCENDING)],
            projection={'value': 1, 'time': 1}
        )
        if not latest_rad or latest_rad.get('value') is None:
            logging.warning("No latest nuclear_radiation value")
            return render_template('accident_result.html', genName=genName,
                                   error="최신 방사선 데이터가 없습니다.")

        current_value = float(latest_rad['value'])

        # 2) 기준선: 과거 평균(필요하면 최근 N건/최근 N일로 제한 가능)
        values_cur = nuclear_radiation_collection.find(
            {'genName': genName},
            {'value': 1}
        )
        radiation_values = [float(d['value']) for d in values_cur if d.get('value') is not None]

        if not radiation_values:
            return render_template('accident_result.html', genName=genName,
                                   error="평균 계산용 데이터가 없습니다.")

        average_radiation = sum(radiation_values) / len(radiation_values)

        # 3) 강우량(표시용)
        weather = db.NPP_weather.find_one(
            {'genName': genName},
            sort=[('time', DESCENDING)],
            projection={'rainfall': 1}
        )
        rainfall = (weather or {}).get('rainfall', 0)

        # 4) 임계치 & 판정
        threshold = average_radiation + 0.097
        status = "accident" if current_value > threshold else "normal"

        result = {
            "status": status,
            "message": "사고 발생 가능성 있음" if status == "accident" else "정상",
            "radiation_level": round(current_value, 4),     # ← 최신값으로 표시
            "average": round(average_radiation, 4),         # 참고용(디버깅/설명)
            "threshold": round(threshold, 4),
            "rainfall": rainfall
        }

        logging.info(f"[accident_result] {result}")
        return render_template('accident_result.html', genName=genName, result=result)

    except PyMongoError as pe:
        logging.error(f"Database error for {genName}: {pe}")
        return render_template('accident_result.html', genName=genName,
                               error="Database error occurred.")
    except Exception as e:
        logging.error(f"Error fetching data for {genName}: {e}")
        return render_template('accident_result.html', genName=genName,
                               error="An unexpected error occurred.")

@app.route('/api/radiation_status/summary', methods=['GET'])
def radiation_status_summary():
    plants = list(genName_mapping.keys())  # ['KR','WS','YK','UJ','SU']
    out = []
    for g in plants:
        st = _compute_status_for(g, recent_n=500)
        if st:
            out.append(st)
    return jsonify(out)

@app.route('/upload/gif', methods=['POST'])
def upload_gif():
    f = request.files.get('file')
    if not f:
        return jsonify({"error": "파일 없음"}), 400
    if f.mimetype != 'image/gif' and not f.filename.lower().endswith('.gif'):
        return jsonify({"error": "GIF만 업로드"}), 400

    kst_now = now_kst()
    name = f"{kst_now:%Y%m%d-%H%M%S}-{secure_filename(f.filename)}"
    save_path = os.path.join(app.config['UPLOAD_FOLDER'], name)
    f.save(save_path)

    rel_url = url_for('serve_uploads', filename=name)

    try:
        uploads_collection.insert_one({
            "type": "gif",
            "filename": name,
            "url": rel_url,
            "uploader": (current_user.email if hasattr(current_user, "is_authenticated") and current_user.is_authenticated else None),
            "created_at": kst_now
        })
    except Exception as e:
        app.logger.error(f"[uploads_collection] insert error: {e}")

    return jsonify({"url": rel_url}), 200

@app.route('/api/uploads/gifs', methods=['GET'])
def list_uploaded_gifs():
    try:
        limit = int(request.args.get('limit', 24))
    except ValueError:
        limit = 24

    # 파일 없는 항목이 있을 수 있으니 여유 있게 더 가져오기
    cur = (uploads_collection
           .find({"type": "gif"})
           .sort("created_at", DESCENDING)
           .limit(limit * 3))

    out = []
    for d in cur:
        fname = d.get("filename")
        if not fname:
            continue

        fpath = os.path.join(app.config['UPLOAD_FOLDER'], fname)
        if not os.path.exists(fpath):
            # 필요하면 DB도 같이 정리하고 싶을 때:
            # uploads_collection.delete_one({"_id": d["_id"]})
            continue  # 파일이 없으면 갤러리에서 제외

        created_at = d.get("created_at")

        # url 필드가 비어 있다면 안전하게 다시 생성
        url = d.get("url") or url_for('serve_uploads', filename=fname)

        out.append({
            "url": url,
            "filename": fname,
            "uploader": d.get("uploader"),
            "created_at": created_at.isoformat() if created_at else None
        })

        if len(out) >= limit:
            break

    return jsonify(out), 200


@app.route('/uploads/<path:filename>')
def serve_uploads(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/workers')
@login_required
def workers_page():
    """
    방재요원 정보 페이지.
    쿼리 파라미터:
      - minDate, maxDate: checkTime 기준(YYYY-MM-DD)
    문자열로 저장된 checkTime도 $expr/$toDate로 필터링.
    """
    try:
        q_and = []   # 다른 필터가 생기면 여기에 and 조건 추가
        min_str = request.args.get('minDate')
        max_str = request.args.get('maxDate')

        # 날짜 파싱(+유효성)
        min_dt = pd.to_datetime(min_str, errors='coerce') if min_str else None
        max_dt = pd.to_datetime(max_str, errors='coerce') if max_str else None

        if min_str and pd.isna(min_dt):
            return render_template('workers.html', data=[], error="Invalid minDate format. Use YYYY-MM-DD")
        if max_str and pd.isna(max_dt):
            return render_template('workers.html', data=[], error="Invalid maxDate format. Use YYYY-MM-DD")

        # 뒤바뀐 범위 자동 스왑
        if min_dt is not None and max_dt is not None and min_dt > max_dt:
            min_dt, max_dt = max_dt, min_dt

        # 날짜 조건(둘 중 하나만 있어도 동작)
        if min_dt is not None or max_dt is not None:
            or_clauses = []

            # 1) BSON Date 타입 직접 비교
            rng = {}
            if min_dt is not None:
                rng["$gte"] = min_dt
            if max_dt is not None:
                rng["$lt"]  = max_dt + pd.Timedelta(days=1)  # 종료일 포함
            if rng:
                or_clauses.append({"checkTime": rng})

            # 2) 문자열인 경우 $toDate 변환해서 비교 ($expr 사용)
            expr_ands = []
            if min_dt is not None:
                expr_ands.append({"$gte": [ {"$toDate": "$checkTime"}, min_dt ]})
            if max_dt is not None:
                expr_ands.append({"$lt":  [ {"$toDate": "$checkTime"}, max_dt + pd.Timedelta(days=1) ]})
            if expr_ands:
                or_clauses.append({"$expr": {"$and": expr_ands}})

            if or_clauses:
                q_and.append({"$or": or_clauses})

        # 최종 쿼리
        q = {"$and": q_and} if q_and else {}

        data = list(
            workers_collection
            .find(q, {'_id': 0})
            .sort([('checkTime', DESCENDING), ('code', 1)])
        )
        return render_template('workers.html', data=data)
    except Exception as e:
        logging.error(f"[workers_page] DB error: {e}")
        return render_template('workers.html', data=[], error="Failed to load data")

@app.route('/export_workers_csv', methods=['GET'])
@login_required
def export_workers_csv():
    """
    방재요원 CSV 다운로드
    - 선택 파라미터:
      * codes: 콤마로 구분된 코드 목록 (예: KR01,KR02)
      * minDate, maxDate: checkTime 기준 (YYYY-MM-DD)
    문자열로 저장된 checkTime도 $expr/$toDate로 필터링.
    """
    q_and = []

    # 코드 필터
    codes = request.args.get('codes')
    if codes:
        code_list = [c.strip() for c in codes.split(',') if c.strip()]
        if code_list:
            q_and.append({'code': {'$in': code_list}})

    # 날짜 필터 + 유효성 가드
    min_str = request.args.get('minDate')
    max_str = request.args.get('maxDate')
    min_dt = pd.to_datetime(min_str, errors='coerce') if min_str else None
    max_dt = pd.to_datetime(max_str, errors='coerce') if max_str else None

    if min_str and pd.isna(min_dt):
        return jsonify({"error": "Invalid minDate format. Use YYYY-MM-DD."}), 400
    if max_str and pd.isna(max_dt):
        return jsonify({"error": "Invalid maxDate format. Use YYYY-MM-DD."}), 400

    # 뒤바뀐 범위 자동 스왑
    if min_dt is not None and max_dt is not None and min_dt > max_dt:
        min_dt, max_dt = max_dt, min_dt

    if min_dt is not None or max_dt is not None:
        or_clauses = []

        # 1) BSON Date
        rng = {}
        if min_dt is not None:
            rng["$gte"] = min_dt
        if max_dt is not None:
            rng["$lt"] = max_dt + pd.Timedelta(days=1)
        if rng:
            or_clauses.append({"checkTime": rng})

        # 2) 문자열 → $toDate
        expr_ands = []
        if min_dt is not None:
            expr_ands.append({"$gte": [{"$toDate": "$checkTime"}, min_dt]})
        if max_dt is not None:
            expr_ands.append({"$lt": [{"$toDate": "$checkTime"}, max_dt + pd.Timedelta(days=1)]})
        if expr_ands:
            or_clauses.append({"$expr": {"$and": expr_ands}})

        if or_clauses:
            q_and.append({"$or": or_clauses})

    q = {"$and": q_and} if q_and else {}

    return export_csv(
        workers_collection,
        "workers_data",
        # ✅ '측정시간' 추가
        ["측정시간", "선량계 코드", "위도", "경도", "현재 방사선량(nSv/h)", "누적 방사선량(nSv)"],
        ["checkTime", "code", "lat", "lng", "doseRate", "cumulativeDose"],
        sort=[("checkTime", DESCENDING)],
        query=q
    )

@app.route('/source_tracking')
def source_tracking():
    # MCMC 분석 전용 페이지를 렌더링
    # 이 페이지는 기존 index.html과 비슷하지만 '분석 실행' 버튼과 '결과 차트' 영역이 추가됩니다.
    return render_template('source_tracking.html')

@app.route('/api/run_mcmc', methods=['POST'])
def run_mcmc_api():
    # 1. 클라이언트로부터 발전소 ID 수신
    plant_id = request.json.get('plant_id')
    
    # 2. 발전소별 기상/선량 데이터 가져오기 (소팅)
    # 3. 위경도 -> UTM 변환 후 MCMC 엔진 실행
    # 4. 결과(위경도, Q값) 반환
    return jsonify({"lat": 35.3213, "lon": 129.2941, "strength": "4.7e9"})

@app.route('/admin/workers/normalize_checktime', methods=['POST'])
@login_required
@admin_required
def normalize_workers_checktime():
    """
    문자열 checkTime -> BSON Date로 마이그레이션
    - checkTime 타입이 str인 문서를 찾아 파싱 후 datetime으로 저장
    - ISO 형식(YYYY-MM-DD HH:mm[:ss]) 가정, 파싱 실패 문서는 건너뜀
    """
    from pymongo import UpdateOne

    batch = []
    scanned = 0
    converted = 0
    skipped = 0

    # 문자열 타입만 스캔
    cursor = workers_collection.find(
        {"$expr": {"$eq": [{"$type": "$checkTime"}, "string"]}},
        {"_id": 1, "checkTime": 1}
    )

    for doc in cursor:
        scanned += 1
        s = doc.get("checkTime")
        if not isinstance(s, str):
            skipped += 1
            continue
        # pandas로 관대하게 파싱
        dt = pd.to_datetime(s, errors='coerce')
        if pd.isna(dt):
            skipped += 1
            continue
        batch.append(UpdateOne({"_id": doc["_id"]}, {"$set": {"checkTime": dt.to_pydatetime()}}))

        # 대량일 때는 배치 커밋
        if len(batch) >= 1000:
            res = workers_collection.bulk_write(batch, ordered=False)
            converted += res.modified_count
            batch.clear()

    if batch:
        res = workers_collection.bulk_write(batch, ordered=False)
        converted += res.modified_count
        batch.clear()

    msg = {
        "scanned": scanned,
        "converted": converted,
        "skipped": skipped
    }
    logging.info(f"[normalize_workers_checktime] {msg}")
    return jsonify({"message": "OK", **msg}), 200

# 방재요원 CSV 업로드 (checkTime은 datetime으로 저장)
# 방재요원 CSV 업로드 (checkTime은 datetime + inputTime(KST) 저장)
@app.route('/upload_workers_csv', methods=['POST'])
@login_required
def upload_workers_csv():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    f = request.files['file']
    if not f or f.filename == '':
        return jsonify({"error": "No selected file"}), 400
    if not f.filename.lower().endswith('.csv'):
        return jsonify({"error": "Only CSV files allowed"}), 400

    raw = f.read()
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = raw.decode('cp949')

    df = pd.read_csv(io.StringIO(text))
    df.columns = df.columns.str.replace('\ufeff', '').str.strip()
    df = df.drop(columns=['_id'], errors='ignore')

    # ===== 헤더 표준화(영/한글/표기 차이 흡수) =====
    def norm(s: str) -> str:
        s = (s or '').strip().lower()
        s = s.replace('μ', 'u').replace('µ', 'u')  # micro 통일
        return ''.join(ch for ch in s if ch.isalnum())

    alias = {
        # 시간
        'checktime': 'checkTime',
        '측정시간': 'checkTime',
        '시간': 'checkTime',
        '일시': 'checkTime',
        '측정일시': 'checkTime',
        'timestamp': 'checkTime',
        'datetime': 'checkTime',
        'date': 'checkTime',

        # 코드
        'code': 'code',
        'devicecode': 'code',
        'genname': 'code',
        '선량계코드': 'code',

        # 좌표
        'lat': 'lat',
        'latitude': 'lat',
        '위도': 'lat',
        'lng': 'lng',
        'longitude': 'lng',
        '경도': 'lng',

        # 현재/누적
        'doserate': 'doseRate',
        'radiation': 'doseRate',
        '현재방사선량': 'doseRate',
        '현재방사선량nsvh': 'doseRate',

        'cumulativedose': 'cumulativeDose',
        'cumulativeradiation': 'cumulativeDose',
        '누적방사선량': 'cumulativeDose',
        '누적방사선량nsv': 'cumulativeDose',
    }

    rename_map = {}
    for c in list(df.columns):
        k = norm(c)
        if k in alias:
            rename_map[c] = alias[k]
    df.rename(columns=rename_map, inplace=True)

    # 최소 필요 컬럼 확인
    required = {'checkTime', 'code'}
    if not required.issubset(df.columns):
        return jsonify({
            "error": "Missing required columns",
            "required": sorted(list(required)),
            "got": df.columns.tolist()
        }), 400

    # ===== 타입 변환 =====
    df['checkTime'] = pd.to_datetime(df['checkTime'], errors='coerce')
    for col in ['lat', 'lng', 'doseRate', 'cumulativeDose']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    # 유효 행만 남기기 (시간/코드 필수)
    df = df.dropna(subset=['checkTime', 'code'])
    if df.empty:
        return jsonify({"error": "No valid rows after type conversion"}), 400

    # tz 제거
    df['checkTime'] = df['checkTime'].dt.tz_localize(None)

    # ✅ 업로드 시각(KST) 컬럼 추가 (이번 배치 공통)
    df['inputTime'] = now_kst()

    uploader = current_user.email if hasattr(current_user, "is_authenticated") and current_user.is_authenticated else None
    df['uploader'] = uploader

    # ===== 업서트(같은 code+checkTime이면 갱신) =====
    from pymongo import UpdateOne
    ops = []
    keep_cols = [c for c in [
        'checkTime', 'code', 'lat', 'lng', 'doseRate', 'cumulativeDose', 'inputTime', 'uploader'
    ] if c in df.columns]

    for r in df[keep_cols].to_dict('records'):
        key = {'code': r.get('code'), 'checkTime': r.get('checkTime')}
        ops.append(UpdateOne(key, {'$set': r}, upsert=True))

    res = workers_collection.bulk_write(ops, ordered=False)
    return jsonify({
        "message": "업로드 완료",
        "upserted": res.upserted_count,
        "modified": res.modified_count
    }), 200

# =============================================================================
# [MCMC 엔진] 물리 모델 및 역추적 클래스 (직접 추가 부분)
# =============================================================================
from pyproj import Transformer

# 위경도 <-> UTM 52N 변환기 (한국 지역 표준)
transformer_to_utm = Transformer.from_crs("epsg:4326", "epsg:32652", always_xy=True)
transformer_to_wgs84 = Transformer.from_crs("epsg:32652", "epsg:4326", always_xy=True)

def get_plume_coordinates(x_map, y_map, sx, sy, wind_dir_meteo):
    """지도 좌표를 플룸 중심축 좌표로 변환"""
    flow_angle_rad = np.radians(270 - wind_dir_meteo)
    dx = x_map - sx
    dy = y_map - sy
    downwind = dx * np.cos(flow_angle_rad) + dy * np.sin(flow_angle_rad)
    crosswind = -dx * np.sin(flow_angle_rad) + dy * np.cos(flow_angle_rad)
    return downwind, crosswind

def gaussian_plume(x, y, H, Q, U):
    """가우시안 플룸 확산 모델"""
    if x <= 0: return 0.0
    sigma_y = 0.11 * (x ** 0.85)
    sigma_z = 0.08 * (x ** 0.81)
    term_Q = Q / (2 * np.pi * U * sigma_y * sigma_z)
    term_Y = np.exp(-(y ** 2) / (2 * sigma_y ** 2))
    term_Z = 2 * np.exp(-(H ** 2) / (2 * sigma_z ** 2))
    return term_Q * term_Y * term_Z

class BayesianSourceFinder:
    def __init__(self, observations, meteo, bounds):
        self.obs = observations
        self.meteo = meteo
        self.bounds = bounds
        self.chain = []

    def get_initial_guess(self):
        """농도 데이터를 기반으로 초기 시작점 추정"""
        sorted_obs = sorted(self.obs, key=lambda x: x['val'], reverse=True)[:3]
        sum_w = sum(o['val'] for o in sorted_obs) + 1e-9
        center_x = sum(o['x'] * o['val'] for o in sorted_obs) / sum_w
        center_y = sum(o['y'] * o['val'] for o in sorted_obs) / sum_w
        
        back_angle_rad = np.radians(270 - self.meteo['wind_dir'] + 180)
        guess_dist = 2000 # 2km 역추적
        start_x = center_x + guess_dist * np.cos(back_angle_rad)
        start_y = center_y + guess_dist * np.sin(back_angle_rad)
        start_q = np.mean(self.bounds['Q'])
        return np.array([start_x, start_y, start_q])

    def log_likelihood(self, state):
        sx, sy, q = state
        # 경계 조건 확인
        if not (self.bounds['x'][0] <= sx <= self.bounds['x'][1] and
                self.bounds['y'][0] <= sy <= self.bounds['y'][1] and
                self.bounds['Q'][0] <= q <= self.bounds['Q'][1]):
            return -np.inf

        wd, u, h = self.meteo['wind_dir'], self.meteo['wind_speed'], self.meteo['release_height']
        sse = 0
        for o in self.obs:
            dw, cw = get_plume_coordinates(o['x'], o['y'], sx, sy, wd)
            pred = gaussian_plume(dw, cw, h, q, u)
            sigma = o['val'] * 0.2 + 10.0 # 20% 오차 모델
            sse += -0.5 * ((o['val'] - pred) / sigma) ** 2
        return sse

    def run(self, n_iter=15000):
        current_state = self.get_initial_guess()
        current_logp = self.log_likelihood(current_state)
        self.chain = []

        for _ in range(n_iter):
            # 제안 분포 (보폭 설정)
            proposal = current_state + np.array([
                np.random.normal(0, 100), 
                np.random.normal(0, 40), 
                current_state[2] * np.random.normal(0, 0.03)
            ])
            proposed_logp = self.log_likelihood(proposal)
            # 메트로폴리스-헤이스팅스 채택 조건
            if np.log(np.random.rand()) < (proposed_logp - current_logp):
                current_state = proposal
                current_logp = proposed_logp
            self.chain.append(current_state.copy())
        return np.array(self.chain)

# 발전소별 대표 위경도 좌표
PLANT_BASE_LOC = {
    "KR": {"lat": 35.3213, "lon": 129.2941},
    "WS": {"lat": 35.7131, "lon": 129.4775},
    "YK": {"lat": 35.4165, "lon": 126.4178},
    "UJ": {"lat": 37.0941, "lon": 129.3819},
    "SU": {"lat": 35.3376, "lon": 129.3115},
}


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False) # debug=False로 변경 권장
