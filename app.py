# app.py
# 이 스크립트는 Flask 웹 애플리케이션으로, 방사선 및 기상 데이터를 MongoDB에서 가져와 API 및 웹 페이지로 제공합니다.
# 데이터 필터링, 최신 데이터 조회, CSV 내보내기 등의 기능을 제공합니다.


from flask import Flask, render_template, jsonify, request, Response, abort, redirect, url_for
from pymongo import MongoClient, DESCENDING
from flask_caching import Cache
import csv
import io
import pandas as pd
from datetime import datetime, timedelta
import logging
from dateutil import parser
from scipy.signal import find_peaks, savgol_filter
import matplotlib.pyplot as plt
import os
from pymongo import MongoClient, DESCENDING
from pymongo.errors import PyMongoError
from map_utils import power_plants, compute_top5_for
from map_utils import power_plants, compute_top5_for, generate_topsis_map_html
from utils import export_csv, upload_csv
from chatbot_utils import get_best_match
from flask import abort
import numpy as np
import json
import pytz
from flask_login import LoginManager, UserMixin, login_user, user_logged_out,login_required, current_user
from flask_bcrypt import Bcrypt
from bson import ObjectId

app = Flask(__name__)

bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'  # 로그인 안 한 상태로 접근시 이동할 페이지

app.config['SECRET_KEY'] = 'supersecretkey'  # 이미 있으면 중복 금지, 없으면 꼭 추가!

# Flask-Caching 설정 비활성화
cache = Cache(app, config={'CACHE_TYPE': 'null'})
app.config['UPLOAD_FOLDER'] = 'uploads'

# MongoDB 연결
client = MongoClient("mongodb://localhost:27017/")
db = client['Data']

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

analysis1_collection = CAU_collection
analysis2_collection = FNC_collection    # ← 여기가 핵심!
analysis3_collection = KAERI_collection
analysis4_collection = RMT_collection

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

@login_manager.user_loader
def load_user(user_id):
    return User.get_by_id(user_id)


# ---------------------------------------------------------------------
# 라우터 설정
# ---------------------------------------------------------------------
# 최신 기상 데이터 조회 (genName 기준으로)
@app.route('/api/data/<genName>/latest', methods=['GET'])
def get_latest_weather_data(genName):
    normalized_genName = genName.upper()
    logging.info(f"Received request for latest data for genName: {normalized_genName}")

    try:
        data =backup_collection.find_one({"genName": normalized_genName}, {"_id": 0}, sort=[("time", DESCENDING)])
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


# 기본 기상 데이터 페이지 (genName 기준으로)
@app.route('/<genName>', methods=['GET', 'POST'])
def region_data(genName):
    date_filter = request.args.get('date')

    # 기본 쿼리 설정: genName을 기준으로 쿼리 설정
    query = {"genName": genName.upper()}  # genName을 기준으로 설정

    if date_filter:
        try:
            # 날짜 필터가 유효한 경우에만 처리
            date_obj = pd.to_datetime(date_filter)
            start_datetime = date_obj.replace(hour=0, minute=0, second=0, microsecond=0)
            end_datetime = date_obj.replace(hour=23, minute=59, second=59, microsecond=999999)

            # MongoDB에서 날짜 필터링
            query["time"] = {"$gte": start_datetime, "$lte": end_datetime}

        except Exception as e:
            logging.error(f"Date parsing error: {e}")
            # 날짜 필터가 잘못된 경우, 쿼리에서 time 필터 제거
            query.pop("time", None)

    # 데이터 조회: 쿼리에 맞는 데이터 가져오기
    data = list(backup_collection.find(query, {"_id": 0}).sort("time", DESCENDING))

    # 해당 발전소 이름 가져오기
    plant_name = genName_mapping.get(genName.upper(), "Unknown Plant")

    # 데이터를 템플릿으로 전달
    return render_template('weather.html', region=genName.upper(), data=data, plant_name=plant_name)


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
        end_date_obj = parser.parse(date) + datetime.timedelta(days=1)
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
        # 이메일 중복 체크
        if db['users'].find_one({'email': email}):
            return render_template('signup.html', error='이미 등록된 이메일입니다.')
        hashed_pw = bcrypt.generate_password_hash(password).decode('utf-8')
        db['users'].insert_one({'email': email, 'password': hashed_pw})
        return redirect(url_for('login'))
    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        user_doc = db['users'].find_one({'email': email})
        if user_doc and bcrypt.check_password_hash(user_doc['password'], password):
            user = User(user_doc)
            login_user(user)
            next_page = request.form.get('next') or url_for('map_home')
            return redirect(next_page)
        else:
            return render_template('login.html', error='이메일 또는 비밀번호가 올바르지 않습니다.')
    return render_template('login.html')

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
    genName = request.args.get('genName')
    expl = request.args.get('expl')

    logging.debug(f"Fetching history for genName: {genName}, expl: {expl}")  # 추가 로그

    if not genName or not expl:
        logging.warning("Missing genName or expl in the request")
        return jsonify([])

    mapped_genName = None
    for code, name in genName_mapping.items():
        if name == genName:
            mapped_genName = code
            break

    if not mapped_genName:
        mapped_genName = genName

    try:
        history_data = list(nuclear_radiation_collection.find(
            {'genName': mapped_genName, 'expl': expl},
            {'_id': 0, 'time': 1, 'value': 1}
        ).sort('time', 1))

        logging.info(f"Fetched history data: {history_data}")  # 여기에 로그 추가
        return jsonify(history_data)
    except Exception as e:
        logging.error(f"Error fetching history data for {genName}, {expl}: {e}")
        return jsonify({"error": "Failed to fetch radiation history data"}), 500


@app.route('/nuclear_radiation_history/<genName>', methods=['GET'])
def show_radiation_history(genName):
    logging.info(f"Received request for radiation history of: {genName}")
    return render_template('nuclear_radiation_history.html', genName=genName)

@app.route('/nuclear_radiation_detail/<genName>/<expl>', methods=['GET'])
def show_radiation_detail(genName, expl):
    logging.info(f"Received request for radiation history detail for: {genName}, {expl}")
    return render_template('nuclear_radiation_detail.html', genName=genName, expl=expl)

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
            {
                "$sort": {
                    "value": -1  # 방사선량 기준으로 내림차순 정렬
                }
            },
            {
                "$group": {
                    "_id": "$genName",  # 발전소 이름으로 그룹화
                    "max_value": {"$max": "$value"},  # 각 발전소별 최고 방사선량 가져오기
                    "time": {"$first": "$time"},  # 첫 번째 측정 시간 가져오기
                    "expl": {"$first": "$expl"}  # 첫 번째 측정 지역 가져오기
                }
            },
            {"$sort": {"max_value": -1}}  # 방사선량이 큰 순서로 정렬
        ]
        result = list(nuclear_radiation_collection.aggregate(pipeline))

        if result:
            return jsonify(result)
        else:
            return jsonify({"error": "데이터가 없습니다."}), 404
    except Exception as e:
        logging.error(f"발전소별 최고 방사선량 가져오기 오류: {e}")
        return jsonify({"error": "데이터를 가져오지 못했습니다."}), 500



@app.route('/api/get_recent_plant_data', methods=['GET'])
def get_recent_plant_data():
    try:
        plants = ['KR', 'WS', 'YK', 'UJ', 'SU']
        recent_data = []

        for plant in plants:
            data = collection.find_one({"region": plant}, {"_id": 0}, sort=[("time", DESCENDING)])
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

@app.route("/chat", methods=["POST"])
def chat():
    user_input = request.json.get("message", "").strip()
    if not user_input:
        return jsonify({"answer": "질문을 입력해 주세요."})

    try:
        result = get_best_match(user_input)
        return jsonify({
            "quesion": result["question"],
            "answer": result["answer"],
            "similarity": result["score"]
        })

    except Exception as e:
        return jsonify({"answer": f"오류가 발생했습니다: {str(e)}"})


# analysis1, analysis2, analysis3, analysis4에 대한 API 엔드포인트 추가
@app.route('/analysis1')
def analysis1():
    try:
        data = list(analysis1_collection.find({}, {"_id": 0}).sort("time", DESCENDING))
        logging.info(f"Fetched data from analysis1_collection: {data}")
        return render_template('analysis1.html', data=data)
    except Exception as e:
        logging.error(f"Error in fetching data from MongoDB: {e}")
        return render_template('analysis1.html', data=[], error="Failed to load data")

@app.route('/analysis2')
def analysis2():
    try:
        data = list(analysis2_collection.find({}, {"_id": 0}).sort("time", DESCENDING))
        logging.info(f"Fetched data from analysis2_collection: {data}")
        return render_template('analysis2.html', data=data)
    except Exception as e:
        logging.error(f"Error in fetching data from MongoDB: {e}")
        return render_template('analysis2.html', data=[], error="Failed to load data")

@app.route('/analysis4')
def analysis4():
    try:
        data = list(analysis4_collection.find({}, {"_id": 0}).sort("time", DESCENDING))
        logging.info(f"Fetched data from analysis4_collection: {data}")
        return render_template('analysis4.html', data=data)
    except Exception as e:
        logging.error(f"Error in fetching data from MongoDB: {e}")
        return render_template('analysis4.html', data=[], error="Failed to load data")

@app.route('/export_csv/<genName>', methods=['GET'])
def export_csv_by_genName(genName):
    normalized = genName.upper()
    # genName 필터, time 내림차순 정렬
    query = {"genName": normalized}
    sort  = [("time", DESCENDING)]
    # CSV 헤더·필드 정의
    header = ['time','temperature','humidity','rainfall','windspeed','winddirection','stability']
    fields = ['time','temperature','humidity','rainfall','windspeed','winddirection','air_stability']
    # utils.export_csv 호출
    return export_csv(
        backup_collection,
        f"{normalized}_data",  # 파일명 (확장자 .csv는 utils에서 붙여줌)
        header,
        fields,
        query=query,
        sort=sort
    )


# ---------------------------------------------------------------------
# 분석1 라우터 그룹
# ---------------------------------------------------------------------
@app.route('/export_analysis1_csv', methods=['GET'])
def export_analysis1_csv():
    header = ["Check Time", "X", "Y", "Energy range (Mev)", "Radiation (nSv/h)"]
    fields = ["time", "x", "y", "Energy range (Mev)", "radiation"]
    filename = "analysis1_data"
    return export_csv(analysis1_collection, filename, header, fields)


# ---------------------------------------------------------------------
# 분석2 라우터 그룹
# ---------------------------------------------------------------------
@app.route('/export_analysis2_csv')
def export_analysis2_csv():
    return export_csv(
        analysis2_collection,
        "analysis2_data",
        ["측정시간","위도","경도","고도 (m)","풍속 (m/s)","풍향 (°)","방사선량 (nSv/h)"],
        ["time","lat","lng","altitude","windspeed","windDir","radiation"],
        sort=[('time', DESCENDING)]
    )

@app.route('/upload_analysis2_csv', methods=['POST'])
def upload_analysis2_csv():
    if 'file' not in request.files:
        return "No file part", 400

    file = request.files['file']
    if file.filename == '':
        return "No selected file", 400

    if file and file.filename.endswith('.csv'):
        return upload_csv(analysis2_collection, file, {
            "측정시간": "time",
            "위도": "lat",
            "경도": "lng",
            "고도": "altitude",
            "풍속": "windspeed",
            "풍향": "windDir",
            "방사선량": "radiation"
        })
    else:
        return "Invalid file type. Only CSV files are allowed.", 400

# ---------------------------------------------------------------------
# 분석4 라우터 그룹
# ---------------------------------------------------------------------
@app.route('/export_analysis4_csv')
def export_analysis4_csv():
    return export_csv(
        analysis4_collection,
        "analysis4_data",
        ["측정시간","위도","경도","풍속 (m/s)","풍향 (°)","방사선량 (nSv/h)"],
        ["time","lat","lng","windspeed","windDir","radiation"],
        sort=[('time', DESCENDING)]
    )

@app.route('/upload_analysis4_csv', methods=['POST'])
def upload_analysis4_csv():
    if 'file' not in request.files:
        return "No file part", 400

    file = request.files['file']
    if file.filename == '':
        return "No selected file", 400

    if file and file.filename.endswith('.csv'):
        return upload_csv(analysis4_collection, file, {
            "측정시간": "time",
            "위도": "lat",
            "경도": "lng",
            "풍속": "windspeed",
            "풍향": "windDir",
            "방사선량": "radiation"
        })
    else:
        return "Invalid file type. Only CSV files are allowed.", 400


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
        # 최근 방사선 데이터 가져오기
        recent_data = list(stats_collection.find({}, {"_id": 0}).sort("date", DESCENDING).limit(35))

        # 필터링 로직: 'value'나 'rain'이 None인 경우 기본값을 설정
        for item in recent_data:
            if item.get('value') is None:
                item['value'] = 0  # 기본값으로 0 설정
            if item.get('rain') is None:
                item['rain'] = False  # 기본값으로 False 설정
            if item.get('genName') is None or item.get('value') == 'Undefined':
                recent_data.remove(item)  # 'Undefined' 또는 'None' 값 제거

        logging.debug(f"Recent Data: {recent_data}")

        # regional_average에서 데이터 가져오기 (평균 방사선량)
        avg_results = list(regional_avg_collection.find({}, {"_id": 0}).sort("date", DESCENDING))

        return render_template('radiation_summary.html', recent_data=recent_data, avg_results=avg_results)

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
        logging.info(f"Fetching data for plant: {genName}")

        # 최신 데이터 가져오기 (genName 기준)
        latest_data = collection.find_one({'genName': genName}, sort=[('time', -1)])
        logging.info(f"Latest data: {latest_data}")

        if not latest_data:
            logging.warning(f"No data found for plant: {genName}")
            return render_template('accident_result.html', genName=genName, error="No data found for this plant.")

        # 강우 유무 판단 (NPP_weather 컬렉션에서 최신 기상 데이터 가져오기)
        weather_data = db.NPP_weather.find_one({'genName': genName}, sort=[('time', -1)])
        rainfall = weather_data.get('rainfall', 0) if weather_data else 0
        logging.info(f"Rainfall: {rainfall} mm")

        # nuclear_radiation에서 해당 발전소(genName) 방사선량 데이터 가져오기
        radiation_data = list(nuclear_radiation_collection.find({'genName': genName}, {'_id': 0, 'value': 1}))
        logging.info(f"Radiation data: {radiation_data}")

        # 해당 발전소의 방사선량 평균 계산
        radiation_values = [float(data['value']) for data in radiation_data if data['value'] is not None]  # float으로 변환
        average_radiation = sum(radiation_values) / len(radiation_values) if radiation_values else 0.0
        logging.info(f"Calculated average radiation for {genName}: {average_radiation} µSv/h")

        # 기준값 계산: 방사선량 평균 + 0.097
        threshold = average_radiation + 0.097
        radiation_level = round(average_radiation, 4)  # 평균 방사선량을 현재 방사선량으로 설정, 소수점 4자리로 반올림

        logging.info(f"Radiation level: {radiation_level}, Threshold: {threshold}")

        # 사고 유무 판단
        if radiation_level > threshold:
            result = {
                "status": "accident",
                "message": "사고 발생 가능성 있음",
                "radiation_level": radiation_level,
                "threshold": round(threshold, 4),  # 소수점 4자리로 표시
                "rainfall": rainfall  # 강우량 추가
            }
        else:
            result = {
                "status": "normal",
                "message": "정상",
                "radiation_level": radiation_level,
                "threshold": round(threshold, 4),  # 소수점 4자리로 표시
                "rainfall": rainfall  # 강우량 추가
            }
        logging.info(f"Result: {result}")
        return render_template('accident_result.html', genName=genName, result=result)

    except PyMongoError as pe:
        logging.error(f"Database error for {genName}: {pe}")
        return render_template('accident_result.html', genName=genName,
                               error="Database error occurred.")
    except Exception as e:
        logging.error(f"Error fetching data for {genName}: {e}")
        return render_template('accident_result.html', genName=genName,
                               error="An unexpected error occurred.")


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))  # 기본적으로 5000번 포트, 환경변수 PORT가 있으면 그 포트로 실행
    app.run(host='0.0.0.0', port=port, debug=True)

