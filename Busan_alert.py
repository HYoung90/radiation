from pymongo import MongoClient
import logging
from statistics import mean
import schedule
import time
import os
import sys
import atexit
from datetime import datetime, timedelta

# 로그 설정 (파일과 콘솔 모두 출력)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(message)s',
    handlers=[
        logging.FileHandler("busan_alert.log"),
        logging.StreamHandler()
    ]
)

def get_mongo_connection():
    """
    MongoDB에 연결하고 클라이언트를 반환합니다.
    연결에 실패하면 스크립트를 종료합니다.
    """
    try:
        raw_uri = os.getenv("MONGO_URI", "").strip()
        if not raw_uri:
            logging.info("MONGO_URI 미설정, 로컬 MongoDB로 연결 시도")
            raw_uri = "mongodb://localhost:27017/"

        client = MongoClient(raw_uri)
        logging.info("MongoDB 연결 성공")
        return client

    except Exception as e:
        logging.error(f"MongoDB 연결 실패: {e}", exc_info=True)
        sys.exit(1)

# MongoDB 클라이언트 초기화
client = get_mongo_connection()
db = client['Data']
radiation_collection = db['Busan_radiation']

# 방사선량 통계 가져오기 및 로그 기록 함수
def fetch_radiation_statistics_and_log():
    current_time_log = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"방사선량 통계 처리 시작 (현재 시간: {current_time_log})")

    try:
        end_time = datetime.now()
        start_time = end_time - timedelta(days=1)

        data_cursor = radiation_collection.find({
            'checkTime': {'$gte': start_time, '$lte': end_time}
        })
        data = list(data_cursor)

        if not data:
            logging.warning("최근 24시간 동안의 부산 방사선 데이터가 없습니다.")
            return

        highest_value = 0.0
        highest_region = "알 수 없음"
        total_radiation = []

        for item in data:
            try:
                radiation_value = float(item.get('dose_nSv_h', 0))
            except (ValueError, KeyError):
                logging.warning(f"유효하지 않은 데이터: {item.get('dose_nSv_h', 'N/A')}")
                continue

            if radiation_value > 0:
                total_radiation.append(radiation_value)
                if radiation_value > highest_value:
                    highest_value = radiation_value
                    highest_region = item.get('locNm', '알 수 없음')

        average_radiation = mean(total_radiation) if total_radiation else 0.0

        logging.info(
            f"부산 실시간 방사선량 요약 (최근 24시간) — 최고: {highest_region} {highest_value:.2f} nSv/h, "
            f"평균: {average_radiation:.2f} nSv/h"
        )

    except Exception as e:
        logging.error(f"방사선량 통계 처리 중 오류 발생: {e}", exc_info=True)

# 스케줄 함수
def scheduled_alert_task():
    current_time_log = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"2시간마다 방사선 통계 작업 실행 중 (현재 시간: {current_time_log})")
    fetch_radiation_statistics_and_log()

# 매 2시간마다 작업 실행 설정
schedule.every(2).hours.do(scheduled_alert_task)

# 스크립트 종료 시 MongoDB 연결 닫기
def close_mongodb_connection():
    if client:
        client.close()
        logging.info("MongoDB 연결이 닫혔습니다.")

atexit.register(close_mongodb_connection)

# 메인 루프
def main():
    logging.info("스케줄러 시작")
    while True:
        schedule.run_pending()
        time.sleep(1)

if __name__ == "__main__":
    main()
