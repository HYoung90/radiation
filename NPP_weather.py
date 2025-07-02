import requests
import xml.etree.ElementTree as ET
from pymongo import MongoClient, DESCENDING
import logging
import schedule
import time
import atexit
import sys
import os
# from dotenv import load_dotenv # 이 라인을 제거합니다.
from datetime import datetime
from telegram_notifier import send_telegram_message # 텔레그램 알림 기능 복원

# 로그 설정
logging.basicConfig(
    filename="weather_data_fetch.log",
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(message)s'
)

# 텔레그램 설정 - Railway 환경 변수를 직접 사용합니다.
TELEGRAM_TOKEN = os.getenv("TELEGRAM_NPP_MONITORING_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

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
        # 텔레그램 알림 전송 (오류 발생 시)
        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, f"🚨 *MongoDB 연결 실패:* 🚨\n{e}")
        sys.exit(1) # 연결 실패 시 스크립트 종료


# MongoDB 연결
client = get_mongo_connection()
db = client['Data']

# 데이터를 저장할 컬렉션 및 백업 컬렉션
collection = db['NPP_weather']
backup_collection = db['NPP_weather_backup']

# 공공 API URL 및 서비스 키
base_url = "http://data.khnp.co.kr/environ/service/realtime/weather"
service_key = os.getenv("Service_key")

# 발전소 코드 및 한글 이름 매핑 (API에서 사용되는 genCode와 매핑)
plant_info = {
    "001": "고리", # 부산 기장
    "002": "월성", # 경북 경주
    "003": "한빛", # 전남 영광
    "004": "한울", # 경북 울진
    "005": "새울"  # 울산 울주
}

# 기존 데이터를 백업 컬렉션으로 이동하는 함수 (매일 0시 0분에 실행)
def backup_existing_data():
    current_date = datetime.now().strftime("%Y-%m-%d")
    backup_collection_name = f"NPP_weather_backup_{current_date}"
    daily_backup_collection = db[backup_collection_name]

    if daily_backup_collection.count_documents({}) == 0:
        documents_to_backup = list(collection.find({}))
        if documents_to_backup:
            try:
                daily_backup_collection.insert_many(documents_to_backup)
                logging.info(f"날씨 데이터가 {backup_collection_name}으로 성공적으로 백업되었습니다.")
            except Exception as e:
                logging.warning(f"날씨 데이터 백업 중 오류 발생 (일부 문서 중복 예상): {e}")
        else:
            logging.info("백업할 날씨 데이터가 없습니다.")
    else:
        logging.info(f"{current_date} 날짜의 날씨 백업이 이미 존재합니다. 추가 백업을 건너킵니다.")

# 데이터 수집 및 저장 함수 (복원)
def fetch_and_store_weather_data():
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"날씨 데이터 수집 시작 (현재 시간: {current_time})")

    for gen_code, gen_name in plant_info.items():
        try:
            # API 요청 파라미터
            params = {
                'serviceKey': service_key,
                'pageNo': '1',
                'numOfRows': '10',
                'genCode': gen_code # 발전소 코드
            }

            # API 호출
            response = requests.get(base_url, params=params, timeout=10)
            response.raise_for_status() # HTTP 오류 발생 시 예외 발생

            # XML 파싱
            root = ET.fromstring(response.content)
            item_elements = root.findall('.//item')

            if item_elements:
                latest_data = {}
                for item in item_elements:
                    # 필요한 데이터 추출 및 가공
                    data_row = {
                        'genName': item.find('genName').text, # 발전소명 (예: 고리, 월성)
                        'tm': item.find('tm').text, # 측정 시간 (yyyyMMddHHmmss)
                        'windDir': item.find('windDir').text, # 풍향 (예: SSW)
                        'windSpd': float(item.find('windSpd').text) if item.find('windSpd') is not None and item.find('windSpd').text else None, # 풍속 (m/s)
                        'temp': float(item.find('temp').text) if item.find('temp') is not None and item.find('temp').text else None, # 온도 (℃)
                        'humid': float(item.find('humid').text) if item.find('humid') is not None and item.find('humid').text else None, # 습도 (%)
                        'prcptType': item.find('prcptType').text, # 강수 형태 (예: 비, 눈, 없음)
                        'prcptFall': float(item.find('prcptFall').text) if item.find('prcptFall') is not None and item.find('prcptFall').text else None, # 강수량 (mm)
                        'data_fetch_time': datetime.now() # 데이터 수집 시간 추가
                    }
                    latest_data = data_row # 가장 최신 데이터를 저장 (API 응답이 최신순이라고 가정)
                    break # 첫 번째 item만 처리 (가장 최신 데이터)

                if latest_data:
                    # MongoDB에 데이터 저장 (genName과 tm 필드를 기준으로 중복 방지 - UPSERT)
                    query = {"genName": latest_data['genName'], "tm": latest_data['tm']}
                    collection.update_one(query, {"$set": latest_data}, upsert=True)
                    logging.info(f"[{gen_name}] 날씨 데이터 저장 성공: {latest_data['tm']} - 온도 {latest_data['temp']}℃")
                else:
                    logging.warning(f"[{gen_name}] API 응답에서 유효한 데이터를 찾을 수 없습니다.")
                    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
                        error_message = f"🚨 *[{gen_name}] API 응답 오류:* 🚨\n유효한 날씨 데이터 없음\n응답: {response.text[:100]}..."
                        send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_message)

        except requests.exceptions.Timeout as e:
            logging.error(f"{gen_name} 발전소 날씨 API 요청 시간 초과: {e}")
            if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
                error_message = f"🚨 *[{gen_name}] API 요청 시간 초과:* 🚨\n{str(e)}"
                send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_message)
            continue
        except requests.exceptions.RequestException as e:
            logging.error(f"{gen_name} 발전소 날씨 API 요청 오류: {e}")
            if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
                error_message = f"🚨 *[{gen_name}] API 요청 오류:* 🚨\n{str(e)}"
                send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_message)
            continue
        except ET.ParseError as e:
            logging.error(f"{gen_name} 발전소 날씨 XML 파싱 오류: {e}")
            if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
                error_message = f"🚨 *[{gen_name}] XML 파싱 오류:* 🚨\n{str(e)}"
                send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_message)
            continue
        except Exception as e:
            logging.error(f"{gen_name} 발전소 날씨 데이터 처리 중 오류 발생: {e}")
            print(f"{gen_name} 발전소 날씨 데이터 처리 중 오류 발생: {e}")
            if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
                error_message = f"🚨 *[{gen_name}] 데이터 처리 중 오류 발생:* 🚨\n{str(e)}"
                send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_message)
            continue

    logging.info(f"날씨 데이터 수집 작업 완료 (현재 시간: {current_time})")


# 데이터 수집 및 저장 반복 실행
def scheduled_task():
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"15분마다 데이터 수집 작업 실행 중... (현재 시간: {current_time})")
    print(f"15분마다 데이터 수집 작업 실행 중... (현재 시간: {current_time})")
    backup_existing_data()
    fetch_and_store_weather_data()

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