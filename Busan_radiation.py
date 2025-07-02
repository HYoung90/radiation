# busan_radiation.py
# 이 스크립트는 부산의 환경 방사선 데이터를 공공 API에서 가져와 MongoDB에 저장합니다.
# 데이터 백업 및 오류 발생 시 텔레그램 알림 기능이 포함되어 있으며, 60분마다 실행됩니다.

import requests
from pymongo import MongoClient
import logging
import schedule
import time
import atexit
import sys
import os
from dotenv import load_dotenv
from telegram_notifier import send_telegram_message
from datetime import datetime # datetime 모듈 추가 (로그 및 시간 처리용)

# 환경 변수 로드
load_dotenv("telegram_config.env")

# 로그 설정
logging.basicConfig(
    filename="busan_radiation_data_fetch.log",
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(message)s'
)

# 텔레그램 설정
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BUSAN_RADIATION_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# 공공 API URL과 서비스 키
base_url    = "http://apis.data.go.kr/6260000/EnvironmentalRadiationInfoService"
service_key = os.getenv("Service_key")  # env에 설정한 이름을 그대로 사용

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
        try:
            send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, f"MongoDB 연결 실패: {e}")
        except NameError:
            logging.error("send_telegram_message 함수가 정의되지 않아 텔레그램 알림 전송 실패.")
        sys.exit(1) # 연결 실패 시 스크립트 종료

# MongoDB 클라이언트 초기화 (기존 client = MongoClient(...) 라인을 대체)
client = get_mongo_connection()
db = client['Data']
radiation_collection        = db['Busan_radiation']
radiation_backup_collection = db['Busan_radiation_backup']


# 기존 데이터를 백업 컬렉션으로 이동하는 함수 (매일 0시 0분에 실행)
def backup_existing_data():
    current_date = datetime.now().strftime("%Y-%m-%d")
    backup_collection_name = f"Busan_radiation_backup_{current_date}"
    daily_backup_collection = db[backup_collection_name]

    # 오늘 날짜에 해당하는 백업 컬렉션이 이미 있는지 확인 (중복 백업 방지)
    if daily_backup_collection.count_documents({}) == 0:
        documents_to_backup = list(radiation_collection.find({}))
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


# 공공 데이터 API에서 방사선 선량률 정보를 가져와 MongoDB에 저장
def fetch_radiation_data():
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"부산 방사선 데이터 수집 시작 (현재 시간: {current_time})")

    try:
        params = {
            'serviceKey': service_key,
            'pageNo': '1',
            'numOfRows': '10',
            'resultType': 'xml' # XML 형식으로 요청
        }
        response = requests.get(f"{base_url}/getEnvironmentalRadiationInfo", params=params, timeout=10)
        response.raise_for_status() # HTTP 오류가 발생하면 예외를 발생시킵니다.

        root = ET.fromstring(response.content)
        items = root.findall('.//item')

        if items:
            for item in items:
                # 필요한 데이터 추출
                # 예시: <locNm>부산</locNm>, <checkTime>202301010000</checkTime>, <data>100.0</data>
                loc_nm = item.find('locNm').text if item.find('locNm') is not None else None
                check_time_str = item.find('checkTime').text if item.find('checkTime') is not None else None
                data_value_str = item.find('data').text if item.find('data') is not None else None

                if loc_nm and check_time_str and data_value_str is not None:
                    try:
                        # checkTime을 datetime 객체로 변환
                        check_time = datetime.strptime(check_time_str, '%Y%m%d%H%M')
                        dose_nSv_h = float(data_value_str) # nSv/h 단위
                        dose_microSv_h = dose_nSv_h / 1000 # μSv/h 단위로 변환

                        radiation_data = {
                            'locNm': loc_nm,
                            'checkTime': check_time, # datetime 객체로 저장
                            'dose_nSv_h': dose_nSv_h, # 원본 nSv/h 단위
                            'dose_microSv_h': dose_microSv_h, # μSv/h 단위
                            'data_fetch_time': datetime.now() # 데이터 수집 시간 추가
                        }

                        # 소수점 둘째 자리까지 반올림하여 출력
                        dose_rounded = round(dose_microSv_h, 2)
                        print(f"지역명: {radiation_data['locNm']}, 방사선량: {dose_rounded:.2f} μSv/h")
                        logging.info(f"지역명: {radiation_data['locNm']}, 방사선량: {dose_rounded:.2f} μSv/h")

                        radiation_collection.update_one(
                            {"checkTime": radiation_data["checkTime"], "locNm": radiation_data["locNm"]},
                            {"$set": radiation_data},
                            upsert=True
                        )
                    except ValueError as ve:
                        logging.error(f"데이터 변환 오류 ({loc_nm}, {check_time_str}, {data_value_str}): {ve}")
                        error_msg = f"데이터 변환 오류 (부산 방사선): {ve}"
                        send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_msg)
                else:
                    logging.warning(f"API 응답에서 필수 데이터(locNm, checkTime, data) 중 누락된 항목이 있습니다. Item: {ET.tostring(item, encoding='unicode')}")
            logging.info(f"부산 방사선 데이터 수집 작업 완료 (현재 시간: {current_time})")
        else:
            error_msg = f"방사선 선량률 정보 조회 실패: API 응답에 item이 없습니다. 응답 코드: {response.status_code}. 응답 내용: {response.text[:200]}"
            logging.error(error_msg)
            print(error_msg)
            send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_msg)

    except requests.exceptions.Timeout as e:
        error_msg = f"부산 방사선 API 요청 시간 초과: {str(e)}"
        logging.error(error_msg)
        print(error_msg)
        send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_msg)
    except requests.exceptions.RequestException as e:
        error_msg = f"부산 방사선 API 요청 오류: {str(e)}"
        logging.error(error_msg)
        print(error_msg)
        send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_msg)
    except ET.ParseError as e:
        error_msg = f"부산 방사선 XML 파싱 오류: {str(e)}"
        logging.error(error_msg)
        print(f"오류 발생: {str(e)}")
        send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_msg)
    except Exception as e:
        error_msg = f"부산 방사선 선량률 정보 조회 중 오류 발생: {str(e)}"
        logging.error(error_msg)
        print(f"오류 발생: {str(e)}")
        send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_msg)
        sys.exit(1)


def scheduled_task():
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"60분마다 부산 방사선 데이터 수집 작업 실행 중... (현재 시간: {current_time})")
    print(f"60분마다 부산 방사선 데이터 수집 작업 실행 중... (현재 시간: {current_time})")
    backup_existing_data() # 백업 먼저 실행
    fetch_radiation_data() # 그 다음 데이터 수집


# 스크립트 종료 시 MongoDB 연결 닫기
def close_mongodb_connection():
    if client:
        client.close()
        logging.info("MongoDB 연결이 닫혔습니다.")

atexit.register(close_mongodb_connection)


# 매일 2시간마다 (정각에) 작업을 실행하는 스케줄 설정 (원래는 60분 간격이었음)
# schedule.every(60).minutes.do(scheduled_task)
# 기존 60분 간격 유지
schedule.every(60).minutes.do(scheduled_task)

# 매일 자정(0시 0분)에 백업 실행
schedule.every().day().at("00:00").do(backup_existing_data)
