# NPP_radiation.py
# 이 스크립트는 원자력 발전소의 방사선 데이터를 공공 API에서 가져와 MongoDB에 저장합니다.
# 데이터 백업을 처리하고, 오류 발생 시 텔레그램 알림을 전송합니다.
# 이 스크립트는 15분마다 실행되어 새로운 데이터를 가져옵니다.

import requests
import xml.etree.ElementTree as ET
from pymongo import MongoClient, DESCENDING # DESCENDING 추가
import logging
import schedule
import time
import os
import sys # sys 모듈 추가 (get_mongo_connection에서 sys.exit 사용)
import atexit # atexit 모듈 추가 (MongoDB 연결 종료용)
from dotenv import load_dotenv
from datetime import datetime
from telegram_notifier import send_telegram_message

# 로그 설정
logging.basicConfig(
    filename="nuclear_radiation_data_fetch.log",
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(message)s'
)

# 텔레그램 설정 - NPP_monitoring 봇 사용
TELEGRAM_NPP_MONITORING_TOKEN = os.getenv("TELEGRAM_NPP_MONITORING_TOKEN") # NPP_monitoring 봇의 토큰
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# 공공 API URL과 서비스 키
base_url = "http://data.khnp.co.kr/environ/service/realtime/radiorate"
service_key = os.getenv("Service_key") # env에 설정한 이름을 그대로 사용

# 발전소 코드 및 한글 이름 매핑 (API에서 사용되는 genCode와 매핑)
# 'genCode'에 해당하는 한글 이름과 'locNm'에 해당하는 한글 이름
# API 응답에는 locNm이 없고 genCode만 있다고 가정
plant_info = {
    "001": {"name": "고리", "locNm": "고리본부"}, # 부산 기장
    "002": {"name": "월성", "locNm": "월성본부"}, # 경북 경주
    "003": {"name": "한빛", "locNm": "한빛본부"}, # 전남 영광
    "004": {"name": "한울", "locNm": "한울본부"}, # 경북 울진
    "005": {"name": "새울", "locNm": "새울본부"}  # 울산 울주
}


# MongoDB 연결 함수
def get_mongo_connection():
    """
    MongoDB에 연결하고 클라이언트를 반환합니다.
    연결에 실패하면 스크립트를 종료합니다.
    """
    try:
        # Railway MONGO_URI 환경 변수를 로드 시도
        railway_mongo_uri = os.getenv("MONGO_URI")

        if railway_mongo_uri:
            # MONGO_URI에 '='가 붙어있을 경우 제거
            cleaned_railway_mongo_uri = railway_mongo_uri.lstrip('=')
            client = MongoClient(cleaned_railway_mongo_uri)
            logging.info("Railway MongoDB 클라이언트 설정 및 연결 시도 성공")
        else:
            # MONGO_URI가 없으면 로컬 MongoDB에 연결
            client = MongoClient("mongodb://localhost:27017/")
            logging.info("로컬 MongoDB 클라이언트 설정 완료")

        return client
    except Exception as e:
        logging.error(f"MongoDB 연결 실패: {e}")
        # 텔레그램 메시지 전송
        try:
            send_telegram_message(TELEGRAM_NPP_MONITORING_TOKEN, TELEGRAM_CHAT_ID, f"MongoDB 연결 실패: {e}")
        except NameError:
            logging.error("send_telegram_message 함수가 정의되지 않아 텔레그램 알림 전송 실패.")
        sys.exit(1) # 연결 실패 시 스크립트 종료


# MongoDB 연결 (기존 client = MongoClient(...) 라인을 대체)
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

    # 오늘 날짜에 해당하는 백업 컬렉션이 이미 있는지 확인 (중복 백업 방지)
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
        genName = info["locNm"] # 텔레그램 메시지용 한글 이름
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
                        'facilDiv': item.find('facilDiv').text, # 시설 구분
                        'locNm': item.find('locNm').text, # 위치명 (예: 고리본부, 월성본부)
                        'tm': item.find('tm').text, # 측정 시간 (yyyyMMddHHmmss)
                        'curVal': float(item.find('curVal').text) if item.find('curVal') is not None and item.find('curVal').text else None, # 현재 방사선량 (μSv/h)
                        'data_fetch_time': datetime.now() # 데이터 수집 시간 추가
                    }
                    latest_data = data_row # 가장 최신 데이터를 저장 (API 응답이 최신순이라고 가정)
                    break # 첫 번째 item만 처리 (가장 최신 데이터)

                if latest_data:
                    # MongoDB에 데이터 저장 (기존 문서 업데이트 또는 새 문서 삽입)
                    # locNm과 tm 필드를 기준으로 중복 방지 (UPSERT)
                    query = {"locNm": latest_data['locNm'], "tm": latest_data['tm']}
                    collection.update_one(query, {"$set": latest_data}, upsert=True)
                    logging.info(f"[{genName}] 방사선 데이터 저장 성공: {latest_data['tm']} - {latest_data['curVal']} μSv/h")
                else:
                    logging.warning(f"[{genName}] API 응답에서 유효한 데이터를 찾을 수 없습니다.")
            else:
                logging.warning(f"[{genName}] API 응답에 item 요소가 없습니다. 응답: {response.text[:200]}...") # 응답의 일부만 로깅
                # 텔레그램 메시지 전송 (응답이 비어있거나 item 요소가 없을 때)
                error_message = f"[{genName}] API 응답 오류: 유효한 방사선 데이터 없음\\n응답: {response.text[:100]}..."
                send_telegram_message(TELEGRAM_NPP_MONITORING_TOKEN, TELEGRAM_CHAT_ID, error_message)

        except requests.exceptions.Timeout as e:
            logging.error(f"{genName} 발전소 API 요청 시간 초과: {e}")
            error_message = f"{genName} 발전소 API 요청 시간 초과:\\n{str(e)}"
            send_telegram_message(TELEGRAM_NPP_MONITORING_TOKEN, TELEGRAM_CHAT_ID, error_message)
            continue
        except requests.exceptions.RequestException as e:
            logging.error(f"{genName} 발전소 API 요청 오류: {e}")
            error_message = f"{genName} 발전소 API 요청 오류:\\n{str(e)}"
            send_telegram_message(TELEGRAM_NPP_MONITORING_TOKEN, TELEGRAM_CHAT_ID, error_message)
            continue
        except ET.ParseError as e:
            logging.error(f"{genName} 발전소 XML 파싱 오류: {e}")
            # 텔레그램 메시지 전송
            error_message = f"{genName} 발전소 XML 파싱 오류:\\n{str(e)}"
            send_telegram_message(TELEGRAM_NPP_MONITORING_TOKEN, TELEGRAM_CHAT_ID, error_message)
            continue
        except Exception as e:
            logging.error(f"{genName} 발전소 데이터 처리 중 오류 발생: {e}")
            print(f"{genName} 발전소 데이터 처리 중 오류 발생: {e}")
            # 텔레그램 메시지 전송
            error_message = f"{genName} 발전소 데이터 처리 중 오류 발생:\\n{str(e)}"
            send_telegram_message(
                TELEGRAM_NPP_MONITORING_TOKEN, TELEGRAM_CHAT_ID, error_message)
            continue

    logging.info(f"방사선 데이터 수집 작업 완료 (현재 시간: {current_time})")


# 스케줄 실행 시 로그 기록
def scheduled_task():
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S") # 현재 시간을 가져옴
    logging.info(f"15분마다 데이터 수집 작업 실행 중... (현재 시간: {current_time})")
    print(f"15분마다 데이터 수집 작업 실행 중... (현재 시간: {current_time})")
    backup_existing_data() # 백업을 먼저 실행
    fetch_and_store_radiation_data() # 그 다음 데이터 수집

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

