import logging
from pymongo import MongoClient
#from dotenv import load_dotenv
import os
import sys  # sys 모듈 추가
from datetime import datetime, timedelta  # 현재 시간 출력을 위한 모듈 추가, timedelta 추가
import schedule  # 스케줄러 모듈
import time  # 스케줄 실행 대기 시간 조절을 위한 모듈
import atexit

# .env 파일에서 환경 변수 불러오기
#load_dotenv("telegram_config.env") # telegram_config.env 파일 명시

# 로깅 설정 (stdout으로 강제하여 모든 출력이 동일하게 처리되도록 설정)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(message)s',
    handlers=[
        logging.FileHandler('radiation_data.log'),
        logging.StreamHandler(sys.stdout)  # sys.stdout을 사용해 stdout으로 로그를 출력
    ]
)

# MongoDB 연결
def get_mongo_connection():
    """
    MongoDB에 연결하고 클라이언트를 반환합니다.
    연결에 실패하면 스크립트를 종료합니다.
    """
    try:
        # Railway MONGO_URI 환경 변수를 로드 시도
        railway_mongo_uri = os.getenv("MONGO_URI")

        if railway_mongo_uri:
            # MONGO_URI에 불필요한 '=' 문자가 붙어있을 경우 제거
            cleaned_railway_mongo_uri = railway_mongo_uri.lstrip('=')
            client = MongoClient(cleaned_railway_mongo_uri)
            logging.info("Railway MongoDB 클라이언트 설정 및 연결 시도 성공")
            print("Railway MongoDB 클라이언트 설정 및 연결 시도 성공")
        else:
            # MONGO_URI가 없으면 로컬 MongoDB에 연결
            client = MongoClient("mongodb://localhost:27017/")
            logging.info("로컬 MongoDB 클라이언트 설정 완료")
            print("로컬 MongoDB 클라이언트 설정 완료")

        return client
    except Exception as e:
        logging.error(f"MongoDB 연결 실패: {e}")
        print(f"MongoDB 연결 실패: {e}")  # 콘솔에 출력
        sys.exit(1) # 연결 실패 시 스크립트 종료

# MongoDB 클라이언트 초기화 (get_mongo_connection 함수 호출)
client = get_mongo_connection()
db = client['Data']

# 컬렉션 정의
weather_collection = db['NPP_weather']
radiation_collection = db['NPP_radiation']
stats_collection = db['radiation_stats'] # 최종 처리 결과를 저장할 컬렉션

# datetime 혹은 문자열을 YYYY-MM-DD 형식으로 반환하는 함수
def format_date(date_value):
    if isinstance(date_value, datetime):
        return date_value.strftime('%Y-%m-%d')
    elif isinstance(date_value, str):
        try:
            return datetime.strptime(date_value, '%Y%m%d%H%M%S').strftime('%Y-%m-%d')
        except ValueError:
            try:
                return datetime.strptime(date_value, '%Y%m%d%H%M').strftime('%Y-%m-%d')
            except ValueError:
                return date_value # 이미 YYYY-MM-DD 형태일 경우 그대로 반환
    return None

# 방사선 데이터 처리 및 저장 함수
def process_radiation_data():
    try:
        # 시작 시간 로그에 기록
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logging.info(f"방사선 데이터 처리 시작 (현재 시간: {current_time})")
        print(f"방사선 데이터 처리 시작 (현재 시간: {current_time})")

        # 24시간 전부터 현재까지의 데이터만 가져오기
        end_time = datetime.now()
        start_time = end_time - timedelta(days=1)

        # NPP_weather 컬렉션에서 최근 24시간 동안의 날씨 데이터만 가져오기
        weather_data_cursor = weather_collection.find({
            'data_fetch_time': {'$gte': start_time, '$lte': end_time}
        })
        weather_data = list(weather_data_cursor)
        logging.info(f"날씨 데이터 {len(weather_data)}건 조회 완료 (최근 24시간).")

        # NPP_radiation 컬렉션에서 최근 24시간 동안의 방사선 데이터만 가져오기
        radiation_data_cursor = radiation_collection.find({
            'data_fetch_time': {'$gte': start_time, '$lte': end_time}
        })
        radiation_data = list(radiation_data_cursor)
        logging.info(f"방사선 데이터 {len(radiation_data)}건 조회 완료 (최근 24시간).")

        # 날씨 데이터를 지역과 시간(YYYY-MM-DD HH)을 키로 하는 딕셔너리로 구성
        weather_map = {}
        for w_item in weather_data:
            # tm 필드가 yyyyMMddHHmmss 형식이라고 가정
            try:
                weather_datetime_str = w_item.get('tm')
                if weather_datetime_str:
                    # 초 부분은 무시하고 시간(HH)까지만 사용
                    weather_dt_key = datetime.strptime(weather_datetime_str[:12], '%Y%m%d%H%M').strftime('%Y-%m-%d %H')
                    region_key = w_item.get('region')
                    if region_key:
                        # 같은 지역, 같은 시간대에는 최신 데이터만 유지 (API에서 최신순으로 오므로 첫 발견 데이터 사용)
                        if (region_key, weather_dt_key) not in weather_map:
                            weather_map[(region_key, weather_dt_key)] = w_item.get('wthStt') # 날씨 상태만 저장
            except ValueError:
                logging.warning(f"날씨 데이터 tm 필드 파싱 오류: {w_item.get('tm')}")
                continue


        processed_data = {} # 중복된 데이터를 필터링하고 처리된 데이터 저장
        # 방사선 데이터와 날씨 데이터 매칭 및 처리
        for r_item in radiation_data:
            # tm 필드가 yyyyMMddHHmmss 형식이라고 가정
            try:
                radiation_datetime_str = r_item.get('tm')
                if radiation_datetime_str:
                    radiation_dt = datetime.strptime(radiation_datetime_str, '%Y%m%d%H%M%S')
                    # 매칭을 위해 날씨 데이터와 동일한 시간 형식 사용
                    radiation_dt_key = radiation_dt.strftime('%Y-%m-%d %H')

                    locNm = r_item.get('locNm') # 방사선 데이터의 지역 이름 (예: 고리본부)
                    curVal = r_item.get('curVal') # 현재 방사선량

                    # NPP_weather의 region과 NPP_radiation의 locNm 매칭
                    # 고리본부 -> KR, 월성본부 -> WS 등 매핑 필요
                    # 여기서는 locNm을 그대로 사용하여 매핑이 이미 되어 있다고 가정
                    # 또는 명시적 매핑 테이블을 사용
                    region_mapping_for_weather = {
                        "고리본부": "KR",
                        "월성본부": "WS",
                        "한빛본부": "YK",
                        "한울본부": "UJ",
                        "새울본부": "SU"
                    }
                    mapped_region_key = region_mapping_for_weather.get(locNm, locNm) # 매핑 없으면 locNm 그대로 사용

                    # 날씨 데이터에서 해당 지역, 해당 시간의 날씨 상태 가져오기
                    weather_status = weather_map.get((mapped_region_key, radiation_dt_key))

                    # 데이터 고유 식별자 (지역명, 측정 시간, 수집 시간)
                    unique_id = (locNm, radiation_datetime_str, r_item.get('data_fetch_time').isoformat())

                    if unique_id not in processed_data:
                        processed_data[unique_id] = {
                            "locNm": locNm,
                            "tm": radiation_dt, # datetime 객체로 저장
                            "curVal": curVal,
                            "wthStt": weather_status, # 매칭된 날씨 상태
                            "data_fetch_time": r_item.get('data_fetch_time')
                        }
                    else:
                        logging.info(f"중복된 방사선 데이터 발견 및 스킵: {unique_id}")

            except ValueError:
                logging.warning(f"방사선 데이터 tm 필드 파싱 오류: {r_item.get('tm')}")
                continue

        # MongoDB radiation_stats 컬렉션에 삽입하기 전에 중복 확인
        # 현재 시간으로 upsert_time을 추가하여 문서 업데이트를 관리
        documents_to_insert = []
        for unique_id, doc in processed_data.items():
            # tm과 locNm을 기준으로 중복 확인
            query = {"locNm": doc['locNm'], "tm": doc['tm']}
            existing_doc = stats_collection.find_one(query)

            # 이미 존재하는 문서라면, data_fetch_time이 더 최신인 경우 업데이트
            if existing_doc:
                if doc['data_fetch_time'] > existing_doc['data_fetch_time']:
                    stats_collection.update_one(query, {"$set": doc})
                    logging.info(f"기존 방사선 데이터 업데이트: {doc['locNm']} - {doc['tm']}")
                else:
                    logging.info(f"기존 방사선 데이터가 더 최신이거나 같으므로 업데이트 스킵: {doc['locNm']} - {doc['tm']}")
            else:
                documents_to_insert.append(doc)

        if documents_to_insert:
            stats_collection.insert_many(documents_to_insert)
            logging.info(f"새로운 방사선 데이터 {len(documents_to_insert)}건 radiation_stats 컬렉션에 저장 완료.")
        else:
            logging.info("radiation_stats 컬렉션에 새로 저장할 데이터가 없습니다.")

        # 완료 시간 로그에 기록
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logging.info(f"방사선 데이터 처리 및 radiation_stats 컬렉션 저장 완료. (현재 시간: {current_time})")
        print(f"방사선 데이터 처리 및 radiation_stats 컬렉션 저장 완료. (현재 시간: {current_time})")

    except Exception as e:
        logging.error(f"process_radiation_data 함수 실행 중 오류 발생: {e}", exc_info=True)
        print(f"process_radiation_data 함수 실행 중 오류 발생: {e}")  # 콘솔에 출력


# 스케줄러에서 실행될 함수 (매일 1시간마다)
def automated_process():
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"1시간마다 방사선 데이터 처리 작업 실행 중... (현재 시간: {current_time})")
    print(f"1시간마다 방사선 데이터 처리 작업 실행 중... (현재 시간: {current_time})")
    process_radiation_data()


# 스케줄 설정
schedule.every(1).hour.do(automated_process) # 매 시간마다 실행

# 스크립트 시작 시 MongoDB 연결 닫기 함수 등록
def close_mongodb_connection():
    if client:
        client.close()
        logging.info("MongoDB 연결이 닫혔습니다.")
        print("MongoDB 연결이 닫혔습니다.")

atexit.register(close_mongodb_connection)

