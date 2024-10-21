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
    """
    텔레그램 봇을 통해 메시지를 전송하는 함수.

    Parameters:
        token (str): 텔레그램 봇의 API 토큰.
        chat_id (str): 메시지를 보낼 채팅 ID.
        message (str): 전송할 메시지 내용.
    """
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",  # 또는 "HTML"을 사용할 수 있습니다.
        "disable_web_page_preview": True  # 링크 미리보기 비활성화
    }
    try:
        response = requests.post(url, data=data)
        response.raise_for_status()
        logging.info("Telegram 메시지가 성공적으로 전송되었습니다.")
        print("Telegram 메시지가 성공적으로 전송되었습니다.")
    except requests.exceptions.RequestException as e:
        logging.error(f"Telegram 메시지 전송 실패: {e}")
        print(f"Telegram 메시지 전송 실패: {e}")
