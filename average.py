import schedule
import time
from pymongo import MongoClient, DESCENDING
import logging
import os
# from dotenv import load_dotenv # 이 라인을 제거합니다.
import sys
from datetime import datetime, timedelta
import requests # 텔레그램 메시지 전송 함수 내부에서 사용 (복원)

# 로깅 설정 (파일과 콘솔 모두 출력)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(message)s',
    handlers=[
        logging.FileHandler("radiation_processing.log"),
        logging.StreamHandler()
    ]
)

# 텔레그램 설정 - Railway 환경 변수를 직접 사용합니다.
TELEGRAM_TOKEN = os.getenv("TELEGRAM_AVERAGE_COUNT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# 발전소 코드에 대한 한글 이름 매핑
plant_names = {
    "WS": "월성발전소 (경북 경주)",
    "KR": "고리발전소 (부산 기장)",
    "YK": "한빛발전소 (전남 영광)",
    "UJ": "한울발전소 (경북 울진)",
    "SU": "새울발전소 (울산 울주)"
}

# 텔레그램 메시지 전송 함수 (복원)
def send_telegram_message(token, chat_id, message):
    if not token or not chat_id:
        logging.warning("텔레그램 토큰 또는 채팅 ID가 설정되지 않아 메시지를 보낼 수 없습니다.")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        logging.info("텔레그램 메시지 전송 성공.")
    except requests.exceptions.Timeout:
        logging.error("텔레그램 메시지 전송 시간 초과.")
    except requests.exceptions.RequestException as e:
        logging.error(f"텔레그램 메시지 전송 중 오류 발생: {e}")
    except Exception as e:
        logging.error(f"예상치 못한 텔레그램 메시지 전송 오류: {e}")


# MongoDB 연결 함수
def get_mongo_connection():
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
        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, f"🚨 *MongoDB 연결 실패:* 🚨\n{e}")
        sys.exit(1)

client = get_mongo_connection()
db = client['Data']
# NPP_radiation 컬렉션에서 데이터 로드
npp_radiation_collection = db['NPP_radiation']
# 통합 방사선 통계 컬렉션 (data.py에서 생성)
radiation_stats_collection = db['radiation_stats']


def calculate_and_report_daily_averages():
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"일일 방사선량 평균 계산 및 리포트 시작. (현재 시간: {current_time})")
    print(f"일일 방사선량 평균 계산 및 리포트 시작. (현재 시간: {current_time})")

    try:
        # 어제 날짜 계산
        yesterday = datetime.now() - timedelta(days=1)
        yesterday_str = yesterday.strftime('%Y%m%d')

        pipeline = [
            # 어제 날짜에 해당하는 데이터만 필터링 (tm 필드의 앞 8자리가 어제 날짜와 일치)
            {
                '$match': {
                    'tm': {'$regex': f'^{yesterday_str}'}
                }
            },
            # 필요한 필드만 선택하고 radiation_value를 숫자로 변환 시도 (문자열인 경우 None)
            {
                '$addFields': {
                    'numeric_radiation_value': {
                        '$convert': {
                            'input': '$radiation_value',
                            'to': 'double',
                            'onError': None, # 변환 실패 시 null
                            'onNull': None   # null 입력 시 null
                        }
                    }
                }
            },
            # 그룹화: locNm (발전소/지역)별로 그룹화
            {
                '$group': {
                    '_id': '$locNm', # locNm 필드로 그룹화
                    'total_radiation': {'$sum': '$numeric_radiation_value'}, # 유효한 값들의 합계
                    'count': {'$sum': {'$cond': [{'$ne': ['$numeric_radiation_value', None]}, 1, 0]}}, # 유효한 값들의 개수
                    'radiation_values': {'$push': '$numeric_radiation_value'} # 각 값들을 배열로 저장
                }
            },
            # 평균 계산 및 최종 필드 추가
            {
                '$project': {
                    '_id': 0, # _id 필드 제거
                    'plant_name': '$_id', # 그룹화된 _id (locNm)를 plant_name으로 변경
                    'avg_radiation': {
                        '$cond': [
                            {'$gt': ['$count', 0]}, # count가 0보다 크면 평균 계산
                            {'$divide': ['$total_radiation', '$count']},
                            0 # 아니면 0
                        ]
                    }
                }
            }
        ]

        avg_data_cursor = radiation_stats_collection.aggregate(pipeline)
        avg_data_list = list(avg_data_cursor)
        logging.info(f"어제 날짜 기준 일일 방사선량 평균 {len(avg_data_list)}개 항목 계산 완료.")

        if avg_data_list:
            message = "*일일 방사선량 평균 리포트 (어제 기준)*\n\n"
            for entry in avg_data_list:
                plant_name = entry.get('plant_name', '알 수 없음')
                avg_radiation = entry.get('avg_radiation', 0)
                message += (
                    f"**발전소: {plant_name}**\n"
                    f"어제 평균 방사선량: `{avg_radiation:.4f}`\n\n" # 단위는 필요에 따라 추가
                )
            if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
                send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, message)
            logging.info("일일 방사선량 평균 리포트 텔레그램 전송 완료.")
        else:
            logging.info("일일 방사선량 평균 리포트를 생성할 데이터가 없습니다.")
            if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
                send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, "⚠️ *경고:* 일일 방사선량 평균 리포트를 생성할 데이터가 없습니다.")

    except Exception as e:
        logging.error(f"일일 방사선량 평균 계산 및 리포트 생성 중 오류 발생: {e}", exc_info=True)
        print(f"일일 방사선량 평균 계산 및 리포트 생성 중 오류 발생: {e}")
        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, f"🚨 *일일 방사선량 평균 리포트 스크립트 오류:* 🚨\n{str(e)}")


def automate():
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"매일 0시 5분에 일일 방사선량 평균 리포트 작업 실행 중... (현재 시간: {current_time})")
    print(f"매일 0시 5분에 일일 방사선량 평균 리포트 작업 실행 중... (현재 시간: {current_time})")
    calculate_and_report_daily_averages()

schedule.every().day.at("00:05").do(automate)

# 스크립트 종료 시 MongoDB 연결 닫기
def close_mongodb_connection():
    if client:
        client.close()
        logging.info("MongoDB 연결이 닫혔습니다.")

atexit.register(close_mongodb_connection)