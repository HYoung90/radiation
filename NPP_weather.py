import requests
import xml.etree.ElementTree as ET
from pymongo import MongoClient
import logging
import schedule
import time
import atexit
import sys
import os
from dotenv import load_dotenv
from datetime import datetime  # 추가된 부분
from pymongo import MongoClient, DESCENDING
from telegram_notifier import send_telegram_message  # 텔레그램 알림 통합

# 환경 변수 로드
load_dotenv("C:/Users/user/Desktop/Server_Final/telegram_config.env")  # 환경 변수 파일명 명시

# 로그 설정
logging.basicConfig(
    filename="weather_data_fetch.log",
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(message)s'
)

# 텔레그램 설정
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# MongoDB 연결
client = MongoClient("mongodb://localhost:27017/")
db = client['Data']

# 발전소별 데이터를 저장할 컬렉션
collection = db['NPP_weather']  # 발전소 데이터를 저장할 컬렉션
backup_collection = db['NPP_weather_backup']  # 백업 컬렉션

# 측정 지역 코드
regions = ['KR', 'WS', 'YK', 'UJ', 'SU']

# 공공 API URL (서비스 키는 동일, region을 동적으로 설정)
weather_base_url = "http://data.khnp.co.kr/environ/service/realtime/weather"
air_stability_base_url = "http://data.khnp.co.kr/environ/service/realtime/air"
service_key = os.getenv("Service_key")  # env에 설정한 이름을 그대로 사용

# 대기안정도 설명 매핑
air_stability_mapping = {
    "A": "심한 불안정",
    "B": "불안정",
    "C": "약한 불안정",
    "D": "중립",
    "E": "약한 안정",
    "F": "안정",
    "G": "심한 안정"
}

def log_program_exit():
    """프로그램 종료 시 로그를 남김"""
    logging.info("프로그램이 종료되었습니다.")
    print("프로그램이 종료되었습니다.")

# 프로그램 종료 시 처리할 작업 등록
atexit.register(log_program_exit)

def backup_existing_data():
    """기존 데이터를 발전소별로 최신 데이터만 남겨두고 백업 컬렉션으로 이동"""
    try:
        # 각 발전소별로 최신 데이터만 찾아서 백업
        for region in regions:
            # 각 발전소의 최신 데이터를 가져옴
            latest_data = collection.find_one({"genName": region}, {"_id": 0}, sort=[("time", DESCENDING)])

            if latest_data:
                # backup_collection에서 같은 genName과 time을 가진 데이터가 있는지 확인
                existing_data = backup_collection.find_one({"genName": region, "time": latest_data["time"]})

                if existing_data:
                    logging.info(f"{region}의 {latest_data['time']}에 해당하는 데이터는 이미 백업되어 있습니다.")
                else:
                    # 최신 데이터는 백업 컬렉션으로 이동
                    backup_collection.insert_one(latest_data)
                    # 최신 데이터 외에 이전 데이터는 삭제
                    collection.delete_many({"genName": region, "time": {"$ne": latest_data["time"]}})
                    logging.info(f"{region}의 최신 데이터를 백업하고 나머지는 삭제 완료.")
            else:
                logging.warning(f"{region}에 대한 최신 데이터가 없습니다.")
    except Exception as e:
        logging.error(f"데이터 백업 중 오류 발생: {e}")
        print(f"데이터 백업 중 오류 발생: {e}")
        # 텔레그램 메시지 전송
        error_message = f"데이터 백업 중 오류 발생:\n{str(e)}"
        send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_message)

def fetch_and_store_data():
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")  # 현재 시간을 가져옴
    logging.info(f"데이터 수집 작업 시작 (현재 시간: {current_time})")

    for region in regions:
        try:
            # 기상 데이터 URL 구성
            weather_api_url = f"{weather_base_url}?serviceKey={service_key}&genName={region}"
            air_stability_api_url = f"{air_stability_base_url}?serviceKey={service_key}&genName={region}"

            # 기상 데이터 API 요청 보내기
            weather_response = requests.get(weather_api_url)
            air_stability_response = requests.get(air_stability_api_url)

            if weather_response.status_code == 200 and air_stability_response.status_code == 200:
                logging.info(f"{region} API 응답 성공. 데이터 파싱 시도 중...")

                # XML 데이터 파싱
                weather_root = ET.fromstring(weather_response.content)
                air_stability_root = ET.fromstring(air_stability_response.content)

                # 각 발전소의 데이터를 저장할 딕셔너리 초기화
                plant_data = {
                    "genName": region,  # region → genName으로 변경
                    "time": None,
                    "temperature": None,
                    "humidity": None,
                    "rainfall": None,
                    "windspeed": None,
                    "winddirection": None,
                    "stability": None  # 대기안정도
                }

                # 기상 데이터 처리
                for item in weather_root.findall('.//item'):
                    expl = item.find('expl').text  # 측정 항목 (온도, 습도, 등)
                    value = float(item.find('value').text)  # 측정 값
                    time_str = item.find('time').text  # 측정 시간

                    # 시간은 모든 데이터에 동일하므로 한 번만 설정
                    if plant_data["time"] is None:
                        plant_data["time"] = time_str

                    # 측정 항목에 따라 데이터를 저장
                    if expl == "온도":
                        plant_data["temperature"] = value
                    elif expl == "습도":
                        plant_data["humidity"] = value
                    elif expl == "강우량":
                        plant_data["rainfall"] = value
                    elif expl == "풍속":
                        plant_data["windspeed"] = value
                    elif expl == "풍향":
                        plant_data["winddirection"] = value

                # 대기안정도 데이터 처리
                for item in air_stability_root.findall('.//item'):
                    stability_code = item.find('value').text.strip()  # 공백 제거 추가
                    plant_data["stability"] = air_stability_mapping.get(stability_code, "Unknown")

                # 중복 데이터 방지 - 동일한 genName과 time이 존재하는지 확인하고, 존재하지 않을 경우에만 삽입
                query = {"genName": region, "time": plant_data["time"]}  # region → genName으로 변경
                update = {"$setOnInsert": plant_data}  # 문서가 없을 경우에만 삽입
                result = collection.update_one(query, update, upsert=True)

                if result.matched_count > 0:
                    logging.info(f"{region} 데이터 중복으로 저장하지 않음: {plant_data}")
                else:
                    # 새로운 데이터가 수집되면 기존 데이터를 백업
                    backup_existing_data()
                    logging.info(f"{region} 데이터 저장 완료: {plant_data}")

            else:
                error_msg = f"{region} API 요청 실패: Weather({weather_response.status_code}) / Air Stability({air_stability_response.status_code})"
                logging.error(error_msg)
                print(error_msg)
                # 텔레그램 메시지 전송
                send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_msg)

        except requests.exceptions.RequestException as e:
            logging.error(f"{region} 데이터 요청 중 네트워크 오류 발생: {e}")
            # 텔레그램 메시지 전송
            error_message = f"{region} 데이터 요청 중 네트워크 오류 발생:\n{str(e)}"
            send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_message)
        except ET.ParseError as e:
            logging.error(f"{region} XML 파싱 오류: {e}")
            # 텔레그램 메시지 전송
            error_message = f"{region} XML 파싱 오류:\n{str(e)}"
            send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_message)
        except Exception as e:
            logging.error(f"{region} 데이터 처리 중 오류 발생: {e}")
            # 텔레그램 메시지 전송
            error_message = f"{region} 데이터 처리 중 오류 발생:\n{str(e)}"
            send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_message)

    logging.info(f"데이터 수집 작업 완료 (현재 시간: {current_time})")


# 데이터 수집 및 저장 반복 실행
def scheduled_task():
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")  # 현재 시간을 가져옴
    logging.info(f"15분마다 데이터 수집 작업 실행 중... (현재 시간: {current_time})")
    print(f"15분마다 데이터 수집 작업 실행 중... (현재 시간: {current_time})")
    fetch_and_store_data()


# 15분마다 작업을 실행하는 스케줄 설정
schedule.every(15).minutes.do(scheduled_task)  # 'schedule' 모듈 사용

if __name__ == "__main__":
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"15분마다 방사선 데이터를 확인하는 스케줄을 시작합니다. (현재 시간: {current_time})")
    logging.info(f"15분마다 방사선 데이터를 확인하는 스케줄을 시작합니다. (현재 시간: {current_time})")

    # 첫 실행
    fetch_and_store_data()

    # 스케줄을 지속적으로 실행
    try:
        while True:
            schedule.run_pending()  # 'schedule' 모듈 사용
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        logging.info("프로그램이 수동으로 종료되었습니다.")
        print("프로그램이 수동으로 종료되었습니다.")
        sys.exit(0)
