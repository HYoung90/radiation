# telegram_notifier.py
# 이 스크립트는 텔레그램 봇을 사용하여 특정 채팅 ID로 메시지를 전송하는 기능을 제공합니다.
# 오류 발생 시 로그를 기록하며, 메시지를 성공적으로 전송했는지도 확인할 수 있습니다.


# telegram_notifier.py

import requests
import os
from dotenv import load_dotenv
import logging

# 환경 변수 로드
load_dotenv()

# 로깅 설정
logging.basicConfig(
    filename='telegram_notifier.log',
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(message)s'
)

def send_telegram_message(token, chat_id, message):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",  # HTML 포맷 사용
        "disable_web_page_preview": True
    }
    try:
        response = requests.post(url, data=data)
        response.raise_for_status()
        logging.info("Telegram 메시지가 성공적으로 전송되었습니다.")
        print("Telegram 메시지가 성공적으로 전송되었습니다.")
    except requests.exceptions.RequestException as e:
        logging.error(f"Telegram 메시지 전송 실패: {e}")
        print(f"Telegram 메시지 전송 실패: {e}")