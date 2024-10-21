from pymongo import MongoClient, DESCENDING
import schedule
import time
from datetime import datetime
import logging
import os
from dotenv import load_dotenv
from telegram_notifier import send_telegram_message  # 텔레그램 알림 통합
import sys  # sys 모듈 임포트 추가

# 환경 변수 로드
load_dotenv()

# 텔레그램 설정
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# 로깅 설정 (콘솔 및 파일)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(message)s',
    handlers=[
        logging.FileHandler("radiation_processing.log"),
        logging.StreamHandler()
    ]
)

# MongoDB 연결 함수
def get_mongo_connection():
    """
    MongoDB에 연결하고 클라이언트를 반환합니다.
    연결에 실패하면 스크립트를 종료합니다.
    """
    try:
        client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017/"))
        logging.info("MongoDB 연결 성공.")
        return client
    except Exception as e:
        logging.error(f"MongoDB 연결 실패: {e}")
        sys.exit(1)

# 방사선 평균값 계산 함수
def calculate_radiation_averages():
    """
    radiation_stats 컬렉션의 모든 데이터를 가져와
    각 지역별로 비가 온 날과 비가 오지 않은 날을 카운트하고,
    각각의 평균 방사선량과 증가율을 계산하여
    regional_average.daily_average 컬렉션에 저장합니다.
    """
    try:
        client = get_mongo_connection()

        # 데이터베이스 및 컬렉션 설정
        db = client['power_plant_weather']
        stats_collection = db['radiation_stats']
        avg_db = client['radiation_statistics']
        avg_collection = avg_db['regional_average']

        # 모든 데이터 가져오기
        data = list(stats_collection.find({}, {"_id": 0}))
        logging.info(f"radiation_stats 컬렉션에서 {len(data)}개의 레코드를 가져왔습니다.")

        if not data:
            logging.info("radiation_stats 컬렉션에 데이터가 없습니다.")
            return

        # rain 필드의 고유 값 확인
        unique_rain_values = set(entry.get('rain') for entry in data)
        logging.info(f"Unique rain values: {unique_rain_values}\n")

        # 강우 여부에 따라 그룹핑 및 카운팅
        grouped_data = {}
        for entry in data:
            region = entry.get('region')
            rain = entry.get('rain')
            value = entry.get('value')

            if not region or value is None:
                logging.warning(f"유효하지 않은 데이터 항목 스킵: {entry}")
                continue

            if region not in grouped_data:
                grouped_data[region] = {
                    'rain_days': 0,
                    'no_rain_days': 0,
                    'rain_values': [],
                    'no_rain_values': []
                }

            # rain 필드의 값에 따라 강우 여부 결정
            if isinstance(rain, str):
                rain_lower = rain.lower()
                if rain_lower == 'rain':
                    grouped_data[region]['rain_days'] += 1
                    grouped_data[region]['rain_values'].append(float(value))
                elif rain_lower == 'no_rain':
                    grouped_data[region]['no_rain_days'] += 1
                    grouped_data[region]['no_rain_values'].append(float(value))
                else:
                    logging.warning(f"알 수 없는 rain 상태 스킵: {entry}")
            elif isinstance(rain, bool):
                if rain:
                    grouped_data[region]['rain_days'] += 1
                    grouped_data[region]['rain_values'].append(float(value))
                else:
                    grouped_data[region]['no_rain_days'] += 1
                    grouped_data[region]['no_rain_values'].append(float(value))
            else:
                try:
                    rain_status = float(rain) > 0
                    if rain_status:
                        grouped_data[region]['rain_days'] += 1
                        grouped_data[region]['rain_values'].append(float(value))
                    else:
                        grouped_data[region]['no_rain_days'] += 1
                        grouped_data[region]['no_rain_values'].append(float(value))
                except (TypeError, ValueError):
                    logging.warning(f"비정상적인 rain 값 스킵: {entry}")
                    continue

        # 평균값 및 증가 비율 계산
        results = []
        for region, values in grouped_data.items():
            # 소수점 반올림 제거: 계산 시 정밀도 유지
            rain_avg = (sum(values['rain_values']) / len(values['rain_values'])) if values['rain_values'] else 0.0
            no_rain_avg = (sum(values['no_rain_values']) / len(values['no_rain_values'])) if values['no_rain_values'] else 0.0

            if no_rain_avg > 0:
                percentage_increase = ((rain_avg - no_rain_avg) / no_rain_avg) * 100
                percentage_increase = round(percentage_increase, 2)
            else:
                percentage_increase = 'N/A'

            results.append({
                "region": region,
                "rain_days": values['rain_days'],
                "no_rain_days": values['no_rain_days'],
                "rain_avg": rain_avg,          # 소수점 반올림 제거
                "no_rain_avg": no_rain_avg,    # 소수점 반올림 제거
                "percentage_increase": percentage_increase
            })

        # 현재 날짜 기준으로 저장
        today_str = str(datetime.now().date())
        for result in results:
            avg_collection.update_one(
                {"region": result["region"], "date": today_str},
                {"$set": {
                    "rain_days": result["rain_days"],
                    "no_rain_days": result["no_rain_days"],
                    "rain_avg": result["rain_avg"],
                    "no_rain_avg": result["no_rain_avg"],
                    "percentage_increase": result["percentage_increase"]
                }},
                upsert=True  # 기존 데이터가 없으면 새로 생성
            )

        # 전체 평균을 하나의 문서로 저장 (for report)
        avg_collection.update_one(
            {"date": today_str},
            {"$set": {"averages": results}},
            upsert=True
        )

        logging.info("강우 유무에 따른 평균 방사선 데이터가 regional_average.daily_average 컬렉션에 저장되었습니다.\n")

    except Exception as e:
        logging.error(f"방사선 평균값 계산 중 오류 발생: {e}", exc_info=True)

# 텔레그램으로 일일 보고서 전송 함수
def send_daily_report():
    """
    regional_average.daily_average 컬렉션에서 최신 날짜의 데이터를 가져와
    텔레그램으로 보고서를 전송합니다.
    """
    try:
        client = get_mongo_connection()

        # 평균 데이터를 저장한 데이터베이스와 컬렉션
        avg_db = client['regional_average']
        avg_collection = avg_db['daily_average']

        # 최신 날짜 기준으로 데이터 가져오기
        latest_entry = avg_collection.find_one(sort=[("date", DESCENDING)])
        if latest_entry:
            message = "*Daily Radiation Averages Report*\n\n"
            message += f"Date: {latest_entry.get('date', 'N/A')}\n\n"

            for entry in latest_entry.get('averages', []):
                region = entry.get('region', 'Unknown')
                rain_days = entry.get('rain_days', 0)
                no_rain_days = entry.get('no_rain_days', 0)
                rain_avg = entry.get('rain_avg', 0)
                no_rain_avg = entry.get('no_rain_avg', 0)
                percentage_increase = entry.get('percentage_increase', '-')

                # 텔레그램 메시지 포맷팅 시 소수점 네 자리까지 표시
                message += f"*Region:* {region}\n"
                message += f"Rain Days: {rain_days}\n"
                message += f"No Rain Days: {no_rain_days}\n"
                message += f"Rain Avg: {rain_avg:.4f} μSv/h\n"          # 소수점 네 자리까지 표시
                message += f"No Rain Avg: {no_rain_avg:.4f} μSv/h\n"    # 소수점 네 자리까지 표시
                message += f"Percentage Increase: {percentage_increase}%\n\n"

            # 텔레그램 메시지 전송
            send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, message)
            logging.info("Daily radiation averages report sent via Telegram.\n")
        else:
            logging.info("No average data found to send.\n")
    except Exception as e:
        logging.error(f"Daily report 전송 중 오류 발생: {e}", exc_info=True)
        error_message = f"Daily report 전송 중 오류 발생:\n{str(e)}"
        send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_message)

# 메인 스케줄러 함수
def main():
    """
    스케줄러를 설정하고 시작합니다.
    """
    # 스케줄러 설정: 매일 새벽 1시에 텔레그램으로 보고서 전송
    schedule.every().day.at("01:00").do(send_daily_report)

    logging.info("Scheduler 시작: 매일 새벽 1시에 텔레그램으로 보고서를 전송합니다.\n")
    print("Scheduler 시작: 매일 새벽 1시에 텔레그램으로 보고서를 전송합니다.\n")

    try:
        while True:
            schedule.run_pending()
            time.sleep(60)  # 1분마다 스케줄 확인
    except (KeyboardInterrupt, SystemExit):
        logging.info("프로그램이 수동으로 종료되었습니다.")
        print("프로그램이 수동으로 종료되었습니다.")
        sys.exit(0)

if __name__ == "__main__":
    print("Radiation averages manual processing started.\n")
    logging.info("Radiation averages manual processing started.\n")
    try:
        # 즉시 계산 및 저장
        calculate_radiation_averages()
        print("Manual processing 완료.\n")
        logging.info("Manual processing 완료.\n")
        # 스케줄러 시작
        main()
    except Exception as e:
        print(f"수동 처리 중 오류 발생: {e}")
        logging.error(f"수동 처리 중 오류 발생: {e}", exc_info=True)
        send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, f"수동 처리 중 오류 발생:\n{str(e)}")
        sys.exit(1)
