import requests
from pymongo import MongoClient
import logging
import schedule
import time
import os
from datetime import datetime
from dotenv import load_dotenv

# 환경 변수 로드
load_dotenv()

# 로그 설정
logging.basicConfig(
    filename="threshold_alarm.log",
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(message)s'
)

# 텔레그램 설정
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# 텔레그램 설정 로드 확인
print(f"텔레그램 토큰: {TELEGRAM_TOKEN}")
print(f"텔레그램 챗 ID: {TELEGRAM_CHAT_ID}")

# MongoDB 연결 설정
client = MongoClient("mongodb://localhost:27017/")
db = client['power_plant_weather']
radiation_collection = db['nuclear_radiation']
avg_db = client['radiation_statistics']  # 평균을 저장할 새로운 데이터베이스
avg_collection = avg_db['regional_average']  # 평균 데이터 저장 컬렉션


def send_telegram_message(token, chat_id, message):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    response = requests.post(url, data=data)

    # 요청 결과 확인
    if response.status_code == 200:
        print("텔레그램 메시지 전송 성공!")
        logging.info("텔레그램 메시지 전송 성공!")
    else:
        print(f"텔레그램 메시지 전송 실패. 상태 코드: {response.status_code}, 응답: {response.text}")
        logging.error(f"텔레그램 메시지 전송 실패. 상태 코드: {response.status_code}, 응답: {response.text}")


def check_sigma_alert():
    try:
        # 각 발전소와 그에 대한 설명을 매핑
        plant_names = {
            "WS": "월성발전소 (경북 경주)",
            "KR": "고리발전소 (부산 기장)",
            "YK": "한빛발전소 (전남 영광)",
            "UJ": "한울발전소 (경북 울진)",
            "SU": "새울발전소 (울산 울주)"
        }

        current_time = datetime.now().strftime("%Y-%m-%d %H:%M")
        message = f"*{current_time} 기준 3시간마다 방사선량 보고*\n\n"

        for plant_code, plant_name in plant_names.items():
            # 해당 지역의 최신 방사선 데이터 가져오기
            latest_radiation = radiation_collection.find_one({"genName": plant_code}, sort=[("time", -1)])

            if not latest_radiation:
                logging.warning(f"{plant_name}의 최신 방사선 데이터를 찾을 수 없습니다.")
                print(f"{plant_name}의 최신 방사선 데이터를 찾을 수 없습니다.")
                continue

            radiation_value = float(latest_radiation.get("value", 0))
            logging.info(f"{plant_name} 최신 방사선량: {radiation_value} μSv/h")
            print(f"{plant_name} 최신 방사선량: {radiation_value} μSv/h")

            # 해당 지역의 평균값 가져오기
            avg_entry = avg_collection.find_one({"region": plant_code}, sort=[("date", -1)])

            if avg_entry:
                no_rain_avg = round(avg_entry.get("no_rain_avg", 0), 3)  # 평균값을 소수점 3자리로 반올림

                # 기준값 계산 (소수점 3자리로 설정)
                threshold = round(no_rain_avg * 2, 3)

                # 최신 강우 데이터 가져오기
                latest_rain_data = db['plant_measurements'].find_one({"region": plant_code}, sort=[("time", -1)])
                rain_status = "강우 없음"  # 기본 강우 상태
                if latest_rain_data:
                    rain_value = latest_rain_data.get("rainfall", 0)
                    if rain_value > 0:
                        rain_status = f"강우 발생 (일일 누적 강우량: {rain_value} mm)"

                # 메시지에 각 발전소의 현재 방사선 값, 평균값, 기준값, 강우 여부 추가
                message += f"발전소: {plant_name}\n"
                message += f"측정값: {radiation_value} μSv/h\n"
                message += f"평균값: {no_rain_avg} μSv/h\n"  # 소수점 3자리로 반올림된 평균값
                message += f"기준값: {threshold} μSv/h\n"
                message += f"강우 여부: {rain_status}\n"  # 강우 여부 추가

                # 기준값 초과 시 경고 메시지 추가
                if radiation_value > threshold:
                    message += "*경고*: 방사선량이 기준값을 초과했습니다!\n\n"
                    logging.info(f"{plant_name} 기준값 초과 알림 전송 완료.")
                    print(f"{plant_name}: 기준값 초과 알림 텔레그램 전송 완료.")
                else:
                    message += "현재 방사선량이 정상 범위입니다.\n\n"
                    logging.info(f"{plant_name} 방사선량 정상: {radiation_value} μSv/h")
                    print(f"{plant_name}: 방사선량 정상 상태입니다.")
            else:
                logging.warning(f"{plant_name}의 평균값을 찾을 수 없습니다.")
                print(f"{plant_name}의 평균값을 찾을 수 없습니다.")
                message += f"{plant_name}의 평균값을 찾을 수 없습니다.\n\n"

        # 모든 데이터 전송
        send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, message)
        print("텔레그램 메시지가 성공적으로 전송되었습니다.")

    except Exception as e:
        logging.error(f"방사선 데이터 처리 중 오류 발생: {e}")
        error_message = f"방사선 데이터 처리 중 오류 발생:\n{str(e)}"
        send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_message)
        print("오류 발생으로 인해 텔레그램 경고 메시지를 전송했습니다.")

# 스크립트가 실행된 후 3시간마다 작업을 실행하는 스케줄 설정
schedule.every(3).hours.do(check_sigma_alert)

if __name__ == "__main__":
    logging.info("스크립트 실행 후 3시간마다 방사선 데이터를 확인하는 스케줄을 시작합니다.")
    print("스크립트 실행 후 3시간마다 방사선 데이터를 확인하는 스케줄을 시작합니다.")

    # 처음 실행 시 즉시 알림을 보냄
    check_sigma_alert()

    # 스케줄을 지속적으로 실행
    while True:
        schedule.run_pending()
        time.sleep(60)
