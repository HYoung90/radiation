# NPP_radiation.py
# 이 스크립트는 원자력 발전소의 방사선 데이터를 공공 API에서 가져와 MongoDB에 저장합니다.
# 데이터 백업을 처리하고, 오류 발생 시 텔레그램 알림을 전송합니다.
# 이 스크립트는 15분마다 실행되어 새로운 데이터를 가져옵니다.


import requests
import xml.etree.ElementTree as ET
from pymongo import MongoClient
import logging
import schedule
import time
import os
from dotenv import load_dotenv
from telegram_notifier import send_telegram_message

# 환경 변수 로드
load_dotenv()

# 로그 설정
logging.basicConfig(
    filename="nuclear_radiation_data_fetch.log",
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(message)s'
)

# 텔레그램 설정
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# 공공 API URL과 서비스 키
base_url = "http://data.khnp.co.kr/environ/service/realtime/radiorate"
service_key = "h%2BQvAtTFBPlY19lErWf4T9JQoowL2d918ciMd6%2B%2FdBFGTV55ykPjAp8V1UWAZPRJHKWawuQOncBubNafaOVwTQ%3D%3D"

# 발전소 목록
plants = ["WS", "KR", "YK", "UJ", "SU"]

# MongoDB 연결 설정
client = MongoClient("mongodb://localhost:27017/")
db = client['power_plant_weather']
radiation_collection = db['nuclear_radiation']
radiation_backup_collection = db['nuclear_radiation_backup']  # 백업 컬렉션

def backup_existing_data():
    """기존 데이터를 백업 컬렉션으로 이동"""
    try:
        existing_data = list(radiation_collection.find({}))
        if existing_data:
            # 데이터를 백업 컬렉션으로 이동
            radiation_backup_collection.insert_many(existing_data)
            # 기존 데이터 삭제
            result = radiation_collection.delete_many({})
            logging.info(f"기존 데이터를 nuclear_radiation_backup으로 이동 완료. {len(existing_data)}개의 데이터 백업됨, {result.deleted_count}개의 데이터 삭제됨.")
        else:
            logging.info("백업할 데이터가 없습니다.")
    except Exception as e:
        logging.error(f"데이터 백업 중 오류 발생: {e}")
        # 텔레그램 메시지 전송
        error_message = f"데이터 백업 중 오류 발생:\n{str(e)}"
        send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_message)

def fetch_and_store_radiation_data():
    for genName in plants:
        url = f"{base_url}?genName={genName}&serviceKey={service_key}"

        try:
            # API 요청
            response = requests.get(url)
            response.raise_for_status()  # 오류 응답 처리
        except requests.exceptions.RequestException as e:
            logging.error(f"{genName} 발전소 API 요청 실패. 오류: {e}")
            print(f"{genName} 발전소 API 요청 실패. 오류: {e}")
            # 텔레그램 메시지 전송
            error_message = f"{genName} 발전소 API 요청 실패.\n오류: {e}"
            send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_message)
            continue

        logging.info(f"{genName} 발전소 API 요청 성공.")

        try:
            # XML 데이터 파싱
            root = ET.fromstring(response.content)
            for item in root.findall(".//item"):
                radiation_data = {
                    "expl": item.findtext("expl"),
                    "name": item.findtext("name"),
                    "time": item.findtext("time"),
                    "value": item.findtext("value"),
                    "genName": genName
                }
                logging.info(f"설명: {radiation_data['expl']}, 측정소: {radiation_data['name']}, 시간: {radiation_data['time']}, 방사선량: {radiation_data['value']} μSv/h")

                # MongoDB에 최신 데이터 저장 (upsert로 중복 방지)
                radiation_collection.update_one(
                    {"name": radiation_data["name"], "time": radiation_data["time"]},
                    {"$set": radiation_data},
                    upsert=True
                )
        except ET.ParseError as e:
            logging.error(f"{genName} 발전소 XML 파싱 오류: {e}")
            print(f"{genName} 발전소 XML 파싱 오류: {e}")
            # 텔레그램 메시지 전송
            error_message = f"{genName} 발전소 XML 파싱 오류:\n{str(e)}"
            send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_message)
            continue
        except Exception as e:
            logging.error(f"{genName} 발전소 데이터 처리 중 오류 발생: {e}")
            print(f"{genName} 발전소 데이터 처리 중 오류 발생: {e}")
            # 텔레그램 메시지 전송
            error_message = f"{genName} 발전소 데이터 처리 중 오류 발생:\n{str(e)}"
            send_telegram_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, error_message)
            continue

# 스케줄 실행 시 로그 기록
def scheduled_task():
    logging.info("15분마다 데이터 수집 작업 실행 중...")
    print("15분마다 데이터 수집 작업 실행 중...")
    backup_existing_data()
    fetch_and_store_radiation_data()

# 15분마다 작업을 실행하는 스케줄 설정
schedule.every(15).minutes.do(scheduled_task)

if __name__ == "__main__":
    print("15분마다 방사선 데이터를 확인하는 스케줄을 시작합니다.")
    logging.info("15분마다 방사선 데이터를 확인하는 스케줄을 시작합니다.")

    # 첫 실행 시 데이터 백업 후 새로운 데이터 수집
    backup_existing_data()
    fetch_and_store_radiation_data()

    # 스케줄을 지속적으로 실행
    while True:
        schedule.run_pending()
        time.sleep(1)
