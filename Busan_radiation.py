# busan_radiation.py
# 이 스크립트는 부산의 환경 방사선 데이터를 공공 API에서 가져와 MongoDB에 저장합니다.
# 데이터 백업 기능이 포함되어 있으며, 60분마다 실행됩니다. 텔레그램 알림 기능은 제거되었습니다.

import requests
from pymongo import MongoClient
import logging
import schedule
import time
import atexit
import sys
import os
# from dotenv import load_dotenv # 이 라인을 제거합니다.
# from telegram_notifier import send_telegram_message # 이 라인을 제거합니다.
from datetime import datetime

# 환경 변수 로드 - 이 부분은 이제 사용되지 않으므로 제거합니다.
# load_dotenv("telegram_config.env")

# 로그 설정
logging.basicConfig(
    filename="busan_radiation_data_fetch.log",
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(message)s'
)

# 텔레그램 설정 - 이 부분은 이제 사용되지 않으므로 제거합니다.
# TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BUSAN_RADIATION_TOKEN")
# TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# 공공 API URL과 서비스 키
base_url    = "http://apis.data.go.kr/6260000/EnvironmentalRadiationInfoService"
service_key = os.getenv("Service_key")

# MongoDB 연결 함수
def get_mongo_connection():
    """
    MongoDB에 연결하고 클라이언트를 반환합니다.
    연결에 실패하면 스크립트를 종료합니다.
    """
    try:
        railway_mongo_uri = os.getenv("MONGO_URI")

        if railway_mongo_uri:
            cleaned_railway_mongo_uri = railway_mongo_uri.lstrip('=')
            client = MongoClient(cleaned_railway_mongo_uri)
            logging.info("Railway MongoDB 클라이언트 설정 및 연결 시도 성공")
        else:
            client = MongoClient("mongodb://localhost:27017/")
            logging.info("로컬 MongoDB 클라이언트 설정 완료")

        return client
    except Exception as e:
        logging.error(f"MongoDB 연결 실패: {e}")
        # 텔레그램 메시지 전송 부분 제거
        # try:
        #     send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, f"MongoDB 연결 실패: {e}")
        # except NameError:
        #     logging.error("send_telegram_message 함수가 정의되지 않아 텔레그램 알림 전송 실패.")
        sys.exit(1) # 연결 실패 시 스크립트 종료

# MongoDB 연결
client = get_mongo_connection()
db = client['Data']

# 데이터를 저장할 컬렉션 및 백업 컬렉션
collection = db['Busan_radiation']
backup_collection = db['Busan_radiation_backup']

# 기존 데이터를 백업 컬렉션으로 이동하는 함수 (매일 0시 0분에 실행)
def backup_existing_data():
    current_date = datetime.now().strftime("%Y-%m-%d")
    backup_collection_name = f"Busan_radiation_backup_{current_date}"
    daily_backup_collection = db[backup_collection_name]

    if daily_backup_collection.count_documents({}) == 0:
        documents_to_backup = list(collection.find({}))
        if documents_to_backup:
            try:
                daily_backup_collection.insert_many(documents_to_backup)
                logging.info(f"부산 방사선 데이터가 {backup_collection_name}으로 성공적으로 백업되었습니다.")
            except Exception as e:
                logging.warning(f"부산 방사선 데이터 백업 중 오류 발생 (일부 문서 중복 예상): {e}")
        else:
            logging.info("백업할 부산 방사선 데이터가 없습니다.")
    else:
        logging.info(f"{current_date} 날짜의 부산 방사선 백업이 이미 존재합니다. 추가 백업을 건너뜁니다.")

# 데이터 수집 및 저장 함수
def fetch_and_store_radiation_data():
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"부산 방사선 데이터 수집 시작 (현재 시간: {current_time})")

    try:
        params = {
            'serviceKey': service_key,
            'pageNo': '1',
            'numOfRows': '10',
            'returnType': 'json' # JSON 응답 요청
        }

        response = requests.get(base_url + "/getEnvironmentalRadiationInfo", params=params, timeout=10)
        response.raise_for_status()
        data = response.json() # JSON 파싱

        items = data.get('getEnvironmentalRadiationInfo', {}).get('item', [])

        if items:
            latest_data = items[0] # 최신 데이터는 첫 번째 item에 있다고 가정
            # 필요한 데이터 추출 및 가공 (API 응답 필드에 따라 조정)
            processed_data = {
                'addr': latest_data.get('addr'), # 주소
                'checkTime': datetime.strptime(latest_data.get('checkTime'), '%Y%m%d%H%M'), # 측정 시간 datetime 객체로 변환
                'dose_nSv_h': float(latest_data.get('dose_nSv_h')) if latest_data.get('dose_nSv_h') else None, # 방사선량 (nSv/h)
                'locNm': latest_data.get('locNm'), # 측정소명 (위치명)
                'data_fetch_time': datetime.now() # 데이터 수집 시간 추가
            }

            # MongoDB에 데이터 저장 (locNm과 checkTime 기준으로 중복 방지 - UPSERT)
            query = {"locNm": processed_data['locNm'], "checkTime": processed_data['checkTime']}
            collection.update_one(query, {"$set": processed_data}, upsert=True)
            logging.info(f"부산 방사선 데이터 저장 성공: {processed_data['locNm']} - {processed_data['checkTime'].strftime('%Y%m%d%H%M')} - {processed_data['dose_nSv_h']} nSv/h")
        else:
            logging.warning("API 응답에서 유효한 부산 방사선 데이터를 찾을 수 없습니다.")
            # 텔레그램 메시지 전송 호출 제거
            # error_message = f"부산 방사선 API 응답 오류: 유효한 데이터 없음\\n응답: {response.text[:100]}..."
            # send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_message)

    except requests.exceptions.Timeout as e:
        error_msg = f"부산 방사선 API 요청 시간 초과: {str(e)}"
        logging.error(error_msg)
        print(error_msg)
        # 텔레그램 메시지 전송 호출 제거
        # send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_msg)
    except requests.exceptions.RequestException as e:
        error_msg = f"부산 방사선 API 요청 오류: {str(e)}"
        logging.error(error_msg)
        print(error_msg)
        # 텔레그램 메시지 전송 호출 제거
        # send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_msg)
    except Exception as e:
        error_msg = f"부산 방사선 선량률 정보 조회 중 오류 발생: {str(e)}"
        logging.error(error_msg)
        print(f"오류 발생: {str(e)}")
        # 텔레그램 메시지 전송 호출 제거
        # send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_msg)
        sys.exit(1)


def scheduled_task():
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"60분마다 부산 방사선 데이터 수집 작업 실행 중... (현재 시간: {current_time})")
    print(f"60분마다 부산 방사선 데이터 수집 작업 실행 중... (현재 시간: {current_time})")
    backup_existing_data()
    fetch_and_store_radiation_data()

# 스크립트 종료 시 MongoDB 연결 닫기
def close_mongodb_connection():
    if client:
        client.close()
        logging.info("MongoDB 연결이 닫혔습니다.")

atexit.register(close_mongodb_connection)

# 60분마다 작업을 실행하는 스케줄 설정
schedule.every(60).minutes.do(scheduled_task)

# 매일 자정(0시 0분)에 백업 실행
schedule.every().day.at("00:00").do(backup_existing_data)