# NPP_radiation.py
# 이 스크립트는 원자력 발전소의 방사선 데이터를 공공 API에서 가져와 MongoDB에 저장합니다.
# 데이터 백업을 처리합니다. 오류 발생 시 텔레그램 알림 기능은 제거되었습니다.

import requests
import xml.etree.ElementTree as ET
from pymongo import MongoClient, DESCENDING
import logging
import schedule
import time
import os
import sys
import atexit
# from dotenv import load_dotenv # 이 라인을 제거합니다.
from datetime import datetime

# from telegram_notifier import send_telegram_message # 이 라인을 제거합니다.

# 로그 설정
logging.basicConfig(
    filename="nuclear_radiation_data_fetch.log",
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(message)s'
)

# 텔레그램 설정 - 이 부분은 이제 사용되지 않으므로 제거합니다.
# TELEGRAM_NPP_MONITORING_TOKEN = os.getenv("TELEGRAM_NPP_MONITORING_TOKEN")
# TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# 공공 API URL과 서비스 키
base_url = "http://data.khnp.co.kr/environ/service/realtime/radiorate"
service_key = os.getenv("Service_key")

# 발전소 코드 및 한글 이름 매핑 (API에서 사용되는 genCode와 매핑)
plant_info = {
    "001": {"name": "고리", "locNm": "고리본부"},
    "002": {"name": "월성", "locNm": "월성본부"},
    "003": {"name": "한빛", "locNm": "한빛본부"},
    "004": {"name": "한울", "locNm": "한울본부"},
    "005": {"name": "새울", "locNm": "새울본부"}
}


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
        #     send_telegram_message(TELEGRAM_NPP_MONITORING_TOKEN, TELEGRAM_CHAT_ID, f"MongoDB 연결 실패: {e}")
        # except NameError:
        #     logging.error("send_telegram_message 함수가 정의되지 않아 텔레그램 알림 전송 실패.")
        sys.exit(1)  # 연결 실패 시 스크립트 종료


# MongoDB 연결
client = get_mongo_connection()
db = client['Data']

# 데이터를 저장할 컬렉션 및 백업 컬렉션
collection = db['NPP_radiation']
backup_collection = db['NPP_radiation_backup']


# 기존 데이터를 백업 컬렉션으로 이동하는 함수 (매일 0시 0분에 실행)
def backup_existing_data():
    current_date = datetime.now().strftime("%Y-%m-%d")
    backup_collection_name = f"NPP_radiation_backup_{current_date}"
    daily_backup_collection = db[backup_collection_name]

    if daily_backup_collection.count_documents({}) == 0:
        documents_to_backup = list(collection.find({}))
        if documents_to_backup:
            try:
                daily_backup_collection.insert_many(documents_to_backup)
                logging.info(f"방사선 데이터가 {backup_collection_name}으로 성공적으로 백업되었습니다.")
            except Exception as e:
                logging.warning(f"방사선 데이터 백업 중 오류 발생 (일부 문서 중복 예상): {e}")
        else:
            logging.info("백업할 방사선 데이터가 없습니다.")
    else:
        logging.info(f"{current_date} 날짜의 방사선 백업이 이미 존재합니다. 추가 백업을 건너뜁니다.")


# 데이터 수집 및 저장 함수
def fetch_and_store_radiation_data():
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"방사선 데이터 수집 시작 (현재 시간: {current_time})")

    for gen_code, info in plant_info.items():
        genName = info["locNm"]
        try:
            params = {
                'serviceKey': service_key,
                'pageNo': '1',
                'numOfRows': '10',
                'genCode': gen_code
            }

            response = requests.get(base_url, params=params, timeout=10)
            response.raise_for_status()

            root = ET.fromstring(response.content)
            item_elements = root.findall('.//item')

            if item_elements:
                latest_data = {}
                for item in item_elements:
                    data_row = {
                        'genName': item.find('genName').text,
                        'facilDiv': item.find('facilDiv').text,
                        'locNm': item.find('locNm').text,
                        'tm': item.find('tm').text,
                        'curVal': float(item.find('curVal').text) if item.find('curVal') is not None and item.find(
                            'curVal').text else None,
                        'data_fetch_time': datetime.now()
                    }
                    latest_data = data_row
                    break

                if latest_data:
                    query = {"locNm": latest_data['locNm'], "tm": latest_data['tm']}
                    collection.update_one(query, {"$set": latest_data}, upsert=True)
                    logging.info(f"[{genName}] 방사선 데이터 저장 성공: {latest_data['tm']} - {latest_data['curVal']} μSv/h")
                else:
                    logging.warning(f"[{genName}] API 응답에서 유효한 데이터를 찾을 수 없습니다.")
                    # 텔레그램 메시지 전송 호출 제거
                    # error_message = f"[{genName}] API 응답 오류: 유효한 방사선 데이터 없음\\n응답: {response.text[:100]}..."
                    # send_telegram_message(TELEGRAM_NPP_MONITORING_TOKEN, TELEGRAM_CHAT_ID, error_message)

        except requests.exceptions.Timeout as e:
            logging.error(f"{genName} 발전소 API 요청 시간 초과: {e}")
            # 텔레그램 메시지 전송 호출 제거
            # error_message = f"{genName} 발전소 API 요청 시간 초과:\\n{str(e)}"
            # send_telegram_message(TELEGRAM_NPP_MONITORING_TOKEN, TELEGRAM_CHAT_ID, error_message)
            continue
        except requests.exceptions.RequestException as e:
            logging.error(f"{genName} 발전소 API 요청 오류: {e}")
            # 텔레그램 메시지 전송 호출 제거
            # error_message = f"{genName} 발전소 API 요청 오류:\\n{str(e)}"
            # send_telegram_message(TELEGRAM_NPP_MONITORING_TOKEN, TELEGRAM_CHAT_ID, error_message)
            continue
        except ET.ParseError as e:

            logging.error(f"{genName} 발전소 XML 파싱 오류: {e}")
            # 텔레그램 메시지 전송 호출 제거
            # error_message = f"{genName} 발전소 XML 파싱 오류:\\n{str(e)}"
            # send_telegram_message(TELEGRAM_NPP_MONITORING_TOKEN, TELEGRAM_CHAT_ID, error_message)
            continue
        except Exception as e:
            logging.error(f"{genName} 발전소 데이터 처리 중 오류 발생: {e}")
            print(f"{genName} 발전소 데이터 처리 중 오류 발생: {e}")
            # 텔레그램 메시지 전송 호출 제거
            # error_message = f"{genName} 발전소 데이터 처리 중 오류 발생:\\n{str(e)}"
            # send_telegram_message(
            #     TELEGRAM_NPP_MONITORING_TOKEN, TELEGRAM_CHAT_ID, error_message)
            continue

    logging.info(f"방사선 데이터 수집 작업 완료 (현재 시간: {current_time})")


# 스케줄 실행 시 로그 기록
def scheduled_task():
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"15분마다 데이터 수집 작업 실행 중... (현재 시간: {current_time})")
    print(f"15분마다 데이터 수집 작업 실행 중... (현재 시간: {current_time})")
    backup_existing_data()
    fetch_and_store_radiation_data()


# 스크립트 종료 시 MongoDB 연결 닫기
def close_mongodb_connection():
    if client:
        client.close()
        logging.info("MongoDB 연결이 닫혔습니다.")


atexit.register(close_mongodb_connection)

# 15분마다 작업을 실행하는 스케줄 설정
schedule.every(15).minutes.do(scheduled_task)

# 매일 자정(0시 0분)에 백업 실행
schedule.every().day.at("00:00").do(backup_existing_data)