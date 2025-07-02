import requests
import xml.etree.ElementTree as ET
from pymongo import MongoClient, DESCENDING
import logging
import schedule
import time
import atexit
import sys
import os
#from dotenv import load_dotenv
from datetime import datetime
from telegram_notifier import send_telegram_message  # 텔레그램 알림 통합

# 환경 변수 로드
#load_dotenv("telegram_config.env")  # 환경 변수 파일명 명시 (프로젝트 루트에 위치할 경우)

# 로그 설정
logging.basicConfig(
    filename="weather_data_fetch.log",
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(message)s'
)

# 텔레그램 설정
# TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") # 기존 라인 주석 처리 또는 삭제
TELEGRAM_TOKEN = os.getenv("TELEGRAM_NPP_MONITORING_TOKEN") # 올바른 환경 변수 이름 사용
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

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
        # send_telegram_message 함수가 정의된 후에 호출되어야 합니다.
        # 이 함수가 호출되기 전에 send_telegram_message가 정의되어 있는지 확인하세요.
        try:
            send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, f"MongoDB 연결 실패: {e}")
        except NameError:
            logging.error("send_telegram_message 함수가 정의되지 않아 텔레그램 알림 전송 실패.")
        sys.exit(1) # 연결 실패 시 스크립트 종료

# MongoDB 연결 (기존 client = MongoClient(...) 라인을 대체)
client = get_mongo_connection()
db = client['Data']

# 발전소별 데이터를 저장할 컬렉션
collection = db['NPP_weather']  # 발전소 데이터를 저장할 컬렉션
backup_collection = db['NPP_weather_backup']  # 백업 컬렉션

# 측정 지역 코드
regions = ['KR', 'WS', 'YK', 'UJ', 'SU']

# 공공 API URL (서비스 키는 동일, region을 동적으로 설정)
weather_base_url = "http://apis.data.go.kr/B551182/nppWthInfoService/getNppWthInfo"
service_key = os.getenv("Service_key") # 환경 변수에서 서비스 키 가져오기 (Railway에 설정)

# 데이터 수집 및 저장 함수
def fetch_and_store_data():
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"데이터 수집 시작 (현재 시간: {current_time})")

    # 기존 데이터 백업
    backup_existing_data()

    for region in regions:
        try:
            # API 요청 파라미터
            params = {
                'serviceKey': service_key,
                'pageNo': '1',
                'numOfRows': '10',
                'region': region
            }

            # API 호출
            response = requests.get(weather_base_url, params=params, timeout=10)
            response.raise_for_status() # HTTP 오류 발생 시 예외 발생

            # XML 파싱
            root = ET.fromstring(response.content)
            item_elements = root.findall('.//item')

            if item_elements:
                latest_data = {}
                for item in item_elements:
                    # 필요한 데이터 추출 및 가공
                    data_row = {
                        'regDt': item.find('regDt').text,
                        'region': item.find('region').text,
                        'tm': item.find('tm').text,
                        'wthStt': item.find('wthStt').text, # 날씨 상태 (예: 맑음, 흐림, 비)
                        'tmpr': float(item.find('tmpr').text) if item.find('tmpr') is not None and item.find('tmpr').text else None, # 기온
                        'hmdt': float(item.find('hmdt').text) if item.find('hmdt') is not None and item.find('hmdt').text else None, # 습도
                        'wsd': float(item.find('wsd').text) if item.find('wsd') is not None and item.find('wsd').text else None, # 풍속
                        'wdr': item.find('wdr').text, # 풍향
                        'prcp': float(item.find('prcp').text) if item.find('prcp') is not None and item.find('prcp').text else None, # 강수량
                        'wtrLvl': float(item.find('wtrLvl').text) if item.find('wtrLvl') is not None and item.find('wtrLvl').text else None, # 수위
                        'data_fetch_time': datetime.now() # 데이터 수집 시간 추가
                    }
                    latest_data = data_row # 가장 최신 데이터를 저장 (API 응답이 최신순이라고 가정)
                    break # 첫 번째 item만 처리 (가장 최신 데이터)

                if latest_data:
                    # MongoDB에 데이터 저장 (기존 문서 업데이트 또는 새 문서 삽입)
                    # region과 tm 필드를 기준으로 중복 방지 (UPSERT)
                    # 데이터가 변경되었을 때만 업데이트하도록 할 수도 있습니다. 여기서는 무조건 최신 데이터로 upsert.
                    query = {"region": latest_data['region'], "tm": latest_data['tm']}
                    collection.update_one(query, {"$set": latest_data}, upsert=True)
                    logging.info(f"[{region}] 날씨 데이터 저장 성공: {latest_data['tm']} - {latest_data['wthStt']}")
                else:
                    logging.warning(f"[{region}] API 응답에서 유효한 데이터를 찾을 수 없습니다.")
            else:
                logging.warning(f"[{region}] API 응답에 item 요소가 없습니다. 응답: {response.text[:200]}...") # 응답의 일부만 로깅
                # 텔레그램 메시지 전송 (응답이 비어있거나 item 요소가 없을 때)
                error_message = f"[{region}] API 응답 오류: 유효한 날씨 데이터 없음\\n응답: {response.text[:100]}..."
                send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_message)

        except requests.exceptions.Timeout as e:
            logging.error(f"{region} API 요청 시간 초과: {e}")
            error_message = f"{region} API 요청 시간 초과:\\n{str(e)}"
            send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_message)
        except requests.exceptions.RequestException as e:
            logging.error(f"{region} API 요청 오류: {e}")
            error_message = f"{region} API 요청 오류:\\n{str(e)}"
            send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_message)
        except ET.ParseError as e:
            logging.error(f"{region} XML 파싱 오류: {e}")
            error_message = f"{region} XML 파싱 오류:\\n{str(e)}"
            send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_message)
        except Exception as e:
            logging.error(f"{region} 데이터 처리 중 오류 발생: {e}")
            error_message = f"{region} 데이터 처리 중 오류 발생:\\n{str(e)}"
            send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_message)

    logging.info(f"데이터 수집 작업 완료 (현재 시간: {current_time})")

# 기존 데이터를 백업 컬렉션으로 이동하는 함수 (매일 0시 0분에 실행)
def backup_existing_data():
    current_date = datetime.now().strftime("%Y-%m-%d")
    backup_collection_name = f"NPP_weather_backup_{current_date}"
    daily_backup_collection = db[backup_collection_name]

    # 오늘 날짜에 해당하는 백업 컬렉션이 이미 있는지 확인 (중복 백업 방지)
    if daily_backup_collection.count_documents({}) == 0:
        # 기존 컬렉션의 모든 데이터를 백업 컬렉션으로 복사
        # cursor = collection.find({}) # 모든 문서 가져오기
        # documents = list(cursor)     # 리스트로 변환

        # 데이터를 삽입하고, 기존 컬렉션은 비웁니다.
        # if documents:
        #    daily_backup_collection.insert_many(documents)
        #    collection.delete_many({}) # 기존 컬렉션 비우기
        #    logging.info(f"날씨 데이터가 {backup_collection_name}으로 성공적으로 백업되었습니다.")
        # else:
        #    logging.info("백업할 날씨 데이터가 없습니다.")

        # 데이터 백업은 누락된 백업 기록만 처리하고 기존 컬렉션을 비우지 않도록 수정
        # 현재 컬렉션의 데이터를 모두 가져와 백업 컬렉션에 삽입합니다.
        # 중복 방지를 위해 _id를 유지하며 upsert를 시도하거나,
        # 단순히 해당 시점의 모든 데이터를 새 컬렉션에 복사하는 방식을 사용할 수 있습니다.
        # 여기서는 단순히 현재 시점의 모든 문서를 복사하는 방식으로 구현합니다.
        documents_to_backup = list(collection.find({}))
        if documents_to_backup:
            try:
                daily_backup_collection.insert_many(documents_to_backup)
                logging.info(f"날씨 데이터가 {backup_collection_name}으로 성공적으로 백업되었습니다.")
            except Exception as e:
                # 이미 _id가 존재하는 경우 등의 에러 처리
                logging.warning(f"날씨 데이터 백업 중 오류 발생 (일부 문서 중복 예상): {e}")
                # 중복 오류가 발생해도 진행할 수 있도록 에러를 무시하거나,
                # 중복되지 않은 문서만 삽입하는 로직을 추가할 수 있습니다.
        else:
            logging.info("백업할 날씨 데이터가 없습니다.")
    else:
        logging.info(f"{current_date} 날짜의 날씨 백업이 이미 존재합니다. 추가 백업을 건너뜝니다.")

# 데이터 수집 및 저장 반복 실행
def scheduled_task():
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")  # 현재 시간을 가져옴
    logging.info(f"15분마다 데이터 수집 작업 실행 중... (현재 시간: {current_time})")
    print(f"15분마다 데이터 수집 작업 실행 중... (현재 시간: {current_time})")
    fetch_and_store_data()

# 스크립트 종료 시 MongoDB 연결 닫기
def close_mongodb_connection():
    if client:
        client.close()
        logging.info("MongoDB 연결이 닫혔습니다.")

atexit.register(close_mongodb_connection)

# 15분마다 작업을 실행하는 스케줄 설정
schedule.every(15).minutes.do(scheduled_task)  # 'schedule' 모듈 사용

# 매일 자정(0시 0분)에 백업 실행 (데이터가 누적되지 않도록)
schedule.every().day.at("00:00").do(backup_existing_data)

