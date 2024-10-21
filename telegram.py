# telegram.py
# 이 스크립트는 텔레그램 봇을 사용하여 메시지 업데이트를 가져오고, 이를 출력하는 용도로 사용됩니다.


import requests
import json

def get_updates(token):
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    response = requests.get(url)
    if response.status_code == 200:
        data = response.json()
        return data
    else:
        print(f"HTTP 오류 발생: {response.status_code}")
        return None

if __name__ == "__main__":
    telegram_token = "7929251346:AAFxduk8B-1HlGUGE7bovbWmIOMljzGvAlo"  # 재발급 받은 봇 토큰으로 교체
    updates = get_updates(telegram_token)
    if updates:
        print(json.dumps(updates, indent=4, ensure_ascii=False))
    else:
        print("업데이트를 가져오는 데 실패했습니다.")
