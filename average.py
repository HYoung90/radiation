import schedule
import time
from pymongo import MongoClient, DESCENDING
import logging
import os
from dotenv import load_dotenv
import sys # sys 모듈 추가 (get_mongo_connection에서 sys.exit 사용)
from datetime import datetime, timedelta # timedelta 추가
import requests # 텔레그램 메시지 전송 함수 내부에서 사용

# 환경 변수 로드 (telegram_config.env 파일을 사용)
load_dotenv("telegram_config.env")

# 텔레그램 설정
TELEGRAM_TOKEN = os.getenv("TELEGRAM_AVERAGE_COUNT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# 로깅 설정 (파일과 콘솔 모두 출력)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(message)s',
    handlers=[
        logging.FileHandler("radiation_processing.log"),
        logging.StreamHandler() # 콘솔에도 출력
    ]
)

# 발전소 코드에 대한 한글 이름 매핑
plant_names = {
    "WS": "월성발전소 (경북 경주)",
    "KR": "고리발전소 (부산 기장)",
    "YK": "한빛발전소 (전남 영광)",
    "UJ": "한울발전소 (경북 울진)",
    "SU": "새울발전소 (울산 울주)"
}

# 텔레그램 메시지 전송 함수
def send_telegram_message(token, chat_id, message):
    """
    텔레그램으로 메시지를 전송하는 함수
    """
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown" # Markdown 포맷팅 사용
        }
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status() # HTTP 오류 발생 시 예외 발생
        logging.info("텔레그램 메시지 전송 성공.")
    except requests.exceptions.Timeout:
        logging.error("텔레그램 메시지 전송 시간 초과.")
    except requests.exceptions.RequestException as e:
        logging.error(f"텔레그램 메시지 전송 중 오류 발생: {e}")
    except Exception as e:
        logging.error(f"예상치 못한 텔레그램 전송 오류: {e}")


# MongoDB 연결 함수
def get_mongo_connection():
    """
    MongoDB에 연결하고 클라이언트를 반환합니다.
    연결에 실패하면 스크립트를 종료합니다.
    """
    try:
        railway_mongo_uri = os.getenv("MONGO_URI")

        if railway_mongo_uri:
            # MONGO_URI에 불필요한 '=' 문자가 붙어있을 경우 제거
            cleaned_railway_mongo_uri = railway_mongo_uri.lstrip('=')
            client = MongoClient(cleaned_railway_mongo_uri)
            logging.info("Railway MongoDB 클라이언트 설정 및 연결 시도 성공")
        else:
            # MONGO_URI가 없으면 로컬 MongoDB에 연결 (개발/테스트용)
            client = MongoClient("mongodb://localhost:27017/")
            logging.info("로컬 MongoDB 클라이언트 설정 완료")

        return client
    except Exception as e:
        logging.error(f"MongoDB 연결 실패: {e}")
        # MongoDB 연결 실패 시 텔레그램 알림
        send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, f"MongoDB 연결 실패: {e}")
        sys.exit(1) # 연결 실패 시 스크립트 종료


# MongoDB 클라이언트 초기화
client = get_mongo_connection()
db = client['Data']

# 컬렉션 정의
radiation_stats_collection = db['radiation_stats'] # data.py에서 처리된 최종 데이터
weather_collection = db['NPP_weather'] # 날씨 데이터
average_results_collection = db['daily_average_radiation'] # 최종 평균 결과 저장


# 날짜에 따른 날씨 데이터 가져오기 (비가 왔는지 안 왔는지)
def get_weather_status(region_code, date_str):
    """
    주어진 지역 코드와 날짜에 해당하는 날씨 상태를 반환합니다.
    '비' 또는 '비/눈'이 있으면 'rain', 아니면 'no_rain'을 반환합니다.
    """
    # 날짜 문자열을 datetime 객체로 변환 (날짜만)
    target_date = datetime.strptime(date_str, '%Y-%m-%d').date()

    # 해당 날짜의 모든 날씨 데이터 조회
    # data_fetch_time이 해당 날짜에 속하는 문서 검색
    start_of_day = datetime.combine(target_date, datetime.min.time())
    end_of_day = datetime.combine(target_date, datetime.max.time())

    weather_data = weather_collection.find({
        'region': region_code,
        'data_fetch_time': {'$gte': start_of_day, '$lte': end_of_day}
    })

    # 해당 날짜에 비가 온 기록이 있는지 확인
    for record in weather_data:
        wth_stt = record.get('wthStt', '').strip()
        if '비' in wth_stt or '눈' in wth_stt:
            return 'rain'
    return 'no_rain'


# 메인 처리 로직
def calculate_and_store_daily_average():
    current_time_log = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"일일 평균 방사선량 계산 시작 (현재 시간: {current_time_log})")
    print(f"일일 평균 방사선량 계산 시작 (현재 시간: {current_time_log})")

    # 어제 날짜 설정
    yesterday = datetime.now() - timedelta(days=1)
    yesterday_date_str = yesterday.strftime('%Y-%m-%d')
    logging.info(f"처리 대상 날짜: {yesterday_date_str}")

    # 어제 날짜에 해당하는 데이터만 필터링
    start_of_yesterday = datetime.combine(yesterday.date(), datetime.min.time())
    end_of_yesterday = datetime.combine(yesterday.date(), datetime.max.time())

    # radiation_stats 컬렉션에서 어제 데이터 가져오기
    pipeline = [
        {
            '$match': {
                'tm': {'$gte': start_of_yesterday, '$lte': end_of_yesterday}
            }
        },
        {
            '$project': {
                '_id': 0,
                'locNm': '$locNm',
                'curVal': '$curVal',
                'wthStt': '$wthStt', # 날씨 상태 필드 포함
                'tm_date': {'$dateToString': {'format': '%Y-%m-%d', 'date': '$tm'}}, # 날짜 부분만 추출
                'tm_hour': {'$hour': '$tm'} # 시간 부분만 추출 (나중에 사용 가능)
            }
        }
    ]
    daily_stats_data = list(radiation_stats_collection.aggregate(pipeline))
    logging.info(f"어제 날짜({yesterday_date_str})의 radiation_stats 데이터 {len(daily_stats_data)}건 조회 완료.")

    # 지역별 데이터 그룹화
    grouped_by_plant = {}
    for item in daily_stats_data:
        locNm = item.get('locNm')
        if locNm not in grouped_by_plant:
            grouped_by_plant[locNm] = []
        grouped_by_plant[locNm].append(item)

    results_to_store = []

    # 각 발전소별로 처리
    for locNm, data_list in grouped_by_plant.items():
        # 날씨 데이터에 사용할 region code 매핑
        region_mapping_for_weather = {
            "고리본부": "KR",
            "월성본부": "WS",
            "한빛본부": "YK",
            "한울본부": "UJ",
            "새울본부": "SU"
        }
        region_code = region_mapping_for_weather.get(locNm, locNm) # 매핑 없으면 locNm 그대로 사용

        # 어제의 실제 날씨 상태 확인
        # get_weather_status 함수는 해당 날짜에 '비'가 있는지 없는지 판단
        actual_weather_status_yesterday = get_weather_status(region_code, yesterday_date_str)
        logging.info(f"[{locNm}] 어제의 실제 날씨 상태 ({yesterday_date_str}): {actual_weather_status_yesterday}")

        total_radiation = {'rain': [], 'no_rain': []}
        rain_days_count = 0
        no_rain_days_count = 0

        # 데이터 순회하며 비/비 없음 분류
        for item in data_list:
            curVal = item.get('curVal')
            if curVal is not None:
                # weather_status = item.get('wthStt') # radiation_stats에 저장된 날씨 상태 (필요시 사용)
                # 여기서는 실제 어제의 날씨 상태를 기준으로 분류
                if actual_weather_status_yesterday == 'rain':
                    total_radiation['rain'].append(curVal)
                else:
                    total_radiation['no_rain'].append(curVal)

        # 평균 계산
        rain_avg = sum(total_radiation['rain']) / len(total_radiation['rain']) if total_radiation['rain'] else 0
        no_rain_avg = sum(total_radiation['no_rain']) / len(total_radiation['no_rain']) if total_radiation['no_rain'] else 0

        # 증가율 계산 (no_rain_avg가 0이 아니어야 함)
        percentage_increase = '-'
        if no_rain_avg > 0:
            increase = rain_avg - no_rain_avg
            percentage_increase = (increase / no_rain_avg) * 100
            percentage_increase = f"{percentage_increase:.2f}"
        else:
            percentage_increase = "N/A" # 비가 오지 않은 날 평균이 0일 경우

        # 결과를 리스트에 추가
        result_entry = {
            "date": yesterday_date_str,
            "plant_name": locNm,
            "rain_days": len(total_radiation['rain']), # 해당 날짜에 비가 왔다고 분류된 데이터 포인트 수
            "no_rain_days": len(total_radiation['no_rain']), # 해당 날짜에 비가 오지 않았다고 분류된 데이터 포인트 수
            "rain_avg": rain_avg,
            "no_rain_avg": no_rain_avg,
            "percentage_increase": percentage_increase,
            "processed_at": datetime.now()
        }
        results_to_store.append(result_entry)
        logging.info(f"[{locNm}] 일일 평균 계산 완료: 비 온 날 평균={rain_avg:.4f} μSv/h, 비 안 온 날 평균={no_rain_avg:.4f} μSv/h, 증가율={percentage_increase}%")


    # MongoDB average_results_collection에 저장 (날짜와 발전소 이름으로 upsert)
    if results_to_store:
        for entry in results_to_store:
            query = {"date": entry["date"], "plant_name": entry["plant_name"]}
            average_results_collection.update_one(query, {"$set": entry}, upsert=True)
        logging.info(f"일일 평균 방사선량 데이터 {len(results_to_store)}건 average_results_collection에 저장/업데이트 완료.")
    else:
        logging.info("저장할 일일 평균 방사선량 데이터가 없습니다.")

    # 텔레그램으로 일일 리포트 전송
    send_daily_report_via_telegram(results_to_store)

    logging.info(f"일일 평균 방사선량 계산 및 저장 작업 완료 (현재 시간: {current_time_log})")
    print(f"일일 평균 방사선량 계산 및 저장 작업 완료 (현재 시간: {current_time_log})")


# 텔레그램으로 일일 리포트 전송 함수
def send_daily_report_via_telegram(avg_data_list):
    try:
        if avg_data_list:
            message = "*일일 방사선량 평균 리포트 (어제 기준)*\n\n"
            for entry in avg_data_list:
                plant_name = entry.get('plant_name', '알 수 없음')
                rain_days = entry.get('rain_days', 0)
                no_rain_days = entry.get('no_rain_days', 0)
                rain_avg = entry.get('rain_avg', 0)
                no_rain_avg = entry.get('no_rain_avg', 0)
                percentage_increase = entry.get('percentage_increase', '-')
                message += (
                    f"**발전소: {plant_name}**\n"
                    f"비 온 날 데이터 포인트 수: {rain_days}개\n"
                    f"비 안 온 날 데이터 포인트 수: {no_rain_days}개\n"
                    f"비 온 날 평균 방사선량: `{rain_avg:.4f} μSv/h`\n"
                    f"비 안 온 날 평균 방사선량: `{no_rain_avg:.4f} μSv/h`\n"
                    f"방사선량 증가율: `{percentage_increase}%`\n\n"
                )
            send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, message)
            logging.info("일일 방사선량 평균 리포트 텔레그램 전송 완료.")
        else:
            logging.info("전송할 일일 평균 데이터가 없습니다.")
    except Exception as e:
        logging.error(f"일일 리포트 전송 중 오류 발생: {e}", exc_info=True)
        send_telegram_message(
            TELEGRAM_TOKEN,
            TELEGRAM_CHAT_ID,
            f"일일 리포트 전송 중 오류 발생:\\n{e}"
        )


# 자동화 함수 (매일 오전 8시에 실행)
def automate():
    calculate_and_store_daily_average()

# 스케줄 설정
schedule.every().day.at("08:00").do(automate) # 매일 오전 8시에 실행
