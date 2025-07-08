# weather_data_fetch.py
# 이 스크립트는 원자력 발전소 기상 데이터를 공공 API에서 가져와 MongoDB에 저장합니다.
# 데이터 백업 및 로그 기록 기능이 포함되어 있으며, 15분마다 실행됩니다.

import requests
import xml.etree.ElementTree as ET
from pymongo import MongoClient
import logging
import schedule
import time
import os
import sys
import atexit
from datetime import datetime

# 로그 설정
logging.basicConfig(
    filename="weather_data_fetch.log",
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(message)s'
)

# 공공 API URL과 서비스 키
BASE_URL = "http://apis.data.go.kr/B551182/nppWthInfoService/getNppWthInfo"
SERVICE_KEY = os.getenv("Service_key", "")

# 측정 지역 코드 목록
REGIONS = ['KR', 'WS', 'YK', 'UJ', 'SU']

# MongoDB 연결 함수
def get_mongo_connection():
    """
    MongoDB에 연결하고 클라이언트를 반환합니다.
    연결 실패 시 로그를 남기고 종료합니다.
    """
    try:
        uri = os.getenv("MONGO_URI", "").strip().lstrip('=')
        if uri:
            client = MongoClient(uri)
            logging.info("원격 MongoDB 연결 성공")
        else:
            client = MongoClient("mongodb://localhost:27017/")
            logging.info("로컬 MongoDB 연결 성공")
        return client
    except Exception as e:
        logging.error(f"MongoDB 연결 실패: {e}", exc_info=True)
        sys.exit(1)

# MongoDB 초기화
client = get_mongo_connection()
db = client['Data']
collection = db['NPP_weather']

# 기존 데이터를 백업으로 이동 (매일 0시)
def backup_existing_data():
    today = datetime.now().strftime("%Y_%m_%d")
    backup_name = f"NPP_weather_backup_{today}"
    backup_col = db[backup_name]

    if backup_col.count_documents({}) == 0:
        docs = list(collection.find({}))
        if docs:
            try:
                backup_col.insert_many(docs)
                logging.info(f"데이터 백업 완료: {backup_name}")
            except Exception as e:
                logging.warning(f"백업 중 오류 발생: {e}")
        else:
            logging.info("백업할 데이터가 없습니다.")
    else:
        logging.info(f"{backup_name} 백업 이미 존재, 건너뜀.")

# 데이터 수집 및 저장 함수
def fetch_and_store_data():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"기상 데이터 수집 시작: {now}")

    if not SERVICE_KEY:
        logging.error("서비스 키 미설정: Service_key 환경변수를 확인하세요.")
        return

    for region in REGIONS:
        try:
            params = {
                'serviceKey': SERVICE_KEY,
                'pageNo': '1',
                'numOfRows': '1',
                'region': region
            }
            response = requests.get(BASE_URL, params=params, timeout=10)
            response.raise_for_status()

            root = ET.fromstring(response.content)
            item = root.find('.//item')
            if item is None:
                logging.warning(f"{region}: API 응답에 item 요소가 없습니다.")
                continue

            # 데이터 파싱
            record = {
                'regDt': item.findtext('regDt'),
                'region': item.findtext('region'),
                'tm': item.findtext('tm'),
                'wthStt': item.findtext('wthStt'),
                'tmpr': float(item.findtext('tmpr')) if item.find('tmpr') is not None else None,
                'hmdt': float(item.findtext('hmdt')) if item.find('hmdt') is not None else None,
                'wsd': float(item.findtext('wsd')) if item.find('wsd') is not None else None,
                'wdr': item.findtext('wdr'),
                'prcp': float(item.findtext('prcp')) if item.find('prcp') is not None else None,
                'wtrLvl': float(item.findtext('wtrLvl')) if item.find('wtrLvl') is not None else None,
                'data_fetch_time': datetime.now()
            }

            # UPSERT
            query = {'region': record['region'], 'tm': record['tm']}
            collection.update_one(query, {'$set': record}, upsert=True)
            logging.info(f"[{region}] 저장 성공: {record['tm']} - {record['wthStt']}")

        except Exception as e:
            logging.error(f"{region} 데이터 처리 중 오류: {e}", exc_info=True)
            continue

    logging.info("기상 데이터 수집 작업 완료.")

# 자동 실행 함수
def scheduled_task():
    fetch_and_store_data()

# 스케줄 설정
schedule.every(15).minutes.do(scheduled_task)
schedule.every().day.at("00:00").do(backup_existing_data)

# 스크립트 종료 시 MongoDB 연결 종료 등록
atexit.register(lambda: (client.close(), logging.info('MongoDB 연결 종료')))

# 메인
if __name__ == '__main__':
    backup_existing_data()
    fetch_and_store_data()
    while True:
        schedule.run_pending()
        time.sleep(1)
