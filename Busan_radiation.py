# busan_radiation.py
# 이 스크립트는 부산의 환경 방사선 데이터를 공공 API에서 가져와 MongoDB에 저장합니다.
# 데이터 백업 및 오류 로깅 기능이 포함되어 있으며, 60분마다 실행됩니다.

import requests
from pymongo import MongoClient
import logging
import schedule
import time
import atexit
import sys
import os
from xml.etree import ElementTree as ET
from datetime import datetime, timedelta

# 로그 설정
logging.basicConfig(
    filename="busan_radiation_data_fetch.log",
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(message)s'
)

# 공공 API 설정
BASE_URL = "http://apis.data.go.kr/6260000/EnvironmentalRadiationInfoService"
SERVICE_KEY = os.getenv("Service_key") or ""

# MongoDB 연결 함수
def get_mongo_connection():
    """
    MongoDB에 연결하고 클라이언트를 반환합니다.
    연결에 실패하면 스크립트를 종료합니다.
    """
    try:
        uri = os.getenv("MONGO_URI", "").strip()
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
radiation_collection = db['Busan_radiation']

# 기존 데이터를 백업 컬렉션으로 이동 (매일 자정 실행)
def backup_existing_data():
    today = datetime.now().strftime("%Y_%m_%d")
    backup_name = f"Busan_radiation_backup_{today}"
    backup_col = db[backup_name]

    if backup_col.count_documents({}) == 0:
        docs = list(radiation_collection.find({}))
        if docs:
            try:
                backup_col.insert_many(docs)
                logging.info(f"데이터 백업 완료: {backup_name}")
            except Exception as e:
                logging.warning(f"백업 중 일부 오류 발생: {e}")
        else:
            logging.info("백업할 데이터가 없습니다.")
    else:
        logging.info(f"{backup_name} 백업 이미 존재, 건너뜀.")

# 공공 데이터 API에서 방사선량 정보 가져와 저장
def fetch_radiation_data():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"데이터 수집 시작: {now}")

    if not SERVICE_KEY:
        logging.error("서비스 키 미설정: Service_key 환경변수를 확인하세요.")
        return
    try:
        params = {
            'serviceKey': SERVICE_KEY,
            'pageNo': '1',
            'numOfRows': '10',
            'resultType': 'xml'
        }
        response = requests.get(f"{BASE_URL}/getEnvironmentalRadiationInfo", params=params, timeout=10)
        response.raise_for_status()

        root = ET.fromstring(response.content)
        items = root.findall('.//item')

        if not items:
            logging.error(f"API 응답에 item 없음, 상태 코드: {response.status_code}")
            return
        for item in items:
            loc = item.findtext('locNm')
            time_str = item.findtext('checkTime')
            data_str = item.findtext('data')
            if not (loc and time_str and data_str):
                logging.warning(f"누락된 필드: loc={loc}, time={time_str}, data={data_str}")
                continue
            try:
                check_time = datetime.strptime(time_str, '%Y%m%d%H%M')
                dose_nSv_h = float(data_str)
                dose_uSv_h = dose_nSv_h / 1000.0

                record = {
                    'locNm': loc,
                    'checkTime': check_time,
                    'dose_nSv_h': dose_nSv_h,
                    'dose_microSv_h': round(dose_uSv_h, 5),
                    'fetched_at': datetime.now()
                }
                radiation_collection.update_one(
                    {'locNm': loc, 'checkTime': check_time},
                    {'$set': record},
                    upsert=True
                )
                logging.info(f"저장 완료: {loc} {dose_uSv_h:.3f} μSv/h at {check_time}")
            except Exception as e:
                logging.error(f"데이터 처리 오류: loc={loc}, time={time_str}, data={data_str}: {e}")
    except requests.exceptions.Timeout as e:
        logging.error(f"API 요청 시간 초과: {e}")
    except requests.exceptions.RequestException as e:
        logging.error(f"API 요청 오류: {e}")
    except ET.ParseError as e:
        logging.error(f"XML 파싱 오류: {e}")
    except Exception as e:
        logging.error(f"예기치 않은 오류: {e}", exc_info=True)

# 스케줄링
schedule.every(60).minutes.do(lambda: (backup_existing_data(), fetch_radiation_data()))
schedule.every().day.at("00:00").do(backup_existing_data)

# 종료 시 연결 종료
atexit.register(lambda: (client.close(), logging.info("MongoDB 연결 종료")))

# 메인 루프
if __name__ == '__main__':
    logging.info("스케줄러 시작")
    # 시작 시 한 번 실행
    backup_existing_data()
    fetch_radiation_data()
    while True:
        schedule.run_pending()
        time.sleep(1)
