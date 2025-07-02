import logging
from pymongo import MongoClient
# from dotenv import load_dotenv # 이 라인을 제거합니다.
import os
import sys
from datetime import datetime, timedelta
import schedule
import time

# 로깅 설정 (stdout으로 강제하여 모든 출력이 동일하게 처리되도록 설정)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(message)s',
    handlers=[
        logging.FileHandler('radiation_data.log'),
        logging.StreamHandler(sys.stdout)
    ]
)

# MongoDB 연결
def get_mongo_connection():
    """
    MongoDB에 연결하고 클라이언트를 반환합니다.
    연결에 실패하면 스크립트를 종료합니다.
    """
    try:
        railway_mongo_uri = os.getenv("MONGO_URI")

        if railway_mongo_uri:
            cleaned_railway_mongo_uri = railway_mongo_uri.lstrip('=')
            client = MongoClient(cleaned_railway_mongo_uri)
            logging.info("Railway MongoDB 클라이언트 설정 및 연결 시도 성공")
        else:
            client = MongoClient("mongodb://localhost:27017/")
            logging.info("로컬 MongoDB 클라이언트 설정 완료")

        return client
    except Exception as e:
        logging.error(f"MongoDB 연결 실패: {e}")
        sys.exit(1)

client = get_mongo_connection()
db = client['Data']
npp_radiation_collection = db['NPP_radiation']
busan_radiation_collection = db['Busan_radiation']
radiation_stats_collection = db['radiation_stats'] # 새로운 통합 방사선 통계 컬렉션

# radiation_stats 컬렉션에 유니크 인덱스 생성 (locNm과 tm 조합)
# 이 코드는 스크립트 시작 시 한 번 실행되어야 합니다.
radiation_stats_collection.create_index(
    [("locNm", 1), ("tm", 1)],
    unique=True
)
logging.info("radiation_stats 컬렉션에 유니크 인덱스 생성 확인/완료.")


def process_radiation_data():
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"방사선 데이터 처리 및 radiation_stats 컬렉션 저장 시작. (현재 시간: {current_time})")
    print(f"방사선 데이터 처리 및 radiation_stats 컬렉션 저장 시작. (현재 시간: {current_time})")

    try:
        # NPP_radiation 데이터 처리
        npp_data_cursor = npp_radiation_collection.find({})
        npp_data = list(npp_data_cursor)
        logging.info(f"NPP_radiation에서 {len(npp_data)}건의 데이터 로드.")

        # Busan_radiation 데이터 처리
        busan_data_cursor = busan_radiation_collection.find({})
        busan_data = list(busan_data_cursor)
        logging.info(f"Busan_radiation에서 {len(busan_data)}건의 데이터 로드.")

        # 두 컬렉션의 데이터를 통합하고 필요한 필드만 추출
        integrated_data = []

        for doc in npp_data:
            integrated_data.append({
                "locNm": doc.get('locNm'),
                "tm": doc.get('tm'),
                "radiation_value": doc.get('curVal'), # NPP는 μSv/h
                "unit": "μSv/h",
                "data_source": "NPP"
            })

        for doc in busan_data:
            # checkTime이 datetime 객체인 경우 문자열로 변환하여 통합
            check_time_str = doc['checkTime'].strftime('%Y%m%d%H%M') if isinstance(doc['checkTime'], datetime) else doc['checkTime']
            integrated_data.append({
                "locNm": doc.get('locNm'),
                "tm": check_time_str, # 부산은 nSv/h, 문자열로 통일
                "radiation_value": doc.get('dose_nSv_h'),
                "unit": "nSv/h",
                "data_source": "Busan"
            })

        documents_to_insert = []
        for doc in integrated_data:
            # 중복 체크: locNm과 tm이 같은 문서가 이미 있는지 확인
            existing_doc = radiation_stats_collection.find_one({
                "locNm": doc['locNm'],
                "tm": doc['tm']
            })

            if existing_doc:
                # 기존 문서가 있다면 업데이트하지 않고 스킵 (가장 최신 데이터만 유지)
                # 이 로직은 API가 항상 최신 데이터를 반환하고, MongoDB에 저장될 때도 최신이라면 괜찮음
                # 만약 업데이트가 필요하다면 $set 등을 사용하여 기존 문서 필드를 업데이트해야 함
                logging.info(f"기존 방사선 데이터가 더 최신이거나 같으므로 업데이트 스킵: {doc['locNm']} - {doc['tm']}")
            else:
                documents_to_insert.append(doc)

        if documents_to_insert:
            radiation_stats_collection.insert_many(documents_to_insert)
            logging.info(f"새로운 방사선 데이터 {len(documents_to_insert)}건 radiation_stats 컬렉션에 저장 완료.")
        else:
            logging.info("radiation_stats 컬렉션에 새로 저장할 데이터가 없습니다.")

        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logging.info(f"방사선 데이터 처리 및 radiation_stats 컬렉션 저장 완료. (현재 시간: {current_time})")
        print(f"방사선 데이터 처리 및 radiation_stats 컬lection 저장 완료. (현재 시간: {current_time})")

    except Exception as e:
        logging.error(f"process_radiation_data 함수 실행 중 오류 발생: {e}", exc_info=True)
        print(f"process_radiation_data 함수 실행 중 오류 발생: {e}")


def automated_process():
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"1시간마다 방사선 데이터 처리 및 통합 저장 작업 실행 중... (현재 시간: {current_time})")
    print(f"1시간마다 방사선 데이터 처리 및 통합 저장 작업 실행 중... (현재 시간: {current_time})")
    process_radiation_data()

schedule.every(1).hour.do(automated_process)

# 스크립트 종료 시 MongoDB 연결 닫기
def close_mongodb_connection():
    if client:
        client.close()
        logging.info("MongoDB 연결이 닫혔습니다.")

atexit.register(close_mongodb_connection)