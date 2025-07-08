# app.py
# 이 스크립트는 Flask 웹 애플리케이션으로, 방사선 및 기상 데이터를 MongoDB에서 가져와 API 및 웹 페이지로 제공합니다.
# 데이터 필터링, 최신 데이터 조회, CSV 내보내기 등의 기능을 제공합니다.

from flask import Flask, render_template, request
from pymongo import MongoClient, DESCENDING
from flask_caching import Cache
import csv
import io
from datetime import datetime, timedelta
import logging
from dateutil import parser
from scipy.signal import find_peaks, savgol_filter
import matplotlib.pyplot as plt
import os
from pymongo.errors import PyMongoError
from map_utils import compute_top5_for, generate_topsis_map_html

# Flask 앱 및 캐시 설정
app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'super-secret-key')
app.config['CACHE_TYPE'] = 'simple'
cache = Cache(app)

# 로그 설정
logging.basicConfig(level=logging.INFO)

# MongoDB 연결 함수
def get_db():
    uri = os.getenv('MONGO_URI', '').strip().lstrip('=')
    if uri:
        client = MongoClient(uri)
        logging.info('원격 MongoDB 연결 성공')
    else:
        client = MongoClient('mongodb://localhost:27017/')
        logging.info('로컬 MongoDB 연결 성공')
    return client['Data']

db = get_db()
radiation_collection = db['NPP_radiation']
weather_collection = db['NPP_weather']
stats_collection = db['radiation_stats']

# 유틸리티 함수: datetime 문자열 파싱
def parse_datetime(dt_str):
    try:
        return parser.parse(dt_str)
    except Exception:
        return None

# 캐싱된 최신 데이터 조회
@cache.cached(timeout=300)
def get_latest_data(collection, limit=5):
    try:
        docs = list(collection.find().sort('data_fetch_time', DESCENDING).limit(limit))
        return docs
    except PyMongoError as e:
        logging.error(f"DB 에러: {e}")
        return []

# 라우트: 홈 (방사선 데이터 시각화)
@app.route('/')
def index():
    data = get_latest_data(radiation_collection, 24)
    return render_template('index.html', data=data)

# 라우트: API - JSON 반환
@app.route('/api/radiation/latest')
def api_latest_radiation():
    limit = int(request.args.get('limit', 5))
    data = get_latest_data(radiation_collection, limit)
    for d in data:
        d['_id'] = str(d['_id'])
        d['tm'] = d['tm'].strftime('%Y-%m-%d %H:%M:%S')
        d['data_fetch_time'] = d['data_fetch_time'].strftime('%Y-%m-%d %H:%M:%S')
    return {'data': data}

# 라우트: CSV 다운로드
@app.route('/download/csv')
def download_csv():
    data = list(stats_collection.find().sort('tm', DESCENDING))
    si = io.StringIO()
    writer = csv.writer(si)
    writer.writerow(['locNm', 'tm', 'curVal', 'wthStt', 'data_fetch_time'])
    for d in data:
        writer.writerow([
            d.get('locNm'),
            d.get('tm').strftime('%Y-%m-%d %H:%M:%S'),
            d.get('curVal'),
            d.get('wthStt'),
            d.get('data_fetch_time').strftime('%Y-%m-%d %H:%M:%S')
        ])
    output = si.getvalue()
    return app.response_class(
        output,
        mimetype='text/csv',
        headers={'Content-Disposition':'attachment;filename=radiation_stats.csv'}
    )

# 라우트: TOP5 Topsis 지도
@app.route('/map')
def map_view():
    end = datetime.now()
    start = end - timedelta(days=1)
    stats = compute_top5_for(stats_collection, start, end)
    map_html = generate_topsis_map_html(stats)
    return render_template('map.html', map_html=map_html)

# 라우트: 데이터 필터
@app.route('/filter', methods=['GET', 'POST'])
def filter_data():
    if request.method == 'POST':
        start = parse_datetime(request.form.get('start'))
        end = parse_datetime(request.form.get('end'))
        data = []
        if start and end:
            data = list(stats_collection.find({'tm': {'$gte': start, '$lte': end}}).sort('tm', DESCENDING))
        return render_template('filter.html', data=data)
    return render_template('filter.html', data=[])

# Graceful shutdown
def shutdown_db():
    db.client.close()
    logging.info("MongoDB 연결 종료")

# 스크립트 종료 시 연결 종료 등록
atexit.register(shutdown_db)

# 앱 실행
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=False)
