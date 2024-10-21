import requests
import xml.etree.ElementTree as ET
import logging
from datetime import datetime

# 로그 설정
logging.basicConfig(filename="weather_data_fetch.log", level=logging.INFO)

# MongoDB 연결 설정
client = MongoClient("mongodb://localhost:27017/")
db = client['weather_data']

# 발전소별 컬렉션 설정
collections = {
    'WS': db['weather_ws'],
    'KR': db['weather_kr'],
    'YK': db['weather_yk'],
    'UJ': db['weather_uj']
}

# 발전소 구분 코드
gen_names = ['WS', 'KR', 'YK', 'UJ']

# 공공 API URL (서비스 키는 동일, genName을 동적으로 설정)
base_url = "http://data.khnp.co.kr/environ/service/realtime/weather"
service_key = "h%2BQvAtTFBPlY19lErWf4T9JQoowL2d918ciMd6%2B%2FdBFGTV55ykPjAp8V1UWAZPRJHKWawuQOncBubNafaOVwTQ%3D%3D"


def fetch_and_store_data():
    for gen_name in gen_names:
        # URL 구성
        api_url = f"{base_url}?serviceKey={service_key}&genName={gen_name}"

        # API 요청 보내기
        response = requests.get(api_url)

        if response.status_code == 200:
            try:
                # XML 데이터 파싱
                root = ET.fromstring(response.content)

                # XML 데이터를 JSON 형식으로 변환 (MongoDB에 삽입하기 위해)
                data = []
                for item in root.findall('.//item'):  # XML 구조에 따라 경로 수정
                    item_data = {child.tag.lower(): child.text.strip() for child in item}
                    unique_field_value = item_data.get('id')  # 고유 필드의 값 가져오기

                    if unique_field_value:
                        item_data['_id'] = f"{gen_name}_{unique_field_value}"
                        data.append(item_data)

                # 발전소별로 컬렉션에 데이터 삽입
                collection = collections[gen_name]
                for document in data:
                    collection.replace_one({'_id': document['_id']}, document, upsert=True)

                logging.info(f"{gen_name} 데이터 MongoDB에 저장 완료")

            except ET.ParseError as e:
                logging.error(f"{gen_name} XML 파싱 오류: {e}")
                logging.error("응답 내용:", response.text)
        else:
            logging.error(f"{gen_name} 데이터 가져오기 실패: {response.status_code}")


# 반복 실행 예시 (원하는 시간 간격에 따라 호출)
fetch_and_store_data()
