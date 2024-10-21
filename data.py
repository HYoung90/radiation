# data.py
# 이 스크립트는 방사선 데이터를 MongoDB에서 처리하고, 기상 데이터와 연계하여 일일 평균값을 계산하여 저장합니다.
# 1일마다 실행되며, 중복 데이터 방지도 포함되어 있습니다.


import schedule
import time
import logging
from pymongo import MongoClient
from dotenv import load_dotenv
import os

# .env 파일에서 환경 변수 불러오기
load_dotenv()

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(message)s',
    handlers=[logging.FileHandler('radiation_data.log'), logging.StreamHandler()]
)

# MongoDB 연결
def get_mongo_connection():
    try:
        client = MongoClient("mongodb://localhost:27017/")
        logging.info("MongoDB 연결 성공")
        return client
    except Exception as e:
        logging.error(f"MongoDB 연결 실패: {e}")
        return None

# 방사선 데이터 처리 함수
def process_radiation_data():
    try:
        client = get_mongo_connection()
        if client is None:
            raise Exception("MongoDB 연결 오류로 인해 방사선 데이터를 처리할 수 없습니다.")

        db = client['power_plant_weather']
        backup_collection = db['nuclear_radiation_backup']
        weather_collection = db['plant_measurements']
        stats_collection = db['radiation_stats']

        # 백업 컬렉션에서 방사선 데이터 가져오기
        backup_data = list(backup_collection.find({}, {"_id": 0}))
        logging.info(f"Backup Data Count: {len(backup_data)}")

        # 기상 데이터 가져오기
        weather_data = list(weather_collection.find({}, {"_id": 0}))
        logging.info(f"Weather Data Count: {len(weather_data)}")

        # 기상 데이터에서 강우 유무 저장
        rainfall_status = {}
        for entry in weather_data:
            date_str = entry.get('time', '')[:10]  # 날짜 부분만 추출 (YYYY-MM-DD)
            region = entry.get('region')
            rainfall = entry.get('rainfall', 0)  # 강우량

            if region and date_str:
                status = 'rain' if rainfall and rainfall > 0 else 'no_rain'
                rainfall_status[(region, date_str)] = status

        # 일일 평균 계산을 위한 데이터 그룹화
        daily_avg_data = {'rain': {}, 'no_rain': {}}

        for entry in backup_data:
            region = entry.get('genName')
            date_str = entry.get('time', '')[:10]
            value = float(entry.get('value', 0.0))

            if region and date_str:
                rain_status = rainfall_status.get((region, date_str), None)
                if rain_status is not None:
                    if rain_status == 'rain':
                        daily_avg_data['rain'].setdefault((region, date_str), []).append(value)
                    else:
                        daily_avg_data['no_rain'].setdefault((region, date_str), []).append(value)

        # 평균값 계산 및 결과 저장
        avg_results = []

        def calculate_avg(data, rain_status):
            for key, values in data.items():
                avg_value = sum(values) / len(values) if values else 0

                # 중복 방지를 위한 기존 데이터 확인
                existing_entry = stats_collection.find_one({"region": key[0], "date": key[1]})
                if existing_entry:
                    logging.info(f"중복 데이터: {key[0]}, {key[1]} - 삽입하지 않음")
                else:
                    avg_results.append({
                        "region": key[0],
                        "date": key[1],
                        "value": avg_value,
                        "rain": rain_status
                    })

        # 강우 시와 강우가 없는 시의 평균값 계산
        calculate_avg(daily_avg_data['rain'], True)
        calculate_avg(daily_avg_data['no_rain'], False)

        # 결과를 stats 컬렉션에 저장
        if avg_results:
            stats_collection.insert_many(avg_results)
            logging.info(f"방사선 데이터가 성공적으로 처리되었습니다. 처리된 데이터 수: {len(avg_results)}")
        else:
            logging.info("중복된 데이터를 제외하고 처리된 데이터가 없습니다.")

        logging.info("방사선 데이터가 radiation_stats 컬렉션에 저장되었습니다.")

    except Exception as e:
        logging.error(f"Error in process_radiation_data: {e}")

# 스케줄링 설정 - 매일 새벽 1시에 작업 실행
schedule.every().day.at("01:00").do(process_radiation_data)

# 스케줄 실행
if __name__ == "__main__":
    logging.info("스케줄링 작업이 시작되었습니다.")
    while True:
        schedule.run_pending()
        time.sleep(1)
