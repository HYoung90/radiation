# app.py
# 이 스크립트는 Flask 웹 애플리케이션으로, 방사선 및 기상 데이터를 MongoDB에서 가져와 API 및 웹 페이지로 제공합니다.
# 데이터 필터링, 최신 데이터 조회, CSV 내보내기 등의 기능을 제공합니다.


from flask import Flask, render_template, jsonify, request, Response
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
import numpy as np
import json
import requests
import pytz

app = Flask(__name__)

# Flask-Caching 설정 비활성화
cache = Cache(app, config={'CACHE_TYPE': 'null'})
app.config['UPLOAD_FOLDER'] = 'uploads'

# MongoDB 연결 설정
client = MongoClient("mongodb://localhost:27017/")
db = client['power_plant_weather']
collection = db['plant_measurements']
busan_radiation_collection = db['busan_radiation']
busan_radiation_backup_collection = db['busan_radiation_backup']
nuclear_radiation_collection = db['nuclear_radiation']
nuclear_radiation_backup_collection = db['nuclear_radiation_backup']
jn_radiation_collection = db['JN_radiation']
analysis1_collection = db['analysis1_data']
analysis2_collection = db['analysis2_data']
analysis4_collection = db['analysis4_data']

# 통계 데이터 컬렉션
stats_collection = db['radiation_stats']
avg_db = client['radiation_statistics']  # 평균을 저장할 새로운 데이터베이스
avg_collection = avg_db['daily_average']  # 평균 데이터 저장 컬렉션
regional_avg_collection = avg_db['regional_average']

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(message)s',
    handlers=[
        logging.FileHandler("app.log"),
        logging.StreamHandler()
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

# ---------------------------------------------------------------------
# 라우터 설정
# ---------------------------------------------------------------------
@app.route('/api/data/<region>/latest', methods=['GET'])
def get_latest_weather_data(region):
    normalized_region = region.upper()
    logging.info(f"Received request for latest data for region: {normalized_region}")

    try:
        data = collection.find_one({"region": normalized_region}, {"_id": 0}, sort=[("time", DESCENDING)])
        if data:
            logging.info(f"Latest data found: {data}")
            return jsonify(data)
        else:
            logging.warning(f"No latest data found for region: {normalized_region}")
            return jsonify({"error": "No data found for this region"}), 404
    except Exception as e:
        logging.error(f"Error fetching latest weather data for {region}: {e}")
        return jsonify({"error": "An error occurred while fetching the data"}), 500

@app.route('/api/data/<region>/filtered', methods=['GET'])
def get_filtered_weather_data(region):
    start_date = request.args.get('start')
    end_date = request.args.get('end')
    normalized_region = region.upper()
    logging.info(f"Received filtered data request for region: {normalized_region} from {start_date} to {end_date}")

    try:
        query = {"region": normalized_region}
        if start_date and end_date:
            start_time_str = f"{start_date} 00:00"
            end_date_obj = parser.parse(end_date) + datetime.timedelta(days=1)
            end_time_str = end_date_obj.strftime("%Y-%m-%d 00:00")
            query["time"] = {"$gte": start_time_str, "$lt": end_time_str}

        data = list(collection.find(query, {"_id": 0}).sort("time", DESCENDING))
        if data:
            logging.info(f"Returning {len(data)} records for region: {normalized_region}")
            return jsonify(data)
        else:
            logging.warning(f"No data found for region: {normalized_region} with given date range.")
            return jsonify({"error": "No data found for this region"}), 404
    except Exception as e:
        logging.error(f"Error in get_filtered_weather_data: {e}")
        return jsonify({"error": "An error occurred while fetching the data"}), 500

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
def map_home():
    return render_template('map.html')

@app.route('/<region>', methods=['GET', 'POST'])
def region_data(region):
    date_filter = request.args.get('date')

    query = {"region": region.upper()}  # Ensure region is uppercase

    if date_filter:
        try:
            date_obj = pd.to_datetime(date_filter)
            start_datetime = date_obj.replace(hour=0, minute=0, second=0, microsecond=0)
            end_datetime = date_obj.replace(hour=23, minute=59, second=59, microsecond=999999)
            query["time"] = {"$gte": start_datetime.strftime("%Y-%m-%d %H:%M"),
                             "$lte": end_datetime.strftime("%Y-%m-%d %H:%M")}
        except Exception as e:
            logging.error(f"Date parsing error: {e}")
            query.pop("time", None)

    data = list(collection.find(query, {"_id": 0}).sort("time", DESCENDING))
    plant_name = genName_mapping.get(region.upper(), "Unknown Plant")

    return render_template('index.html', region=region.upper(), data=data, plant_name=plant_name)

@app.route('/busan_radiation')
def busan_radiation_page():
    return render_template('busan_radiation.html')

@app.route('/nuclear_radiation')
def nuclear_radiation_page():
    return render_template('nuclear_radiation.html')

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

@app.route('/export_csv/<region>', methods=['GET'])
def export_csv_by_region(region):
    try:
        data = list(collection.find({"region": region.upper()}, {"_id": 0}).sort("time", DESCENDING))

        logging.info(f"Exporting CSV for region: {region}, Number of records: {len(data)}")
        logging.debug(f"Data: {data}")

        if not data:
            return jsonify({"error": "No data found for this region"}), 404

        def generate_csv():
            header = ['time', 'temperature', 'humidity', 'rainfall', 'windspeed', 'winddirection', 'stability']
            output = io.StringIO()
            output.write('\ufeff')

            writer = csv.DictWriter(output, fieldnames=header)
            writer.writeheader()

            for row in data:
                logging.debug(f"Row data: {row}")
                writer.writerow({
                    'time': row.get('time', ''),
                    'temperature': row.get('temperature', 'N/A'),
                    'humidity': row.get('humidity', 'N/A'),
                    'rainfall': row.get('rainfall', '0'),
                    'windspeed': row.get('windspeed', 'N/A'),
                    'winddirection': row.get('winddirection', 'N/A'),
                    'stability': row.get('air_stability', 'Unknown')
                })

            output.seek(0)
            return output.getvalue()

        return Response(generate_csv(), mimetype='text/csv',
                        headers={"Content-Disposition": f"attachment;filename={region}_data.csv"})

    except Exception as e:
        logging.error(f"Error exporting CSV for {region}: {e}")
        return jsonify({"error": f"An error occurred while exporting the CSV: {str(e)}"}), 500

# ---------------------------------------------------------------------
# 분석1 라우터 그룹
# ---------------------------------------------------------------------
@app.route('/export_analysis1_csv', methods=['GET'])
def export_analysis1_csv():
    header = ["Check Time", "X", "Y", "Energy range (Mev)", "Radiation (nSv/h)"]
    fields = ["time", "x", "y", "Energy range (Mev)", "radiation"]
    filename = "analysis1_data"
    return export_csv(analysis1_collection, filename, header, fields)

@app.route('/upload_analysis1_csv', methods=['POST'])
def upload_analysis1_csv():
    if 'file' not in request.files:
        return "No file part", 400

    file = request.files['file']
    if file.filename == '':
        return "No selected file", 400

    if file and file.filename.endswith('.csv'):
        return upload_csv(analysis1_collection, file, {
            "Check Time": "time",
            "X": "x",
            "Y": "y",
            "Energy range (Mev)": "Energy range (Mev)",
            "Radiation (nSv/h)": "radiation"
        })
    else:
        return "Invalid file type. Only CSV files are allowed.", 400

# ---------------------------------------------------------------------
# 분석2 라우터 그룹
# ---------------------------------------------------------------------
@app.route('/export_analysis2_csv', methods=['GET'])
def export_analysis2_csv():
    header = ["측정시간", "위도", "경도", "고도 (m)", "풍속 (m/s)", "풍향 (°)", "방사선량 (nSv/h)"]
    fields = ["time", "lat", "lng", "altitude", "windspeed", "windDir", "radiation"]
    filename = "analysis2_data"
    return export_csv(analysis2_collection, filename, header, fields)

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
@app.route('/export_analysis4_csv', methods=['GET'])
def export_analysis4_csv():
    header = ["측정시간", "위도", "경도", "풍속 (m/s)", "풍향 (°)", "방사선량 (nSv/h)"]
    fields = ["time", "lat", "lng", "windspeed", "windDir", "radiation"]
    filename = "analysis4_data"
    return export_csv(analysis4_collection, filename, header, fields)

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
# 바람 장미
# ---------------------------------------------------------------------
@app.route('/windRose/<region>', methods=['GET'])
def wind_rose(region):
    normalized_region = region.upper()
    logging.info(f"Generating wind rose for region: {normalized_region}")

    try:
        data = list(collection.find({"region": normalized_region}, {"_id": 0, "winddirection": 1, "windspeed": 1}))

        if not data:
            logging.warning(f"No wind direction data found for region: {normalized_region}")
            return render_template('wind_rose_chart.html', region=normalized_region, wind_data={}, error="데이터가 없습니다.")

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
            logging.warning(f"No valid wind direction and speed data found for region: {normalized_region}")
            return render_template('wind_rose_chart.html', region=normalized_region, wind_data={}, error="유효한 데이터가 없습니다.")

        wind_data_percent = {}
        for direction, bins in direction_bins.items():
            wind_data_percent[direction] = {}
            for bin_name, count in bins.items():
                wind_data_percent[direction][bin_name] = round((count / total_counts) * 100, 2)

        logging.debug(f"Wind direction and speed percentages for {normalized_region}: {wind_data_percent}")

        return render_template('wind_rose_chart.html', region=normalized_region, wind_data=wind_data_percent)

    except Exception as e:
        logging.error(f"Error generating wind rose for {normalized_region}: {e}")
        return render_template('wind_rose_chart.html', region=normalized_region, wind_data={}, error="데이터를 불러오는 중 오류가 발생했습니다.")

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
        # 평균값 가져오기
        avg_results_raw = list(regional_avg_collection.find({}, {"_id": 0}))
        avg_results = []
        for result in avg_results_raw:
            avg_results.append({
                'region': result['region'],
                'rain_avg': result.get('rain_avg', 0),
                'no_rain_avg': result.get('no_rain_avg', 0),
                'percentage_increase': result.get('percentage_increase', 0)
            })

        # 최근 방사선량 데이터 가져오기
        recent_data = list(stats_collection.find({}, {"_id": 0}).sort("date", -1).limit(35))

        # None 값이 있는지 확인하고 기본값 설정
        for item in recent_data:
            if item.get('value') is None:
                item['value'] = 0  # 기본값으로 0 설정
            if item.get('rain') is None:
                item['rain'] = False  # 기본값으로 False 설정

        logging.debug(f"Avg Results: {avg_results}")
        logging.debug(f"Recent Data: {recent_data}")

        return render_template('radiation_summary.html', avg_results=avg_results, recent_data=recent_data)

    except Exception as e:
        logging.error(f"Error fetching data: {e}")
        return render_template('radiation_summary.html', avg_results=[], error="Failed to load data")


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
