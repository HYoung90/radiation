# process_radiation_stats.py
# 이 스크립트는 24시간치 기상 및 방사선 데이터를 조회하여 결합 처리하고 MongoDB에 저장합니다.
# 1시간마다 자동 실행되며, 로깅은 파일과 stdout에 동시에 출력됩니다.

import logging
import schedule
import time
import os
import sys
import atexit
from pymongo import MongoClient
from datetime import datetime, timedelta

# 로깅 설정: 파일 및 stdout
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(message)s',
    handlers=[
        logging.FileHandler('radiation_data.log'),
        logging.StreamHandler(sys.stdout)
    ]
)

# MongoDB 연결 함수
def get_mongo_connection():
    try:
        uri = os.getenv('MONGO_URI', '').lstrip('=').strip()
        if uri:
            client = MongoClient(uri)
            logging.info('원격 MongoDB 연결 성공')
        else:
            client = MongoClient('mongodb://localhost:27017/')
            logging.info('로컬 MongoDB 연결 성공')
        return client
    except Exception as e:
        logging.error(f'MongoDB 연결 실패: {e}', exc_info=True)
        sys.exit(1)

# MongoDB 초기화
client = get_mongo_connection()
db = client['Data']
weather_collection   = db['NPP_weather']
radiation_collection = db['NPP_radiation']
stats_collection     = db['radiation_stats']

# 기상 및 방사선 데이터 결합 처리 함수
def process_radiation_data():
    end   = datetime.now()
    start = end - timedelta(days=1)
    logging.info(f'데이터 처리 시작: {end.strftime("%Y-%m-%d %H:%M:%S")}')

    # 최근 24시간 기상/방사선 데이터 조회
    weather_data   = list(weather_collection.find({'data_fetch_time': {'$gte': start, '$lte': end}}))
    radiation_data = list(radiation_collection.find({'data_fetch_time': {'$gte': start, '$lte': end}}))
    logging.info(f'기상 데이터 {len(weather_data)}건, 방사선 데이터 {len(radiation_data)}건 조회')

    # (지역, 시간) 키로 날씨 상태 맵 생성
    weather_map = {}
    for item in weather_data:
        tm_raw = item.get('tm')
        region = item.get('region')
        if not (tm_raw and region):
            continue
        try:
            dt_key = datetime.strptime(tm_raw[:12], '%Y%m%d%H%M').strftime('%Y-%m-%d %H')
            key    = (region, dt_key)
            if key not in weather_map:
                weather_map[key] = item.get('wthStt')
        except Exception as e:
            logging.warning(f'날씨 데이터 파싱 오류(tm={tm_raw}): {e}')

    # 결합 후 중복 제거
    processed = {}
    region_map = {'고리본부':'KR','월성본부':'WS','한빛본부':'YK','한울본부':'UJ','새울본부':'SU'}
    for item in radiation_data:
        tm_raw = item.get('tm')
        loc    = item.get('locNm')
        val    = item.get('curVal')
        fetched = item.get('data_fetch_time')
        if not (tm_raw and loc):
            continue
        try:
            dt_key = datetime.strptime(tm_raw[:12], '%Y%m%d%H%M').strftime('%Y-%m-%d %H')
            region = region_map.get(loc, loc)
            weather_status = weather_map.get((region, dt_key))
            key = (loc, tm_raw, fetched)
            if key not in processed:
                processed[key] = {
                    'locNm': loc,
                    'tm': datetime.strptime(tm_raw, '%Y%m%d%H%M%S'),
                    'curVal': val,
                    'wthStt': weather_status,
                    'data_fetch_time': fetched
                }
        except Exception as e:
            logging.warning(f'방사선 데이터 파싱 오류(tm={tm_raw}): {e}')

    # MongoDB에 upsert
    to_insert = []
    for doc in processed.values():
        query    = {'locNm': doc['locNm'], 'tm': doc['tm']}
        existing = stats_collection.find_one(query)
        if existing:
            if doc['data_fetch_time'] > existing.get('data_fetch_time'):
                stats_collection.update_one(query, {'$set': doc})
                logging.info(f'업데이트: {doc["locNm"]} {doc["tm"]}')
        else:
            to_insert.append(doc)

    if to_insert:
        stats_collection.insert_many(to_insert)
        logging.info(f'신규 데이터 {len(to_insert)}건 저장 완료')
    else:
        logging.info('신규 데이터 없음')

    logging.info('데이터 처리 완료')

# 자동 실행 함수
def automated_process():
    logging.info('자동 처리 시작')
    process_radiation_data()

# 메인 함수
def main():
    automated_process()  # 시작 즉시 실행
    schedule.every(1).hour.do(automated_process)
    logging.info('스케줄러 시작')
    while True:
        schedule.run_pending()
        time.sleep(1)

# 종료 시 MongoDB 연결 종료 등록
atexit.register(lambda: (client.close(), logging.info('MongoDB 연결 종료')))

if __name__ == '__main__':
    main()
