from pymongo import MongoClient
import logging
from statistics import mean
import requests
import schedule
import time
import os
#from dotenv import load_dotenv
import sys # sys 모듈 추가
import atexit # atexit 모듈 추가
from datetime import datetime, timedelta # datetime, timedelta 모듈 추가

# 환경 변수 로드
#load_dotenv("telegram_config.env") # .env 파일에서 로드

# 로그 설정 (파일과 콘솔 모두 출력)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(message)s',
    handlers=[
        logging.FileHandler("busan_alert.log"),
        logging.StreamHandler()
    ]
)

# 텔레그램 설정 - Busan Radiation 봇 사용
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BUSAN_RADIATION_TOKEN") # .env 파일에서 Busan Radiation 봇의 토큰을 가져옴
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") # .env 파일에서 채팅 ID를 가져옴

# MongoDB 연결 함수
def get_mongo_connection():
    """
    MongoDB에 연결하고 클라이언트를 반환합니다.
    연결에 실패하면 스크립트를 종료합니다.
    """
    try:
        # 1) 환경변수에서 URI 읽고 앞뒤 공백/개행 제거
        raw_uri = os.getenv("MONGO_URI", "")
        uri = raw_uri.strip()

        # 2) 로컬 Fallback (원하시면)
        if not uri:
            logging.info("MONGO_URI 미설정, 로컬 MongoDB로 연결 시도")
            uri = "mongodb://localhost:27017/"

        # 3) 실제 연결
        client = MongoClient(uri)
        logging.info("Railway MongoDB 클라이언트 설정 및 연결 시도 성공")
        return client

    except Exception as e:
        logging.error(f"MongoDB 연결 실패: {e}", exc_info=True)
        # 연결 실패 시 필요한 경우 텔레그램 알림도 보내고 종료
        try:
            send_alert_to_another_bot(f"🚨 MongoDB 연결 실패: {e}")
        except Exception:
            logging.error("send_alert_to_another_bot 호출 실패")
        sys.exit(1)


# MongoDB 클라이언트 초기화 (기존 client = MongoClient(...) 라인을 대체)
client = get_mongo_connection()
db = client['Data']
radiation_collection = db['Busan_radiation'] # Busan_radiation 컬렉션 사용

# 텔레그램 메시지 전송 함수
def send_alert_to_another_bot(message):
    chat_id = TELEGRAM_CHAT_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown" # 메시지 포맷팅을 위해 Markdown 사용
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status() # HTTP 오류 발생 시 예외 발생
        logging.info("텔레그램 알림 전송 성공.")
    except requests.exceptions.Timeout:
        logging.error("텔레그램 알림 전송 시간 초과.")
    except requests.exceptions.RequestException as e:
        logging.error(f"텔레그램 알림 전송 중 오류 발생: {e}")
    except Exception as e:
        logging.error(f"예상치 못한 텔레그램 알림 전송 오류: {e}")


# 방사선량 통계 가져오기 및 알림 전송 함수
def fetch_radiation_statistics_and_alert():
    current_time_log = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"방사선량 통계 가져오기 및 알림 시작 (현재 시간: {current_time_log})")
    print(f"방사선량 통계 가져오기 및 알림 시작 (현재 시간: {current_time_log})")

    try:
        # 최근 24시간 동안의 데이터만 가져오기
        end_time = datetime.now()
        start_time = end_time - timedelta(days=1)

        # checkTime 필드가 datetime 객체임을 가정하고 범위 조회
        data_cursor = radiation_collection.find({
            'checkTime': {'$gte': start_time, '$lte': end_time}
        })
        data = list(data_cursor)

        if not data:
            logging.warning("최근 24시간 동안의 부산 방사선 데이터가 없습니다.")
            send_alert_to_another_bot("⚠️ *경고:* 최근 24시간 동안의 부산 방사선 데이터가 없습니다.")
            return

        highest_value = 0
        highest_region = "알 수 없음"
        total_radiation = [] # 모든 유효한 방사선량 값을 저장할 리스트

        for item in data:
            try:
                # 'data' 필드는 nSv/h 단위의 문자열 또는 숫자일 수 있으므로 float로 변환
                radiation_value = float(item['dose_nSv_h']) # dose_nSv_h 필드 사용
            except (ValueError, KeyError):
                logging.warning(f"유효하지 않은 방사선량 데이터 또는 필드 누락: {item.get('dose_nSv_h', 'N/A')}")
                continue

            if radiation_value > 0: # 유효한 양의 값만 포함
                total_radiation.append(radiation_value)

                if radiation_value > highest_value:
                    highest_value = radiation_value
                    highest_region = item.get('locNm', '알 수 없음') # 지역명 필드 사용

        if total_radiation:
            average_radiation = mean(total_radiation)
        else:
            average_radiation = 0

        # 결과 메시지 포맷팅 (줄 바꿈 추가 및 강조)
        result_message = (
            f"📍 *부산 실시간 방사선량 요약 (최근 24시간)* 📍\n\n"
            f"✨ *가장 높은 방사선량 지역:*\n"
            f"   *{highest_region}* ({highest_value:.2f} nSv/h)\n\n"
            f"📊 *부산 전체 평균 방사선량:*\n"
            f"   *{average_radiation:.2f} nSv/h*\n\n"
            f"_\_본 데이터는 공공 API를 통해 수집된 정보입니다._"
        )
        print(result_message)
        logging.info(f"생성된 알림 메시지:\n{result_message}")

        # 텔레그램 알림 전송
        send_alert_to_another_bot(result_message)

    except Exception as e:
        logging.error(f"방사선량 통계 가져오기 중 오류 발생: {e}", exc_info=True)
        print(f"방사선량 통계 가져오기 중 오류 발생: {e}")
        send_alert_to_another_bot(f"🚨 *부산 방사선량 알림 스크립트 오류:* 🚨\n{str(e)}")


# 스케줄 함수
def scheduled_alert_task():
    current_time_log = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"2시간마다 부산 방사선 통계 알림 작업 실행 중... (현재 시간: {current_time_log})")
    print(f"2시간마다 부산 방사선 통계 알림 작업 실행 중... (현재 시간: {current_time_log})")
    fetch_radiation_statistics_and_alert()

# 매일 2시간마다 (정각에) 작업을 실행하는 스케줄 설정
schedule.every(2).hours.do(scheduled_alert_task)

# 스크립트 종료 시 MongoDB 연결 닫기
def close_mongodb_connection():
    if client:
        client.close()
        logging.info("MongoDB 연결이 닫혔습니다.")
        print("MongoDB 연결이 닫혔습니다.")

atexit.register(close_mongodb_connection)

